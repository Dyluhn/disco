"""Persistent, owner-scoped Reference Pack library.

This module is deliberately independent of the dormant Build Platform library
registry.  A pack is inert copied data: the manifest is metadata and the files
under its immutable version directory are the authoritative bytes.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import uuid4

from disco.core.env import disco_env

from .workspace_process_fence import try_workspace_process_fence

_MUTATION_LOCKS: dict[str, threading.RLock] = {}
_MUTATION_LOCKS_GUARD = threading.Lock()


def _local_mutation_lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _MUTATION_LOCKS_GUARD:
        lock = _MUTATION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _MUTATION_LOCKS[key] = lock
        return lock


def _synchronized_mutation(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize a store mutation in-process and across POSIX workers."""

    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            with self.mutation_lock():
                return await method(self, *args, **kwargs)

        return cast(Callable[..., Any], async_wrapper)

    @wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self.mutation_lock():
            return method(self, *args, **kwargs)

    return wrapper


class ReferencePackError(ValueError):
    """Expected, user-correctable Reference Pack failure."""


class ReferencePackNotFound(LookupError):
    """The requested pack/version is absent or not visible to the owner."""


class ReferencePackForbidden(PermissionError):
    """The requested pack belongs to another owner."""


class WorkspaceReader(Protocol):
    """Host-injected reader for the active conversation sandbox/workspace.

    The store intentionally does not accept a workspace root.  Implementations
    may expose any one of ``read_bytes``, ``read_file``, or ``read``; optional
    ``is_file``/``is_symlink`` checks allow the host to preserve sandbox policy.
    """

    def read_bytes(self, path: str) -> bytes: ...


@dataclass(frozen=True)
class ReferencePackLimits:
    max_files: int = 100
    max_file_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    max_name_chars: int = 160
    max_description_chars: int = 4000
    max_pack_markdown_chars: int = 24_000
    max_text_chars: int = 12_000


DEFAULT_LIMITS = ReferencePackLimits()


@dataclass(frozen=True)
class ReferencePackFile:
    name: str
    media_type: str
    size: int
    sha256: str
    path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
            "path": self.path or self.name,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ReferencePackFile:
        return cls(
            name=str(raw["name"]),
            media_type=str(raw["media_type"]),
            size=int(raw["size"]),
            sha256=str(raw["sha256"]),
            path=str(raw.get("path") or raw["name"]),
        )


@dataclass(frozen=True)
class ReferencePackVersion:
    id: str
    pack_id: str
    owner_id: str
    description: str
    files: tuple[ReferencePackFile, ...]
    created_at: str
    content_sha256: str

    @property
    def version_id(self) -> str:
        return self.id

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version_id": self.id,
            "pack_id": self.pack_id,
            "owner_id": self.owner_id,
            "description": self.description,
            "files": [item.as_dict() for item in self.files],
            "created_at": self.created_at,
            "content_sha256": self.content_sha256,
            "total_bytes": self.total_bytes,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ReferencePackVersion:
        return cls(
            id=str(raw.get("id") or raw["version_id"]),
            pack_id=str(raw["pack_id"]),
            owner_id=str(raw["owner_id"]),
            description=str(raw.get("description") or ""),
            files=tuple(ReferencePackFile.from_dict(item) for item in raw.get("files", [])),
            created_at=str(raw.get("created_at") or ""),
            content_sha256=str(raw.get("content_sha256") or ""),
        )


@dataclass(frozen=True)
class ReferencePack:
    id: str
    owner_id: str
    name: str
    description: str
    current_version_id: str
    current: ReferencePackVersion
    created_at: str
    updated_at: str

    @property
    def current_version(self) -> ReferencePackVersion:
        return self.current

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pack_id": self.id,
            "owner_id": self.owner_id,
            "name": self.name,
            "description": self.description,
            "current_version_id": self.current_version_id,
            "current": self.current.as_dict(),
            "files": [item.as_dict() for item in self.current.files],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ReferencePackFileInput:
    """A model/tool supplied file selector, resolved by the injected reader."""

    path: str
    name: str | None = None
    media_type: str | None = None


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _data_root() -> Path:
    raw = disco_env("DATA_DIR")
    return Path(raw) if raw else Path.cwd() / ".disco-data"


def _safe_component(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(".-")
    return normalized[:80] or fallback


def _reader_callable(reader: Any) -> Callable[[str], Any]:
    if reader is None:
        raise ReferencePackError("an active sandbox/workspace reader is required")
    for name in ("read_bytes", "read_file", "read"):
        candidate = getattr(reader, name, None)
        if callable(candidate):
            return lambda path, fn=candidate: fn(path)
    if isinstance(reader, Mapping):
        return lambda path: reader[path]
    if callable(reader):
        return cast(Callable[[str], Any], reader)
    raise ReferencePackError("the injected workspace reader cannot read files")


def _coerce_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    raise ReferencePackError("workspace reader returned non-bytes")


def _reader_check(reader: Any, method: str, path: str) -> bool | None:
    fn = getattr(reader, method, None)
    if not callable(fn):
        return None
    value = fn(path)
    return bool(value)


async def _await_value(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _reader_check_async(reader: Any, method: str, path: str) -> bool | None:
    fn = getattr(reader, method, None)
    if callable(fn):
        return bool(await _await_value(fn(path)))
    stat = getattr(reader, "stat", None)
    if callable(stat):
        facts = await _await_value(stat(path))
        if isinstance(facts, Mapping):
            if method == "is_file" and "is_file" in facts:
                return bool(facts["is_file"])
            if method == "is_symlink" and "is_symlink" in facts:
                return bool(facts["is_symlink"])
        else:
            value = getattr(facts, method, None)
            if value is not None:
                return bool(value() if callable(value) else value)
    return None


def _canonical_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ReferencePackError("file path must be a non-empty relative string")
    if "\\" in raw:
        raise ReferencePackError("backslash is not allowed in a workspace-relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReferencePackError(f"invalid workspace-relative path: {raw!r}")
    normalized = unicodedata.normalize("NFC", "/".join(path.parts))
    if not normalized or PurePosixPath(normalized).name.casefold() == "pack.md":
        raise ReferencePackError("PACK.md is reserved and cannot be included")
    return normalized


def _input_parts(raw: Any) -> ReferencePackFileInput:
    if isinstance(raw, str):
        return ReferencePackFileInput(path=raw)
    # Tool arguments arrive as the shared Pydantic ``ReferencePackFileArg``
    # model, while HTTP callers arrive as mappings.  Normalize the model at
    # this boundary so both paths use the exact same canonical validation.
    model_dump = getattr(raw, "model_dump", None)
    if callable(model_dump):
        raw = model_dump()
    if isinstance(raw, Mapping):
        path = raw.get("path", raw.get("name"))
        if not isinstance(path, str):
            raise ReferencePackError("each file requires a path")
        name = raw.get("name")
        media_type = raw.get("media_type") or raw.get("mime_type")
        return ReferencePackFileInput(
            path=path,
            name=name if isinstance(name, str) else None,
            media_type=media_type if isinstance(media_type, str) else None,
        )
    if isinstance(raw, ReferencePackFileInput):
        return raw
    raise ReferencePackError("files must be workspace-relative paths or file objects")


def _file_media_type(name: str, supplied: str | None) -> str:
    if supplied:
        value = supplied.strip().lower()
        if value and re.fullmatch(
            r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*(?:;\s*charset=[^;]+)?", value
        ):
            return value
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _canonical_inputs(
    files: list[Any] | tuple[Any, ...],
    reader: Any,
    limits: ReferencePackLimits,
) -> tuple[tuple[str, bytes, str], ...]:
    if not isinstance(files, (list, tuple)):
        raise ReferencePackError("files must be a list")
    if len(files) == 0:
        raise ReferencePackError("a Reference Pack requires at least one file")
    if len(files) > limits.max_files:
        raise ReferencePackError(f"too many files (maximum {limits.max_files})")
    read = _reader_callable(reader)
    seen: dict[str, str] = {}
    selected: list[tuple[str, bytes, str]] = []
    for raw in files:
        item = _input_parts(raw)
        path = _canonical_path(item.path)
        key = path.casefold()
        if key in seen:
            raise ReferencePackError(f"duplicate or case-colliding file: {path}")
        seen[key] = path
        if _reader_check(reader, "is_symlink", item.path) is True:
            raise ReferencePackError(f"symlink files are not allowed: {path}")
        if _reader_check(reader, "is_file", item.path) is False:
            raise ReferencePackError(f"workspace entry is not a regular file: {path}")
        try:
            data = _coerce_bytes(read(item.path))
        except (KeyError, FileNotFoundError, IsADirectoryError) as exc:
            raise ReferencePackError(f"workspace file is unavailable: {path}") from exc
        if len(data) > limits.max_file_bytes:
            raise ReferencePackError(f"file exceeds the {limits.max_file_bytes}-byte limit: {path}")
        selected.append((path, data, _file_media_type(item.name or path, item.media_type)))
    selected.sort(key=lambda row: (row[0].casefold(), row[0]))
    total = sum(len(row[1]) for row in selected)
    if total > limits.max_total_bytes:
        raise ReferencePackError(f"files exceed the {limits.max_total_bytes}-byte total limit")
    return tuple(selected)


async def _canonical_inputs_async(
    files: list[Any] | tuple[Any, ...],
    reader: Any,
    limits: ReferencePackLimits,
) -> tuple[tuple[str, bytes, str], ...]:
    """Strict host ingress used by production async action/route seams."""
    if not isinstance(files, (list, tuple)):
        raise ReferencePackError("files must be a list")
    if not files:
        raise ReferencePackError("a Reference Pack requires at least one file")
    if len(files) > limits.max_files:
        raise ReferencePackError(f"too many files (maximum {limits.max_files})")
    if reader is None:
        raise ReferencePackError("an active sandbox/workspace reader is required")
    read = _reader_callable(reader)
    seen: dict[str, str] = {}
    selected: list[tuple[str, bytes, str]] = []
    for raw in files:
        item = _input_parts(raw)
        path = _canonical_path(item.path)
        if path.casefold() in seen:
            raise ReferencePackError(f"duplicate or case-colliding file: {path}")
        seen[path.casefold()] = path
        symlink = await _reader_check_async(reader, "is_symlink", item.path)
        regular = await _reader_check_async(reader, "is_file", item.path)
        if symlink is not False or regular is not True:
            raise ReferencePackError(
                f"workspace reader must prove a non-symlink regular file: {path}"
            )
        value = await _await_value(read(item.path))
        data = _coerce_bytes(value)
        if len(data) > limits.max_file_bytes:
            raise ReferencePackError(f"file exceeds the {limits.max_file_bytes}-byte limit: {path}")
        selected.append((path, data, _file_media_type(item.name or path, item.media_type)))
    selected.sort(key=lambda row: (row[0].casefold(), row[0]))
    if sum(len(row[1]) for row in selected) > limits.max_total_bytes:
        raise ReferencePackError(f"files exceed the {limits.max_total_bytes}-byte total limit")
    return tuple(selected)


def _content_digest(files: tuple[ReferencePackFile, ...]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ReferencePackStore:
    """Durable owner-scoped packs with immutable version directories."""

    def __init__(
        self, root: str | Path | None = None, *, limits: ReferencePackLimits = DEFAULT_LIMITS
    ) -> None:
        self.root = Path(root) if root is not None else _data_root() / "reference-packs"
        self.limits = limits
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def mutation_lock(self):
        """One store-boundary lock shared by library commits and bindings.

        The re-entrant local lock prevents interleaving in one process; the
        existing workspace process fence supplies the same ordering across
        POSIX workers. Binding admission uses this exact context as well, so a
        snapshot cannot copy a head while it is being replaced or removed.
        """

        with _local_mutation_lock(self.root):
            with try_workspace_process_fence(self.root):
                yield

    def _owner_root(self, owner_id: str) -> Path:
        if not isinstance(owner_id, str) or not owner_id or "\x00" in owner_id:
            raise ReferencePackError("invalid owner id")
        # Keep a readable suffix, but bind the complete owner identity to the
        # directory name so lossy sanitization can never merge tenants.
        identity = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        return self.root / f"{identity}-{_safe_component(owner_id, fallback='owner')}"

    def _pack_root(self, owner_id: str, pack_id: str) -> Path:
        return self._owner_root(owner_id) / _safe_component(pack_id, fallback="pack")

    def _load_head(self, owner_id: str, pack_id: str) -> dict[str, Any]:
        path = self._pack_root(owner_id, pack_id) / "head.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ReferencePackNotFound(pack_id) from exc
        if str(raw.get("owner_id")) != owner_id or str(raw.get("id")) != pack_id:
            raise ReferencePackForbidden(pack_id)
        return cast(dict[str, Any], raw)

    def _load_version(self, owner_id: str, pack_id: str, version_id: str) -> ReferencePackVersion:
        head = self._load_head(owner_id, pack_id)
        version_path = (
            self._pack_root(owner_id, pack_id) / "versions" / version_id / "manifest.json"
        )
        try:
            raw = json.loads(version_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ReferencePackNotFound(version_id) from exc
        version = ReferencePackVersion.from_dict(raw)
        if (
            version.pack_id != pack_id
            or version.owner_id != owner_id
            or head.get("owner_id") != owner_id
        ):
            raise ReferencePackForbidden(pack_id)
        return version

    def _write_version(
        self,
        owner_id: str,
        pack_id: str,
        name: str,
        description: str,
        files: tuple[tuple[str, bytes, str], ...],
    ) -> ReferencePackVersion:
        pack_root = self._pack_root(owner_id, pack_id)
        versions = pack_root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        version_id = uuid4().hex
        stage = Path(tempfile.mkdtemp(prefix=f".{version_id}.", dir=versions))
        try:
            stored: list[ReferencePackFile] = []
            for name_path, data, media_type in files:
                target = stage / "files" / name_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                digest = hashlib.sha256(data).hexdigest()
                stored.append(
                    ReferencePackFile(name_path, media_type, len(data), digest, name_path)
                )
            version = ReferencePackVersion(
                id=version_id,
                pack_id=pack_id,
                owner_id=owner_id,
                description=description,
                files=tuple(stored),
                created_at=_now(),
                content_sha256=_content_digest(tuple(stored)),
            )
            _atomic_json(stage / "manifest.json", version.as_dict())
            os.replace(stage, versions / version_id)
            return version
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _remove_version(self, owner_id: str, pack_id: str, version_id: str) -> None:
        shutil.rmtree(
            self._pack_root(owner_id, pack_id) / "versions" / version_id, ignore_errors=True
        )

    @staticmethod
    def _validate_text(name: str, description: str, limits: ReferencePackLimits) -> tuple[str, str]:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > limits.max_name_chars
        ):
            raise ReferencePackError("pack name is empty or too long")
        if not isinstance(description, str) or len(description) > limits.max_description_chars:
            raise ReferencePackError("pack description is too long")
        return unicodedata.normalize("NFC", name.strip()), unicodedata.normalize("NFC", description)

    @_synchronized_mutation
    def create(
        self,
        owner_id: str,
        name: str,
        description: str,
        files: list[Any] | tuple[Any, ...],
        *,
        reader: Any,
    ) -> ReferencePack:
        name, description = self._validate_text(name, description, self.limits)
        selected = _canonical_inputs(files, reader, self.limits)
        pack_id = uuid4().hex
        version = self._write_version(owner_id, pack_id, name, description, selected)
        created = version.created_at
        try:
            _atomic_json(
                self._pack_root(owner_id, pack_id) / "head.json",
                {
                    "id": pack_id,
                    "owner_id": owner_id,
                    "name": name,
                    "description": description,
                    "current_version_id": version.id,
                    "created_at": created,
                    "updated_at": created,
                },
            )
        except Exception:
            shutil.rmtree(self._pack_root(owner_id, pack_id), ignore_errors=True)
            raise
        return self.get(owner_id, pack_id)

    @_synchronized_mutation
    async def acreate(
        self,
        owner_id: str,
        name: str,
        description: str,
        files: list[Any] | tuple[Any, ...],
        *,
        reader: Any,
    ) -> ReferencePack:
        """Production ingress: host stats and async reads precede the atomic commit."""
        name, description = self._validate_text(name, description, self.limits)
        selected = await _canonical_inputs_async(files, reader, self.limits)
        pack_id = uuid4().hex
        version = self._write_version(owner_id, pack_id, name, description, selected)
        created = version.created_at
        try:
            _atomic_json(
                self._pack_root(owner_id, pack_id) / "head.json",
                {
                    "id": pack_id,
                    "owner_id": owner_id,
                    "name": name,
                    "description": description,
                    "current_version_id": version.id,
                    "created_at": created,
                    "updated_at": created,
                },
            )
        except Exception:
            shutil.rmtree(self._pack_root(owner_id, pack_id), ignore_errors=True)
            raise
        return self.get(owner_id, pack_id)

    def get(self, owner_id: str, pack_id: str) -> ReferencePack:
        head = self._load_head(owner_id, pack_id)
        version = self._load_version(owner_id, pack_id, str(head["current_version_id"]))
        return ReferencePack(
            id=str(head["id"]),
            owner_id=str(head["owner_id"]),
            name=str(head["name"]),
            description=str(head.get("description") or version.description),
            current_version_id=version.id,
            current=version,
            created_at=str(head.get("created_at") or version.created_at),
            updated_at=str(head.get("updated_at") or version.created_at),
        )

    def get_version(self, owner_id: str, pack_id: str, version_id: str) -> ReferencePackVersion:
        return self._load_version(owner_id, pack_id, version_id)

    def read_version_file(self, owner_id: str, pack_id: str, version_id: str, name: str) -> bytes:
        version = self.get_version(owner_id, pack_id, version_id)
        item = next((entry for entry in version.files if entry.name == name), None)
        if item is None:
            raise ReferencePackNotFound(name)
        # Manifests are durable data, so do not allow a corrupted/tampered
        # entry to turn a read into a path traversal or symlink follow.
        safe_name = _canonical_path(item.name)
        version_root = self._pack_root(owner_id, pack_id) / "versions" / version_id
        path = version_root / "files" / safe_name
        try:
            resolved = path.resolve(strict=True)
            files_root = (version_root / "files").resolve(strict=True)
            resolved.relative_to(files_root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ReferencePackError("immutable Reference Pack file path is invalid") from exc
        if path.is_symlink() or not path.is_file():
            raise ReferencePackError("immutable Reference Pack file is not a regular file")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise ReferencePackError("immutable Reference Pack bytes failed hash verification")
        return data

    def list(self, owner_id: str) -> tuple[ReferencePack, ...]:
        owner_root = self._owner_root(owner_id)
        if not owner_root.is_dir():
            return ()
        result: list[ReferencePack] = []
        for child in sorted(owner_root.iterdir(), key=lambda p: p.name):
            if child.is_dir() and (child / "head.json").is_file():
                try:
                    result.append(self.get(owner_id, child.name))
                except ReferencePackNotFound:
                    continue
        return tuple(result)

    @_synchronized_mutation
    def update(
        self,
        owner_id: str,
        pack_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        files: list[Any] | tuple[Any, ...] | None = None,
        reader: Any | None = None,
    ) -> ReferencePack:
        current = self.get(owner_id, pack_id)
        next_name, next_description = self._validate_text(
            name if name is not None else current.name,
            description if description is not None else current.description,
            self.limits,
        )
        if files is None:
            selected = tuple(
                (
                    item.name,
                    self.read_version_file(
                        owner_id, pack_id, current.current_version_id, item.name
                    ),
                    item.media_type,
                )
                for item in current.current.files
            )
        else:
            selected = _canonical_inputs(files, reader, self.limits)
        previous_version_id = current.current_version_id
        version = self._write_version(owner_id, pack_id, next_name, next_description, selected)
        head = self._load_head(owner_id, pack_id)
        head.update(
            {
                "name": next_name,
                "description": next_description,
                "current_version_id": version.id,
                "updated_at": version.created_at,
            }
        )
        try:
            _atomic_json(self._pack_root(owner_id, pack_id) / "head.json", head)
        except Exception:
            self._remove_version(owner_id, pack_id, version.id)
            raise
        # The library deliberately has one mutable head, not a user-visible
        # version-history product. Builds that already selected this pack own
        # their copied snapshot, so the replaced library bytes are no longer an
        # authority and can be removed after the atomic head swap.
        if previous_version_id != version.id:
            self._remove_version(owner_id, pack_id, previous_version_id)
        return self.get(owner_id, pack_id)

    @_synchronized_mutation
    async def aupdate(
        self,
        owner_id: str,
        pack_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        files: list[Any] | tuple[Any, ...] | None = None,
        reader: Any | None = None,
    ) -> ReferencePack:
        current = self.get(owner_id, pack_id)
        next_name, next_description = self._validate_text(
            name if name is not None else current.name,
            description if description is not None else current.description,
            self.limits,
        )
        if files is None:
            selected = tuple(
                (
                    item.name,
                    self.read_version_file(
                        owner_id, pack_id, current.current_version_id, item.name
                    ),
                    item.media_type,
                )
                for item in current.current.files
            )
        else:
            selected = await _canonical_inputs_async(files, reader, self.limits)
        previous_version_id = current.current_version_id
        version = self._write_version(owner_id, pack_id, next_name, next_description, selected)
        head = self._load_head(owner_id, pack_id)
        head.update(
            {
                "name": next_name,
                "description": next_description,
                "current_version_id": version.id,
                "updated_at": version.created_at,
            }
        )
        try:
            _atomic_json(self._pack_root(owner_id, pack_id) / "head.json", head)
        except Exception:
            self._remove_version(owner_id, pack_id, version.id)
            raise
        if previous_version_id != version.id:
            self._remove_version(owner_id, pack_id, previous_version_id)
        return self.get(owner_id, pack_id)

    @staticmethod
    def _canonical_uploaded_files(
        files: list[tuple[str, bytes, str]],
        limits: ReferencePackLimits,
    ) -> tuple[tuple[str, bytes, str], ...]:
        """Validate already-read browser uploads before starting a new version.

        Uploads are deliberately kept separate from ``_canonical_inputs``:
        Settings is allowed to send bytes directly, while Agent-created packs
        must continue to read only through an active workspace reader.
        """
        if not isinstance(files, list):
            raise ReferencePackError("uploaded files must be a list")
        if len(files) > limits.max_files:
            raise ReferencePackError(f"too many files (maximum {limits.max_files})")
        seen: dict[str, str] = {}
        selected: list[tuple[str, bytes, str]] = []
        for raw_name, data, media_type in files:
            path = _canonical_path(raw_name)
            if path.casefold() in seen:
                raise ReferencePackError(f"duplicate or case-colliding file: {path}")
            seen[path.casefold()] = path
            if not isinstance(data, bytes):
                raise ReferencePackError(f"uploaded file is not bytes: {path}")
            if len(data) > limits.max_file_bytes:
                raise ReferencePackError(
                    f"file exceeds the {limits.max_file_bytes}-byte limit: {path}"
                )
            selected.append((path, data, _file_media_type(path, media_type)))
        selected.sort(key=lambda row: (row[0].casefold(), row[0]))
        if sum(len(row[1]) for row in selected) > limits.max_total_bytes:
            raise ReferencePackError(f"files exceed the {limits.max_total_bytes}-byte total limit")
        return tuple(selected)

    @_synchronized_mutation
    def update_files(
        self,
        owner_id: str,
        pack_id: str,
        *,
        uploads: list[tuple[str, bytes, str]],
        remove: list[str] | tuple[str, ...] = (),
    ) -> ReferencePack:
        """Atomically replace/add uploads and remove exact current paths.

        All bytes and path operations are validated before ``_write_version``.
        The existing head remains authoritative until the staged version is
        complete, so a rejected batch cannot partially change a pack.
        """
        current = self.get(owner_id, pack_id)
        uploaded = self._canonical_uploaded_files(uploads, self.limits)
        remove_paths: set[str] = set()
        for raw_name in remove:
            path = _canonical_path(raw_name)
            if path.casefold() in remove_paths:
                raise ReferencePackError(f"duplicate or case-colliding removal: {path}")
            remove_paths.add(path.casefold())
        upload_paths = {path.casefold() for path, _data, _media in uploaded}
        if remove_paths & upload_paths:
            raise ReferencePackError("a file cannot be uploaded and removed in the same batch")

        existing: dict[str, tuple[str, bytes, str]] = {}
        for item in current.current.files:
            key = item.name.casefold()
            if key not in remove_paths:
                existing[key] = (
                    item.name,
                    self.read_version_file(
                        owner_id, pack_id, current.current_version_id, item.name
                    ),
                    item.media_type,
                )
        for item in uploaded:
            existing[item[0].casefold()] = item
        if not existing:
            raise ReferencePackError("a Reference Pack requires at least one file")
        if len(existing) > self.limits.max_files:
            raise ReferencePackError(f"too many files (maximum {self.limits.max_files})")
        selected = tuple(sorted(existing.values(), key=lambda row: (row[0].casefold(), row[0])))
        if sum(len(row[1]) for row in selected) > self.limits.max_total_bytes:
            raise ReferencePackError(
                f"files exceed the {self.limits.max_total_bytes}-byte total limit"
            )

        previous_version_id = current.current_version_id
        version = self._write_version(
            owner_id, pack_id, current.name, current.description, selected
        )
        head = self._load_head(owner_id, pack_id)
        head.update({"current_version_id": version.id, "updated_at": version.created_at})
        try:
            _atomic_json(self._pack_root(owner_id, pack_id) / "head.json", head)
        except Exception:
            self._remove_version(owner_id, pack_id, version.id)
            raise
        if previous_version_id != version.id:
            self._remove_version(owner_id, pack_id, previous_version_id)
        return self.get(owner_id, pack_id)

    @_synchronized_mutation
    def delete(self, owner_id: str, pack_id: str) -> bool:
        self._load_head(owner_id, pack_id)
        pack_root = self._pack_root(owner_id, pack_id)
        tombstone = pack_root.with_name(f".deleted-{pack_root.name}-{secrets.token_hex(8)}")
        # Rename first so the pack disappears atomically from all future
        # listings/bindings, then remove the complete library copy. Existing
        # Build snapshots live in the binding store and remain untouched.
        os.replace(pack_root, tombstone)
        shutil.rmtree(tombstone)
        return True


__all__ = [
    "DEFAULT_LIMITS",
    "ReferencePack",
    "ReferencePackError",
    "ReferencePackFile",
    "ReferencePackFileInput",
    "ReferencePackForbidden",
    "ReferencePackLimits",
    "ReferencePackNotFound",
    "ReferencePackStore",
    "ReferencePackVersion",
    "WorkspaceReader",
]

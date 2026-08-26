"""Immutable conversation snapshots and Build workspace materialization."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast
from uuid import uuid4

from disco.core.env import disco_env

from .reference_pack_store import (
    ReferencePack,
    ReferencePackError,
    ReferencePackFile,
    ReferencePackNotFound,
    ReferencePackStore,
)


@dataclass(frozen=True)
class PinnedReferencePack:
    pack_id: str
    version_id: str
    name: str
    description: str
    files: tuple[ReferencePackFile, ...]
    content_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "version_id": self.version_id,
            "name": self.name,
            "description": self.description,
            "files": [item.as_dict() for item in self.files],
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PinnedReferencePack:
        return cls(
            pack_id=str(raw["pack_id"]),
            version_id=str(raw["version_id"]),
            name=str(raw["name"]),
            description=str(raw.get("description") or ""),
            files=tuple(ReferencePackFile.from_dict(item) for item in raw.get("files", [])),
            content_sha256=str(raw.get("content_sha256") or ""),
        )


@dataclass(frozen=True)
class ReferencePackBinding:
    id: str
    owner_id: str
    conversation_id: str
    packs: tuple[PinnedReferencePack, ...]
    created_at: str

    @property
    def pack_ids(self) -> tuple[str, ...]:
        return tuple(item.pack_id for item in self.packs)

    @property
    def version_ids(self) -> tuple[str, ...]:
        return tuple(item.version_id for item in self.packs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "binding_id": self.id,
            "owner_id": self.owner_id,
            "conversation_id": self.conversation_id,
            "packs": [item.as_dict() for item in self.packs],
            "pack_ids": list(self.pack_ids),
            "version_ids": list(self.version_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ReferencePackBinding:
        return cls(
            id=str(raw.get("id") or raw["binding_id"]),
            owner_id=str(raw["owner_id"]),
            conversation_id=str(raw["conversation_id"]),
            packs=tuple(PinnedReferencePack.from_dict(item) for item in raw.get("packs", [])),
            created_at=str(raw.get("created_at") or ""),
        )


@dataclass(frozen=True)
class ReferencePackSelection:
    """Observed mutable-head identity supplied at Build admission."""

    pack_id: str
    version_id: str
    content_sha256: str


class ReferencePackBindingConflict(ReferencePackError):
    """A conversation already has a different immutable pack selection."""


class ReferenceMaterializationTarget(Protocol):
    """The narrow async workspace seam used by Build materialization.

    A target is deliberately not a ``Path`` (or a sandbox-specific class).  The
    host owns path validation, directory creation, and isolation; this module
    only sends relative paths and exact bytes across the seam.  ``read_file``
    is optional at runtime and, when present, is used as a post-write integrity
    check by :func:`amaterialize_reference_binding`.
    """

    async def write_file(self, path: str, data: bytes) -> None: ...

    async def read_file(self, path: str) -> bytes: ...


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _root(root: str | Path | None) -> Path:
    if root is not None:
        return Path(root)
    raw = disco_env("DATA_DIR")
    return (Path(raw) if raw else Path.cwd() / ".disco-data") / "reference-pack-bindings"


def _safe(value: str, fallback: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return value[:100] or fallback


def _scoped(value: str, fallback: str) -> str:
    return f"{hashlib.sha256(value.encode('utf-8')).hexdigest()}-{_safe(value, fallback)}"


def _safe_snapshot_name(raw: str) -> str:
    """Validate a persisted file name before joining it to a snapshot root."""

    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ReferencePackError("pinned Reference Pack file path is invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReferencePackError("pinned Reference Pack file path is invalid")
    return unicodedata.normalize("NFC", "/".join(path.parts))


class ReferencePackBindingStore:
    """One immutable binding per owner/conversation, durable across restart."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = _root(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _conversation_root(self, owner_id: str, conversation_id: str) -> Path:
        return self.root / _scoped(owner_id, "owner") / _scoped(conversation_id, "conversation")

    def _read(self, owner_id: str, conversation_id: str) -> ReferencePackBinding | None:
        path = self._conversation_root(owner_id, conversation_id) / "binding.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        binding = ReferencePackBinding.from_dict(raw)
        if binding.owner_id != owner_id or binding.conversation_id != conversation_id:
            raise ReferencePackError("binding owner/conversation identity mismatch")
        return binding

    @staticmethod
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

    def get(self, owner_id: str, conversation_id: str) -> ReferencePackBinding | None:
        return self._read(owner_id, conversation_id)

    def bind(
        self,
        owner_id: str,
        conversation_id: str,
        selections: Iterable[ReferencePackSelection | Mapping[str, Any] | str],
        *,
        packs: ReferencePackStore,
    ) -> ReferencePackBinding:
        # Admission and byte copying share the library's mutation boundary.
        # This prevents update/delete from racing a snapshot read between its
        # mutable head check and immutable byte copy.
        with packs.mutation_lock():
            return self._bind(owner_id, conversation_id, selections, packs=packs)

    @staticmethod
    def _parse_selections(
        selections: Iterable[ReferencePackSelection | Mapping[str, Any] | str],
    ) -> list[ReferencePackSelection]:
        parsed: list[ReferencePackSelection] = []
        for raw in selections:
            if isinstance(raw, ReferencePackSelection):
                parsed.append(raw)
                continue
            if not isinstance(raw, Mapping):
                raise ReferencePackError(
                    "pack selection identity is required (pack_id, version_id, content_sha256)"
                )
            try:
                parsed.append(
                    ReferencePackSelection(
                        str(raw["pack_id"]), str(raw["version_id"]), str(raw["content_sha256"])
                    )
                )
            except KeyError as exc:
                raise ReferencePackError(
                    "each pack selection requires pack_id, version_id, and content_sha256"
                ) from exc
        if not parsed:
            raise ReferencePackError("at least one Reference Pack must be selected")
        if len({item.pack_id for item in parsed}) != len(parsed):
            raise ReferencePackError("duplicate Reference Pack selection")
        return parsed

    @staticmethod
    def _copy_selected(
        owner_id: str,
        parsed: list[ReferencePackSelection],
        packs: ReferencePackStore,
    ) -> list[tuple[ReferencePack, tuple[tuple[ReferencePackFile, bytes], ...]]]:
        selected = []
        for selection in parsed:
            pack = packs.get(owner_id, selection.pack_id)
            if (
                pack.current_version_id != selection.version_id
                or pack.current.content_sha256 != selection.content_sha256
            ):
                raise ReferencePackBindingConflict(
                    f"Reference Pack {selection.pack_id!r} changed after selection"
                )
            copied = []
            for item in pack.current.files:
                data = packs.read_version_file(
                    owner_id, pack.id, pack.current_version_id, item.name
                )
                if hashlib.sha256(data).hexdigest() != item.sha256:
                    raise ReferencePackError("pack bytes failed hash verification during binding")
                copied.append((item, data))
            selected.append((pack, tuple(copied)))
        return selected

    def _install_binding(
        self,
        owner_id: str,
        conversation_id: str,
        parsed: list[ReferencePackSelection],
        selected: list[tuple[ReferencePack, tuple[tuple[ReferencePackFile, bytes], ...]]],
    ) -> ReferencePackBinding:
        binding_id = uuid4().hex
        stage = Path(tempfile.mkdtemp(prefix=f".{binding_id}.", dir=self.root))
        try:
            pinned = []
            for pack, copied_files in selected:
                pack_stage = stage / "packs" / pack.id / pack.current_version_id / "files"
                pack_stage.mkdir(parents=True, exist_ok=True)
                for item, data in copied_files:
                    target = pack_stage / item.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                pinned.append(
                    PinnedReferencePack(
                        pack_id=pack.id,
                        version_id=pack.current_version_id,
                        name=pack.name,
                        description=pack.description,
                        files=tuple(item for item, _ in copied_files),
                        content_sha256=pack.current.content_sha256,
                    )
                )
            binding = ReferencePackBinding(
                binding_id, owner_id, conversation_id, tuple(pinned), _now()
            )
            self._atomic_json(stage / "binding.json", binding.as_dict())
            target = self._conversation_root(owner_id, conversation_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                return self._existing_winner(owner_id, conversation_id, parsed)
            try:
                os.replace(stage, target)
            except FileExistsError:
                return self._existing_winner(owner_id, conversation_id, parsed)
            return binding
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _existing_winner(
        self, owner_id: str, conversation_id: str, parsed: list[ReferencePackSelection]
    ) -> ReferencePackBinding:
        winner = self._read(owner_id, conversation_id)
        if winner is None:
            raise ReferencePackError("an existing Reference Pack binding is unreadable")
        same = tuple(
            (item.pack_id, item.version_id, item.content_sha256) for item in winner.packs
        ) == tuple((item.pack_id, item.version_id, item.content_sha256) for item in parsed)
        if not same:
            raise ReferencePackBindingConflict(
                "conversation already has a different Reference Pack snapshot"
            )
        return winner

    def _bind(
        self,
        owner_id: str,
        conversation_id: str,
        selections: Iterable[ReferencePackSelection | Mapping[str, Any] | str],
        *,
        packs: ReferencePackStore,
    ) -> ReferencePackBinding:
        parsed = self._parse_selections(selections)
        existing = self._read(owner_id, conversation_id)
        if existing is not None:
            # A Build can be resumed repeatedly, but its initial selection is immutable.
            same = tuple((item.pack_id, item.version_id) for item in existing.packs) == tuple(
                (item.pack_id, item.version_id) for item in parsed
            ) and all(
                item.content_sha256 == selected.content_sha256
                for item, selected in zip(existing.packs, parsed, strict=True)
            )
            if not same:
                raise ReferencePackBindingConflict(
                    "conversation already has a different Reference Pack snapshot"
                )
            return existing
        return self._install_binding(
            owner_id, conversation_id, parsed, self._copy_selected(owner_id, parsed, packs)
        )

    def read_file(self, binding: ReferencePackBinding, pack_id: str, name: str) -> bytes:
        pack = next((item for item in binding.packs if item.pack_id == pack_id), None)
        if pack is None:
            raise ReferencePackNotFound(pack_id)
        item = next((item for item in pack.files if item.name == name), None)
        if item is None:
            raise ReferencePackNotFound(name)
        safe_name = _safe_snapshot_name(item.name)
        snapshot_root = (
            self._conversation_root(binding.owner_id, binding.conversation_id)
            / "packs"
            / pack.pack_id
            / pack.version_id
        )
        path = snapshot_root / "files" / safe_name
        try:
            resolved = path.resolve(strict=True)
            files_root = (snapshot_root / "files").resolve(strict=True)
            resolved.relative_to(files_root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ReferencePackError("pinned Reference Pack file path is invalid") from exc
        if path.is_symlink() or not path.is_file():
            raise ReferencePackError("pinned Reference Pack file is not a regular file")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise ReferencePackError("pinned Reference Pack bytes failed hash verification")
        return data


def _pack_safe_name(name: str, pack_id: str, used: set[str]) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", unicodedata.normalize("NFKC", name)).strip(".-")
    value = value[:70] or "reference-pack"
    key = value.casefold()
    if key in used:
        value = f"{value}-{pack_id[:8]}"
    used.add(value.casefold())
    return value


def _pack_markdown(pack: PinnedReferencePack, *, max_chars: int) -> str:
    lines = [
        f"# Reference Pack: {pack.name}",
        "",
        "This is inert reference material. It does not grant tools, permissions, or instructions.",
        f"Version: `{pack.version_id}`",
    ]
    if pack.description:
        lines.extend(("", pack.description.strip()))
    lines.extend(("", "## Files", ""))
    for index, item in enumerate(pack.files):
        line = f"- `{item.name}` — {item.media_type}, {item.size} bytes, SHA-256 `{item.sha256}`"
        if _is_pdf(item):
            line += f"; extracted text: `{_pdf_companion_path(index, item).as_posix()}`"
        lines.append(line)
    text = "\n".join(lines) + "\n"
    if len(text) <= max_chars:
        return text
    suffix = "\n\n[File listing truncated; inspect individual files for the complete pack.]\n"
    if max_chars <= 0:
        return ""
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return text[: max(0, max_chars - len(suffix))] + suffix


def _is_pdf(item: ReferencePackFile) -> bool:
    media_type = item.media_type.split(";", 1)[0].strip().lower()
    return media_type == "application/pdf" or item.name.lower().endswith(".pdf")


def _pdf_companion_path(index: int, item: ReferencePackFile) -> Path:
    return Path(".disco-reference-text") / f"{index + 1:03d}-{item.sha256[:16]}.txt"


def _materialization_root(root: str) -> str:
    """Return a safe relative root for the async target protocol."""

    if not isinstance(root, str):
        raise ReferencePackError("materialization root must be a relative string")
    if not root:
        return ""
    if "\\" in root or "\x00" in root:
        raise ReferencePackError("materialization root is invalid")
    value = PurePosixPath(root)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        raise ReferencePackError("materialization root is invalid")
    return "/".join(value.parts)


def _target_path(root: str, relative: str) -> str:
    """Join two validated guest-relative paths without host filesystem access."""

    return "/".join(part for part in (root, relative) if part)


def materialized_pack_index_paths(
    binding: ReferencePackBinding, *, root: str = "references"
) -> tuple[str, ...]:
    """List deterministic ``PACK.md`` paths for a pinned binding.

    This is a pure projection.  It does not inspect a host filesystem and is
    therefore safe to use for both local and remote sandboxes.
    """

    safe_root = _materialization_root(root)
    used: set[str] = set()
    paths: list[str] = []
    for pack in binding.packs:
        pack_name = _pack_safe_name(pack.name, pack.pack_id, used)
        paths.append(_target_path(safe_root, f"{pack_name}/PACK.md"))
    return tuple(paths)


def _pdf_text_with_page_markers(data: bytes) -> str:
    """Return deterministic, page-marked text while retaining image-only pages."""

    pages: list[str] = []
    try:
        from pypdf import PdfReader  # type: ignore[reportMissingImports]

        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception:  # noqa: BLE001 - optional parser; bounded fallback below
        try:
            from .uploads_ingest import _pdf_to_text

            pages = [_pdf_to_text(data).strip()]
        except Exception:  # noqa: BLE001 - an unreadable PDF remains an honest asset
            pages = []
    if not pages:
        pages = [""]
    return (
        "\n\n".join(
            f"--- Page {number} ---\n{text or '[No extractable text on this page]'}"
            for number, text in enumerate(pages, start=1)
        )
        + "\n"
    )


async def amaterialize_reference_binding(
    binding: ReferencePackBinding,
    snapshot_store: ReferencePackBindingStore,
    target: ReferenceMaterializationTarget,
    *,
    root: str = "references",
    max_pack_markdown_chars: int = 24_000,
    verify_writes: bool = True,
) -> tuple[str, ...]:
    """Materialize an immutable binding through an async sandbox protocol.

    ``target`` is the active Build sandbox (or a remote equivalent), never a
    host ``Path``.  Every source byte is fetched from the pinned snapshot and
    written under a deterministic, binding-local path.  If the target exposes
    ``read_file`` (all production sandbox instances do), each write is read
    back and compared so a transport/backend alias cannot silently leak or
    corrupt another conversation's materialization.

    The operation intentionally has no host-side staging directory: a remote
    sandbox cannot be represented by one.  Hosts that need a transactional
    publish can provide an atomic ``atomic_write`` method; ordinary targets
    still receive complete file writes and never an interpolated shell command.
    """

    safe_root = _materialization_root(root)
    writer_value = getattr(target, "atomic_write", None) or getattr(target, "write_file", None)
    if not callable(writer_value):
        raise ReferencePackError("materialization target cannot write files")
    writer = cast(Callable[[str, bytes], Awaitable[None]], writer_value)
    reader_value = getattr(target, "read_file", None)
    if verify_writes and not callable(reader_value):
        raise ReferencePackError("materialization target must expose a safe read_file verifier")
    reader = cast(Callable[[str], Awaitable[bytes]], reader_value)

    used: set[str] = set()
    index_paths: list[str] = []

    async def write(path: str, data: bytes) -> None:
        await writer(path, data)
        if verify_writes:
            observed = await reader(path)
            if not isinstance(observed, bytes) or observed != data:
                raise ReferencePackError(f"sandbox materialization verification failed: {path}")

    for pack in binding.packs:
        pack_name = _pack_safe_name(pack.name, pack.pack_id, used)
        pack_root = _target_path(safe_root, f"{pack_name}")
        pack_md_path = _target_path(pack_root, "PACK.md")
        index_paths.append(pack_md_path)
        await write(
            pack_md_path,
            _pack_markdown(pack, max_chars=max_pack_markdown_chars).encode("utf-8"),
        )
        for index, item in enumerate(pack.files):
            safe_name = _safe_snapshot_name(item.name)
            relative = "/".join((pack_root, safe_name))
            data = snapshot_store.read_file(binding, pack.pack_id, item.name)
            if hashlib.sha256(data).hexdigest() != item.sha256:
                raise ReferencePackError("pinned Reference Pack bytes failed hash verification")
            await write(relative, data)
            if _is_pdf(item):
                companion_name = _pdf_companion_path(index, item).as_posix()
                companion = _target_path(pack_root, companion_name)
                await write(companion, _pdf_text_with_page_markers(data).encode("utf-8"))
    return tuple(index_paths)


# Shorter host-facing alias; the sync API below remains for existing local
# integrations and tests.
materialize_binding_async = amaterialize_reference_binding


def materialize_reference_binding(
    binding: ReferencePackBinding,
    snapshot_store: ReferencePackBindingStore,
    workspace: str | Path,
    *,
    max_pack_markdown_chars: int = 24_000,
) -> tuple[Path, ...]:
    """Materialize pinned bytes into ``references/<safe-pack>/`` atomically."""

    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    destination = workspace_path / "references"
    stage = Path(tempfile.mkdtemp(prefix=".reference-pack.", dir=workspace_path))
    paths: list[Path] = []
    used: set[str] = set()
    try:
        for pack in binding.packs:
            pack_name = _pack_safe_name(pack.name, pack.pack_id, used)
            pack_dir = stage / pack_name
            pack_dir.mkdir(parents=True, exist_ok=True)
            (pack_dir / "PACK.md").write_text(
                _pack_markdown(pack, max_chars=max_pack_markdown_chars), encoding="utf-8"
            )
            for index, item in enumerate(pack.files):
                safe_name = _safe_snapshot_name(item.name)
                target = pack_dir / safe_name
                try:
                    target.resolve().relative_to(pack_dir.resolve())
                except ValueError as exc:
                    raise ReferencePackError("pinned Reference Pack file path is invalid") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                data = snapshot_store.read_file(binding, pack.pack_id, item.name)
                target.write_bytes(data)
                if _is_pdf(item):
                    companion = pack_dir / _pdf_companion_path(index, item)
                    companion.parent.mkdir(parents=True, exist_ok=True)
                    companion.write_text(_pdf_text_with_page_markers(data), encoding="utf-8")
            paths.append(destination / pack_name)
        # Swap the complete directory tree in one rename sequence.  Readers
        # see either the previous complete materialization or the new one;
        # there is no partially populated ``references/`` tree.
        backup: Path | None = None
        if destination.exists():
            backup = destination.with_name(f".references-old-{secrets.token_hex(6)}")
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except Exception:
            if backup is not None and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        return tuple(paths)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# Friendly aliases used by host integration and tests.
ReferencePackBindingService = ReferencePackBindingStore
materialize_binding = materialize_reference_binding


__all__ = [
    "PinnedReferencePack",
    "ReferencePackBinding",
    "ReferencePackBindingConflict",
    "ReferencePackBindingService",
    "ReferencePackBindingStore",
    "ReferenceMaterializationTarget",
    "ReferencePackSelection",
    "amaterialize_reference_binding",
    "materialize_binding",
    "materialize_binding_async",
    "materialize_reference_binding",
    "materialized_pack_index_paths",
]

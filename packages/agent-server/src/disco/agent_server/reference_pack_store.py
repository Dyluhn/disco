"""Persistent, owner-scoped Reference Packs: reusable collections of a user's files.

A Reference Pack is inert user data — a name, a description and an ordered set
of copied files with their hashes and readability states. It grants nothing
(no tools, no capabilities, no policy); instruction-like text inside it is
reference material. The library keeps one editable current revision; a Build
that selects a pack snapshots its exact bytes (see ``reference_pack_binding``).

Layout under the projects root:

    reference-packs/<pack_id>/manifest.json
    reference-packs/<pack_id>/files/<name>

Creation is atomic (the pack is assembled in a temporary directory and renamed
into place), so a failed multi-file save commits nothing. Bounds: 50 files,
25 MB per file, 200 MB per pack.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .uploads_ingest import _pdf_to_text

MAX_FILES = 50
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PACK_BYTES = 200 * 1024 * 1024
MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 2000

FileState = Literal["ready", "asset_only", "unreadable"]

_SAFE_ID = re.compile(r"^rp_[0-9a-f]{32}$")
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".html",
        ".htm",
        ".svg",
        ".xml",
        ".css",
        ".js",
        ".mjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".cs",
        ".sh",
        ".sql",
        ".ini",
        ".cfg",
        ".env.example",
        ".rst",
        ".tex",
    }
)
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
}


class ReferencePackError(ValueError):
    """A pack operation was refused; the message is user-facing."""


def safe_filename(raw: str) -> str | None:
    """The uploads rule: basename only, NFC, no leading dots, whitespace runs → '-'."""
    name = unicodedata.normalize("NFC", Path(raw).name).strip().lstrip(".")
    name = re.sub(r"\s+", "-", name)
    return name or None


def media_type_for(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def classify_file(name: str, data: bytes) -> FileState:
    """``ready`` = the agent can read it as text (a text PDF included);
    ``asset_only`` = pixels or a PDF with no extractable text; ``unreadable`` = neither."""
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "asset_only"
    if suffix == ".pdf":
        return "ready" if _pdf_to_text(data).strip() else "asset_only"
    if suffix in _TEXT_EXTENSIONS or not suffix:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return "unreadable"
        return "ready"
    return "unreadable"


def extracted_text(name: str, data: bytes) -> str | None:
    """The text the agent reads for a ``ready`` file: the bytes for text files,
    the extraction (with page markers) for a text PDF, None otherwise."""
    if classify_file(name, data) != "ready":
        return None
    if Path(name).suffix.lower() == ".pdf":
        return _pdf_to_text(data)
    return data.decode("utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class ReferencePackFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    media_type: str
    bytes: int
    sha256: str
    state: FileState


class ReferencePackRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    pack_id: str
    owner_id: str
    name: str
    description: str = ""
    created_at: str
    updated_at: str
    digest: str
    files: tuple[ReferencePackFile, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    def summary(self) -> dict:
        return {
            "pack_id": self.pack_id,
            "name": self.name,
            "description": self.description,
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "digest": self.digest,
        }

    def detail(self) -> dict:
        return self.summary() | {"files": [f.model_dump() for f in self.files]}


def pack_digest(files: tuple[ReferencePackFile, ...]) -> str:
    return hashlib.sha256("\n".join(f"{f.name}:{f.sha256}" for f in files).encode()).hexdigest()


def _validate_meta(name: str, description: str) -> tuple[str, str]:
    name = re.sub(r"\s+", " ", name.strip())
    if not name or len(name) > MAX_NAME_CHARS:
        raise ReferencePackError(f"name must be 1–{MAX_NAME_CHARS} characters")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ReferencePackError(f"description must be at most {MAX_DESCRIPTION_CHARS} characters")
    return name, description


def _validate_file(name: str, data: bytes) -> str:
    clean = safe_filename(name)
    if clean is None or clean == "manifest.json":
        raise ReferencePackError(f"invalid file name {name!r}")
    if len(data) > MAX_FILE_BYTES:
        raise ReferencePackError(f"{clean} exceeds the 25 MB per-file limit")
    return clean


class JsonReferencePackStore:
    """Read/write pack records and bytes under ``<projects_root>/reference-packs``."""

    def __init__(self, projects_root: str | Path) -> None:
        self._dir = Path(projects_root).expanduser() / "reference-packs"

    @property
    def root(self) -> Path:
        return self._dir

    def _pack_dir(self, pack_id: str) -> Path:
        if not _SAFE_ID.fullmatch(pack_id):
            raise ReferencePackError(f"unsafe pack id {pack_id!r}")
        return self._dir / pack_id

    def _write_manifest(self, directory: Path, record: ReferencePackRecord) -> None:
        tmp = directory / f".manifest-{uuid.uuid4().hex}.json"
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, directory / "manifest.json")

    def _load(self, pack_id: str) -> ReferencePackRecord | None:
        path = self._pack_dir(pack_id) / "manifest.json"
        if not path.is_file():
            return None
        try:
            return ReferencePackRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    # ---- reads ---------------------------------------------------------------

    def list(self, owner_id: str) -> list[ReferencePackRecord]:
        if not self._dir.is_dir():
            return []
        rows = []
        for path in self._dir.iterdir():
            if _SAFE_ID.fullmatch(path.name):
                record = self._load(path.name)
                if record is not None and record.owner_id == owner_id:
                    rows.append(record)
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    def get(self, pack_id: str, owner_id: str) -> ReferencePackRecord | None:
        try:
            record = self._load(pack_id)
        except ReferencePackError:
            return None
        return record if record is not None and record.owner_id == owner_id else None

    def file_path(self, pack_id: str, owner_id: str, name: str) -> Path | None:
        record = self.get(pack_id, owner_id)
        if record is None or not any(f.name == name for f in record.files):
            return None
        path = self._pack_dir(pack_id) / "files" / name
        return path if path.is_file() else None

    def read_file(self, pack_id: str, owner_id: str, name: str) -> bytes | None:
        path = self.file_path(pack_id, owner_id, name)
        return path.read_bytes() if path is not None else None

    # ---- writes --------------------------------------------------------------

    def create(
        self,
        *,
        owner_id: str,
        name: str,
        description: str = "",
        files: list[tuple[str, bytes]],
    ) -> ReferencePackRecord:
        """Atomic: the pack lands whole or not at all."""
        name, description = _validate_meta(name, description)
        if not files:
            raise ReferencePackError("a pack needs at least one file")
        if len(files) > MAX_FILES:
            raise ReferencePackError(f"a pack holds at most {MAX_FILES} files")
        entries: list[ReferencePackFile] = []
        clean_files: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for raw_name, data in files:
            clean = _validate_file(raw_name, data)
            if clean in seen:
                raise ReferencePackError(f"duplicate file name {clean!r}")
            seen.add(clean)
            entries.append(_entry(clean, data))
            clean_files.append((clean, data))
        if sum(len(d) for _, d in clean_files) > MAX_PACK_BYTES:
            raise ReferencePackError("a pack holds at most 200 MB")
        pack_id = f"rp_{uuid.uuid4().hex}"
        now = _now()
        record = ReferencePackRecord(
            pack_id=pack_id,
            owner_id=owner_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
            digest=pack_digest(tuple(entries)),
            files=tuple(entries),
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        staging = self._dir / f".staging-{pack_id}"
        try:
            (staging / "files").mkdir(parents=True)
            for clean, data in clean_files:
                (staging / "files" / clean).write_bytes(data)
            self._write_manifest(staging, record)
            os.rename(staging, self._dir / pack_id)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return record

    def update_meta(
        self,
        pack_id: str,
        owner_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> ReferencePackRecord | None:
        record = self.get(pack_id, owner_id)
        if record is None:
            return None
        new_name, new_description = _validate_meta(
            record.name if name is None else name,
            record.description if description is None else description,
        )
        updated = record.model_copy(
            update={"name": new_name, "description": new_description, "updated_at": _now()}
        )
        self._write_manifest(self._pack_dir(pack_id), updated)
        return updated

    def put_file(
        self, pack_id: str, owner_id: str, name: str, data: bytes
    ) -> ReferencePackRecord | None:
        """Add a file, or replace the one with the same name (order preserved)."""
        record = self.get(pack_id, owner_id)
        if record is None:
            return None
        clean = _validate_file(name, data)
        others = [f for f in record.files if f.name != clean]
        if len(others) + 1 > MAX_FILES:
            raise ReferencePackError(f"a pack holds at most {MAX_FILES} files")
        if sum(f.bytes for f in others) + len(data) > MAX_PACK_BYTES:
            raise ReferencePackError("a pack holds at most 200 MB")
        entry = _entry(clean, data)
        files = tuple(entry if f.name == clean else f for f in record.files)
        if not any(f.name == clean for f in record.files):
            files = (*record.files, entry)
        directory = self._pack_dir(pack_id)
        tmp = directory / "files" / f".{clean}.{uuid.uuid4().hex}"
        tmp.write_bytes(data)
        os.replace(tmp, directory / "files" / clean)
        updated = record.model_copy(
            update={"files": files, "digest": pack_digest(files), "updated_at": _now()}
        )
        self._write_manifest(directory, updated)
        return updated

    def remove_file(self, pack_id: str, owner_id: str, name: str) -> ReferencePackRecord | None:
        record = self.get(pack_id, owner_id)
        if record is None:
            return None
        files = tuple(f for f in record.files if f.name != name)
        if len(files) == len(record.files):
            raise ReferencePackError(f"no file named {name!r} in the pack")
        if not files:
            raise ReferencePackError("a pack keeps at least one file; delete the pack instead")
        directory = self._pack_dir(pack_id)
        updated = record.model_copy(
            update={"files": files, "digest": pack_digest(files), "updated_at": _now()}
        )
        self._write_manifest(directory, updated)
        (directory / "files" / name).unlink(missing_ok=True)
        return updated

    def delete(self, pack_id: str, owner_id: str) -> bool:
        if self.get(pack_id, owner_id) is None:
            return False
        directory = self._pack_dir(pack_id)
        doomed = self._dir / f".deleting-{pack_id}-{uuid.uuid4().hex}"
        os.rename(directory, doomed)
        shutil.rmtree(doomed, ignore_errors=True)
        return True


def _entry(name: str, data: bytes) -> ReferencePackFile:
    return ReferencePackFile(
        name=name,
        media_type=media_type_for(name),
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        state=classify_file(name, data),
    )

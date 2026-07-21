"""ProjectStore — the disk-side catalogue + tree under the user-chosen path.

Layout (the chosen design — mirrored 1:1, no zip-per-project):

    <projects_root>/
      <conversation_id>/
        manifest.json
        workspace/<file tree>

The manifest is the cheap row for the list view (title, file_count, byte total,
last_snapshot_at); the workspace dir is the load-bearing artifact. Path traversal
is rejected (no `..`, no absolute child paths).

`validate_root()` returns a typed status the agent-server maps to clear, named
errors — never crashes, never silently "reloads empty." The full graceful-
failure matrix:
  - root unset (`projects_root == ""`)            → StorageStatus.UNSET
  - root path doesn't exist                       → StorageStatus.NOT_FOUND
  - exists but isn't a directory                  → StorageStatus.NOT_A_DIRECTORY
  - exists but can't be written / probed          → StorageStatus.NOT_WRITABLE
  - all good                                      → StorageStatus.OK

Zero-config self-healing (E4):
`resolve_projects_root(configured)` converts a configured value to an effective
path.  When `configured` is empty (fresh install / unset), it falls back to
`default_projects_root()` and ensures that directory exists.  When `configured`
is non-empty, it is returned verbatim so the user-set path keeps its current
validate behavior (NOT_FOUND / NOT_WRITABLE surface correctly).

`ProjectStore(root_str)` calls `resolve_projects_root` internally, so
`ProjectStore("")` gives a ready-to-use store pointing at the auto-default path
rather than raising / returning UNSET on a fresh machine.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from disco.core.owners import install_owner_id
from disco.core.release.spec import IntentUpgradeError, ReleaseIntent, parse_release_intent
from pydantic import ValidationError

from .archive import is_runtime_secret_path

try:  # POSIX on Linux/WSL2/macOS; strict cross-process proof fails closed without it.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - native Windows is not a supported strict host yet
    _fcntl = None  # type: ignore[assignment]

# Used both as the manifest filename and the workspace subdirectory; constants
# here so the agent-server doesn't depend on string literals scattered around.
_MANIFEST = "manifest.json"
_WORKSPACE = "workspace"
# WO-5: the host-owned typed release-intent sidecar. Lives NEXT TO manifest.json
# (i.e. in `<root>/<cid>/`), OUTSIDE the `workspace/` tree — a workspace file can
# HINT at release shape but can never self-assert this host-owned record.
_RELEASE_INTENT = "release-intent.json"
_VERSIONS = "versions"
_VERSIONS_INDEX = "versions.json"
_VERSION_METADATA = "version.json"
_VERSION_SEQUENCE = "version-sequence.json"
_VERSION_ALLOCATOR_MARKER = "version-allocator-v1.json"
_VERSION_LOCK = ".versions.lock"
_LOCKS_DIRECTORY = ".project-version-locks-v1"
_PIN_JOURNAL = ".versions.pin.journal"

# UX retention: unlabeled auto-cuts are cheap, but the picker must stay bounded.
_MAX_UNLABELED = 20
# Disk retention: one conversation should not silently consume a whole data volume.
_MAX_VERSION_BYTES = 512 * 1024 * 1024

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_MISSING_JSON = object()


def default_projects_root() -> str:
    """Compute the platform-appropriate default projects_root for this machine.

    Priority (first match wins):
    1. ``DISCO_DATA_DIR`` (or legacy ``PMX_DATA_DIR``) env var → ``<DATA_DIR>/projects``
       This is what the container entrypoint sets (default ``/data``).
    2. ``XDG_DATA_HOME`` env var → ``<XDG_DATA_HOME>/disco/projects``
    3. POSIX fallback → ``~/.local/share/disco/projects``

    The directory is NOT created here; callers that want ensure-exists behaviour
    should use :func:`resolve_projects_root` instead.
    """
    from disco.core.env import disco_env

    data_dir = disco_env("DATA_DIR")
    if data_dir:
        return str(Path(data_dir) / "projects")
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(xdg) / "disco" / "projects")


def resolve_projects_root(configured: str) -> str:
    """Return the effective projects_root path, ENSURING it exists.

    Both an explicit ``configured`` path AND the zero-config default are created
    with ``mkdir -p`` so a path that doesn't exist YET but is creatable (writable
    parent) is ready to use — "works on a fresh machine", whether the user set a
    path or not. A genuinely UNREACHABLE path (unplugged drive / permission) makes
    mkdir fail; we swallow that here and let :func:`validate_root` surface
    NOT_FOUND at the use site, so the UI can warn + offer the default rather than
    silently relocating the user's data.

    This is the single place that turns a configured-or-empty value into a real,
    live directory; all other code calls this rather than branching themselves.
    """
    root = configured.strip() or default_projects_root()
    try:
        Path(root).expanduser().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # unreachable path → validate_root reports NOT_FOUND at the use site
    return root


class StorageStatus(str, Enum):
    """The validation result for a candidate projects_root. The settings PUT
    endpoint maps each to a named 400 reason; the agent-server's snapshot path
    checks before writing so a misconfigured root fails loud, never silent."""

    OK = "ok"
    UNSET = "unset"
    NOT_FOUND = "not_found"
    NOT_A_DIRECTORY = "not_a_directory"
    NOT_WRITABLE = "not_writable"


class StorageError(Exception):
    """Raised when an operation against the store fails for a reason the caller
    needs to surface (project missing, manifest corrupt, etc.)."""


@dataclass(frozen=True)
class ProjectRecord:
    """A row in the Projects list — read from the manifest, plus a live check on
    whether the workspace tree is still on disk (the graceful-failure flag)."""

    conversation_id: str
    title: str | None
    owner_id: str | None
    created_at: str | None  # ISO-8601, may be None for legacy entries
    last_snapshot_at: str | None  # ISO-8601
    file_count: int
    total_bytes: int
    files_missing: bool  # True iff manifest exists but workspace/ is gone or empty
    legacy_unclaimed_owner: bool = False
    # Release provenance: True iff this project's workspace was SEEDED by a code
    # import (`/api/projects/import`), not built from scratch here. An imported tree
    # whose stack the detector can't recognize fails closed to `needs_review` rather
    # than `not_web` (WO-3 rung 4). Durable: preserved across re-snapshots. Distinct
    # from `conversation.origin="imported"`, which marks a READ-ONLY shared bundle.
    imported: bool = False


@dataclass(frozen=True)
class VersionRecord:
    """A workspace version summary. The same shape is written to versions.json
    and to each per-version version.json sidecar."""

    seq: int
    ts: str
    label: str
    trigger: str
    file_count: int
    total_bytes: int
    tree_digest: str
    pinned: bool


@dataclass(frozen=True)
class WorkspaceTreeFacts:
    """Freshly measured facts for a strict, consumer-visible workspace tree."""

    file_count: int
    total_bytes: int
    tree_digest: str


@dataclass(frozen=True)
class VerifiedFile:
    """One file authorized by a freshly verified immutable tree."""

    path: str
    size: int
    sha256: str


class VerifiedVersionHandle:
    """Lock-scoped capability for consuming exact immutable version bytes."""

    def __init__(
        self,
        *,
        record: VersionRecord,
        files: tuple[VerifiedFile, ...],
        workspace_fd: int,
    ) -> None:
        self.record = record
        self.files = files
        self._workspace_fd = workspace_fd
        self._by_path = {entry.path: entry for entry in files}
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._workspace_fd)
            self._closed = True

    @staticmethod
    def _normalized_rel(path: str) -> str:
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            raise StorageError("verified file path is invalid")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
            raise StorageError("verified file path is not a safe relative path")
        return parsed.as_posix()

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        if self._closed:
            raise StorageError("verified version handle is closed")
        rel = self._normalized_rel(path)
        entry = self._by_path.get(rel)
        if entry is None or is_runtime_secret_path(rel):
            raise StorageError(f"file is not in the verified version manifest: {rel}")
        if max_bytes is not None and entry.size > max_bytes:
            raise StorageError(f"verified file exceeds the read limit: {rel}")
        data = _read_regular_file_beneath(self._workspace_fd, rel, expected_size=entry.size)
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise StorageError(f"verified file bytes changed before consumption: {rel}")
        return data

    def iter_bytes(self) -> Iterator[tuple[VerifiedFile, bytes]]:
        for entry in self.files:
            yield entry, self.read_bytes(entry.path)

    def zip_bytes(self) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry, data in self.iter_bytes():
                info = zipfile.ZipInfo(entry.path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        return output.getvalue()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _manifest_owner(data: dict[str, Any]) -> tuple[str, bool]:
    raw_owner = data.get("owner_id")
    if isinstance(raw_owner, str) and raw_owner.strip():
        return raw_owner.strip(), False
    return install_owner_id(), True


def _safe_segment(s: str) -> bool:
    """A conversation ID is a primary key but defensively: reject anything that
    isn't a plain segment (no slashes, no dot-dot, no nulls). Belt and braces
    against a poisoned input becoming a path traversal."""
    if not s or s in {".", ".."}:
        return False
    if "/" in s or "\\" in s or "\x00" in s:
        return False
    return True


def _safe_seq(seq: object) -> bool:
    if type(seq) is not int:
        return False
    return seq > 0


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _read_regular_file_beneath(root_fd: int, rel: str, *, expected_size: int) -> bytes:
    """Read one manifest path through no-follow directory descriptors."""

    parts = PurePosixPath(rel).parts
    if not parts:
        raise StorageError("verified file path is empty")
    parent_fd = os.dup(root_fd)
    file_fd: int | None = None
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            except OSError as exc:
                raise StorageError(f"verified directory path changed: {rel}: {exc}") from exc
            os.close(parent_fd)
            parent_fd = child_fd
        # Do not let a pathname replacement with a FIFO block the server before
        # the fstat type check can reject it.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        except OSError as exc:
            raise StorageError(f"verified file path changed: {rel}: {exc}") from exc
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise StorageError(f"verified file is no longer the expected regular file: {rel}")
        chunks: list[bytes] = []
        remaining = expected_size + 1
        while remaining > 0:
            block = os.read(file_fd, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(file_fd)
        try:
            path_after = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise StorageError(f"verified file path changed during read: {rel}") from exc
        if _stat_identity(before) != _stat_identity(after) or _stat_identity(
            after
        ) != _stat_identity(path_after):
            raise StorageError(f"verified file changed during read: {rel}")
        data = b"".join(chunks)
        if len(data) != expected_size:
            raise StorageError(f"verified file size changed during read: {rel}")
        return data
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _scan_tree(root: Path) -> tuple[dict[str, str], int]:
    if not root.is_dir():
        raise StorageError(f"workspace directory missing: {root}")
    hashes: dict[str, str] = {}
    total_bytes = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if is_runtime_secret_path(rel):
            continue
        stat = path.stat()
        hashes[rel] = _file_sha256(path)
        total_bytes += stat.st_size
    return hashes, total_bytes


def _scan_immutable_tree_details(root: Path) -> tuple[dict[str, tuple[str, int]], int]:
    """Hash a published version tree without following or ignoring symlinks.

    ``_scan_tree`` intentionally skips runtime-secret paths and symlinks while
    scanning the mutable mirror.  Neither is acceptable in an immutable
    version: silently omitting one would let the recorded digest describe fewer
    bytes than a consumer can actually reach.
    """

    if root.is_symlink() or not root.is_dir():
        raise StorageError(f"immutable workspace directory missing or symlinked: {root}")

    details: dict[str, tuple[str, int]] = {}
    total_bytes = 0
    try:
        root_before = root.lstat()
        entries = sorted(root.rglob("*"))
    except OSError as exc:
        raise StorageError(f"immutable workspace unreadable: {exc}") from exc
    initial_paths = tuple(path.relative_to(root).as_posix() for path in entries)
    identities: dict[str, tuple[int, int, int, int, int, int, int]] = {}

    for path in entries:
        rel = path.relative_to(root).as_posix()
        try:
            entry_stat = path.lstat()
        except OSError as exc:
            raise StorageError(f"immutable workspace entry unreadable: {rel}: {exc}") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise StorageError(f"immutable workspace contains symlink: {rel}")
        if stat.S_ISDIR(entry_stat.st_mode):
            identities[rel] = _stat_identity(entry_stat)
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise StorageError(f"immutable workspace contains non-regular entry: {rel}")
        if is_runtime_secret_path(rel):
            raise StorageError(f"immutable workspace contains runtime secret: {rel}")

        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise StorageError(f"immutable workspace file unreadable: {rel}: {exc}") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise StorageError(f"immutable workspace contains non-regular entry: {rel}")
            if _stat_identity(before) != _stat_identity(entry_stat):
                raise StorageError(f"immutable workspace path changed before hashing: {rel}")
            h = hashlib.sha256()
            while block := os.read(fd, 1024 * 1024):
                h.update(block)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise StorageError(f"immutable workspace path changed while hashing: {rel}") from exc
        identity_before = _stat_identity(before)
        identity_after = _stat_identity(after)
        if identity_before != identity_after:
            raise StorageError(f"immutable workspace file changed while hashing: {rel}")
        if identity_after != _stat_identity(path_after):
            raise StorageError(f"immutable workspace path changed while hashing: {rel}")
        identities[rel] = identity_after
        details[rel] = (h.hexdigest(), after.st_size)
        total_bytes += after.st_size

    try:
        final_entries = sorted(root.rglob("*"))
        root_after = root.lstat()
    except OSError as exc:
        raise StorageError(f"immutable workspace changed while hashing: {exc}") from exc
    final_paths = tuple(path.relative_to(root).as_posix() for path in final_entries)
    if final_paths != initial_paths or _stat_identity(root_after) != _stat_identity(root_before):
        raise StorageError("immutable workspace membership changed while hashing")
    for path in final_entries:
        rel = path.relative_to(root).as_posix()
        try:
            identity = _stat_identity(path.lstat())
        except OSError as exc:
            raise StorageError(f"immutable workspace entry changed after hashing: {rel}") from exc
        if identities.get(rel) != identity:
            raise StorageError(f"immutable workspace entry changed after hashing: {rel}")
    return details, total_bytes


def _scan_immutable_tree(root: Path) -> tuple[dict[str, str], int]:
    details, total_bytes = _scan_immutable_tree_details(root)
    return {rel: digest for rel, (digest, _size) in details.items()}, total_bytes


def _tree_digest_from_hashes(file_hashes: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(file_hashes):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(file_hashes[rel].encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def tree_digest(root: Path) -> str:
    """Stable sha256 over sorted workspace-relative path + file-sha256 pairs."""
    file_hashes, _ = _scan_tree(root)
    return _tree_digest_from_hashes(file_hashes)


def tree_digest_of_files(files: Mapping[str, bytes]) -> str:
    """`tree_digest` over an in-memory {rel: bytes} snapshot — same file-set +
    algorithm as `tree_digest(root)`. For verifying a source tree that was read
    once into memory (no re-traversal, so no read-vs-read TOCTOU)."""
    return _tree_digest_from_hashes(
        {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
    )


def _version_to_dict(record: VersionRecord) -> dict[str, Any]:
    return {
        "seq": record.seq,
        "ts": record.ts,
        "label": record.label,
        "trigger": record.trigger,
        "file_count": record.file_count,
        "total_bytes": record.total_bytes,
        "tree_digest": record.tree_digest,
        "pinned": record.pinned,
    }


def _version_from_dict(data: dict[str, Any]) -> VersionRecord:
    return VersionRecord(
        seq=int(data["seq"]),
        ts=str(data["ts"]),
        label=str(data.get("label") or ""),
        trigger=str(data.get("trigger") or ""),
        file_count=int(data.get("file_count") or 0),
        total_bytes=int(data.get("total_bytes") or 0),
        tree_digest=str(data["tree_digest"]),
        pinned=bool(data.get("pinned") or False),
    )


def _payload_matches_record(data: dict[str, Any], record: VersionRecord) -> bool:
    """Exact JSON-shape comparison (``True`` must not compare equal to ``1``)."""

    expected = _version_to_dict(record)
    return data.keys() == expected.keys() and all(
        type(data[key]) is type(value) and data[key] == value for key, value in expected.items()
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(path: Path) -> None:
    """Flush every staged file and directory before publishing its name."""

    entries = sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for entry in entries:
        entry_stat = entry.lstat()
        if stat.S_ISLNK(entry_stat.st_mode):
            raise StorageError(f"cannot sync symlinked staged entry: {entry}")
        if stat.S_ISREG(entry_stat.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(entry, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif not stat.S_ISDIR(entry_stat.st_mode):
            raise StorageError(f"cannot sync non-regular staged entry: {entry}")
    for directory in sorted(
        (item for item in entries if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        _fsync_directory(directory)
    _fsync_directory(path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Durably replace JSON without following a caller-planted temp symlink."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise StorageError(f"JSON parent directory is missing or symlinked: {path.parent}")
    # Remove the predictable temp name used by the legacy writer. Unlinking a
    # planted symlink removes the link itself, never its target; the new writer
    # then uses a unique O_EXCL tempfile instead.
    legacy_tmp = path.with_suffix(path.suffix + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        legacy_tmp.unlink()
    encoded = json.dumps(payload, indent=2).encode("utf-8")
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _read_json_regular_nofollow(
    path: Path,
    *,
    label: str,
    missing_ok: bool = False,
    max_bytes: int = 16 * 1024 * 1024,
) -> Any:
    """Read bounded JSON from one stable regular-file identity."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return _MISSING_JSON
        raise StorageError(f"{label} is missing") from None
    except OSError as exc:
        raise StorageError(f"{label} is symlinked or could not be opened safely: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise StorageError(f"{label} is not a regular file")
        if before.st_size > max_bytes:
            raise StorageError(f"{label} exceeds its size bound")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        after = os.fstat(fd)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise StorageError(f"{label} changed while it was read") from exc
        if _stat_identity(before) != _stat_identity(after) or _stat_identity(
            after
        ) != _stat_identity(path_after):
            raise StorageError(f"{label} changed while it was read")
        data = b"".join(chunks)
        if len(data) != before.st_size:
            raise StorageError(f"{label} size changed while it was read")
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError(f"{label} contains invalid JSON: {exc}") from exc
    finally:
        os.close(fd)


def validate_root(root_str: str) -> StorageStatus:
    """Classify a candidate projects_root path. Pure, side-effect-free EXCEPT
    for a probe-write that's torn down before returning — that's the only way
    to detect "not writable" cross-platform without trusting `os.access`,
    which lies on some filesystems."""
    if not root_str.strip():
        return StorageStatus.UNSET
    root = Path(root_str).expanduser()
    if not root.exists():
        return StorageStatus.NOT_FOUND
    if not root.is_dir():
        return StorageStatus.NOT_A_DIRECTORY
    # probe write: create a unique sentinel, then remove it. Cross-platform
    # truth about writability without relying on POSIX permission bits.
    probe = root / f".pmx-write-probe-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return StorageStatus.NOT_WRITABLE
    return StorageStatus.OK


# ---------------------------------------------------------------------------
# Shared path helpers — used by ProjectStore, _VerifiedVersionReader, and
# _VersionStateCoordinator to avoid method duplication.
# ---------------------------------------------------------------------------


def _project_dir(root: Path, cid: str) -> Path:
    if not _safe_segment(cid):
        raise StorageError(f"unsafe conversation_id: {cid!r}")
    return root / cid


def _versions_dir(root: Path, cid: str) -> Path:
    return _project_dir(root, cid) / _VERSIONS


def _versions_index_path(root: Path, cid: str) -> Path:
    return _project_dir(root, cid) / _VERSIONS_INDEX


def _version_dir_path(root: Path, cid: str, record: VersionRecord) -> Path:
    digest12 = record.tree_digest[:12]
    return _versions_dir(root, cid) / f"{record.seq:03d}-{digest12}"


def _version_sequence_path(root: Path, cid: str) -> Path:
    return _project_dir(root, cid) / _VERSION_SEQUENCE


def _version_allocator_marker_path(root: Path, cid: str) -> Path:
    return _project_dir(root, cid) / _VERSION_ALLOCATOR_MARKER


def _pin_journal_path(root: Path, cid: str) -> Path:
    return _project_dir(root, cid) / _PIN_JOURNAL


def _require_plain_project_dir(root: Path, cid: str) -> Path:
    project_dir = _project_dir(root, cid)
    if project_dir.is_symlink() or not project_dir.is_dir():
        raise StorageError(f"project directory missing or symlinked: {project_dir}")
    return project_dir


def _ensure_versions_dir(root: Path, cid: str) -> Path:
    _require_plain_project_dir(root, cid)
    versions = _versions_dir(root, cid)
    if versions.is_symlink():
        raise StorageError(f"versions directory is symlinked: {versions}")
    try:
        versions.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"versions directory unavailable: {exc}") from exc
    if versions.is_symlink() or not versions.is_dir():
        raise StorageError(f"versions directory missing or symlinked: {versions}")
    return versions


# ---------------------------------------------------------------------------
# Private collaborators — decomposing ProjectStore without weakening caps.
# ---------------------------------------------------------------------------


class _VerifiedVersionReader:
    """Strict version reading, payload verification, and fd-proof trees.

    Every read is a fresh proof against the filesystem. This collaborator
    never caches counts, never trusts directory-name digests, and rejects
    symlinked or missing directories, entries, or metadata at every layer.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def read_version_index(self, cid: str) -> list[VersionRecord]:
        index = _versions_index_path(self._root, cid)
        raw = _read_json_regular_nofollow(
            index,
            label="versions index",
            missing_ok=True,
        )
        if raw is _MISSING_JSON:
            return []
        if not isinstance(raw, list):
            raise StorageError("versions index unreadable: expected a list")
        records: list[VersionRecord] = []
        seen_seqs: set[int] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise StorageError("versions index unreadable: malformed row")
            try:
                record = _version_from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageError(f"versions index unreadable: malformed row: {exc}") from exc
            if (
                not _payload_matches_record(item, record)
                or not _safe_seq(record.seq)
                or record.seq in seen_seqs
                or record.file_count < 0
                or record.total_bytes < 0
                or len(record.tree_digest) != 64
                or any(char not in "0123456789abcdef" for char in record.tree_digest)
            ):
                raise StorageError("versions index unreadable: invalid or duplicate row")
            seen_seqs.add(record.seq)
            records.append(record)
        records.sort(key=lambda r: r.seq)
        return records

    def existing_version_records(self, cid: str) -> list[VersionRecord]:
        records: list[VersionRecord] = []
        for record in self.read_version_index(cid):
            version_dir = _version_dir_path(self._root, cid, record)
            workspace = version_dir / _WORKSPACE
            if version_dir.is_symlink() or workspace.is_symlink():
                raise StorageError(f"version directory or workspace is symlinked: {record.seq}")
            if workspace.is_dir():
                records.append(record)
        return records

    def verify_version(self, cid: str, seq: int) -> VersionRecord:
        """Return a version only after fresh tree, index, sidecar, and byte proof."""
        record, _details = self.verify_version_details(cid, seq)
        return record

    def verify_version_details(
        self,
        cid: str,
        seq: int,
    ) -> tuple[VersionRecord, dict[str, tuple[str, int]]]:
        """Return a version and exact file manifest after all fresh proofs.

        This method never trusts cached counts or the directory-name digest.  It
        rejects missing/pruned trees, symlinked directories or entries, secret
        files, malformed metadata, and any disagreement between all three views.
        """

        if not _safe_seq(seq):
            raise StorageError(f"unsafe version seq: {seq!r}")
        _require_plain_project_dir(self._root, cid)
        versions = _versions_dir(self._root, cid)
        if versions.is_symlink() or not versions.is_dir():
            raise StorageError("versions directory is missing or symlinked")
        records = self.read_version_index(cid)
        matches = [record for record in records if record.seq == seq]
        if len(matches) != 1:
            raise StorageError(f"unknown or duplicate version seq: {seq!r}")
        record = matches[0]

        version_dir = _version_dir_path(self._root, cid, record)
        workspace = version_dir / _WORKSPACE
        metadata = version_dir / _VERSION_METADATA
        if version_dir.is_symlink() or not version_dir.is_dir():
            raise StorageError(f"version directory missing or symlinked: {seq!r}")
        if workspace.is_symlink() or not workspace.is_dir():
            raise StorageError(f"version workspace missing or symlinked: {seq!r}")
        raw_metadata = _read_json_regular_nofollow(
            metadata,
            label=f"version metadata {seq!r}",
        )
        if not isinstance(raw_metadata, dict) or not _payload_matches_record(raw_metadata, record):
            raise StorageError(f"version metadata disagrees with index: {seq!r}")

        details, total_bytes = _scan_immutable_tree_details(workspace)
        hashes = {rel: digest for rel, (digest, _size) in details.items()}
        facts = WorkspaceTreeFacts(
            file_count=len(details),
            total_bytes=total_bytes,
            tree_digest=_tree_digest_from_hashes(hashes),
        )
        if (
            facts.file_count != record.file_count
            or facts.total_bytes != record.total_bytes
            or facts.tree_digest != record.tree_digest
        ):
            raise StorageError(f"version workspace disagrees with metadata: {seq!r}")
        return record, details

    def open_version_workspace_fd(
        self,
        cid: str,
        record: VersionRecord,
    ) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        opened: list[int] = []
        try:
            current = os.open(self._root, flags)
            opened.append(current)
            for segment in (
                cid,
                _VERSIONS,
                _version_dir_path(self._root, cid, record).name,
                _WORKSPACE,
            ):
                current = os.open(segment, flags, dir_fd=current)
                opened.append(current)
            workspace_fd = opened.pop()
            return workspace_fd
        except OSError as exc:
            raise StorageError(f"verified version path changed: {exc}") from exc
        finally:
            for fd in reversed(opened):
                os.close(fd)


class _VersionStateCoordinator:
    """Allocator (never-reuse seq), WAL-protected pin/unpin, and version life-cycle.

    Every write is durably sequenced — journal first, then metadata sidecar, then
    index — so any crash leaves a single journal entry the next EX transaction
    recovers idempotently.  The allocator state file is published before its
    migration marker so a crash never permanently poisons a project.
    """

    def __init__(
        self,
        root: Path,
        reader: _VerifiedVersionReader,
        *,
        version_byte_budget: int = _MAX_VERSION_BYTES,
    ) -> None:
        self._root = root
        self._reader = reader
        self._version_byte_budget = version_byte_budget

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _tree_facts(root: Path) -> WorkspaceTreeFacts:
        hashes, total_bytes = _scan_immutable_tree(root)
        return WorkspaceTreeFacts(
            file_count=len(hashes),
            total_bytes=total_bytes,
            tree_digest=_tree_digest_from_hashes(hashes),
        )

    def _version_has_shared_inodes(self, cid: str, record: VersionRecord) -> bool:
        workspace = _version_dir_path(self._root, cid, record) / _WORKSPACE
        return any(
            path.lstat().st_nlink > 1
            for path in workspace.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def write_version_index(self, cid: str, records: list[VersionRecord]) -> None:
        payload = [_version_to_dict(r) for r in sorted(records, key=lambda r: r.seq)]
        _write_json_atomic(_versions_index_path(self._root, cid), payload)

    # -- WAL journal (pin/unpin crash recovery) ---------------------------------

    def _write_pin_journal_durably(
        self,
        cid: str,
        before: VersionRecord,
        after: VersionRecord,
    ) -> None:
        journal_path = _pin_journal_path(self._root, cid)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "conversation_id": cid,
            "before": _version_to_dict(before),
            "after": _version_to_dict(after),
        }
        _write_json_atomic(journal_path, payload)

    def _read_pin_journal(self, cid: str) -> tuple[VersionRecord, VersionRecord] | None:
        """Read the pin journal with full no-follow identity checks.

        Validates the canonical schema, conversation_id, that before/after are
        valid VersionRecords, and that they differ ONLY in the ``pinned`` bool.
        Returns ``None`` if no journal exists.  Raises ``StorageError`` for any
        malformed, tampered, or symlinked journal.
        """
        _require_plain_project_dir(self._root, cid)
        journal_path = _pin_journal_path(self._root, cid)
        raw = _read_json_regular_nofollow(
            journal_path,
            label="pin journal",
            missing_ok=True,
        )
        if raw is _MISSING_JSON:
            return None

        if not isinstance(raw, dict):
            raise StorageError("pin journal is malformed — expected a JSON object")
        if raw.keys() != {"schema_version", "conversation_id", "before", "after"}:
            raise StorageError("pin journal is malformed: non-canonical top-level fields")
        if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
            raise StorageError("pin journal has unknown schema_version")
        if raw.get("conversation_id") != cid:
            raise StorageError("pin journal conversation_id mismatch")

        before_raw = raw.get("before")
        after_raw = raw.get("after")
        if not isinstance(before_raw, dict) or not isinstance(after_raw, dict):
            raise StorageError("pin journal is malformed — missing before/after version records")

        # Validate key sets and pinned types in the raw journal JSON before any
        # coercion so that malformed entries fail closed before recovery logic.
        expected_record_keys = _version_to_dict(
            VersionRecord(1, "", "", "", 0, 0, "0" * 64, False)
        ).keys()
        if before_raw.keys() != expected_record_keys or after_raw.keys() != expected_record_keys:
            raise StorageError(
                "pin journal is malformed: before/after have different shapes or "
                "non-canonical fields"
            )
        if before_raw.keys() != after_raw.keys():
            raise StorageError("pin journal before/after have different shapes")

        raw_before_pinned = before_raw.get("pinned")
        raw_after_pinned = after_raw.get("pinned")
        if type(raw_before_pinned) is not bool or type(raw_after_pinned) is not bool:
            raise StorageError("pin journal pinned fields are not bool type")
        if raw_before_pinned == raw_after_pinned:
            raise StorageError("pin journal before/after pinned values are identical — tampered")

        try:
            before = _version_from_dict(before_raw)
            after = _version_from_dict(after_raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"pin journal version record malformed: {exc}") from exc

        if not _payload_matches_record(before_raw, before) or not _payload_matches_record(
            after_raw, after
        ):
            raise StorageError("pin journal before/after records are non-canonical")
        before_dict = _version_to_dict(before)
        after_dict = _version_to_dict(after)
        differing = [key for key in before_dict if before_dict[key] != after_dict[key]]
        if differing != ["pinned"]:
            raise StorageError(f"pin journal before/after differ in non-pinned fields: {differing}")

        return before, after

    def _recover_pin_journal_if_pending(self, cid: str) -> VersionRecord | None:
        """Check for and recover any pending pin journal.

        Must be called under the EX lock.  Valid reachable states:
          - after/after  → clean up journal (fully applied)
          - after/before → roll forward index (metadata applied, index stale)
          - before/before → roll the durable intent forward from the journal
          - before/after → fail closed (impossible ordering, disk tampered)

        Any other inconsistent state also fails closed.  The immutable tree is
        freshly proved before any roll-forward.
        """
        journal = self._read_pin_journal(cid)
        if journal is None:
            return None

        before, after = journal

        # Prove the immutable tree is still reachable before any roll-forward.
        # We must NOT call verify_version_details here — that enforces
        # metadata/index consistency which is exactly the inconsistency we are
        # recovering.  Instead, prove only the workspace tree integrity.
        try:
            _require_plain_project_dir(self._root, cid)
            version_dir = _version_dir_path(self._root, cid, after)
            workspace = version_dir / _WORKSPACE
            if version_dir.is_symlink() or not version_dir.is_dir():
                raise StorageError("version directory missing or symlinked")
            if workspace.is_symlink() or not workspace.is_dir():
                raise StorageError("version workspace missing or symlinked")
            details, total_bytes = _scan_immutable_tree_details(workspace)
            hashes = {rel: digest for rel, (digest, _size) in details.items()}
            if (
                len(details) != after.file_count
                or total_bytes != after.total_bytes
                or _tree_digest_from_hashes(hashes) != after.tree_digest
            ):
                raise StorageError("version workspace disagrees with journal after record")
        except StorageError as exc:
            raise StorageError(
                f"pin journal recovery failed: tree verification failed: {exc}"
            ) from exc

        metadata_matches_after = False
        metadata_matches_before = False
        index_matches_after = False
        index_matches_before = False

        version_dir = _version_dir_path(self._root, cid, after)
        metadata_path = version_dir / _VERSION_METADATA
        raw_metadata = _read_json_regular_nofollow(
            metadata_path,
            label="version metadata during pin recovery",
        )
        if isinstance(raw_metadata, dict):
            metadata_matches_after = _payload_matches_record(raw_metadata, after)
            metadata_matches_before = _payload_matches_record(raw_metadata, before)

        # Validate the complete index before any recovery write. Duplicate,
        # malformed, or non-canonical non-target rows make the state tampered,
        # not a partially applied transaction.
        records = self._reader.read_version_index(cid)
        target_record = next((record for record in records if record.seq == after.seq), None)
        if target_record is not None:
            index_matches_after = target_record == after
            index_matches_before = target_record == before

        if metadata_matches_after and index_matches_after:
            self._reader.verify_version_details(cid, after.seq)
            self._unlink_journal(cid)
            return after
        elif metadata_matches_after and index_matches_before:
            next_records: list[VersionRecord] = []
            found = False
            for record in records:
                if record.seq == after.seq:
                    next_records.append(after)
                    found = True
                else:
                    next_records.append(record)
            if not found:
                raise StorageError("pin journal recovery failed: version not found in index")
            self.write_version_index(cid, next_records)
            self._reader.verify_version_details(cid, after.seq)
            self._unlink_journal(cid)
            return after
        elif metadata_matches_before and index_matches_before:
            _write_json_atomic(metadata_path, _version_to_dict(after))
            redo_records: list[VersionRecord] = []
            found = False
            for record in records:
                if record.seq == after.seq:
                    redo_records.append(after)
                    found = True
                else:
                    redo_records.append(record)
            if not found:
                raise StorageError("pin journal recovery failed: version not found in index")
            self.write_version_index(cid, redo_records)
            self._reader.verify_version_details(cid, after.seq)
            self._unlink_journal(cid)
            return after
        elif metadata_matches_before and index_matches_after:
            raise StorageError(
                "pin journal recovery failed: index was written but metadata was not — "
                "impossible ordering, disk may be tampered"
            )
        else:
            raise StorageError("pin journal recovery failed: inconsistent metadata/index state")

    def _unlink_journal(self, cid: str) -> None:
        journal_path = _pin_journal_path(self._root, cid)
        if journal_path.exists():
            if journal_path.is_symlink():
                raise StorageError("pin journal is symlinked — cannot unlink safely")
            journal_path.unlink()
            _fsync_directory(journal_path.parent)

    # -- sequence allocation ----------------------------------------------------

    def known_version_high_watermark(self, cid: str) -> int:
        known = max((record.seq for record in self._reader.read_version_index(cid)), default=0)
        versions = _versions_dir(self._root, cid)
        if versions.is_symlink():
            raise StorageError(f"versions directory is symlinked: {versions}")
        if not versions.exists():
            return known
        if not versions.is_dir():
            raise StorageError(f"versions path is not a directory: {versions}")
        try:
            entries = list(versions.iterdir())
        except OSError as exc:
            raise StorageError(f"versions directory unreadable: {exc}") from exc
        for entry in entries:
            if entry.is_symlink():
                raise StorageError(f"versions directory contains symlink: {entry.name}")
            prefix, separator, _rest = entry.name.partition("-")
            if separator and prefix.isdecimal():
                known = max(known, int(prefix))
        return known

    def reserve_version_seq(self, cid: str) -> int:
        """Durably reserve a never-reused sequence before creating its tree.

        A crash may leave a gap, but pruning can never make a later cut recycle
        an already-issued sequence.  Projects created before this state file was
        introduced are migrated from all still-observable index rows/directories.
        """

        _require_plain_project_dir(self._root, cid)
        high_watermark = self.known_version_high_watermark(cid)
        state_path = _version_sequence_path(self._root, cid)
        marker_path = _version_allocator_marker_path(self._root, cid)
        if state_path.is_symlink():
            raise StorageError("version sequence state is symlinked")
        if marker_path.is_symlink():
            raise StorageError("version allocator marker is symlinked")
        marker_exists = marker_path.exists()
        if marker_exists:
            if not marker_path.is_file():
                raise StorageError("version allocator marker is not a regular file")
            try:
                marker = json.loads(marker_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise StorageError(f"version allocator marker unreadable: {exc}") from exc
            if marker != {"schema_version": 1}:
                raise StorageError("version allocator marker is malformed or tampered")
        if state_path.exists():
            if not state_path.is_file():
                raise StorageError("version sequence state is not a regular file")
            try:
                raw = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise StorageError(f"version sequence state unreadable: {exc}") from exc
            legacy = isinstance(raw, dict) and raw.keys() == {"next_version_seq"}
            current = isinstance(raw, dict) and raw.keys() == {
                "schema_version",
                "next_version_seq",
            }
            if (
                not isinstance(raw, dict)
                or not (legacy or current)
                or (current and raw.get("schema_version") != 1)
                or type(raw.get("next_version_seq")) is not int
                or not _safe_seq(raw["next_version_seq"])
            ):
                raise StorageError("version sequence state unreadable: malformed payload")
            seq = raw["next_version_seq"]
            if seq <= high_watermark:
                raise StorageError("version sequence state is stale or tampered")
        else:
            if marker_exists:
                raise StorageError("version sequence state is missing after allocator migration")
            seq = high_watermark + 1

        _write_json_atomic(
            state_path,
            {"schema_version": 1, "next_version_seq": seq + 1},
        )
        if not marker_exists:
            _write_json_atomic(marker_path, {"schema_version": 1})
        return seq

    # -- pin / unpin (WAL-protected) --------------------------------------------

    def set_version_pinned_locked(
        self,
        cid: str,
        seq: int,
        pinned: bool,
    ) -> VersionRecord:
        if not _safe_seq(seq):
            raise StorageError(f"unsafe version seq: {seq!r}")
        self._reader.verify_version_details(cid, seq)
        records = self._reader.read_version_index(cid)
        current: VersionRecord | None = None
        for record in records:
            if record.seq == seq:
                current = record
                break
        if current is None:
            raise StorageError(f"unknown version seq: {seq!r}")

        desired = bool(pinned)
        if current.pinned == desired:
            return self._reader.verify_version(cid, seq)

        updated = replace(current, pinned=desired)
        metadata_path = _version_dir_path(self._root, cid, updated) / _VERSION_METADATA
        if not metadata_path.is_file():
            raise StorageError(f"version metadata missing: {seq!r}")

        self._write_pin_journal_durably(cid, current, updated)
        try:
            _write_json_atomic(metadata_path, _version_to_dict(updated))
            next_records: list[VersionRecord] = []
            for r in records:
                if r.seq == seq:
                    next_records.append(updated)
                else:
                    next_records.append(r)
            self.write_version_index(cid, next_records)
            self._reader.verify_version_details(cid, seq)
            self._unlink_journal(cid)
        except Exception:
            raise

        return self._reader.verify_version(cid, seq)

    # -- version life-cycle -----------------------------------------------------

    def _copy_version_workspace(
        self,
        cid: str,
        *,
        live_workspace: Path,
        dest_workspace: Path,
        live_hashes: dict[str, str],
        previous: VersionRecord | None,
    ) -> None:
        previous_workspace: Path | None = None
        previous_hashes: dict[str, str] = {}
        if previous is not None:
            previous_workspace = _version_dir_path(self._root, cid, previous) / _WORKSPACE
            if previous_workspace.is_dir():
                previous_hashes, _ = _scan_tree(previous_workspace)

        for rel in sorted(live_hashes):
            source = live_workspace / rel
            target = dest_workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if previous_workspace is not None and previous_hashes.get(rel) == live_hashes[rel]:
                previous_source = previous_workspace / rel
                try:
                    os.link(previous_source, target)
                    continue
                except OSError:
                    pass
            shutil.copy2(source, target)

    def create_version(
        self,
        cid: str,
        *,
        label: str,
        trigger: str,
        live_workspace: Path,
        live_hashes: dict[str, str],
        total_bytes: int,
        records: list[VersionRecord],
        previous: VersionRecord | None,
        pinned: bool,
        prune: bool,
    ) -> VersionRecord:
        """Stage, prove, and publish one new version index row."""

        digest = _tree_digest_from_hashes(live_hashes)
        seq = self.reserve_version_seq(cid)
        record = VersionRecord(
            seq=seq,
            ts=_now_iso(),
            label=label,
            trigger=trigger,
            file_count=len(live_hashes),
            total_bytes=total_bytes,
            tree_digest=digest,
            pinned=pinned,
        )
        versions = _ensure_versions_dir(self._root, cid)
        version_dir = _version_dir_path(self._root, cid, record)
        if version_dir.is_symlink() or version_dir.exists():
            raise StorageError(f"version directory already exists: {version_dir}")

        staging_dir = Path(tempfile.mkdtemp(prefix=f".{seq:03d}-staging-", dir=versions))
        dest_workspace = staging_dir / _WORKSPACE
        published = False
        indexed = False
        try:
            dest_workspace.mkdir(parents=False, exist_ok=False)
            self._copy_version_workspace(
                cid,
                live_workspace=live_workspace,
                dest_workspace=dest_workspace,
                live_hashes=live_hashes,
                previous=previous,
            )
            staged_facts = self._tree_facts(dest_workspace)
            if (
                staged_facts.file_count != record.file_count
                or staged_facts.total_bytes != record.total_bytes
                or staged_facts.tree_digest != record.tree_digest
            ):
                raise StorageError("staged version bytes disagree with the source scan")
            _write_json_atomic(staging_dir / _VERSION_METADATA, _version_to_dict(record))
            _fsync_tree(staging_dir)
            staging_dir.replace(version_dir)
            published = True
            _fsync_directory(versions)
            published_facts = self._tree_facts(version_dir / _WORKSPACE)
            if published_facts != staged_facts:
                raise StorageError("published version bytes disagree with staged proof")
            self.write_version_index(cid, [*records, record])
            indexed = True
        except Exception:
            if staging_dir.exists() and not staging_dir.is_symlink():
                shutil.rmtree(staging_dir)
            if published and not indexed and version_dir.exists() and not version_dir.is_symlink():
                shutil.rmtree(version_dir)
            raise
        finally:
            if staging_dir.exists() and not staging_dir.is_symlink():
                shutil.rmtree(staging_dir)

        if prune:
            self._prune(cid)
        return record

    def cut_version_locked(
        self,
        cid: str,
        *,
        label: str = "",
        trigger: str,
        live_workspace: Path,
    ) -> VersionRecord | None:
        live_hashes, total_bytes = _scan_tree(live_workspace)
        digest = _tree_digest_from_hashes(live_hashes)
        records = self._reader.existing_version_records(cid)
        newest = records[-1] if records else None
        if newest is not None and newest.tree_digest == digest:
            return None
        return self.create_version(
            cid,
            label=label,
            trigger=trigger,
            live_workspace=live_workspace,
            live_hashes=live_hashes,
            total_bytes=total_bytes,
            records=records,
            previous=newest,
            pinned=False,
            prune=True,
        )

    def cut_verified_version_locked(
        self,
        cid: str,
        *,
        label: str = "",
        trigger: str,
        pin: bool = False,
        live_workspace: Path,
    ) -> VersionRecord | None:

        live_hashes, total_bytes = _scan_immutable_tree(live_workspace)
        digest = _tree_digest_from_hashes(live_hashes)
        records = self._reader.read_version_index(cid)
        newest = records[-1] if records else None
        verified_previous: VersionRecord | None = None
        if newest is not None:
            verified_previous = self._reader.verify_version(cid, newest.seq)
            if verified_previous.tree_digest == digest and not self._version_has_shared_inodes(
                cid, verified_previous
            ):
                candidate = verified_previous
                if pin and not candidate.pinned:
                    candidate = self.set_version_pinned_locked(
                        cid,
                        candidate.seq,
                        True,
                    )
                    candidate = self._reader.verify_version(cid, candidate.seq)
                self._prune(cid)
                return self._reader.verify_version(cid, candidate.seq)

        candidate = self.create_version(
            cid,
            label=label,
            trigger=trigger,
            live_workspace=live_workspace,
            live_hashes=live_hashes,
            total_bytes=total_bytes,
            records=records,
            previous=None,
            pinned=pin,
            prune=False,
        )
        candidate = self._reader.verify_version(cid, candidate.seq)
        self._prune(cid)
        return self._reader.verify_version(cid, candidate.seq)

    # -- prune ------------------------------------------------------------------

    def _versions_total_bytes(self, cid: str, records: list[VersionRecord]) -> int:
        seen: set[tuple[int, int]] = set()
        total = 0
        for record in records:
            workspace = _version_dir_path(self._root, cid, record) / _WORKSPACE
            if not workspace.is_dir():
                continue
            for path in sorted(
                p for p in workspace.rglob("*") if p.is_file() and not p.is_symlink()
            ):
                if is_runtime_secret_path(path.relative_to(workspace).as_posix()):
                    continue
                st = path.stat()
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += st.st_size
        return total

    def _drop_version(self, cid: str, record: VersionRecord) -> None:
        version_dir = _version_dir_path(self._root, cid, record)
        if version_dir.exists():
            shutil.rmtree(version_dir)

    def _prune(self, cid: str) -> None:
        records = self._reader.existing_version_records(cid)
        changed = len(records) != len(self._reader.read_version_index(cid))

        prunable = [record for record in records if not record.label and not record.pinned]
        for record in prunable[:-_MAX_UNLABELED]:
            self._drop_version(cid, record)
            records.remove(record)
            changed = True

        while self._versions_total_bytes(cid, records) > self._version_byte_budget:
            candidate = next(
                (record for record in records if not record.label and not record.pinned),
                None,
            )
            if candidate is None:
                break
            self._drop_version(cid, candidate)
            records.remove(candidate)
            changed = True

        if changed:
            self.write_version_index(cid, records)


# ---------------------------------------------------------------------------
# ProjectStore — the public API surface.
# ---------------------------------------------------------------------------


class ProjectStore:
    """The disk side of project persistence. List/get/delete + path resolvers
    callers (the agent-server) use to wire snapshot, rehydrate, and download
    against a per-project directory.

    Construction does NOT throw on a bad root — the validation status is
    queryable via `status()` / `validate_root(...)`. Operations that REQUIRE
    a valid root (snapshot, list) raise StorageError with the reason; the
    settings round-trip + the read-only browse endpoint work even on a bad
    root (the user has to be able to see + fix the configuration)."""

    def __init__(self, root_str: str, *, version_byte_budget: int = _MAX_VERSION_BYTES) -> None:
        self._configured_str = root_str
        self._root_str = resolve_projects_root(root_str)
        self._root = Path(self._root_str).expanduser().resolve()
        self._version_byte_budget = version_byte_budget
        self._reader = _VerifiedVersionReader(self._root)
        self._coordinator = _VersionStateCoordinator(
            self._root,
            self._reader,
            version_byte_budget=version_byte_budget,
        )

    # -- identity & status ------------------------------------------------------

    @property
    def root(self) -> Path | None:
        """The expanded absolute effective root path. Always set (never None)
        after construction; the auto-default is created on first use."""
        return self._root

    def status(self) -> StorageStatus:
        return validate_root(self._root_str)

    # -- path resolution (public + private) -------------------------------------

    def path_for(self, conversation_id: str) -> Path:
        """The on-disk `workspace/` directory for a project. Raises on a poisoned
        ID — the agent-server only ever passes UUIDs, but this is defense in
        depth against any tampered input."""
        if self._root is None:
            raise StorageError("projects_root is not configured")
        if not _safe_segment(conversation_id):
            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
        return self._root / conversation_id / _WORKSPACE

    def manifest_for(self, conversation_id: str) -> Path:
        if self._root is None:
            raise StorageError("projects_root is not configured")
        if not _safe_segment(conversation_id):
            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
        return self._root / conversation_id / _MANIFEST

    def _project_dir(self, conversation_id: str) -> Path:
        return _project_dir(self._root, conversation_id)

    def _versions_dir(self, conversation_id: str) -> Path:
        return _versions_dir(self._root, conversation_id)

    def _versions_index(self, conversation_id: str) -> Path:
        return _versions_index_path(self._root, conversation_id)

    def _version_dir(self, conversation_id: str, record: VersionRecord) -> Path:
        return _version_dir_path(self._root, conversation_id, record)

    # -- serialization (cross-process EX flock + in-process RLock) ---------------

    @contextmanager
    def _version_transaction(
        self,
        conversation_id: str,
        *,
        exclusive: bool,
        allow_missing_project: bool = False,
    ) -> Iterator[None]:
        """Serialize version state across instances and server processes.

        The lock lives outside the project directory so project deletion cannot
        create an ABA split where waiters retain the old inode while a new
        caller locks a newly-created file. The in-process RLock also serializes
        threads because POSIX flock semantics alone are process-oriented on
        some supported filesystems.
        """

        if _fcntl is None:
            raise StorageError("strict version proof unsupported: POSIX flock is unavailable")
        if not _safe_segment(conversation_id):
            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
        root = self._root
        if root is None or root.is_symlink() or not root.is_dir():
            raise StorageError("projects root is missing or symlinked")
        locks = root / _LOCKS_DIRECTORY
        if locks.is_symlink():
            raise StorageError("project version lock directory is symlinked")
        locks.mkdir(parents=False, exist_ok=True)
        if locks.is_symlink() or not locks.is_dir():
            raise StorageError("project version lock directory is unavailable")
        lock_path = locks / f"{conversation_id}.lock"
        key = str(lock_path)
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(key, threading.RLock())
        with process_lock:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise StorageError(f"project version lock unavailable: {exc}") from exc
            try:
                lock_stat = os.fstat(fd)
                path_stat = lock_path.lstat()
                if not stat.S_ISREG(lock_stat.st_mode) or _stat_identity(
                    lock_stat
                ) != _stat_identity(path_stat):
                    raise StorageError("project version lock path changed or is not regular")
                operation = _fcntl.LOCK_EX if exclusive else _fcntl.LOCK_SH
                _fcntl.flock(fd, operation)
                project_dir = self._project_dir(conversation_id)
                if project_dir.is_symlink() or (project_dir.exists() and not project_dir.is_dir()):
                    raise StorageError("project path is symlinked or is not a directory")
                if project_dir.exists():
                    if exclusive:
                        self._coordinator._recover_pin_journal_if_pending(conversation_id)
                    elif self._coordinator._read_pin_journal(conversation_id) is not None:
                        raise StorageError("version state has a pending pin journal")
                elif not allow_missing_project:
                    raise StorageError("project directory is missing")
                yield
            finally:
                with contextlib.suppress(OSError):
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                os.close(fd)

    # -- workspace inspection ---------------------------------------------------

    @staticmethod
    def _tree_facts(root: Path) -> WorkspaceTreeFacts:
        hashes, total_bytes = _scan_immutable_tree(root)
        return WorkspaceTreeFacts(
            file_count=len(hashes),
            total_bytes=total_bytes,
            tree_digest=_tree_digest_from_hashes(hashes),
        )

    def inspect_workspace(self, conversation_id: str) -> WorkspaceTreeFacts:
        """Fresh strict facts for the published mutable snapshot mirror."""
        return self._tree_facts(self.path_for(conversation_id))

    # -- version read / verify / open -------------------------------------------

    def verify_version(self, conversation_id: str, seq: int) -> VersionRecord:
        """Return a version only after fresh tree, index, sidecar, and byte proof."""
        with self._version_transaction(conversation_id, exclusive=False):
            return self._reader.verify_version(conversation_id, seq)

    @contextmanager
    def open_verified_version(
        self,
        conversation_id: str,
        seq: int,
    ) -> Iterator[VerifiedVersionHandle]:
        """Yield the only capability authorized to consume committed bytes.

        The shared interprocess lock remains held until the caller has copied or
        read every required byte. No naked path escapes this boundary.
        """

        with self._version_transaction(conversation_id, exclusive=False):
            record, details = self._reader.verify_version_details(conversation_id, seq)
            workspace_fd = self._reader.open_version_workspace_fd(conversation_id, record)
            handle = VerifiedVersionHandle(
                record=record,
                files=tuple(
                    VerifiedFile(path=rel, sha256=digest, size=size)
                    for rel, (digest, size) in sorted(details.items())
                ),
                workspace_fd=workspace_fd,
            )
            try:
                yield handle
            finally:
                handle.close()

    # -- version life-cycle -----------------------------------------------------

    def cut_version(
        self, conversation_id: str, *, label: str = "", trigger: str
    ) -> VersionRecord | None:
        """Capture the live workspace mirror as a deduplicated version snapshot."""
        with self._version_transaction(conversation_id, exclusive=True):
            return self._coordinator.cut_version_locked(
                conversation_id,
                label=label,
                trigger=trigger,
                live_workspace=self.path_for(conversation_id),
            )

    def cut_verified_version(
        self,
        conversation_id: str,
        *,
        label: str = "",
        trigger: str,
        pin: bool = False,
    ) -> VersionRecord | None:
        """Cut or reuse, freshly prove, and optionally retention-pin a version.

        Unlike :meth:`cut_version`, an unchanged tree returns its verified
        existing record.  ``None`` remains in the return type for cut-style API
        compatibility, but every successful current implementation path returns
        a record.  Callers creating a durable final seal must pass ``pin=True``.
        """
        with self._version_transaction(conversation_id, exclusive=True):
            return self._coordinator.cut_verified_version_locked(
                conversation_id,
                label=label,
                trigger=trigger,
                pin=pin,
                live_workspace=self.path_for(conversation_id),
            )

    def list_versions(self, conversation_id: str) -> list[VersionRecord]:
        """Version summaries, newest first. Missing version dirs are ignored."""
        with self._version_transaction(
            conversation_id,
            exclusive=False,
            allow_missing_project=True,
        ):
            project_dir = self._project_dir(conversation_id)
            if not project_dir.exists():
                return []
            versions = self._versions_dir(conversation_id)
            if versions.is_symlink() or (versions.exists() and not versions.is_dir()):
                raise StorageError("versions path is symlinked or is not a directory")
            if not versions.exists():
                return []
            records = self._reader.existing_version_records(conversation_id)
            records.sort(key=lambda r: r.seq, reverse=True)
            return records

    def _require_plain_project_dir(self, conversation_id: str) -> Path:
        return _require_plain_project_dir(self._root, conversation_id)

    def set_version_pinned(self, conversation_id: str, seq: int, pinned: bool) -> VersionRecord:
        """Mark a version as retention-protected in the index and sidecar."""
        with self._version_transaction(conversation_id, exclusive=True):
            return self._set_version_pinned_locked(conversation_id, seq, pinned)

    # -- private delegator seams (preserved for internal/test callers) ----------

    def _cut_version_locked(
        self, conversation_id: str, *, label: str = "", trigger: str
    ) -> VersionRecord | None:
        return self._coordinator.cut_version_locked(
            conversation_id,
            label=label,
            trigger=trigger,
            live_workspace=self.path_for(conversation_id),
        )

    def _cut_verified_version_locked(
        self,
        conversation_id: str,
        *,
        label: str = "",
        trigger: str,
        pin: bool = False,
    ) -> VersionRecord | None:
        return self._coordinator.cut_verified_version_locked(
            conversation_id,
            label=label,
            trigger=trigger,
            pin=pin,
            live_workspace=self.path_for(conversation_id),
        )

    def _set_version_pinned_locked(
        self, conversation_id: str, seq: int, pinned: bool
    ) -> VersionRecord:
        return self._coordinator.set_version_pinned_locked(conversation_id, seq, pinned)

    def version_workspace_path(self, conversation_id: str, seq: int) -> Path:
        if not _safe_seq(seq):
            raise StorageError(f"unsafe version seq: {seq!r}")
        with self._version_transaction(conversation_id, exclusive=False):
            for record in self._reader.read_version_index(conversation_id):
                if record.seq != seq:
                    continue
                workspace = self._version_dir(conversation_id, record) / _WORKSPACE
                if not workspace.is_dir():
                    raise StorageError(f"version workspace missing: {seq!r}")
                return workspace
        raise StorageError(f"unknown version seq: {seq!r}")

    # -- project CRUD -----------------------------------------------------------

    def list_projects(self) -> list[ProjectRecord]:
        """All projects with a manifest under the root, newest snapshot first.
        Returns an empty list (not an error) if the root is unset / missing,
        so the list endpoint can fall back to its own graceful-empty state."""
        if self._root is None or not self._root.exists():
            return []
        records: list[ProjectRecord] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / _MANIFEST
            if not manifest.is_file():
                continue
            try:
                data = json.loads(manifest.read_text())
            except (OSError, json.JSONDecodeError):
                continue  # don't surface a corrupt manifest as a project row
            workspace = entry / _WORKSPACE
            files_missing = (
                not workspace.exists() or not workspace.is_dir() or not any(workspace.rglob("*"))
            )
            owner_id, legacy_unclaimed = _manifest_owner(data)
            records.append(
                ProjectRecord(
                    conversation_id=entry.name,
                    title=data.get("title"),
                    owner_id=owner_id,
                    created_at=data.get("created_at"),
                    last_snapshot_at=data.get("last_snapshot_at"),
                    file_count=int(data.get("file_count") or 0),
                    total_bytes=int(data.get("total_bytes") or 0),
                    files_missing=files_missing,
                    legacy_unclaimed_owner=legacy_unclaimed,
                    imported=bool(data.get("imported")),
                )
            )
        records.sort(key=lambda r: r.last_snapshot_at or "", reverse=True)
        return records

    def get(self, conversation_id: str) -> ProjectRecord | None:
        """Single-row lookup. Returns None if the project doesn't exist (the
        manifest is gone). Returns a record with files_missing=True if the
        manifest is there but the workspace is gone — that's a real state."""
        manifest = self.manifest_for(conversation_id)
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"manifest unreadable: {exc}") from exc
        workspace = self.path_for(conversation_id)
        files_missing = (
            not workspace.exists() or not workspace.is_dir() or not any(workspace.rglob("*"))
        )
        owner_id, legacy_unclaimed = _manifest_owner(data)
        return ProjectRecord(
            conversation_id=conversation_id,
            title=data.get("title"),
            owner_id=owner_id,
            created_at=data.get("created_at"),
            last_snapshot_at=data.get("last_snapshot_at"),
            file_count=int(data.get("file_count") or 0),
            total_bytes=int(data.get("total_bytes") or 0),
            files_missing=files_missing,
            legacy_unclaimed_owner=legacy_unclaimed,
            imported=bool(data.get("imported")),
        )

    def _existing_imported(self, conversation_id: str) -> bool:
        """The `imported` flag currently recorded in the on-disk manifest (False if
        the manifest is absent, unreadable, or predates the field). Used to PRESERVE
        import provenance across re-snapshots, which rewrite the manifest."""
        manifest = self.manifest_for(conversation_id)
        if not manifest.is_file():
            return False
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return bool(data.get("imported"))

    def write_manifest(
        self,
        conversation_id: str,
        *,
        title: str | None,
        owner_id: str | None,
        created_at: str | None,
        file_count: int,
        total_bytes: int,
        imported: bool | None = None,
    ) -> Path:
        """Write the manifest after a snapshot. Atomic via tmp + rename so a
        crash mid-write never leaves the file half-written. Returns the manifest
        path.

        `imported` records release provenance (see `ProjectRecord.imported`). It is
        sticky: pass `True` at IMPORT time; leave it `None` on every later re-snapshot
        so the recorded value is preserved (a re-snapshot that silently dropped it
        would demote an imported project back to `not_web`). Pass `False` only to
        explicitly clear it."""
        manifest = self.manifest_for(conversation_id)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if imported is None:
            imported = self._existing_imported(conversation_id)
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "title": title,
            "owner_id": owner_id,
            "created_at": created_at,
            "last_snapshot_at": _now_iso(),
            "file_count": file_count,
            "total_bytes": total_bytes,
            "imported": imported,
        }
        tmp = manifest.with_suffix(manifest.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(manifest)  # atomic on POSIX; close-enough elsewhere
        return manifest

    def release_intent_for(self, conversation_id: str) -> Path:
        """The host-owned `release-intent.json` sidecar path for a project — NEXT
        TO manifest.json (in `<root>/<cid>/`), OUTSIDE the `workspace/` tree. Same
        poisoned-id guard as `manifest_for` / `path_for`."""
        return self._project_dir(conversation_id) / _RELEASE_INTENT

    def write_release_intent(self, conversation_id: str, intent: ReleaseIntent) -> Path:
        """Persist the TYPED release intent as a host-owned sidecar next to
        manifest.json (OUTSIDE the workspace/ tree), ATOMICALLY via tmp + replace —
        the identical discipline `_write_json_atomic` uses for the manifest and
        version records, so a crash mid-write never leaves a half-written file and a
        re-declare overwrites in one atomic swap.

        The record is a CANDIDATE input to release DETECTION, never a verification /
        assessment claim, and it is value-free BY CONSTRUCTION: `ReleaseIntent`
        carries env-var NAMES only, so no secret value is ever written here. Returns
        the sidecar path."""
        path = self.release_intent_for(conversation_id)
        _write_json_atomic(path, intent.model_dump(mode="json"))
        return path

    def read_release_intent(self, conversation_id: str) -> ReleaseIntent | None:
        """Read + version-gate the host-owned release-intent sidecar (§8.11). Returns
        None when no intent has been declared (the sidecar is absent). A persisted v1
        (or legacy, version-less) shape is deterministically MIGRATED to the current
        schema by `parse_release_intent`. A version-gate refusal — a NEWER schema, or a
        v1 shape carrying a field the current schema forbids (an upgrade this build
        cannot perform without guessing) — propagates as `IntentUpgradeError` so the
        caller can surface the exact `intent_upgrade_required` blocker (§8.11); a
        genuinely corrupt / unreadable / malformed sidecar raises StorageError. Never
        silently reports 'no intent' for a broken file, and never reinterprets an older
        shape in place."""
        path = self.release_intent_for(conversation_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"release intent unreadable: {exc}") from exc
        try:
            return parse_release_intent(raw)
        except IntentUpgradeError:
            # A version-gate refusal is NOT a corrupt sidecar: let it propagate distinctly
            # so the release route surfaces `intent_upgrade_required` (needs_review) rather
            # than collapsing it into a StorageError / HTTP 500 (§8.11).
            raise
        except (ValidationError, ValueError) as exc:
            raise StorageError(f"release intent invalid: {exc}") from exc

    def delete(self, conversation_id: str) -> bool:
        """Remove a project's manifest + workspace. The conversation events in
        the SQLite store are left untouched — that's a separate decision the
        caller (the agent-server) makes. Returns True if anything was removed."""
        if self._root is None:
            return False
        with self._version_transaction(
            conversation_id,
            exclusive=True,
            allow_missing_project=True,
        ):
            project_dir = self._project_dir(conversation_id)
            if not project_dir.exists():
                return False
            if project_dir.is_symlink() or not project_dir.is_dir():
                raise StorageError("project directory is not a plain directory")
            shutil.rmtree(project_dir)
            _fsync_directory(self._root)
            return True

    def iter_workspace(self, conversation_id: str) -> Iterator[Path]:
        """Yield every file path in a project's workspace tree, sorted. Used by
        the download endpoint and by tests. Raises if the workspace is gone."""
        workspace = self.path_for(conversation_id)
        if not workspace.is_dir():
            raise StorageError(f"workspace directory missing: {workspace}")
        yield from sorted(
            p
            for p in workspace.rglob("*")
            if p.is_file()
            and not p.is_symlink()
            and not is_runtime_secret_path(p.relative_to(workspace).as_posix())
        )

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
import os
import stat
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from disco.core.owners import install_owner_id
from disco.core.release.spec import ReleaseIntent

from .archive import is_runtime_secret_path

try:  # POSIX on Linux/WSL2/macOS; strict cross-process proof fails closed without it.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - native Windows is not a supported strict host yet
    _fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Interior collaborators — decomposing ProjectStore without weakening caps.
# See `store_parts/` (house pattern for oversized modules): each import below
# is a cohesive piece split out to keep this module's own logical-line count
# (and, for the two collaborator classes, their own class-logical-size and
# per-method cyclomatic-complexity) within budget. Every name below remains
# reachable at `disco.tools.projects.store.<name>` exactly as before. Every
# store_parts callable that needs a name defined below (constants, StorageError,
# the dataclasses) resolves it through THIS module's own binding at call time
# (never a top-of-file import in the store_parts module), so import order here
# is unconstrained and `_write_json_atomic` monkeypatching stays effective no
# matter which collaborator ends up calling it.
#
# Compatibility re-exports (PKG-10-PROJECTS): imports written with a redundant
# `X as X` alias below are NOT used by this module — they are pure re-exports of
# top-level names this module exposed before the split, kept so the module
# surface is unchanged for anything that reaches them through the module object.
# isort interleaves them with the real imports; the alias is what marks them.
# PKG-13-FACADES owns their eventual deletion.
#
# Three parent-era names are deliberately NOT restored, as a recorded root
# override of the facade-surface differential (Epic 10-A): `json`, `shutil` and
# `tempfile` were bare `import <stdlib>` bindings whose only uses moved into
# `store_parts/`. Root re-ran the consumer analysis and `store.json`,
# `store.shutil` and `store.tempfile` have zero call sites anywhere in the
# tree, and re-adding an unused plain `import` would require a per-line lint
# suppression the engineering rules forbid.
# ---------------------------------------------------------------------------
from collections.abc import Mapping as Mapping
from dataclasses import replace as replace

from disco.core.release.spec import IntentUpgradeError as IntentUpgradeError
from disco.core.release.spec import parse_release_intent as parse_release_intent
from pydantic import ValidationError as ValidationError

from .store_parts.fs_proof import _file_sha256 as _file_sha256
from .store_parts.fs_proof import _read_regular_file_beneath, _stat_identity
from .store_parts.json_atomic import _fsync_directory as _fsync_directory
from .store_parts.json_atomic import _fsync_tree as _fsync_tree
from .store_parts.json_atomic import _read_json_regular_nofollow as _read_json_regular_nofollow
from .store_parts.json_atomic import (
    _write_json_atomic,
)
from .store_parts.project_store_ops import (
    _collect_project_records,
    _delete_project_dir,
    _project_record_from_manifest,
    _read_existing_imported_flag,
    _read_release_intent_sidecar,
    _require_version_lock_path,
    _write_project_manifest,
)
from .store_parts.tree_scan import (
    _scan_immutable_tree,
    _tree_digest_from_hashes,
)
from .store_parts.tree_scan import _scan_immutable_tree_details as _scan_immutable_tree_details
from .store_parts.tree_scan import _scan_tree as _scan_tree
from .store_parts.tree_scan import tree_digest as tree_digest
from .store_parts.tree_scan import tree_digest_of_files as tree_digest_of_files
from .store_parts.verified_reader import _VerifiedVersionReader
from .store_parts.version_coordinator import _VersionStateCoordinator
from .store_parts.version_serde import _payload_matches_record as _payload_matches_record
from .store_parts.version_serde import _version_from_dict as _version_from_dict
from .store_parts.version_serde import _version_to_dict as _version_to_dict

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

        lock_path = _require_version_lock_path(self._root, conversation_id)
        # `_require_version_lock_path` already refuses when POSIX flock is
        # unavailable, but that guard now lives in another module, so it no
        # longer narrows `_fcntl` here. Re-establish the precondition locally
        # rather than assert it away: this function dereferences `_fcntl`
        # directly, including from the `finally` below, so it must own the
        # check and bind the non-optional module BEFORE the try/finally — a
        # binding made inside the `try` would be unbound in `finally` on any
        # earlier failure and would mask the real error with UnboundLocalError.
        fcntl_mod = _fcntl
        if fcntl_mod is None:
            raise StorageError("strict version proof unsupported: POSIX flock is unavailable")
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
                operation = fcntl_mod.LOCK_EX if exclusive else fcntl_mod.LOCK_SH
                fcntl_mod.flock(fd, operation)
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
                    fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)
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
        return _collect_project_records(self._root)

    def get(self, conversation_id: str) -> ProjectRecord | None:
        """Single-row lookup. Returns None if the project doesn't exist (the
        manifest is gone). Returns a record with files_missing=True if the
        manifest is there but the workspace is gone — that's a real state."""
        return _project_record_from_manifest(
            conversation_id, self.manifest_for(conversation_id), self.path_for(conversation_id)
        )

    def _existing_imported(self, conversation_id: str) -> bool:
        """The `imported` flag currently recorded in the on-disk manifest (False if
        the manifest is absent, unreadable, or predates the field). Used to PRESERVE
        import provenance across re-snapshots, which rewrite the manifest."""
        return _read_existing_imported_flag(self.manifest_for(conversation_id))

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
        if imported is None:
            imported = self._existing_imported(conversation_id)
        _write_project_manifest(
            manifest,
            conversation_id=conversation_id,
            title=title,
            owner_id=owner_id,
            created_at=created_at,
            file_count=file_count,
            total_bytes=total_bytes,
            imported=imported,
        )
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
        return _read_release_intent_sidecar(self.release_intent_for(conversation_id))

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
            return _delete_project_dir(self._root, conversation_id)

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

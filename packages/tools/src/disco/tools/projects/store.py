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

import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

# Used both as the manifest filename and the workspace subdirectory; constants
# here so the agent-server doesn't depend on string literals scattered around.
_MANIFEST = "manifest.json"
_WORKSPACE = "workspace"


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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_segment(s: str) -> bool:
    """A conversation ID is a primary key but defensively: reject anything that
    isn't a plain segment (no slashes, no dot-dot, no nulls). Belt and braces
    against a poisoned input becoming a path traversal."""
    if not s or s in {".", ".."}:
        return False
    if "/" in s or "\\" in s or "\x00" in s:
        return False
    return True


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


class ProjectStore:
    """The disk side of project persistence. List/get/delete + path resolvers
    callers (the agent-server) use to wire snapshot, rehydrate, and download
    against a per-project directory.

    Construction does NOT throw on a bad root — the validation status is
    queryable via `status()` / `validate_root(...)`. Operations that REQUIRE
    a valid root (snapshot, list) raise StorageError with the reason; the
    settings round-trip + the read-only browse endpoint work even on a bad
    root (the user has to be able to see + fix the configuration)."""

    def __init__(self, root_str: str) -> None:
        # _configured keeps the raw value for the settings DTO (so the UI can
        # display what the user explicitly saved, not the auto-default path).
        self._configured_str = root_str
        # _root_str is the effective path used for all disk operations.  Empty
        # root_str → resolve_projects_root auto-creates and returns the default.
        self._root_str = resolve_projects_root(root_str)
        self._root = Path(self._root_str).expanduser()

    @property
    def root(self) -> Path | None:
        """The expanded absolute effective root path. Always set (never None)
        after construction; the auto-default is created on first use."""
        return self._root

    def status(self) -> StorageStatus:
        return validate_root(self._root_str)

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
                not workspace.exists()
                or not workspace.is_dir()
                or not any(workspace.rglob("*"))
            )
            records.append(
                ProjectRecord(
                    conversation_id=entry.name,
                    title=data.get("title"),
                    owner_id=data.get("owner_id"),
                    created_at=data.get("created_at"),
                    last_snapshot_at=data.get("last_snapshot_at"),
                    file_count=int(data.get("file_count") or 0),
                    total_bytes=int(data.get("total_bytes") or 0),
                    files_missing=files_missing,
                )
            )
        # Newest last-snapshot first; entries with no timestamp sort last.
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
            not workspace.exists()
            or not workspace.is_dir()
            or not any(workspace.rglob("*"))
        )
        return ProjectRecord(
            conversation_id=conversation_id,
            title=data.get("title"),
            owner_id=data.get("owner_id"),
            created_at=data.get("created_at"),
            last_snapshot_at=data.get("last_snapshot_at"),
            file_count=int(data.get("file_count") or 0),
            total_bytes=int(data.get("total_bytes") or 0),
            files_missing=files_missing,
        )

    def write_manifest(
        self,
        conversation_id: str,
        *,
        title: str | None,
        owner_id: str | None,
        created_at: str | None,
        file_count: int,
        total_bytes: int,
    ) -> Path:
        """Write the manifest after a snapshot. Atomic via tmp + rename so a
        crash mid-write never leaves the file half-written. Returns the manifest
        path."""
        manifest = self.manifest_for(conversation_id)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "title": title,
            "owner_id": owner_id,
            "created_at": created_at,
            "last_snapshot_at": _now_iso(),
            "file_count": file_count,
            "total_bytes": total_bytes,
        }
        tmp = manifest.with_suffix(manifest.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(manifest)  # atomic on POSIX; close-enough elsewhere
        return manifest

    def delete(self, conversation_id: str) -> bool:
        """Remove a project's manifest + workspace. The conversation events in
        the SQLite store are left untouched — that's a separate decision the
        caller (the agent-server) makes. Returns True if anything was removed."""
        if self._root is None:
            return False
        project_dir = self._root / conversation_id
        if not project_dir.exists():
            return False
        shutil.rmtree(project_dir)
        return True

    def iter_workspace(self, conversation_id: str) -> Iterator[Path]:
        """Yield every file path in a project's workspace tree, sorted. Used by
        the download endpoint and by tests. Raises if the workspace is gone."""
        workspace = self.path_for(conversation_id)
        if not workspace.is_dir():
            raise StorageError(f"workspace directory missing: {workspace}")
        yield from sorted(p for p in workspace.rglob("*") if p.is_file())

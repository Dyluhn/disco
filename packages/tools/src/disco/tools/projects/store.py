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
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

# Used both as the manifest filename and the workspace subdirectory; constants
# here so the agent-server doesn't depend on string literals scattered around.
_MANIFEST = "manifest.json"
_WORKSPACE = "workspace"
_VERSIONS = "versions"
_VERSIONS_INDEX = "versions.json"
_VERSION_METADATA = "version.json"

# UX retention: unlabeled auto-cuts are cheap, but the picker must stay bounded.
_MAX_UNLABELED = 20
# Disk retention: one conversation should not silently consume a whole data volume.
_MAX_VERSION_BYTES = 512 * 1024 * 1024


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


def _scan_tree(root: Path) -> tuple[dict[str, str], int]:
    if not root.is_dir():
        raise StorageError(f"workspace directory missing: {root}")
    hashes: dict[str, str] = {}
    total_bytes = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        hashes[rel] = _file_sha256(path)
        total_bytes += stat.st_size
    return hashes, total_bytes


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


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic on POSIX; close-enough elsewhere


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

    def __init__(
        self, root_str: str, *, version_byte_budget: int = _MAX_VERSION_BYTES
    ) -> None:
        # _configured keeps the raw value for the settings DTO (so the UI can
        # display what the user explicitly saved, not the auto-default path).
        self._configured_str = root_str
        # _root_str is the effective path used for all disk operations.  Empty
        # root_str → resolve_projects_root auto-creates and returns the default.
        self._root_str = resolve_projects_root(root_str)
        self._root = Path(self._root_str).expanduser()
        self._version_byte_budget = version_byte_budget

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

    def _project_dir(self, conversation_id: str) -> Path:
        if self._root is None:
            raise StorageError("projects_root is not configured")
        if not _safe_segment(conversation_id):
            raise StorageError(f"unsafe conversation_id: {conversation_id!r}")
        return self._root / conversation_id

    def _versions_dir(self, conversation_id: str) -> Path:
        return self._project_dir(conversation_id) / _VERSIONS

    def _versions_index(self, conversation_id: str) -> Path:
        return self._project_dir(conversation_id) / _VERSIONS_INDEX

    def _version_dir(self, conversation_id: str, record: VersionRecord) -> Path:
        digest12 = record.tree_digest[:12]
        return self._versions_dir(conversation_id) / f"{record.seq:03d}-{digest12}"

    def _read_version_index(self, conversation_id: str) -> list[VersionRecord]:
        index = self._versions_index(conversation_id)
        if not index.is_file():
            return []
        try:
            raw = json.loads(index.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"versions index unreadable: {exc}") from exc
        if not isinstance(raw, list):
            raise StorageError("versions index unreadable: expected a list")
        records: list[VersionRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                raise StorageError("versions index unreadable: malformed row")
            records.append(_version_from_dict(item))
        records.sort(key=lambda r: r.seq)
        return records

    def _write_version_index(
        self, conversation_id: str, records: list[VersionRecord]
    ) -> None:
        payload = [_version_to_dict(record) for record in sorted(records, key=lambda r: r.seq)]
        _write_json_atomic(self._versions_index(conversation_id), payload)

    def _existing_version_records(self, conversation_id: str) -> list[VersionRecord]:
        return [
            record
            for record in self._read_version_index(conversation_id)
            if (self._version_dir(conversation_id, record) / _WORKSPACE).is_dir()
        ]

    def _copy_version_workspace(
        self,
        conversation_id: str,
        *,
        live_workspace: Path,
        dest_workspace: Path,
        live_hashes: dict[str, str],
        previous: VersionRecord | None,
    ) -> None:
        previous_workspace: Path | None = None
        previous_hashes: dict[str, str] = {}
        if previous is not None:
            previous_workspace = self._version_dir(conversation_id, previous) / _WORKSPACE
            if previous_workspace.is_dir():
                previous_hashes, _ = _scan_tree(previous_workspace)

        for rel in sorted(live_hashes):
            source = live_workspace / rel
            target = dest_workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if (
                previous_workspace is not None
                and previous_hashes.get(rel) == live_hashes[rel]
            ):
                previous_source = previous_workspace / rel
                try:
                    os.link(previous_source, target)
                    continue
                except OSError:
                    pass  # cross-device / unsupported hardlinks still produce a copy
            shutil.copy2(source, target)

    def cut_version(
        self, conversation_id: str, *, label: str = "", trigger: str
    ) -> VersionRecord | None:
        """Capture the live workspace mirror as a deduplicated version snapshot."""
        live_workspace = self.path_for(conversation_id)
        live_hashes, total_bytes = _scan_tree(live_workspace)
        digest = _tree_digest_from_hashes(live_hashes)
        records = self._existing_version_records(conversation_id)
        newest = records[-1] if records else None
        if newest is not None and newest.tree_digest == digest:
            return None

        seq = (max((record.seq for record in records), default=0) + 1)
        record = VersionRecord(
            seq=seq,
            ts=_now_iso(),
            label=label,
            trigger=trigger,
            file_count=len(live_hashes),
            total_bytes=total_bytes,
            tree_digest=digest,
            pinned=False,
        )
        version_dir = self._version_dir(conversation_id, record)
        if version_dir.exists():
            raise StorageError(f"version directory already exists: {version_dir}")
        dest_workspace = version_dir / _WORKSPACE
        dest_workspace.mkdir(parents=True, exist_ok=False)
        try:
            self._copy_version_workspace(
                conversation_id,
                live_workspace=live_workspace,
                dest_workspace=dest_workspace,
                live_hashes=live_hashes,
                previous=newest,
            )
            _write_json_atomic(version_dir / _VERSION_METADATA, _version_to_dict(record))
            records.append(record)
            self._write_version_index(conversation_id, records)
            self._prune(conversation_id)
        except Exception:
            if version_dir.exists():
                shutil.rmtree(version_dir)
            raise
        return record

    def list_versions(self, conversation_id: str) -> list[VersionRecord]:
        """Version summaries, newest first. Missing version dirs are ignored."""
        records = self._existing_version_records(conversation_id)
        records.sort(key=lambda r: r.seq, reverse=True)
        return records

    def set_version_pinned(
        self, conversation_id: str, seq: int, pinned: bool
    ) -> VersionRecord:
        """Mark a version as retention-protected in the index and sidecar."""
        if not _safe_seq(seq):
            raise StorageError(f"unsafe version seq: {seq!r}")
        records = self._read_version_index(conversation_id)
        updated: VersionRecord | None = None
        next_records: list[VersionRecord] = []
        for record in records:
            if record.seq == seq:
                updated = replace(record, pinned=bool(pinned))
                next_records.append(updated)
            else:
                next_records.append(record)
        if updated is None:
            raise StorageError(f"unknown version seq: {seq!r}")

        version_dir = self._version_dir(conversation_id, updated)
        metadata = version_dir / _VERSION_METADATA
        if not metadata.is_file():
            raise StorageError(f"version metadata missing: {seq!r}")

        _write_json_atomic(metadata, _version_to_dict(updated))
        self._write_version_index(conversation_id, next_records)
        return updated

    def version_workspace_path(self, conversation_id: str, seq: int) -> Path:
        if not _safe_seq(seq):
            raise StorageError(f"unsafe version seq: {seq!r}")
        for record in self._read_version_index(conversation_id):
            if record.seq != seq:
                continue
            workspace = self._version_dir(conversation_id, record) / _WORKSPACE
            if not workspace.is_dir():
                raise StorageError(f"version workspace missing: {seq!r}")
            return workspace
        raise StorageError(f"unknown version seq: {seq!r}")

    def _versions_total_bytes(
        self, conversation_id: str, records: list[VersionRecord]
    ) -> int:
        seen: set[tuple[int, int]] = set()
        total = 0
        for record in records:
            workspace = self._version_dir(conversation_id, record) / _WORKSPACE
            if not workspace.is_dir():
                continue
            for path in sorted(p for p in workspace.rglob("*") if p.is_file()):
                stat = path.stat()
                key = (stat.st_dev, stat.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += stat.st_size
        return total

    def _drop_version(self, conversation_id: str, record: VersionRecord) -> None:
        version_dir = self._version_dir(conversation_id, record)
        if version_dir.exists():
            shutil.rmtree(version_dir)

    def _prune(self, conversation_id: str) -> None:
        records = self._existing_version_records(conversation_id)
        changed = len(records) != len(self._read_version_index(conversation_id))

        prunable = [record for record in records if not record.label and not record.pinned]
        for record in prunable[:-_MAX_UNLABELED]:
            self._drop_version(conversation_id, record)
            records.remove(record)
            changed = True

        while self._versions_total_bytes(conversation_id, records) > self._version_byte_budget:
            candidate = next(
                (record for record in records if not record.label and not record.pinned),
                None,
            )
            if candidate is None:
                break
            self._drop_version(conversation_id, candidate)
            records.remove(candidate)
            changed = True

        if changed:
            self._write_version_index(conversation_id, records)

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

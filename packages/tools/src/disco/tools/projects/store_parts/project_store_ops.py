"""Free functions behind `ProjectStore`'s CRUD, lock-path, and release-intent
methods.

`ProjectStore` remains the authority — it keeps every public method, and the
methods keep their exact signatures and error strings — but each method body
here is a thin delegator to one of these functions, which is what brings the
class's own logical-line count back under budget (`_require_version_lock_path`
additionally decomposes `_version_transaction`'s branching, clearing its
cyclomatic-complexity violation — relocation alone does not reduce McCabe).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from disco.core.release.spec import IntentUpgradeError, ReleaseIntent, parse_release_intent
from pydantic import ValidationError

if TYPE_CHECKING:
    from ..store import ProjectRecord


def _require_version_lock_path(root: Path | None, conversation_id: str) -> Path:
    """Validate the root/segment/locks-dir invariants and return the
    per-conversation lock file path."""
    from disco.tools.projects import store

    if store._fcntl is None:
        raise store.StorageError("strict version proof unsupported: POSIX flock is unavailable")
    if not store._safe_segment(conversation_id):
        raise store.StorageError(f"unsafe conversation_id: {conversation_id!r}")
    if root is None or root.is_symlink() or not root.is_dir():
        raise store.StorageError("projects root is missing or symlinked")
    locks = root / store._LOCKS_DIRECTORY
    if locks.is_symlink():
        raise store.StorageError("project version lock directory is symlinked")
    locks.mkdir(parents=False, exist_ok=True)
    if locks.is_symlink() or not locks.is_dir():
        raise store.StorageError("project version lock directory is unavailable")
    return locks / f"{conversation_id}.lock"


def _collect_project_records(root: Path) -> list[ProjectRecord]:
    from disco.tools.projects import store

    records: list[ProjectRecord] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / store._MANIFEST
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue  # don't surface a corrupt manifest as a project row
        workspace = entry / store._WORKSPACE
        files_missing = (
            not workspace.exists() or not workspace.is_dir() or not any(workspace.rglob("*"))
        )
        owner_id, legacy_unclaimed = store._manifest_owner(data)
        records.append(
            store.ProjectRecord(
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


def _project_record_from_manifest(
    conversation_id: str, manifest: Path, workspace: Path
) -> ProjectRecord | None:
    from disco.tools.projects import store

    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise store.StorageError(f"manifest unreadable: {exc}") from exc
    files_missing = (
        not workspace.exists() or not workspace.is_dir() or not any(workspace.rglob("*"))
    )
    owner_id, legacy_unclaimed = store._manifest_owner(data)
    return store.ProjectRecord(
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


def _read_existing_imported_flag(manifest: Path) -> bool:
    """The `imported` flag currently recorded in the on-disk manifest (False if
    the manifest is absent, unreadable, or predates the field)."""
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("imported"))


def _write_project_manifest(
    manifest: Path,
    *,
    conversation_id: str,
    title: str | None,
    owner_id: str | None,
    created_at: str | None,
    file_count: int,
    total_bytes: int,
    imported: bool,
) -> None:
    from disco.tools.projects import store

    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "conversation_id": conversation_id,
        "title": title,
        "owner_id": owner_id,
        "created_at": created_at,
        "last_snapshot_at": store._now_iso(),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "imported": imported,
    }
    tmp = manifest.with_suffix(manifest.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(manifest)  # atomic on POSIX; close-enough elsewhere


def _read_release_intent_sidecar(path: Path) -> ReleaseIntent | None:
    """Read + version-gate the host-owned release-intent sidecar (§8.11)."""
    from disco.tools.projects import store

    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise store.StorageError(f"release intent unreadable: {exc}") from exc
    try:
        return parse_release_intent(raw)
    except IntentUpgradeError:
        # A version-gate refusal is NOT a corrupt sidecar: let it propagate distinctly
        # so the release route surfaces `intent_upgrade_required` (needs_review) rather
        # than collapsing it into a StorageError / HTTP 500 (§8.11).
        raise
    except (ValidationError, ValueError) as exc:
        raise store.StorageError(f"release intent invalid: {exc}") from exc


def _delete_project_dir(root: Path, conversation_id: str) -> bool:
    from disco.tools.projects import store

    project_dir = store._project_dir(root, conversation_id)
    if not project_dir.exists():
        return False
    if project_dir.is_symlink() or not project_dir.is_dir():
        raise store.StorageError("project directory is not a plain directory")
    shutil.rmtree(project_dir)
    store._fsync_directory(root)
    return True

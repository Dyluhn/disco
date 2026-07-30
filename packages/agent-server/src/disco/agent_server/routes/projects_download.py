"""Project manifest and source-bound archive mechanics."""

from __future__ import annotations

import asyncio
import contextlib
import io
import posixpath
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from disco.core import ConversationStatus, DeliverableEvent, StatusEvent
from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore, install_owner_id
from disco.tools.projects import (
    ProjectRecord,
    ProjectStore,
    StorageError,
    StorageStatus,
    VersionRecord,
    aiter_zip_workspace,
    is_runtime_secret_path,
)
from disco.tools.projects.store import tree_digest_of_files
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..auth import current_session
from ..runtime import ConversationRuntime
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from ._common import require_owned_conversation

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def project_owner_for_session(
    raw_owner_id: str | None,
    session: AuthSession,
    *,
    legacy_unclaimed_owner: bool = False,
) -> str | None:
    owner_id = raw_owner_id.strip() if isinstance(raw_owner_id, str) else ""
    legacy_unclaimed = legacy_unclaimed_owner or not owner_id
    effective_owner = owner_id or install_owner_id()
    if effective_owner != session.owner_id or (legacy_unclaimed and not session.is_admin):
        return None
    return effective_owner


async def _consumer_workspace(
    event_store: SqliteEventStore,
    project_store: ProjectStore,
    conversation_id: str,
) -> tuple[Path | None, VersionRecord | None, int | None, list[Any]]:
    events = await event_store.get_events(conversation_id)
    latest_status = next(
        (event for event in reversed(events) if isinstance(event, StatusEvent)),
        None,
    )
    if latest_status is not None and latest_status.status is ConversationStatus.FINISHED:
        try:
            committed = resolve_committed_workspace(events, project_store, conversation_id)
        except WorkspaceCommitUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "workspace_unsealed"},
            ) from exc
        return None, committed.record, committed.event.seq, events
    return project_store.path_for(conversation_id), None, None, events


def _project_store(runtime: ConversationRuntime | None) -> ProjectStore:
    project_store = runtime.project_store() if runtime is not None else None
    if project_store is None or project_store.status() != StorageStatus.OK:
        raise HTTPException(status_code=404, detail={"reason": "storage_unavailable"})
    return project_store


def _owner_scoped_record(
    project_store: ProjectStore,
    conversation_id: str,
    request: Request,
) -> ProjectRecord:
    record = project_store.get(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
    owner = project_owner_for_session(
        record.owner_id,
        current_session(request),
        legacy_unclaimed_owner=record.legacy_unclaimed_owner,
    )
    if owner is None:
        raise HTTPException(status_code=403, detail={"reason": "project_forbidden"})
    return record


async def _best_effort_overlay(
    project_store: ProjectStore,
    record: ProjectRecord,
    live_mirror: Path,
) -> tuple[dict[str, str], int | None]:
    try:
        from .release import assess_project

        assessed = await asyncio.to_thread(
            assess_project,
            project_store,
            record,
            live_mirror,
            record.conversation_id,
        )
        return assessed.overlay_files, assessed.response.version_seq
    except Exception:  # assessment must never break the plain download
        return {}, None


def _verified_download_payload(
    project_store: ProjectStore,
    record: ProjectRecord,
    committed: VersionRecord,
    overlay: dict[str, str],
    assessed_seq: int | None,
) -> bytes:
    with project_store.open_verified_version(record.conversation_id, committed.seq) as verified:
        if not overlay or assessed_seq != committed.seq:
            return verified.zip_bytes()
        combined = {
            entry.path: verified.read_bytes(entry.path)
            for entry in verified.files
            if not is_runtime_secret_path(entry.path)
        }
        for rel, text in overlay.items():
            if rel not in combined and not is_runtime_secret_path(rel):
                combined[rel] = text.encode("utf-8")
        return _deterministic_zip(combined)


async def handle_download_project(
    conversation_id: str,
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> Response:
    project_store = _project_store(runtime)
    record = _owner_scoped_record(project_store, conversation_id, request)
    workspace, committed, _seal_seq, _events = await _consumer_workspace(
        store,
        project_store,
        conversation_id,
    )
    if committed is None and record.files_missing:
        raise HTTPException(status_code=404, detail={"reason": "files_missing"})
    live_mirror = workspace or project_store.path_for(conversation_id)
    overlay, assessed_seq = await _best_effort_overlay(project_store, record, live_mirror)
    headers = {"Content-Disposition": f'attachment; filename="{conversation_id}.zip"'}
    if committed is not None:
        try:
            payload = _verified_download_payload(
                project_store,
                record,
                committed,
                overlay,
                assessed_seq,
            )
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"reason": "workspace_unsealed"},
            ) from exc
        return Response(content=payload, media_type="application/zip", headers=headers)
    assert workspace is not None
    body = (
        _aiter_zip_with_overlay(workspace, overlay) if overlay else aiter_zip_workspace(workspace)
    )
    return StreamingResponse(body, media_type="application/zip", headers=headers)


def _verified_manifest_files(
    project_store: ProjectStore,
    conversation_id: str,
    committed: VersionRecord,
) -> list[dict[str, Any]]:
    with project_store.open_verified_version(conversation_id, committed.seq) as verified:
        return [
            {"path": entry.path, "bytes": entry.size}
            for entry in verified.files
            if not PurePosixPath(entry.path).name.startswith("_codeact")
        ]


def _mutable_manifest_files(workspace: Path | None) -> list[dict[str, Any]]:
    if workspace is None or not workspace.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(workspace.rglob("*")):
        rel = path.relative_to(workspace).as_posix()
        if (
            path.is_file()
            and not path.is_symlink()
            and not path.name.startswith("_codeact")
            and not is_runtime_secret_path(rel)
        ):
            files.append({"path": str(path.relative_to(workspace)), "bytes": path.stat().st_size})
    return files


def _last_deliverable(events: list[Any], seal_seq: int | None) -> dict[str, Any] | None:
    eligible = [
        event for event in events if seal_seq is None or event.seq is None or event.seq <= seal_seq
    ]
    event = next(
        (candidate for candidate in reversed(eligible) if isinstance(candidate, DeliverableEvent)),
        None,
    )
    if event is None:
        return None
    return {
        "title": event.title,
        "path": event.path,
        "kind": event.artifact_kind,
        "deployment_url": event.deployment_url,
    }


async def handle_project_manifest(
    conversation_id: str,
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict[str, Any]:
    project_store = _project_store(runtime)
    record = _owner_scoped_record(project_store, conversation_id, request)
    workspace, committed, seal_seq, events = await _consumer_workspace(
        store,
        project_store,
        conversation_id,
    )
    try:
        files = (
            _verified_manifest_files(project_store, conversation_id, committed)
            if committed is not None
            else _mutable_manifest_files(workspace)
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"reason": "workspace_unsealed"}) from exc
    deliverable = None
    with contextlib.suppress(Exception):
        deliverable = _last_deliverable(events, seal_seq)
    return {
        "conversation_id": conversation_id,
        "title": record.title or "(untitled)",
        "created_at": record.created_at,
        "last_snapshot_at": record.last_snapshot_at,
        "file_count": committed.file_count if committed is not None else record.file_count,
        "total_bytes": committed.total_bytes if committed is not None else record.total_bytes,
        "files": files,
        "deliverable": deliverable,
    }


async def resolve_project_for_read(
    request: Request,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    conversation_id: str,
) -> tuple[ProjectStore, ProjectRecord, Path]:
    conversation_id = await require_owned_conversation(request, store, conversation_id)
    project_store = _project_store(runtime)
    record = _owner_scoped_record(project_store, conversation_id, request)
    if record.files_missing:
        raise HTTPException(status_code=404, detail={"reason": "files_missing"})
    return project_store, record, project_store.path_for(conversation_id)


def _ancestor_dirs(rel: str) -> list[str]:
    parts = rel.split("/")
    return ["/".join(parts[: index + 1]) for index in range(len(parts) - 1)]


class _ArchiveNames:
    def __init__(self) -> None:
        self.files: set[str] = set()
        self.files_lower: set[str] = set()
        self.directories: set[str] = set()
        self.directories_lower: set[str] = set()

    def reserve(self, rel: str) -> None:
        self.files.add(rel)
        self.files_lower.add(rel.lower())
        for parent in _ancestor_dirs(rel):
            self.directories.add(parent)
            self.directories_lower.add(parent.lower())

    def available(self, rel: str) -> bool:
        ancestors = _ancestor_dirs(rel)
        return (
            rel not in self.files
            and rel.lower() not in self.files_lower
            and rel not in self.directories
            and rel.lower() not in self.directories_lower
            and not any(
                parent in self.files or parent.lower() in self.files_lower for parent in ancestors
            )
        )


def _safe_overlay_rel(raw: str) -> str | None:
    rel = posixpath.normpath(raw.replace("\\", "/"))
    if (
        rel in {"", ".", ".."}
        or rel.startswith("../")
        or posixpath.isabs(rel)
        or is_runtime_secret_path(rel)
    ):
        return None
    return rel


def _write_workspace(
    archive: zipfile.ZipFile,
    source: Path,
    names: _ArchiveNames,
) -> None:
    for path in sorted(
        candidate
        for candidate in source.rglob("*")
        if candidate.is_file() and not candidate.is_symlink()
    ):
        rel = path.relative_to(source).as_posix()
        if is_runtime_secret_path(rel):
            continue
        archive.write(path, arcname=rel)
        names.reserve(posixpath.normpath(rel))


def _write_overlay(
    archive: zipfile.ZipFile,
    overlay: dict[str, str],
    names: _ArchiveNames,
) -> None:
    for raw in sorted(overlay):
        rel = _safe_overlay_rel(raw)
        if rel is None or not names.available(rel):
            continue
        archive.writestr(rel, overlay[raw].encode("utf-8"))
        names.reserve(rel)


def _zip_workspace_with_overlay(source: Path, overlay: dict[str, str]) -> Iterator[bytes]:
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"workspace directory not found: {source}")
    buffer = io.BytesIO()
    names = _ArchiveNames()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_workspace(archive, source, names)
        _write_overlay(archive, overlay, names)
    buffer.seek(0)
    while block := buffer.read(64 * 1024):
        yield block


async def _aiter_zip_with_overlay(
    source: Path,
    overlay: dict[str, str],
) -> AsyncIterator[bytes]:
    for block in _zip_workspace_with_overlay(source, overlay):
        yield block


class _BoundReject(Exception):
    def __init__(self, status_code: int, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.status_code = status_code
        self.reason = reason
        self.message = message or reason


def _deterministic_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(filename=rel, date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[rel])
    return buffer.getvalue()


def _version_source_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file() and not candidate.is_symlink()
    ):
        rel = path.relative_to(root).as_posix()
        if not is_runtime_secret_path(rel):
            files[rel] = path.read_bytes()
    return files


def _bound_source(
    project_store: ProjectStore,
    conversation_id: str,
    version_seq: int,
) -> tuple[VersionRecord, dict[str, bytes]]:
    try:
        workspace = project_store.version_workspace_path(conversation_id, version_seq)
    except StorageError as exc:
        raise _BoundReject(410, "version_not_found", str(exc)) from exc
    record = next(
        (
            version
            for version in project_store.list_versions(conversation_id)
            if version.seq == version_seq
        ),
        None,
    )
    if record is None:
        raise _BoundReject(410, "version_not_found", f"no committed version {version_seq}")
    try:
        files = _version_source_files(workspace)
    except OSError as exc:
        raise _BoundReject(410, "version_not_found", "version workspace was removed") from exc
    if tree_digest_of_files(files) != record.tree_digest:
        raise _BoundReject(
            409,
            "source_integrity_failed",
            "version bytes no longer match the recorded digest",
        )
    return record, files


def _bound_overlay(
    project_store: ProjectStore,
    record: ProjectRecord,
    version: VersionRecord,
    source_files: dict[str, bytes],
    spec_digest_value: str,
) -> dict[str, str]:
    from .release import assess_release

    try:
        intent = project_store.read_release_intent(record.conversation_id)
    except StorageError as exc:
        raise _BoundReject(500, "release_intent_unreadable", str(exc)) from exc
    assessed = assess_release(
        source_files,
        intent=intent,
        project_name=record.title or record.conversation_id,
        version_seq=version.seq,
        tree_digest=version.tree_digest,
        source_snapshotted=True,
        imported=record.imported,
    )
    response = assessed.response
    if not response.self_host or response.spec_digest is None:
        raise _BoundReject(
            409,
            "not_self_hostable",
            "the bound version is not a self-host candidate",
        )
    if response.spec_digest != spec_digest_value:
        raise _BoundReject(
            409,
            "spec_digest_mismatch",
            "spec_digest does not match the bound version",
        )
    return assessed.overlay_files


def _build_bound_download_zip(
    project_store: ProjectStore,
    record: ProjectRecord,
    conversation_id: str,
    version_seq: int,
    spec_digest_value: str,
) -> bytes:
    version, source_files = _bound_source(project_store, conversation_id, version_seq)
    overlay = _bound_overlay(
        project_store,
        record,
        version,
        source_files,
        spec_digest_value,
    )
    combined = dict(source_files)
    for rel, text in overlay.items():
        if rel not in combined and not is_runtime_secret_path(rel):
            combined[rel] = text.encode("utf-8")
    return _deterministic_zip(combined)


async def _aiter_bytes(payload: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), 64 * 1024):
        yield payload[start : start + 64 * 1024]


async def _bound_download_response(
    project_store: ProjectStore,
    record: ProjectRecord,
    version_seq: int | None,
    spec_digest: str | None,
    headers: dict[str, str],
) -> StreamingResponse:
    if version_seq is None or spec_digest is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "incomplete_binding",
                "message": "a bound download requires BOTH version_seq and spec_digest",
            },
        )
    try:
        payload = await asyncio.to_thread(
            _build_bound_download_zip,
            project_store,
            record,
            record.conversation_id,
            version_seq,
            spec_digest,
        )
    except _BoundReject as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc
    return StreamingResponse(_aiter_bytes(payload), media_type="application/zip", headers=headers)


async def download_response(
    project_store: ProjectStore,
    record: ProjectRecord,
    workspace: Path,
    version_seq: int | None,
    spec_digest: str | None,
) -> StreamingResponse:
    headers = {"Content-Disposition": f'attachment; filename="{record.conversation_id}.zip"'}
    if version_seq is not None or spec_digest is not None:
        return await _bound_download_response(
            project_store,
            record,
            version_seq,
            spec_digest,
            headers,
        )
    overlay, _assessed_seq = await _best_effort_overlay(project_store, record, workspace)
    body = (
        _aiter_zip_with_overlay(workspace, overlay) if overlay else aiter_zip_workspace(workspace)
    )
    return StreamingResponse(body, media_type="application/zip", headers=headers)

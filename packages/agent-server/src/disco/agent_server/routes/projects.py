"""Build-project route translation and lightweight project operations."""

from __future__ import annotations

from typing import Any

from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import ProjectRecord, ProjectStore, StorageStatus
from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..auth import current_owner_id, current_session
from ..runtime import ConversationRuntime
from ._common import require_owned_conversation
from .projects_download import (
    _zip_workspace_with_overlay as _zip_workspace_with_overlay,
)
from .projects_download import (
    download_response as _download_response,
)
from .projects_download import (
    handle_download_project as _handle_download_project,
)
from .projects_download import (
    handle_project_manifest as _handle_project_manifest,
)
from .projects_download import (
    project_owner_for_session as _project_owner_for_session,
)
from .projects_download import (
    resolve_project_for_read as _resolve_project_for_read,
)
from .projects_import import (
    ImportLimits,
    ImportRejected,
    clone_git_url,
    parse_import_source,
)
from .projects_import import (
    import_project as _import_project,
)

_MAX_IMPORT_ZIP_BYTES = 50 * 1024 * 1024
_MAX_IMPORT_TREE_BYTES = 200 * 1024 * 1024
_MAX_IMPORT_FILES = 2000

# Preserve the established private seams used by tests and in-process consumers.
_clone_git_url = clone_git_url
_parse_import_source = parse_import_source
_ImportRejected = ImportRejected


def _project_payload(
    record: ProjectRecord,
    summary: Any,
    owner_id: str,
) -> dict[str, Any]:
    return {
        "id": record.conversation_id,
        "owner_id": owner_id,
        "title": (summary.title if summary else None) or record.title or "(untitled)",
        "surface": (summary.surface if summary else None) or "build",
        "created_at": (summary.created_at if summary else None) or record.created_at,
        "last_snapshot_at": record.last_snapshot_at,
        "file_count": record.file_count,
        "total_bytes": record.total_bytes,
        "files_missing": record.files_missing,
    }


def _visible_project(
    record: ProjectRecord,
    session: AuthSession,
    summaries: dict[str, Any],
) -> dict[str, Any] | None:
    owner_id = _project_owner_for_session(
        record.owner_id,
        session,
        legacy_unclaimed_owner=record.legacy_unclaimed_owner,
    )
    if owner_id is None:
        return None
    return _project_payload(record, summaries.get(record.conversation_id), owner_id)


async def _handle_list_projects(
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict[str, Any]:
    session = current_session(request)
    project_store = runtime.project_store() if runtime is not None else None
    if project_store is None:
        return {"projects": [], "status": StorageStatus.UNSET.value}
    status = project_store.status()
    if status != StorageStatus.OK:
        return {
            "projects": [],
            "status": status.value,
            "root": str(project_store.root or ""),
        }
    rows = await store.list_conversation_summaries(
        owner_id=session.owner_id,
        limit=500,
        cursor=None,
    )
    summaries = {row.conversation_id: row for row in rows}
    projects = [
        project
        for record in project_store.list_projects()
        if (project := _visible_project(record, session, summaries)) is not None
    ]
    return {
        "projects": projects,
        "status": status.value,
        "root": str(project_store.root or ""),
    }


async def _handle_import_project(
    request: Request,
    *,
    owner_id: str,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict[str, Any]:
    project_store = runtime.project_store() if runtime is not None else None
    if project_store is None or project_store.status() != StorageStatus.OK:
        raise HTTPException(status_code=503, detail={"reason": "storage_unavailable"})
    assert runtime is not None
    limits = ImportLimits(
        zip_bytes=_MAX_IMPORT_ZIP_BYTES,
        tree_bytes=_MAX_IMPORT_TREE_BYTES,
        files=_MAX_IMPORT_FILES,
    )
    try:
        return await _import_project(
            request,
            owner_id=owner_id,
            store=store,
            runtime=runtime,
            project_store=project_store,
            limits=limits,
            clone_git=_clone_git_url,
        )
    except _ImportRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc


async def _handle_backfill_titles(
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    retitle_fallbacks: bool,
) -> dict[str, Any]:
    owner_id = current_owner_id(request)
    if runtime is None:
        return {"titled": {}, "scanned": 0, "status": "no-runtime"}
    summaries = await store.list_conversation_summaries(
        owner_id=owner_id,
        limit=500,
        cursor=None,
    )
    candidates = [
        summary.conversation_id for summary in summaries if not summary.title or retitle_fallbacks
    ]
    titled = await runtime._title_service.backfill(
        candidates,
        retitle_fallbacks=retitle_fallbacks,
    )
    return {"titled": titled, "count": len(titled), "scanned": len(candidates)}


def _project_store_for_delete(
    runtime: ConversationRuntime | None,
) -> ProjectStore:
    project_store = runtime.project_store() if runtime is not None else None
    if project_store is None or project_store.status() != StorageStatus.OK:
        raise HTTPException(status_code=404, detail={"reason": "storage_unavailable"})
    return project_store


def _authorize_delete(
    project_store: ProjectStore,
    record: ProjectRecord,
    request: Request,
) -> None:
    owner_id = _project_owner_for_session(
        record.owner_id,
        current_session(request),
        legacy_unclaimed_owner=record.legacy_unclaimed_owner,
    )
    if owner_id is None:
        raise HTTPException(status_code=403, detail={"reason": "project_forbidden"})


async def _handle_delete_project(
    conversation_id: str,
    request: Request,
    *,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> dict[str, Any]:
    del store
    project_store = _project_store_for_delete(runtime)
    record = project_store.get(conversation_id)
    if record is None:
        return {"id": conversation_id, "deleted": False}
    _authorize_delete(project_store, record, request)
    if runtime is None:
        deleted = project_store.delete(conversation_id)
    else:
        async with runtime.workspace_mutation(
            conversation_id,
            "project.delete",
            paths=(".",),
        ):
            deleted = project_store.delete(conversation_id)
    return {"id": conversation_id, "deleted": deleted}


def make_projects_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects")
    async def list_projects(request: Request) -> dict:
        return await _handle_list_projects(request, store=store, runtime=runtime)

    @router.post("/api/projects/backfill-titles")
    async def backfill_titles(
        request: Request,
        retitle_fallbacks: bool = Query(default=False),
    ) -> dict:
        return await _handle_backfill_titles(
            request,
            store=store,
            runtime=runtime,
            retitle_fallbacks=retitle_fallbacks,
        )

    @router.post("/api/projects/import")
    async def import_project(request: Request) -> dict:
        return await _handle_import_project(
            request,
            owner_id=current_owner_id(request),
            store=store,
            runtime=runtime,
        )

    @router.get("/api/projects/{conversation_id}/download")
    async def download_project(
        conversation_id: str,
        request: Request,
        version_seq: int | None = Query(default=None),
        spec_digest: str | None = Query(default=None),
    ) -> Response:
        if version_seq is not None or spec_digest is not None:
            project_store, record, workspace = await _resolve_project_for_read(
                request,
                store,
                runtime,
                conversation_id,
            )
            return await _download_response(
                project_store,
                record,
                workspace,
                version_seq,
                spec_digest,
            )
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        return await _handle_download_project(
            conversation_id,
            request,
            store=store,
            runtime=runtime,
        )

    @router.get("/api/projects/{conversation_id}/manifest")
    async def project_manifest(conversation_id: str, request: Request) -> dict:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        return await _handle_project_manifest(
            conversation_id,
            request,
            store=store,
            runtime=runtime,
        )

    @router.delete("/api/projects/{conversation_id}")
    async def delete_project(conversation_id: str, request: Request) -> dict:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        return await _handle_delete_project(
            conversation_id,
            request,
            store=store,
            runtime=runtime,
        )

    return router

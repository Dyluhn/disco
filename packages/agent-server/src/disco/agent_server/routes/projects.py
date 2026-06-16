"""Build-project routes — persistence list / download / manifest / delete."""

from __future__ import annotations

import contextlib

from disco.core import DEFAULT_OWNER_ID, DeliverableEvent
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus, aiter_zip_workspace
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..runtime import ConversationRuntime


def make_projects_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects")
    async def list_projects(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
    ) -> dict:
        """List Build projects under the configured projects_root, joined with
        their conversation metadata (title/created_at). Returns an empty list
        with a clear `status` field when the storage isn't configured/valid —
        graceful empty, never crash."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None:
            return {"projects": [], "status": StorageStatus.UNSET.value}
        status = ps.status()
        if status != StorageStatus.OK:
            return {"projects": [], "status": status.value, "root": str(ps.root or "")}
        records = ps.list_projects()
        # cross-reference with conversations so the row title/created_at always
        # come from the authoritative store (manifest can drift on rename).
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id, limit=500, cursor=None
        )
        by_id = {s.conversation_id: s for s in summaries}
        projects = []
        for r in records:
            s = by_id.get(r.conversation_id)
            projects.append(
                {
                    "id": r.conversation_id,
                    "owner_id": r.owner_id or (s.owner_id if s else owner_id),
                    "title": (s.title if s else None) or r.title or "(untitled)",
                    # Surface so the Projects list resumes each row on the right
                    # surface ("agent" → /agent/:cid, else /build/:cid). Build-like
                    # surfaces are the only ones that snapshot, so default to "build".
                    "surface": (s.surface if s else None) or "build",
                    "created_at": (s.created_at if s else None) or r.created_at,
                    "last_snapshot_at": r.last_snapshot_at,
                    "file_count": r.file_count,
                    "total_bytes": r.total_bytes,
                    "files_missing": r.files_missing,
                }
            )
        return {"projects": projects, "status": status.value, "root": str(ps.root or "")}

    @router.get("/api/projects/{conversation_id}/download")
    async def download_project(conversation_id: str) -> StreamingResponse:
        """Stream a zip of the project's workspace. 404 with a specific reason
        when the storage is unconfigured / the project is unknown / the files
        have been deleted under the manifest."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None or ps.status() != StorageStatus.OK:
            raise HTTPException(
                status_code=404,
                detail={"reason": "storage_unavailable"},
            )
        record = ps.get(conversation_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
        if record.files_missing:
            raise HTTPException(status_code=404, detail={"reason": "files_missing"})
        workspace = ps.path_for(conversation_id)
        headers = {
            "Content-Disposition": (
                f'attachment; filename="{conversation_id}.zip"'
            ),
        }
        return StreamingResponse(
            aiter_zip_workspace(workspace),
            media_type="application/zip",
            headers=headers,
        )

    @router.get("/api/projects/{conversation_id}/manifest")
    async def project_manifest(conversation_id: str) -> dict:
        """Export a JSON manifest of the project: metadata, the file tree (path +
        bytes), and the agent's last deliverable handoff (title/path/kind +
        deployment_url). The honest, portable description of what the run produced —
        the companion to the workspace zip download."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None or ps.status() != StorageStatus.OK:
            raise HTTPException(status_code=404, detail={"reason": "storage_unavailable"})
        record = ps.get(conversation_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"reason": "project_not_found"})
        # file tree (workspace-relative path + size), skipping the codeact scratch files
        files: list[dict] = []
        workspace = ps.path_for(conversation_id)
        if workspace and workspace.is_dir():
            for p in sorted(workspace.rglob("*")):
                if p.is_file() and not p.name.startswith("_codeact"):
                    files.append(
                        {"path": str(p.relative_to(workspace)), "bytes": p.stat().st_size}
                    )
        # the agent's last deliverable handoff, if any
        deliverable = None
        with contextlib.suppress(Exception):
            for e in reversed(await store.get_events(conversation_id)):
                if isinstance(e, DeliverableEvent):
                    deliverable = {
                        "title": e.title,
                        "path": e.path,
                        "kind": e.artifact_kind,
                        "deployment_url": e.deployment_url,
                    }
                    break
        return {
            "conversation_id": conversation_id,
            "title": record.title or "(untitled)",
            "created_at": record.created_at,
            "last_snapshot_at": record.last_snapshot_at,
            "file_count": record.file_count,
            "total_bytes": record.total_bytes,
            "files": files,
            "deliverable": deliverable,
        }

    @router.delete("/api/projects/{conversation_id}")
    async def delete_project(conversation_id: str) -> dict:
        """Remove a project's manifest + workspace from disk. The conversation
        events in SQLite are left alone (deleting those is a separate concern,
        and matches the History surface's existing delete semantics)."""
        ps = runtime.project_store() if runtime is not None else None
        if ps is None or ps.status() != StorageStatus.OK:
            raise HTTPException(
                status_code=404,
                detail={"reason": "storage_unavailable"},
            )
        deleted = ps.delete(conversation_id)
        return {"id": conversation_id, "deleted": deleted}

    return router

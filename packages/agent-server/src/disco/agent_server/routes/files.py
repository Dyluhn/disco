"""File-upload and declared-artifact routes."""

from __future__ import annotations

from typing import Annotated

from disco.core import ConversationStatus
from disco.core.env import disco_env
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from ..runtime import ConversationRuntime
from ._common import _MAX_FILES_PER_REQUEST, _reject_if_imported, require_owned_conversation
from .files_support import (
    ArtifactRejected,
    ingest_uploads,
    resolve_artifact,
)
from .files_support import (
    _read_artifact_bytes as _read_artifact_bytes,
)

_DEFAULT_PREVIEW_ANCESTORS = (
    "http://localhost:5173 http://127.0.0.1:5173 http://localhost:8000 http://127.0.0.1:8000"
)
_INLINE_CSP = (
    "sandbox allow-scripts; "
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "frame-ancestors 'self' "
    + disco_env("PREVIEW_FRAME_ANCESTORS", _DEFAULT_PREVIEW_ANCESTORS).strip()
).strip()


def _register_upload_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    @router.post("/conversations/{conversation_id}/files")
    async def upload_files(
        conversation_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File()],
    ) -> JSONResponse:
        """Upload accepted files under uploads/."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        _reject_if_imported(store, conversation_id)
        state = await store.get_state(conversation_id)
        if state.execution_status == ConversationStatus.ERROR:
            raise HTTPException(status_code=409, detail={"reason": "conversation_in_error_state"})
        if len(files) > _MAX_FILES_PER_REQUEST:
            raise HTTPException(
                status_code=413,
                detail={"reason": f"too_many_files_per_request (max {_MAX_FILES_PER_REQUEST})"},
            )
        if runtime is None:
            raise HTTPException(status_code=409, detail={"reason": "no_active_sandbox"})
        batch = await ingest_uploads(
            files,
            conversation_id=conversation_id,
            store=store,
            runtime=runtime,
        )
        return JSONResponse(batch.payload(), status_code=batch.status_code)


def _register_artifact_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    @router.get("/conversations/{conversation_id}/artifacts/{path:path}")
    async def artifact_file(
        conversation_id: str,
        path: str,
        request: Request,
        inline: bool = Query(default=False),
    ) -> Response:
        """Download a declared artifact, using sealed bytes after FINISHED."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            raise HTTPException(status_code=404)
        try:
            artifact = await resolve_artifact(
                path,
                inline=inline,
                conversation_id=conversation_id,
                store=store,
                runtime=runtime,
            )
        except ArtifactRejected as exc:
            if exc.detail is None:
                raise HTTPException(status_code=exc.status_code) from exc
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return _artifact_response(
            artifact.data,
            artifact.media_type,
            artifact.basename,
            inline,
        )


def make_files_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()
    _register_upload_routes(router, store, runtime)
    _register_artifact_routes(router, store, runtime)
    return router


def _artifact_response(data: bytes, media_type: str, basename: str, inline: bool) -> Response:
    disposition = "inline" if inline else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{basename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if inline:
        headers["Content-Security-Policy"] = _INLINE_CSP
    return Response(content=data, media_type=media_type, headers=headers)

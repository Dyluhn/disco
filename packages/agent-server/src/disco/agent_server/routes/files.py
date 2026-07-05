"""File-upload + declared-artifact download routes."""

from __future__ import annotations

import contextlib
import posixpath
from pathlib import Path
from typing import Annotated, Any

from disco.core import (
    ConversationStatus,
    DatasourceEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
)
from disco.core.env import disco_env
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus
from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from ..runtime import ConversationRuntime
from ..uploads_ingest import parse_upload_to_doc
from ._common import (
    _ARTIFACT_TYPES,
    _MAX_CONV_BYTES,
    _MAX_FILE_BYTES,
    _MAX_FILES_PER_REQUEST,
    _declared_artifacts,
    _reject_if_imported,
    _sanitize_name,
)

# C5 — Content-Security-Policy applied when ?inline=true. sandbox allow-scripts
# sandboxes the document but permits slide-navigation JS; default-src 'none' blocks
# all loads; style-src/img-src/font-src permit inline CSS + same-origin/data assets.
# frame-ancestors limits who may EMBED the artifact (anti-clickjacking). 'self' covers
# the production same-origin case (frontend + agent-server behind one origin). In DEV
# the Vite UI (:5173) is a DIFFERENT origin from the agent-server (:8000), so the
# PreviewPane iframe was blocked ("permission") — R8. We allow the configured frontend
# origin(s) explicitly (NOT a blanket `*`, which would re-open cross-site clickjacking).
# DISCO_PREVIEW_FRAME_ANCESTORS = space-separated extra origins; the default covers the
# standard local dev + loopback hosts (override to add a tunnel/tailnet origin).
_DEFAULT_PREVIEW_ANCESTORS = (
    "http://localhost:5173 http://127.0.0.1:5173 "
    "http://localhost:8000 http://127.0.0.1:8000"
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


async def _read_artifact_bytes(
    runtime: Any, conversation_id: str, norm: str
) -> bytes | None:
    """Read a declared artifact's bytes: the live sandbox first, then the host
    ProjectStore snapshot (so a FINISHED run with a reaped sandbox still serves).
    Returns None if neither source has it. Caller enforces size + 404."""
    # 1) live sandbox (a running/suspended-but-live conversation)
    session = runtime.live_session(conversation_id)
    if session is not None:
        with contextlib.suppress(Exception):
            return await session.read_file(norm)
    # 2) host ProjectStore snapshot (finished run, sandbox reaped)
    ps = runtime.project_store()
    if ps is not None and ps.status() == StorageStatus.OK:
        with contextlib.suppress(Exception):
            workspace = ps.path_for(conversation_id).resolve()
            resolved = (workspace / norm).resolve()
            if resolved.is_relative_to(workspace) and resolved.is_file():
                return resolved.read_bytes()
    return None


def make_files_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.post("/conversations/{conversation_id}/files")
    async def upload_files(
        conversation_id: str,
        files: Annotated[list[UploadFile], File()],
    ) -> JSONResponse:
        """Upload files into the conversation's sandbox under uploads/.

        Returns 200 {"saved": [...], "rejected": [...]} unless ALL files are
        rejected (413).  Allowed in every state except terminal ERROR.
        """
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
        session = runtime.upload_session(conversation_id)

        # Existing uploads/ contents for quota and collision detection.
        # DC-07: account for server-side sidecar uploads as well.
        server_names = runtime.get_upload_names(conversation_id)
        try:
            sandbox_names: set[str] = set(await session.list_dir("uploads"))
        except Exception:  # noqa: BLE001 — uploads/ may not exist yet
            sandbox_names = set()

        existing_names = server_names | sandbox_names

        # Truth for quota is the server-side sidecar, but we check the sandbox for
        # anything that might have been added manually (best effort).
        existing_bytes = runtime.get_upload_size(conversation_id)
        for fname in sandbox_names:
            if fname not in server_names:
                try:
                    existing_bytes += len(await session.read_file(f"uploads/{fname}"))
                except Exception:  # noqa: BLE001
                    pass

        saved: list[dict] = []
        rejected: list[dict] = []
        running_total = existing_bytes

        for upload in files:
            raw_name = upload.filename or ""
            clean = _sanitize_name(raw_name)
            if clean is None:
                rejected.append({"name": raw_name, "reason": "empty filename after sanitization"})
                continue

            data = await upload.read()
            if len(data) > _MAX_FILE_BYTES:
                rejected.append({
                    "name": raw_name,
                    "reason": f"file exceeds 25 MB limit ({len(data):,} bytes)",
                })
                continue

            if running_total + len(data) > _MAX_CONV_BYTES:
                rejected.append({
                    "name": raw_name,
                    "reason": "conversation upload quota (100 MB) would be exceeded",
                })
                continue

            # Collision: suffix -2, -3, …
            stem = Path(clean).stem
            suffix = Path(clean).suffix
            candidate = clean
            counter = 2
            while candidate in existing_names:
                candidate = f"{stem}-{counter}{suffix}"
                counter += 1
            final_name = candidate

            # DC-07: write to sandbox AND server-side storage.
            await session.write_file(f"uploads/{final_name}", data)
            runtime.store_upload(conversation_id, final_name, data)

            existing_names.add(final_name)
            running_total += len(data)

            # G1/DR-4 F2: for supported document types, parse the bytes
            # into Passages and add them to the per-conversation upload corpus so
            # they become citable in DR runs and basic research (F3).
            # Unsupported types (.zip, binaries, etc.) remain in the sandbox for the
            # agent to read but are NOT indexed as retrievable passages.
            upload_doc = parse_upload_to_doc(final_name, data, conversation_id)
            if upload_doc is not None and upload_doc.passages:
                runtime.add_upload_passages(conversation_id, list(upload_doc.passages))

            saved.append({"name": final_name, "bytes": len(data)})

        if saved:
            parts = ", ".join(f"uploads/{s['name']} ({s['bytes']:,} bytes)" for s in saved)
            announcement = f"User uploaded: {parts}"
            await store.append(
                conversation_id,
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=announcement),
                ),
            )
            # D6: emit exactly ONE DatasourceEvent at the attach site so the
            # View pins the contract (condensation-immune, see events.py:509).
            # An uploaded file IS a durable data source — the verbatim contract
            # (path + size) must survive arbitrarily long builds instead of
            # dissolving into a lossy summary.
            if len(saved) == 1:
                ds_name = f"uploads/{saved[0]['name']}"
                ds_docs = (
                    f"path=uploads/{saved[0]['name']} "
                    f"size={saved[0]['bytes']:,} bytes"
                )
            else:
                ds_name = f"uploads/{len(saved)}_files"
                ds_docs = "\n".join(
                    f"- uploads/{s['name']}  ({s['bytes']:,} bytes)"
                    for s in saved
                )
            await store.append(
                conversation_id,
                DatasourceEvent(name=ds_name, docs=ds_docs),
            )

        if not saved and rejected:
            return JSONResponse({"saved": saved, "rejected": rejected}, status_code=413)
        return JSONResponse({"saved": saved, "rejected": rejected}, status_code=200)

    @router.get("/conversations/{conversation_id}/artifacts/{path:path}")
    async def artifact_file(
        conversation_id: str,
        path: str,
        inline: bool = Query(default=False),
    ) -> Response:
        """Download a generated artifact by its workspace-relative path.
        Jails: (1) the path must have been DECLARED as an artifact in the event log;
        (2) extension allowlist (_ARTIFACT_TYPES); (3) traversal-normalized + host-path
        resolve-jail.  Reads the live sandbox first, falling back to the host
        ProjectStore snapshot so a FINISHED run (no live session) still serves.
        404 uniformly on any rejection (no probe).

        C5: ?inline=true returns HTML with Content-Disposition: inline + a strict
        Content-Security-Policy (sandbox; no allow-same-origin) so an HTML artifact
        can be embedded in an iframe without granting same-origin access to this
        instance's APIs.  Only .html is allowed in inline mode — all other extensions
        still 404 on ?inline=true so the inline allowlist stays minimal.
        The default (no param) is unchanged: always attachment."""
        norm = posixpath.normpath(path)
        if posixpath.isabs(norm) or norm.startswith(".."):
            raise HTTPException(status_code=404)
        _, ext = posixpath.splitext(norm)
        media_type = _ARTIFACT_TYPES.get(ext.lower())
        if media_type is None:
            raise HTTPException(status_code=404)
        # C5: inline mode is restricted to .html only — no other type may be inlined.
        if inline and ext.lower() != ".html":
            raise HTTPException(status_code=404)
        if runtime is None:
            raise HTTPException(status_code=404)
        if norm not in await _declared_artifacts(store, conversation_id):
            raise HTTPException(status_code=404)

        data = await _read_artifact_bytes(runtime, conversation_id, norm)
        if data is None:
            raise HTTPException(status_code=404)
        if len(data) > 50 * 1024 * 1024:  # 50 MB cap
            raise HTTPException(status_code=404)

        return _artifact_response(data, media_type, posixpath.basename(norm), inline)

    return router


def _artifact_response(data: bytes, media_type: str, basename: str, inline: bool) -> Response:
    """Build the artifact download Response. inline=True (C5, .html only) serves
    with Content-Disposition: inline + a strict CSP so a sandboxed iframe can
    render it without same-origin; default is an attachment download."""
    disposition = "inline" if inline else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{basename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if inline:
        # No allow-same-origin on the caller's iframe sandbox attr → the framed
        # page cannot reach this instance's APIs even though it runs scripts.
        headers["Content-Security-Policy"] = _INLINE_CSP
    return Response(content=data, media_type=media_type, headers=headers)

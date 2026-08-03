"""DISCO_INSPECT debug routes — read the per-conversation request trace.

These exist to PROVE the request chain end-to-end (UI → loop → router → provider
→ back) without grepping prod logs. They are inert unless ``DISCO_INSPECT=1``:
with the flag off the trace registry is never populated and these return 404 with
a hint, so the surface can't leak routing internals in a default deployment.
"""

from __future__ import annotations

from typing import Any

from disco.core.evidence.schema import redact
from disco.core.inspect import inspect_enabled, registry
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..runtime import ConversationRuntime
from ._common import require_owned_conversation


def make_debug_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    def _disabled() -> JSONResponse:
        return JSONResponse(
            {
                "error": "inspect disabled",
                "hint": "start the agent-server with DISCO_INSPECT=1 to capture traces",
            },
            status_code=404,
        )

    @router.get("/api/debug/inspect")
    async def inspect_status() -> JSONResponse:
        """Is inspect on, and which conversations currently have a trace?"""
        if not inspect_enabled():
            return _disabled()
        return JSONResponse({"enabled": True, "conversations": registry().conversations()})

    @router.get("/api/debug/trace/{conversation_id}")
    async def trace(conversation_id: str, request: Request) -> JSONResponse:
        """The full interleaved routing+span trace for one conversation.

        404 when inspect is off, or when no trace exists yet for the id (the
        conversation hasn't made a model call, or it aged out of the ring)."""
        if not inspect_enabled():
            return _disabled()
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        snap = registry().snapshot(conversation_id)
        if snap is None:
            return JSONResponse(
                {"error": "no trace", "conversation_id": conversation_id},
                status_code=404,
            )
        return JSONResponse(redact(snap))

    @router.get("/api/debug/evidence/{conversation_id}")
    async def evidence(conversation_id: str, request: Request) -> JSONResponse:
        """Bundled, redacted evidence for one conversation: state, event log,
        inspect trace, and project manifest (metadata only — no file contents).

        Gated behind the same ``DISCO_INSPECT=1`` flag as ``/api/debug/trace``.
        Returns 404 with a clear hint when the flag is off so the endpoint is
        completely inert in a default deployment.

        The entire bundle is passed through :func:`disco.core.evidence.schema.redact`
        before serialisation — any dict key matching a secret pattern (api_key,
        token, secret, …) has its value replaced with ``'***REDACTED***'``.

        Path-traversal safety: FastAPI's ``{conversation_id}`` path segment does
        not permit ``/`` or ``..``. No workspace files or arbitrary paths are read;
        only structured store data is returned."""
        if not inspect_enabled():
            return _disabled()
        conversation_id = await require_owned_conversation(request, store, conversation_id)

        # Conversation state (reconstructed from the append-only event log).
        state_dict: dict[str, Any] = (await store.get_state(conversation_id)).model_dump(
            mode="json"
        )

        # Full event log, each event serialised to its JSON-ready dict form.
        events_list: list[dict[str, Any]] = [
            e.model_dump(mode="json") for e in await store.get_events(conversation_id)
        ]

        # Inspect trace — may be None when inspect is on but no model call has
        # been captured yet for this conversation (or it aged out of the ring).
        inspect_trace: dict[str, Any] | None = registry().snapshot(conversation_id)

        # Project manifest — metadata only (title, dates, counts, last deliverable).
        # Workspace file paths and content are intentionally excluded to eliminate
        # any path-traversal or workspace-content leakage risk.
        project_manifest: dict[str, Any] | None = None
        runtime_state: dict[str, Any] | None = None
        if runtime is not None:
            runtime_state = {
                "sandbox_backend": runtime.sandbox_backend_name(),
                "sandbox_state": runtime.sandbox_state(conversation_id),
                "sandbox_instance_ids": runtime.sandbox_instance_ids(conversation_id),
                "live_session": runtime.live_sessions.live_session(conversation_id) is not None,
                "mcp_retrieval_searches": [
                    str(getattr(provider, "name", type(provider).__name__))
                    for provider in runtime.mcp._retrieval_searches
                ],
                "mcp_retrieval_extractions": [
                    str(getattr(provider, "name", type(provider).__name__))
                    for provider in runtime.mcp._retrieval_extractions
                ],
            }
            ps = runtime.project_store()
            record = ps.get(conversation_id)
            if record is not None:
                project_manifest = {
                    "conversation_id": conversation_id,
                    "title": record.title or "(untitled)",
                    "created_at": record.created_at,
                    "last_snapshot_at": record.last_snapshot_at,
                    "file_count": record.file_count,
                    "total_bytes": record.total_bytes,
                }

        bundle: dict[str, Any] = {
            "conversation_id": conversation_id,
            "state": state_dict,
            "events": events_list,
            "inspect_trace": inspect_trace,
            "project_manifest": project_manifest,
            "runtime": runtime_state,
        }

        # Redact sensitive keys before any bytes leave the process.
        return JSONResponse(redact(bundle))

    return router

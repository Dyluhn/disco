"""DISCO_INSPECT debug routes — read the per-conversation request trace.

These exist to PROVE the request chain end-to-end (UI → loop → router → provider
→ back) without grepping prod logs. They are inert unless ``DISCO_INSPECT=1``:
with the flag off the trace registry is never populated and these return 404 with
a hint, so the surface can't leak routing internals in a default deployment.
"""

from __future__ import annotations

from disco.core.inspect import inspect_enabled, registry
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..runtime import ConversationRuntime


def make_debug_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
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
        return JSONResponse(
            {"enabled": True, "conversations": registry().conversations()}
        )

    @router.get("/api/debug/trace/{conversation_id}")
    async def trace(conversation_id: str) -> JSONResponse:
        """The full interleaved routing+span trace for one conversation.

        404 when inspect is off, or when no trace exists yet for the id (the
        conversation hasn't made a model call, or it aged out of the ring)."""
        if not inspect_enabled():
            return _disabled()
        snap = registry().snapshot(conversation_id)
        if snap is None:
            return JSONResponse(
                {"error": "no trace", "conversation_id": conversation_id},
                status_code=404,
            )
        return JSONResponse(snap)

    return router

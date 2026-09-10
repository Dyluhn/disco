"""Writer replay route — write a Deep Research report from a saved pool.

A finished run saves the writer's entire input as one JSON file
(``retrieval.deep_research.pool``). This route hands that file back to the
writer with the server's live router and NLI, so a writer change is judged
against a run that already happened rather than by paying for the research loop
again. The events land on the conversation exactly as a normal run's do, ending
with the ordinary ReportEvent + FINISHED, so every reader — the UI, the
harness, an export — sees a report indistinguishable from a researched one
except that no research happened.

The request may name a pool the server already holds (``pool_id``) or carry the
document inline (``pool``), which is what makes the file portable: a pool
captured on one machine replays on another.
"""

from __future__ import annotations

from typing import Any

from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.deep_research import ResearchPoolError, load_pool, read_pool
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..runtime import ConversationRuntime
from ._common import require_owned_conversation


class WriteFromPoolBody(BaseModel):
    """Which pool to write, and where the events go.

    Exactly one of ``pool_id`` / ``pool`` is required. ``conversation_id`` is
    the conversation the replay writes into — a fresh one, normally, since
    replaying into the run's own conversation would append a second report to
    it.
    """

    conversation_id: str
    pool_id: str | None = None
    pool: dict[str, Any] | None = None


def make_research_pool_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.post("/research/write-from-pool")
    async def write_from_pool(request: Request, body: WriteFromPoolBody) -> dict:
        """Write the report again from a saved evidence pool.

        Returns 200 with ``{"ok": true, ...}`` once the report has been
        appended to the conversation. Returns 400 when the body names neither
        or both sources, or when the document is not a readable pool; 404 when
        the named pool is not on this server; 503 without a runtime.

        The call takes as long as the writer does — minutes — and the
        conversation's WebSocket streams the same progress a run streams, so a
        client that wants to watch should connect before posting.
        """
        if runtime is None:
            raise HTTPException(status_code=503, detail={"ok": False, "reason": "no_runtime"})
        if (body.pool_id is None) == (body.pool is None):
            raise HTTPException(
                status_code=400,
                detail={"reason": "pool_required", "detail": "name pool_id or send pool"},
            )
        conversation_id = await require_owned_conversation(request, store, body.conversation_id)
        document = body.pool
        if document is None:
            try:
                document = read_pool(body.pool_id or "")
            except ResearchPoolError as exc:
                raise HTTPException(
                    status_code=404,
                    detail={"reason": "pool_not_found", "detail": str(exc)},
                ) from exc
        try:
            pool = load_pool(document)
        except ResearchPoolError as exc:
            raise HTTPException(
                status_code=400,
                detail={"reason": "pool_unreadable", "detail": str(exc)},
            ) from exc
        await runtime.deep_research.write_from_pool(conversation_id, pool)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "pool_id": pool.pool_id,
            "passages": len(pool.outcome.passages),
        }

    return router

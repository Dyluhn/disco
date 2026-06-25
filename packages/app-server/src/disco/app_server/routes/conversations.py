"""Library routes — owner-scoped conversation list + delete (§6.1).

These read/write the shared core `EventStore` directly (not `ConfigState`); the
factory closes over `store`. `ConversationSummaryDTO` is the row shape the
History surface lists (mirrors the frontend `ConversationSummary`).
"""

from __future__ import annotations

import logging

import httpx
from disco.core import DEFAULT_OWNER_ID
from disco.core.env import disco_env
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Best-effort agent-server notify on delete (finding #5): the app-server owns the DB
# rows but the per-conversation RUNTIME caches (the kernel pin, cached loop, live task,
# sandbox) live in the SEPARATE agent-server process. When `DISCO_AGENT_BASE` points at
# the agent-server, a delete fires a best-effort DELETE there so it releases that state;
# unset → no-op (the agent-server's own delete route still cleans up when hit directly).
_NOTIFY_TIMEOUT = httpx.Timeout(4.0, connect=2.0)


async def _notify_agent_delete(conversation_id: str, owner_id: str) -> None:
    base = disco_env("AGENT_BASE", "").rstrip("/")
    if not base:
        return  # no agent-server URL configured — nothing to notify (graceful)
    url = f"{base}/conversations/{conversation_id}"
    try:
        async with httpx.AsyncClient(timeout=_NOTIFY_TIMEOUT) as client:
            await client.delete(url, params={"owner_id": owner_id})
    except Exception:  # noqa: BLE001 — cleanup notify must never fail the delete
        logger.warning("agent-server delete notify failed for %s", conversation_id, exc_info=True)


class ConversationSummaryDTO(BaseModel):
    """Library row the History surface lists (mirrors frontend ConversationSummary)."""

    id: str
    owner_id: str
    title: str | None = None
    created_at: str
    status: str | None = None
    surface: str = "research"  # "research" | "build" | "agent" | "deep_research" — routing
    origin: str | None = None  # "imported" → read-only, routes to /imported/:cid


def make_conversations_router(store: SqliteEventStore) -> APIRouter:
    router = APIRouter()

    @router.get("/api/conversations")
    async def list_conversations(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
    ) -> list[ConversationSummaryDTO]:
        # BW-08: the History surface never shows 0-event ghost conversations.
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id, limit=limit, cursor=cursor, nonempty_only=True
        )
        return [
            ConversationSummaryDTO(
                id=s.conversation_id,
                owner_id=s.owner_id,
                title=s.title,
                created_at=s.created_at,
                status=s.status,
                surface=s.surface,
                origin=s.origin,
            )
            for s in summaries
        ]

    @router.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
    ) -> dict:
        # Owner-scoped: a caller can only delete its own (no cross-owner deletes).
        deleted = await store.delete_conversation(conversation_id, owner_id=owner_id)
        # Release the agent-server's in-memory runtime state for this cid (finding #5),
        # best-effort — only when an agent-server URL is configured; never blocks/fails
        # the delete (the DB rows are already gone).
        if deleted:
            await _notify_agent_delete(conversation_id, owner_id)
        return {"id": conversation_id, "deleted": deleted}

    return router

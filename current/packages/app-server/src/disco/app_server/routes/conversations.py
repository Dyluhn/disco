"""Library routes — owner-scoped conversation list + delete (§6.1).

These read/write the shared core `EventStore` directly (not `ConfigState`); the
factory closes over `store`. `ConversationSummaryDTO` is the row shape the
History surface lists (mirrors the frontend `ConversationSummary`).
"""

from __future__ import annotations

import re

import httpx
from disco.core import ConversationStatus
from disco.core.auth import CSRF_HEADER, SESSION_COOKIE, cookie_header_values
from disco.core.env import disco_env
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.deep_research.recovery import remove_research_artifacts
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..auth import current_owner_id

_CONVERSATION_ID_RE = re.compile(r"^conv_[A-Za-z0-9_-]+$")

# The runtime owner drains work while the owner-scoped row still exists.
_DELETE_TIMEOUT = httpx.Timeout(30.0, connect=2.0)


async def _delete_via_agent(conversation_id: str, request: Request) -> bool:
    base = disco_env("AGENT_BASE", "").rstrip("/")
    if not base:
        return False
    url = f"{base}/conversations/{conversation_id}"
    # Forward the authenticated browser's session and CSRF proof to the trusted
    # configured sibling. An owner_id query argument is not authentication.
    tokens = cookie_header_values(request.headers.get("cookie"), SESSION_COOKIE)
    headers = {
        "Cookie": "; ".join(f"{SESSION_COOKIE}={token}" for token in tokens),
        CSRF_HEADER: request.headers.get(CSRF_HEADER, ""),
    }
    if origin := request.headers.get("origin"):
        headers["Origin"] = origin
    try:
        async with httpx.AsyncClient(timeout=_DELETE_TIMEOUT) as client:
            response = await client.delete(url, headers=headers)
            response.raise_for_status()
            result = response.json()
            if (
                not isinstance(result, dict)
                or result.get("id") != conversation_id
                or result.get("deleted") is not True
            ):
                raise ValueError("runtime did not confirm deletion")
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "runtime_delete_unconfirmed",
                "message": "Deletion could not be confirmed by the Agent server. Refresh History "
                "and retry if the conversation remains. Other conversations remain usable.",
            },
        ) from exc
    return True


class ConversationSummaryDTO(BaseModel):
    """Library row the History surface lists (mirrors frontend ConversationSummary)."""

    id: str
    owner_id: str
    space_id: str | None = None
    title: str | None = None
    created_at: str
    status: str | None = None
    surface: str = "research"  # "research" | "build" | "agent" | "deep_research" — routing
    origin: str | None = None  # "imported" → read-only, routes to /imported/:cid


class SetConversationSpaceBody(BaseModel):
    space_id: str | None = None


async def _require_owned_conversation(
    store: SqliteEventStore, conversation_id: str, owner_id: str
) -> str:
    if not _CONVERSATION_ID_RE.fullmatch(conversation_id):
        raise HTTPException(status_code=404, detail={"reason": "conversation_not_found"})
    owner = await store.conversation_owner_id(conversation_id)
    if owner is None:
        raise HTTPException(status_code=404, detail={"reason": "conversation_not_found"})
    if owner != owner_id:
        raise HTTPException(status_code=403, detail={"reason": "conversation_forbidden"})
    return conversation_id


def make_conversations_router(store: SqliteEventStore) -> APIRouter:
    router = APIRouter()

    @router.get("/api/conversations")
    async def list_conversations(
        request: Request,
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
        space_id: str | None = Query(default=None),
    ) -> list[ConversationSummaryDTO]:
        owner_id = current_owner_id(request)
        # BW-08: the History surface never shows 0-event ghost conversations.
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id,
            limit=limit,
            cursor=cursor,
            nonempty_only=True,
            space_id=space_id,
        )
        return [
            ConversationSummaryDTO(
                id=s.conversation_id,
                owner_id=s.owner_id,
                space_id=s.space_id,
                title=s.title,
                created_at=s.created_at,
                status=s.status,
                surface=s.surface,
                origin=s.origin,
            )
            for s in summaries
        ]

    @router.post("/api/conversations/{conversation_id}/space")
    async def set_conversation_space(
        conversation_id: str,
        body: SetConversationSpaceBody,
        request: Request,
    ) -> dict:
        owner_id = current_owner_id(request)
        conversation_id = await _require_owned_conversation(store, conversation_id, owner_id)
        clean_space_id = body.space_id.strip() if body.space_id else None
        await store.set_conversation_space(conversation_id, clean_space_id)
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "space_id": clean_space_id,
        }

    @router.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        request: Request,
    ) -> dict:
        # Owner-scoped: a caller can only delete its own (no cross-owner deletes).
        owner_id = current_owner_id(request)
        conversation_id = await _require_owned_conversation(store, conversation_id, owner_id)
        if await _delete_via_agent(conversation_id, request):
            if await store.conversation_owner_id(conversation_id) is not None:
                raise HTTPException(status_code=503, detail={"reason": "runtime_store_mismatch"})
            return {"id": conversation_id, "deleted": True}
        if (await store.get_state(conversation_id)).execution_status is ConversationStatus.RUNNING:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "runtime_owner_required",
                    "message": "This conversation is running. Delete it through the Agent server "
                    "so active work stops before its data is removed. "
                    "Other conversations remain usable.",
                },
            )
        deleted = await store.delete_conversation(conversation_id, owner_id=owner_id)
        if deleted:
            remove_research_artifacts(conversation_id)
        return {"id": conversation_id, "deleted": deleted}

    return router

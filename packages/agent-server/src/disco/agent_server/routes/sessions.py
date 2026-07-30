"""Live tmux session routes for a conversation's sandbox (BP-14)."""

from __future__ import annotations

from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, HTTPException, Query, Request

from ..runtime import ConversationRuntime
from ._common import _MAX_SESSION_TAIL_CHARS, require_owned_conversation


def make_sessions_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/conversations/{conversation_id}/sessions")
    async def list_sessions(conversation_id: str, request: Request) -> dict:
        """Live tmux session list for the conversation's sandbox (BP-14).
        No sandbox / finished conversation → empty list (200, not 404)."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if runtime is None:
            return {"sessions": []}
        sessions, stale = await runtime.sessions_snapshot(conversation_id)
        return {
            "sessions": [
                {
                    "name": s.name,
                    "busy": s.busy,
                    "last_line": next(
                        (line for line in reversed(s.last_lines.splitlines()) if line.strip()),
                        "",
                    ),
                }
                for s in sessions
            ],
            "stale": stale,
        }

    @router.get("/conversations/{conversation_id}/sessions/{name}/view")
    async def get_session_view(
        conversation_id: str,
        name: str,
        request: Request,
        tail_chars: int = Query(default=10_000, ge=1, le=_MAX_SESSION_TAIL_CHARS),
    ) -> dict:
        """Live capture-pane tail for one named session (BP-14).
        Internal __-prefixed sessions → 404. Unknown name → 404."""
        conversation_id = await require_owned_conversation(request, store, conversation_id)
        if name.startswith("__"):
            raise HTTPException(status_code=404, detail="session not found")
        if runtime is None:
            raise HTTPException(status_code=404, detail="no runtime")
        sessions = (await runtime.sessions_snapshot(conversation_id))[0]
        if not any(s.name == name for s in sessions):
            raise HTTPException(status_code=404, detail="session not found")
        view = await runtime.session_view(conversation_id, name, tail_chars)
        if view is None:
            raise HTTPException(status_code=404, detail="no sandbox")
        return {"name": name, "busy": view.running, "content": view.output}

    return router

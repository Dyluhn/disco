"""The app-server HTTP surface — the user-facing gateway (BoD §5.2).

Owns the *settings* surface (the deterministic, manual model-assignment matrix +
the skills/MCP scaffolds) and the *library* surface (owner-scoped conversation
list + delete) over the shared core `EventStore`. It does NOT run agent loops;
the per-conversation runtime + the live WebSocket stay in the agent-server.

CORS is open (no credentials — ownership is an explicit query param, not a
cookie) so the dev frontend on another origin can call it.
"""

from __future__ import annotations

from disco.core import DEFAULT_OWNER_ID
from disco.core.store.sqlite import SqliteEventStore
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config_state import ConfigState
from .routes import (
    make_config_router,
    make_health_router,
    make_mcp_router,
    make_models_router,
    make_openrouter_router,
    make_skills_router,
)


class ConversationSummaryDTO(BaseModel):
    """Library row the History surface lists (mirrors frontend ConversationSummary)."""

    id: str
    owner_id: str
    title: str | None = None
    created_at: str
    status: str | None = None
    surface: str = "research"  # "research" | "build" | "agent" | "deep_research" — routing
    origin: str | None = None  # "imported" → read-only, routes to /imported/:cid


def create_app(store: SqliteEventStore, config: ConfigState | None = None) -> FastAPI:
    """Build the app-server over a shared store. The store is injected so tests
    drive it headlessly and so it shares conversations with the agent-server."""
    app = FastAPI(title="disco app-server", version="0.1.0")
    # Default-construct ConfigState wired to the SHARED store connection so MCP
    # approvals persist to the mcp_approvals table in the deployed app — not only
    # when a test injects an explicit ConfigState. `__main__.create_app(store)`
    # takes this branch; without the db_conn, POST /api/mcp/servers/{name}/approve
    # 500s ("no DB connection for approval persistence") and GET /api/mcp can never
    # project an approved/connected server. The store's _conn already carries the
    # mcp_approvals table (core SqliteEventStore schema).
    state = config or ConfigState(db_conn=store._conn)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev: open. ownership is an explicit param, not a cookie.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(make_health_router())
    app.include_router(make_models_router(state))
    app.include_router(make_config_router(state))
    app.include_router(make_openrouter_router(state))
    app.include_router(make_skills_router(state))
    app.include_router(make_mcp_router(state))

    # ---- library: owner-scoped conversation list + delete (§6.1) ------------

    @app.get("/api/conversations")
    async def list_conversations(
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50),
    ) -> list[ConversationSummaryDTO]:
        summaries = await store.list_conversation_summaries(
            owner_id=owner_id, limit=limit, cursor=cursor
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

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        owner_id: str = Query(default=DEFAULT_OWNER_ID),
    ) -> dict:
        # Owner-scoped: a caller can only delete its own (no cross-owner deletes).
        deleted = await store.delete_conversation(conversation_id, owner_id=owner_id)
        return {"id": conversation_id, "deleted": deleted}

    return app

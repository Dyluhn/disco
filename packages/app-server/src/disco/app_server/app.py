"""The app-server HTTP surface — the user-facing gateway (BoD §5.2).

Owns the *settings* surface (the deterministic, manual model-assignment matrix +
the skills/MCP scaffolds) and the *library* surface (owner-scoped conversation
list + delete) over the shared core `EventStore`. It does NOT run agent loops;
the per-conversation runtime + the live WebSocket stay in the agent-server.

CORS is open (no credentials — ownership is an explicit query param, not a
cookie) so the dev frontend on another origin can call it.
"""

from __future__ import annotations

import httpx
from disco.core import DEFAULT_OWNER_ID
from disco.core.store.sqlite import SqliteEventStore
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config.dtos import (
    McpConnectionDTO,
    McpServerApproveDTO,
    McpServerConfigDTO,
    OpenRouterKeyBody,
    OpenRouterKeyStatus,
    OpenRouterModelDTO,
    SkillCreate,
    SkillDTO,
    SkillPatch,
)
from .config.mappers import normalize_openrouter
from .config_state import ConfigState
from .routes import make_config_router, make_health_router, make_models_router

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


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

    # ---- OpenRouter: live catalogue proxy + encrypted key -------------------

    @app.get("/api/models/openrouter")
    async def get_openrouter_models() -> list[OpenRouterModelDTO]:
        # Public endpoint (no key needed to list). Proxied so the browser avoids
        # CORS and gets a normalized shape. Adding a model reuses POST /api/models.
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(_OPENROUTER_MODELS_URL)
                resp.raise_for_status()
                data = resp.json().get("data", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"OpenRouter unreachable: {exc}") from exc
        return normalize_openrouter(data)

    @app.get("/api/openrouter/key")
    async def get_openrouter_key() -> OpenRouterKeyStatus:
        return state.openrouter_key_status()

    @app.put("/api/openrouter/key")
    async def put_openrouter_key(body: OpenRouterKeyBody) -> OpenRouterKeyStatus:
        try:
            return state.set_openrouter_key(body.key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/openrouter/key")
    async def delete_openrouter_key() -> OpenRouterKeyStatus:
        return state.clear_openrouter_key()

    # ---- skills — real, persistent .md instruction modules ------------------

    @app.get("/api/skills")
    async def get_skills() -> list[SkillDTO]:
        return state.skills()

    @app.post("/api/skills", status_code=201)
    async def create_skill(create: SkillCreate) -> SkillDTO:
        return state.create_skill(create)

    @app.put("/api/skills/{skill_id}")
    async def put_skill(skill_id: str, patch: SkillPatch) -> SkillDTO:
        updated = state.update_skill(skill_id, patch)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"unknown skill {skill_id!r}")
        return updated

    @app.delete("/api/skills/{skill_id}", status_code=204)
    async def delete_skill(skill_id: str) -> None:
        if not state.delete_skill(skill_id):
            raise HTTPException(status_code=404, detail=f"unknown skill {skill_id!r}")

    # ---- mcp connections (live, persistent CRUD — rung B) -------------------

    @app.get("/api/mcp")
    async def get_mcp() -> list[McpConnectionDTO]:
        return state.mcp_connections()

    @app.post("/api/mcp/servers", status_code=201)
    async def create_mcp_server(body: McpServerConfigDTO) -> McpConnectionDTO:
        try:
            return state.create_mcp_server(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/mcp/servers/{name}")
    async def update_mcp_server(name: str, body: McpServerConfigDTO) -> McpConnectionDTO:
        result = state.update_mcp_server(name, body)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown server {name!r}")
        return result

    @app.delete("/api/mcp/servers/{name}", status_code=204)
    async def delete_mcp_server(name: str) -> None:
        if not state.delete_mcp_server(name):
            raise HTTPException(status_code=404, detail=f"unknown server {name!r}")

    @app.post("/api/mcp/servers/{name}/approve")
    async def approve_mcp_server(name: str, body: McpServerApproveDTO) -> McpConnectionDTO:
        try:
            return state.approve_mcp_server(name, body)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown server {name!r}") from None
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

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

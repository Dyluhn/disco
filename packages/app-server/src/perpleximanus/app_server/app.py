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
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from perpleximanus.core import DEFAULT_OWNER_ID
from perpleximanus.core.store.sqlite import SqliteEventStore
from pydantic import BaseModel

from .config_state import (
    AssignmentsDTO,
    AssignmentsPatch,
    ConfigState,
    McpConnectionDTO,
    ModelDTO,
    ModelUpsert,
    OpenRouterKeyBody,
    OpenRouterKeyStatus,
    OpenRouterModelDTO,
    SandboxConfigDTO,
    SkillDTO,
    SkillPatch,
    normalize_openrouter,
)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class ConversationSummaryDTO(BaseModel):
    """Library row the History surface lists (mirrors frontend ConversationSummary)."""

    id: str
    owner_id: str
    title: str | None = None
    created_at: str


def create_app(store: SqliteEventStore, config: ConfigState | None = None) -> FastAPI:
    """Build the app-server over a shared store. The store is injected so tests
    drive it headlessly and so it shares conversations with the agent-server."""
    app = FastAPI(title="perpleximanus app-server", version="0.1.0")
    state = config or ConfigState()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev: open. ownership is an explicit param, not a cookie.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "service": "app-server"}

    # ---- models + assignments (the absolute, manual model story) ------------

    @app.get("/api/models")
    async def get_models() -> list[ModelDTO]:
        return state.models()

    @app.post("/api/models", status_code=201)
    async def add_model(upsert: ModelUpsert) -> list[ModelDTO]:
        try:
            return state.add_model(upsert)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/models/assignments")
    async def get_assignments() -> AssignmentsDTO:
        return state.assignments()

    @app.put("/api/models/assignments")
    async def put_assignments(patch: AssignmentsPatch) -> AssignmentsDTO:
        # Absolute: the system uses exactly what is set; capabilities are advisory
        # + fail-loud at runtime, not blocked here. The one structural guard is that
        # the model KEY must exist in the catalogue (else routing can't resolve it).
        try:
            return state.update_assignments(patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sandbox/config")
    async def get_sandbox_config() -> SandboxConfigDTO:
        return state.sandbox_config()

    @app.put("/api/sandbox/config")
    async def put_sandbox_config(dto: SandboxConfigDTO) -> SandboxConfigDTO:
        return state.update_sandbox_config(dto)

    # Declared AFTER /assignments so that literal path wins over {model_id}.
    @app.put("/api/models/{model_id}")
    async def put_model(model_id: str, upsert: ModelUpsert) -> list[ModelDTO]:
        try:
            return state.update_model(model_id, upsert)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/models/{model_id}")
    async def delete_model(model_id: str) -> list[ModelDTO]:
        try:
            return state.remove_model(model_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    # ---- skills (wiring-pending scaffold) -----------------------------------

    @app.get("/api/skills")
    async def get_skills() -> list[SkillDTO]:
        return state.skills()

    @app.put("/api/skills/{skill_id}")
    async def put_skill(skill_id: str, patch: SkillPatch) -> list[SkillDTO]:
        return state.set_skill_enabled(skill_id, patch.enabled)

    # ---- mcp connections (wiring-pending scaffold) --------------------------

    @app.get("/api/mcp")
    async def get_mcp() -> list[McpConnectionDTO]:
        return state.mcp_connections()

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

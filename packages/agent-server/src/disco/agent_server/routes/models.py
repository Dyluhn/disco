"""Driver-model catalogue route for the Build chat model picker."""

from __future__ import annotations

from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter

from ..runtime import ConversationRuntime


def make_models_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/models")
    async def list_models() -> dict:
        """The driver-eligible models for the Build chat model picker (+ the default).
        Sourced from the live router config so it reflects Settings assignments."""
        if runtime is None:
            return {"models": [], "default": None}
        return runtime.driver_models()

    @router.get("/models/last-selected")
    async def get_last_selected_model() -> dict:
        """P3 — the globally-persisted last-picked driver model, or null when no
        pick has ever been made. The frontend pill reads this to default a new
        conversation to the user's most-recent choice. Server-side only (no
        localStorage). Returns {"model": "<key>" | null}."""
        if runtime is None:
            return {"model": None}
        return {"model": runtime.get_last_selected_model()}

    return router

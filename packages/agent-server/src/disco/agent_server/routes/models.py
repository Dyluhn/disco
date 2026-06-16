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

    return router

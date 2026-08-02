"""Splash suggestion chips for the entry surfaces."""

from __future__ import annotations

import logging
from typing import Annotated

from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, Query

from ..runtime import ConversationRuntime
from ..suggestion_service import SuggestionSurface, curated_suggestions

_LOG = logging.getLogger(__name__)


def make_suggestions_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/suggestions")
    async def get_suggestions(
        surface: Annotated[SuggestionSurface, Query()],
    ) -> dict:
        if runtime is not None:
            try:
                suggestions = await runtime.suggestions.get_generated(surface)
                return {
                    "surface": surface,
                    "suggestions": suggestions[:8],
                    "source": "generated",
                }
            except Exception:
                _LOG.debug("generated suggestions failed for %s", surface, exc_info=True)
        return {
            "surface": surface,
            "suggestions": curated_suggestions(surface),
            "source": "curated",
        }

    return router

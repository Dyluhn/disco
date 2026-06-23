"""Report-export capabilities probe (RP-07) + the shared template catalogue."""

from __future__ import annotations

import dataclasses

from disco.core.brand import list_templates
from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter

from ..runtime import ConversationRuntime


def make_export_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/export/capabilities")
    async def export_capabilities_route() -> dict:
        """Which export formats THIS environment can actually produce. md is always
        available; pdf if weasyprint is importable in the agent-server. The UI enables
        each button from this — never offering a 500."""
        from ..report_export import pdf_available

        return {"md": True, "pdf": pdf_available()}

    @router.get("/api/templates")
    async def templates_route() -> dict:
        """The export-template gallery, shared by the DR-report PDF export and the
        slide-deck export selectors. One source of truth (core.brand catalogue) so the
        two surfaces never drift. Each entry carries `id`, `label`, `description`, and
        an `accent`/`bg` swatch."""
        return {"templates": [dataclasses.asdict(t) for t in list_templates()]}

    return router

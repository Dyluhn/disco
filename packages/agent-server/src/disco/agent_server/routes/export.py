"""Report-export capabilities probe (RP-07)."""

from __future__ import annotations

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
        available; pdf if weasyprint is importable in the agent-server; docx renders
        via pandoc in a transient sandbox, so it needs a CONTAINER backend (the image
        ships pandoc). The UI enables each button from this — never offering a 500."""
        from ..report_export import pdf_available

        backend = runtime.sandbox_backend_name() if runtime is not None else None
        # docx renders in a throwaway sandbox container; the process backend (host,
        # no container) can't, so docx is gated on a real container backend.
        docx_ok = backend in ("local", "gvisor", "podman")
        return {"md": True, "pdf": pdf_available(), "docx": docx_ok}

    return router

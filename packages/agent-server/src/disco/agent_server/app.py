"""The agent-server wire + REST surface — event-state-contract.md §7.

A thin adapter over the core `EventStore`: no business logic in the request
path (BoD §5.2/§7.6). Side effects, when they exist, are event-stream callbacks
— Phase 0 has none, so the handlers only append events and read history. There
is no agent loop here yet; control frames that drive a loop (confirm/reject/
pause/resume/cancel) are accepted but have no loop to act on in Phase 0.

`create_app` is a thin assembler: it builds the app, wires the lifespan +
middleware, and `include_router`s one `APIRouter` per domain (see `routes/`).
The route handlers themselves live in `routes/<domain>.py`; the shared helpers
+ constants + request models they close over live in `routes/_common.py`.
"""

from __future__ import annotations

import asyncio
import contextlib

from disco.core.store.sqlite import SqliteEventStore
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .host_proxy import HostPreviewProxyMiddleware, make_preview_session_resolver
from .routes import (
    make_activity_router,
    make_conversations_router,
    make_debug_router,
    make_deck_editor_router,
    make_export_router,
    make_files_router,
    make_health_router,
    make_mcp_router,
    make_models_router,
    make_preview_edit_router,
    make_preview_router,
    make_probes_router,
    make_projects_router,
    make_report_router,
    make_schedules_router,
    make_sessions_router,
    make_share_router,
    make_storage_router,
    make_ws_router,
)
from .routes._common import _sanitize_name, make_preview_upstream_resolver
from .runtime import ConversationRuntime

# `_sanitize_name` is re-exported here for tests that import it from this module
# (test_upload.py) — its definition now lives in routes/_common.py.
__all__ = ["_sanitize_name", "create_app"]


def create_app(store: SqliteEventStore, *, runtime: ConversationRuntime | None = None) -> FastAPI:
    """Build the FastAPI app over a given store. The store is injected so tests
    drive it headlessly. `runtime` runs the agent loop with real inference (Stage
    2); pass None in tests that only exercise the wire layer (the loop won't run)."""
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # On startup, start the MCP pool (RP-05) and reconcile orphaned RUNNING
        # conversations — loops that died with a previous server process. Without
        # this they show 'RUNNING' forever in History / the Deep Research read-only
        # view (and may have leaked a sandbox).
        idle_sweep_task: asyncio.Task | None = None
        schedule_task: asyncio.Task | None = None
        if runtime is not None:
            try:
                await runtime._start_mcp_pool()
            except Exception:
                # D1: _start_mcp_pool handles ApprovalRequired internally;
                # unexpected errors are logged but must not block boot.
                import logging
                _LOG = logging.getLogger(__name__)
                _LOG.warning("MCP pool startup failed", exc_info=True)
            with contextlib.suppress(Exception):  # never block boot on reconciliation
                await runtime.reconcile_orphaned_runs()
            with contextlib.suppress(Exception):  # warm the live /props cache off-loop
                await runtime.prewarm_model_probe()
            with contextlib.suppress(Exception):  # V2/V4: probe live vision modality once
                await runtime.prewarm_vision_probe()
            idle_sweep_task = asyncio.create_task(runtime._idle_sweep_loop())
            # RP-08: start the schedule manager loop alongside the idle sweep.
            schedule_task = asyncio.create_task(runtime._schedule_manager_loop())
        yield
        if schedule_task is not None:
            schedule_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await schedule_task
        if idle_sweep_task is not None:
            idle_sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle_sweep_task
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime._close_mcp_pool()

    app = FastAPI(
        title="disco agent-server", version="0.1.0", lifespan=lifespan
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # dev: open (ownership is an explicit param, not a cookie)
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=make_preview_upstream_resolver(runtime),
        # Fix 2 (codex P1): in-sandbox liveness fallback so the canonical iframe
        # renders on sealed/filtered backends that publish no host port.
        session_resolver=make_preview_session_resolver(runtime),
    )

    # Per-domain routers (routes/<domain>.py). Registration order preserves the
    # original relative order; the `{path:path}` catch-alls (workspace/artifacts/
    # preview-app/port) live inside their domain routers after the literal routes.
    app.include_router(make_health_router(store, runtime))
    app.include_router(make_mcp_router(store, runtime))
    app.include_router(make_conversations_router(store, runtime))
    app.include_router(make_models_router(store, runtime))
    app.include_router(make_files_router(store, runtime))
    app.include_router(make_deck_editor_router(store, runtime))
    app.include_router(make_preview_router(store, runtime))
    app.include_router(make_preview_edit_router(store, runtime))
    app.include_router(make_sessions_router(store, runtime))
    app.include_router(make_projects_router(store, runtime))
    app.include_router(make_storage_router(store, runtime))
    app.include_router(make_schedules_router(store, runtime))
    app.include_router(make_activity_router(store, runtime))
    app.include_router(make_ws_router(store, runtime))
    app.include_router(make_export_router(store, runtime))
    app.include_router(make_report_router(store, runtime))
    app.include_router(make_share_router(store, runtime))
    app.include_router(make_debug_router(store, runtime))
    app.include_router(make_probes_router())

    return app

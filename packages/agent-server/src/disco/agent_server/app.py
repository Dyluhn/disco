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
import logging

from disco.core.auth import allowed_frontend_origins
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import StorageStatus
from disco.tools.workflow_seed import seed_builtin_workflows
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .appkit_cloudflare import CloudflareDeployCorsMiddleware, make_cloudflare_router
from .auth import AgentAuthMiddleware, make_auth_router
from .host_proxy import HostPreviewProxyMiddleware, make_preview_session_resolver
from .host_service_bus import make_host_service_bus_router
from .host_token_store import HostTokenStore
from .routes import (
    make_activity_router,
    make_conversation_library_router,
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
    make_sandbox_router,
    make_schedules_router,
    make_sessions_router,
    make_share_router,
    make_spaces_router,
    make_storage_router,
    make_suggestions_router,
    make_workflows_router,
    make_ws_router,
)
from .routes._common import _sanitize_name, make_preview_upstream_resolver
from .runtime import ConversationRuntime

# `_sanitize_name` is re-exported here for tests that import it from this module
# (test_upload.py) — its definition now lives in routes/_common.py.
__all__ = ["_sanitize_name", "create_app"]

_LOG = logging.getLogger(__name__)


def _seed_builtin_workflows_for_runtime(runtime: ConversationRuntime) -> None:
    try:
        project_store = runtime.project_store()
        status = project_store.status()
        root = project_store.root
        if status != StorageStatus.OK or root is None:
            _LOG.warning(
                "Builtin workflow seed skipped: project storage unavailable (%s)",
                getattr(status, "value", str(status)),
            )
            return
        # Workflow routing reads this JSON store at request time, so startup must
        # refresh builtins before the first router/list-workflows request.
        seed_builtin_workflows(root)
    except Exception:
        _LOG.warning("Builtin workflow seed failed", exc_info=True)


def create_app(
    store: SqliteEventStore,
    *,
    runtime: ConversationRuntime | None = None,
    host_token_store: HostTokenStore | None = None,
) -> FastAPI:
    """Build the FastAPI app over a given store. The store is injected so tests
    drive it headlessly. `runtime` runs the agent loop with real inference (Stage
    2); pass None in tests that only exercise the wire layer (the loop won't run)."""

    # WO-A2.2: durable operational token store for the host-service bus. Shares
    # the event-store DB path so tokens survive restarts; falls back to :memory:
    # for ephemeral wire tests.
    owns_token_store = host_token_store is None
    token_store = host_token_store or HostTokenStore(store.db_path or ":memory:")

    @contextlib.asynccontextmanager
    async def _runtime_lifespan(_app: FastAPI):
        # On startup, start the MCP pool (RP-05) and reconcile orphaned RUNNING
        # conversations — loops that died with a previous server process. Without
        # this they show 'RUNNING' forever in History / the Deep Research read-only
        # view (and may have leaked a sandbox).
        idle_sweep_task: asyncio.Task | None = None
        schedule_task: asyncio.Task | None = None
        if runtime is not None:
            _seed_builtin_workflows_for_runtime(runtime)
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

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with _runtime_lifespan(_app):
                yield
        finally:
            if owns_token_store:
                token_store.close()

    app = FastAPI(title="disco agent-server", version="0.1.0", lifespan=lifespan)
    app.state.host_token_store = token_store
    app.add_middleware(AgentAuthMiddleware, store=store)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_frontend_origins()),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=make_preview_upstream_resolver(runtime),
        # Fix 2 (codex P1): in-sandbox liveness fallback so the canonical iframe
        # renders on sealed/filtered backends that publish no host port.
        session_resolver=make_preview_session_resolver(runtime),
        require_capability=True,
    )
    # EPIC O P0-3 — strip the permissive wildcard CORS from the owner-only
    # Cloudflare deploy surface. Added LAST so it is the OUTERMOST middleware and
    # can override the global CORSMiddleware's headers on those paths.
    app.add_middleware(CloudflareDeployCorsMiddleware)

    # Per-domain routers (routes/<domain>.py). Registration order preserves the
    # original relative order; the `{path:path}` catch-alls (workspace/artifacts/
    # preview-app/port) live inside their domain routers after the literal routes.
    app.include_router(make_auth_router())
    # WO-A2.2: host-service bus. Included early so the literal `/_disco/svc/{service}`
    # route is matched before any catch-all `{path:path}` routers.
    app.include_router(make_host_service_bus_router(store, runtime, token_store))
    app.include_router(make_health_router(store, runtime))
    app.include_router(make_mcp_router(store, runtime))
    app.include_router(make_conversations_router(store, runtime, token_store))
    app.include_router(make_conversation_library_router(store, runtime))
    app.include_router(make_models_router(store, runtime))
    app.include_router(make_files_router(store, runtime))
    app.include_router(make_deck_editor_router(store, runtime))
    app.include_router(make_preview_router(store, runtime))
    app.include_router(make_preview_edit_router(store, runtime))
    app.include_router(make_sessions_router(store, runtime))
    app.include_router(make_projects_router(store, runtime))
    app.include_router(make_storage_router(store, runtime))
    app.include_router(make_suggestions_router(store, runtime))
    app.include_router(make_workflows_router(store, runtime))
    app.include_router(make_spaces_router(store, runtime))
    app.include_router(make_schedules_router(store, runtime))
    app.include_router(make_activity_router(store, runtime))
    app.include_router(make_ws_router(store, runtime))
    app.include_router(make_export_router(store, runtime))
    app.include_router(make_report_router(store, runtime))
    app.include_router(make_share_router(store, runtime))
    app.include_router(make_debug_router(store, runtime))
    app.include_router(make_probes_router(store))
    # Sandbox reachability (health banner + Settings "Test connection") — probed
    # HERE because the agent-server owns the sandbox environment, not the app-server.
    app.include_router(make_sandbox_router(runtime))
    # EPIC O — owner-only Cloudflare deploy API (real deploy is HARD-GATED +
    # dry-run by default; the mutation path sits OUTSIDE the LLM tool loop).
    app.include_router(make_cloudflare_router(store, runtime))

    return app

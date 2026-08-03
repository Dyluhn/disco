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
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from disco.core.auth import allowed_frontend_origins
from disco.core.inspect import InspectJournal, inspect_enabled, registry
from disco.core.origin_approvals import OriginApprovalStore
from disco.core.quota import SqliteQuotaStore
from disco.core.store.sqlite import SqliteEventStore
from disco.core.stripe_host_service import StripeAppConfigStore
from disco.core.webhook_host_service import WebhookAppConfigStore
from disco.tools.projects import StorageStatus
from disco.tools.workflow_seed import seed_builtin_workflows
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .appkit_cloudflare import CloudflareDeployCorsMiddleware, make_cloudflare_router
from .appkit_cloudflare.stripe_deploy import StripeDeployDependencies
from .appkit_cloudflare.webhook_deploy import WebhookDeployDependencies
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
    make_release_router,
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

type _AppLifespan = Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]


async def _resolve_canonical_authority(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    conversation_id: str,
    port: int,
    workspace_version: int | None,
) -> str | None:
    # Imported lazily to keep the app assembler free of route-module cycles.
    from .routes.preview import _canonical_preview_authority

    return await _canonical_preview_authority(
        store,
        runtime,
        conversation_id,
        port,
        workspace_version=workspace_version,
    )


def _webhook_approvals(
    runtime: ConversationRuntime | None,
) -> OriginApprovalStore | None:
    """Build the approval port at the composition boundary."""

    if runtime is None:
        return None
    result = runtime._config_store.approvals.approval_store(secret_store=runtime._secret_store)
    return result if isinstance(result, OriginApprovalStore) else None


def _attach_inspect_journal(event_db_path: str) -> None:
    """DISCO_INSPECT only: give the trace registry durability across restarts.

    The registry is an in-memory ring, so a server restart resets it AND the
    global sequence counter — a conversation audited across the restart sees
    its trace vanish and reappear renumbered from 1, indistinguishable from
    evidence loss. Attached at lifespan start, before anything can emit, so the
    restored prefix is never interleaved with fresh events. Nothing is attached
    when inspect is off, so the documented zero-cost contract holds; an
    in-memory store has nowhere durable to put it and is skipped.
    """
    if not inspect_enabled() or event_db_path == ":memory:":
        return
    try:
        journal = InspectJournal(Path(event_db_path).with_name("inspect-trace.jsonl"))
        restored = registry().attach_journal(journal)
    except Exception:  # noqa: BLE001 — debug evidence never blocks boot
        _LOG.warning("inspect journal could not be attached", exc_info=True)
        return
    if restored:
        _LOG.info("inspect journal restored %d trace event(s)", restored)


def _seed_builtin_workflows_for_runtime(runtime: ConversationRuntime) -> None:
    try:
        project_store = runtime.projects.current_project_store()
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


async def _cancel_lifespan_task(
    task: asyncio.Task[None] | None,
    *,
    label: str,
) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        _LOG.warning("%s task failed during shutdown", label, exc_info=True)


def _make_runtime_lifespan(
    event_db_path: str,
    runtime: ConversationRuntime | None,
) -> _AppLifespan:
    @contextlib.asynccontextmanager
    async def runtime_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # On startup, start the MCP pool (RP-05) and reconcile orphaned RUNNING
        # conversations — loops that died with a previous server process. Without
        # this they show 'RUNNING' forever in History / the Deep Research read-only
        # view (and may have leaked a sandbox).
        mcp_start_task: asyncio.Task[None] | None = None
        idle_sweep_task: asyncio.Task[None] | None = None
        schedule_task: asyncio.Task[None] | None = None
        _attach_inspect_journal(event_db_path)
        if runtime is not None:
            # Narrow once outside the nested startup coroutine. Type checkers
            # correctly refuse to retain Optional narrowing for a closure over
            # the outer parameter, even though create_app never reassigns it.
            active_runtime = runtime
            _seed_builtin_workflows_for_runtime(active_runtime)

            async def _start_mcp_without_owning_readiness() -> None:
                try:
                    await active_runtime.mcp._start_mcp_pool()
                except Exception:
                    # Approval and per-server connection failures are handled by
                    # McpManager. An unexpected aggregate error is still logged,
                    # but an optional external service never owns Agent readiness.
                    _LOG.warning("MCP pool startup failed", exc_info=True)

            mcp_start_task = asyncio.create_task(
                _start_mcp_without_owning_readiness(), name="mcp-startup"
            )
            with contextlib.suppress(Exception):  # never block boot on reconciliation
                await active_runtime.reconcile_orphaned_runs()
            with contextlib.suppress(Exception):  # warm the live /props cache off-loop
                await active_runtime.drivers.prewarm_model_probe()
            with contextlib.suppress(Exception):  # V2/V4: probe live vision modality once
                await active_runtime.drivers.prewarm_vision_probe()
            idle_sweep_task = asyncio.create_task(active_runtime._idle_sweeper.run())
            # RP-08: start the schedule manager loop alongside the idle sweep.
            schedule_task = asyncio.create_task(active_runtime.schedules._schedule_manager_loop())
        try:
            yield
        finally:
            await _cancel_lifespan_task(schedule_task, label="schedule")
            await _cancel_lifespan_task(idle_sweep_task, label="idle-sweep")
            await _cancel_lifespan_task(mcp_start_task, label="mcp-startup")
            if runtime is not None:
                try:
                    await runtime.aclose()
                finally:
                    with contextlib.suppress(Exception):
                        await runtime.mcp._close_mcp_pool()

    return runtime_lifespan


def _make_lifespan(
    runtime_lifespan: _AppLifespan,
    token_store: HostTokenStore,
    quotas: SqliteQuotaStore,
    stripe_configs: StripeAppConfigStore,
    webhook_configs: WebhookAppConfigStore,
    *,
    owns_token_store: bool,
    owns_quota_store: bool,
    owns_stripe_config_store: bool,
    owns_webhook_config_store: bool,
) -> _AppLifespan:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with runtime_lifespan(app):
                yield
        finally:
            if owns_token_store:
                token_store.close()
            if owns_quota_store:
                quotas.close()
            if owns_stripe_config_store:
                stripe_configs.close()
            if owns_webhook_config_store:
                webhook_configs.close()

    return lifespan


def _configure_middleware(
    app: FastAPI,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
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
        session_resolver=make_preview_session_resolver(
            runtime.live_sessions if runtime is not None else None
        ),
        require_capability=True,
        redemption_store=store,
        local_lease_resolver=store.resolve_local_preview_lease,
        local_storage_reset_committer=store.complete_local_preview_storage_reset,
        canonical_authority_resolver=(
            None
            if runtime is None
            else lambda conversation_id, port, workspace_version: _resolve_canonical_authority(
                store,
                runtime,
                conversation_id,
                port,
                workspace_version,
            )
        ),
    )
    # EPIC O P0-3 — strip the permissive wildcard CORS from the owner-only
    # Cloudflare deploy surface. Added LAST so it is the OUTERMOST middleware and
    # can override the global CORSMiddleware's headers on those paths.
    app.add_middleware(CloudflareDeployCorsMiddleware)


def _include_routers(
    app: FastAPI,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
    token_store: HostTokenStore,
    quotas: SqliteQuotaStore,
    stripe_configs: StripeAppConfigStore,
    webhook_configs: WebhookAppConfigStore,
) -> None:
    # Per-domain routers (routes/<domain>.py). Registration order preserves the
    # original relative order; the `{path:path}` catch-alls (workspace/artifacts/
    # preview-app/port) live inside their domain routers after the literal routes.
    app.include_router(make_auth_router())
    # WO-A2.2: host-service bus. Included early so the literal `/_disco/svc/{service}`
    # route is matched before any catch-all `{path:path}` routers.
    app.include_router(
        make_host_service_bus_router(
            store,
            runtime,
            token_store,
            quotas,
            stripe_configs,
            webhook_configs,
        )
    )
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
    app.include_router(make_release_router(store, runtime))
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
    app.include_router(
        make_cloudflare_router(
            store,
            runtime,
            stripe_dependencies=StripeDeployDependencies(token_store, stripe_configs),
            webhook_dependencies=WebhookDeployDependencies(
                token_store,
                webhook_configs,
                _webhook_approvals(runtime),
            ),
        )
    )


def create_app(
    store: SqliteEventStore,
    *,
    runtime: ConversationRuntime | None = None,
    host_token_store: HostTokenStore | None = None,
    quota_store: SqliteQuotaStore | None = None,
    stripe_config_store: StripeAppConfigStore | None = None,
    webhook_config_store: WebhookAppConfigStore | None = None,
) -> FastAPI:
    """Build the FastAPI app over a given store. The store is injected so tests
    drive it headlessly. `runtime` runs the agent loop with real inference (Stage
    2); pass None in tests that only exercise the wire layer (the loop won't run)."""

    # WO-A2.2: durable operational token store for the host-service bus. Shares
    # the event-store DB path so tokens survive restarts; falls back to :memory:
    # for ephemeral wire tests.
    owns_token_store = host_token_store is None
    event_db_path = getattr(store, "db_path", None) or ":memory:"
    token_store = host_token_store or HostTokenStore(event_db_path)
    owns_quota_store = quota_store is None
    quotas = quota_store or SqliteQuotaStore(event_db_path)
    owns_stripe_config_store = stripe_config_store is None
    stripe_configs = stripe_config_store or StripeAppConfigStore(event_db_path)
    owns_webhook_config_store = webhook_config_store is None
    webhook_configs = webhook_config_store or WebhookAppConfigStore(event_db_path)
    lifespan = _make_lifespan(
        _make_runtime_lifespan(event_db_path, runtime),
        token_store,
        quotas,
        stripe_configs,
        webhook_configs,
        owns_token_store=owns_token_store,
        owns_quota_store=owns_quota_store,
        owns_stripe_config_store=owns_stripe_config_store,
        owns_webhook_config_store=owns_webhook_config_store,
    )
    app = FastAPI(title="disco agent-server", version="0.1.0", lifespan=lifespan)
    app.state.host_token_store = token_store
    app.state.quota_store = quotas
    app.state.stripe_config_store = stripe_configs
    app.state.webhook_config_store = webhook_configs
    _configure_middleware(app, store, runtime)
    _include_routers(
        app,
        store,
        runtime,
        token_store,
        quotas,
        stripe_configs,
        webhook_configs,
    )
    return app

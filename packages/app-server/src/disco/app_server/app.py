"""The app-server HTTP surface — the user-facing gateway (BoD §5.2).

Owns the *settings* surface (the deterministic, manual model-assignment matrix +
the skills/MCP scaffolds) and the *library* surface (owner-scoped conversation
list + delete) over the shared core `EventStore`. It does NOT run agent loops;
the per-conversation runtime + the live WebSocket stay in the agent-server.

CORS is open (no credentials — ownership is an explicit query param, not a
cookie) so the dev frontend on another origin can call it.
"""

from __future__ import annotations

import contextlib

from disco.core.auth import allowed_frontend_origins
from disco.core.quota import SqliteQuotaStore
from disco.core.store.sqlite import SqliteEventStore
from disco.core.stripe_host_service import StripeAppConfigStore
from disco.core.webhook_host_service import WebhookAppConfigStore
from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import AppAuthMiddleware, make_auth_router
from .config_state import ConfigState
from .routes import (
    make_config_router,
    make_conversations_router,
    make_health_router,
    make_mcp_router,
    make_models_router,
    make_openrouter_router,
    make_providers_router,
    make_quota_router,
    make_secrets_router,
    make_security_router,
    make_skills_router,
    make_stripe_router,
    make_webhooks_router,
)


def create_app(store: SqliteEventStore, config: ConfigState | None = None) -> FastAPI:
    """Build the app-server over a shared store. The store is injected so tests
    drive it headlessly and so it shares conversations with the agent-server."""
    # Default-construct ConfigState wired to the SHARED store connection so MCP
    # approvals persist to the mcp_approvals table in the deployed app — not only
    # when a test injects an explicit ConfigState. `__main__.create_app(store)`
    # takes this branch; without the db_conn, POST /api/mcp/servers/{name}/approve
    # 500s ("no DB connection for approval persistence") and GET /api/mcp can never
    # project an approved/connected server. The store's _conn already carries the
    # mcp_approvals table (core SqliteEventStore schema).
    owned_stripe_configs: StripeAppConfigStore | None = None
    owned_webhook_configs: WebhookAppConfigStore | None = None
    owned_quota_store: SqliteQuotaStore | None = None
    if config is None:
        event_db_path = getattr(store, "db_path", None) or ":memory:"
        owned_stripe_configs = StripeAppConfigStore(event_db_path)
        owned_webhook_configs = WebhookAppConfigStore(event_db_path)
        owned_quota_store = SqliteQuotaStore(event_db_path)
        state = ConfigState(
            db_conn=store._conn,
            stripe_configs=owned_stripe_configs,
            webhook_configs=owned_webhook_configs,
            quota_store=owned_quota_store,
        )
    else:
        state = config

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owned_stripe_configs is not None:
                owned_stripe_configs.close()
            if owned_webhook_configs is not None:
                owned_webhook_configs.close()
            if owned_quota_store is not None:
                owned_quota_store.close()

    app = FastAPI(title="disco app-server", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def redact_secret_validation(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        if not request.url.path.startswith(("/api/stripe/", "/api/webhooks/")):
            return await request_validation_exception_handler(request, exc)
        # Pydantic's default 422 includes the rejected `input`, which would echo
        # a malformed/overlong credential. Preserve useful field diagnostics but
        # remove both input and validator context from this write-only surface.
        details = [
            {
                "type": error.get("type", "value_error"),
                "loc": error.get("loc", ()),
                "msg": error.get("msg", "invalid value"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    app.add_middleware(AppAuthMiddleware, store=store)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_frontend_origins()),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(make_auth_router())
    app.include_router(make_health_router())
    app.include_router(make_models_router(state))
    app.include_router(make_config_router(state))
    app.include_router(make_openrouter_router(state))
    app.include_router(make_providers_router(state))
    app.include_router(make_secrets_router(state))
    app.include_router(make_security_router(state))
    app.include_router(make_stripe_router(state))
    app.include_router(make_quota_router(state))
    app.include_router(make_webhooks_router(state))
    app.include_router(make_skills_router(state))
    app.include_router(make_mcp_router(state))
    app.include_router(make_conversations_router(store))

    return app

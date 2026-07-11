"""Per-domain APIRouter factories for the app-server settings + library surface.

`create_app` assembles the FastAPI app and `include_router`s each of these. Every
factory takes exactly the deps its handlers close over (`state: ConfigState` for
the settings routes, `store: SqliteEventStore` for the library routes) and returns
an `APIRouter` — a 1:1 extraction of the route closures that used to live inside
`create_app` (ZERO behaviour change: identical paths/methods/responses)."""

from __future__ import annotations

from .config import make_config_router
from .conversations import make_conversations_router
from .health import make_health_router
from .mcp import make_mcp_router
from .models import make_models_router
from .openrouter import make_openrouter_router
from .providers import make_providers_router
from .quota import make_quota_router
from .secrets import make_secrets_router
from .security import make_security_router
from .skills import make_skills_router
from .stripe import make_stripe_router
from .webhooks import make_webhooks_router

__all__ = [
    "make_config_router",
    "make_conversations_router",
    "make_health_router",
    "make_mcp_router",
    "make_models_router",
    "make_openrouter_router",
    "make_providers_router",
    "make_quota_router",
    "make_secrets_router",
    "make_security_router",
    "make_skills_router",
    "make_stripe_router",
    "make_webhooks_router",
]

"""Per-domain APIRouter factories for the app-server settings + library surface.

`create_app` assembles the FastAPI app and `include_router`s each of these. Every
factory takes exactly the deps its handlers close over (`state: ConfigState` for
the settings routes, `store: SqliteEventStore` for the library routes) and returns
an `APIRouter` — a 1:1 extraction of the route closures that used to live inside
`create_app` (ZERO behaviour change: identical paths/methods/responses)."""

from __future__ import annotations

from .config import make_config_router
from .health import make_health_router
from .models import make_models_router

__all__ = [
    "make_config_router",
    "make_health_router",
    "make_models_router",
]

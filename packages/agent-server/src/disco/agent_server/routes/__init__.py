"""Per-domain APIRouter factories for the agent-server REST + WS surface.

`create_app` assembles the FastAPI app and `include_router`s each of these. Every
factory takes `(store, runtime)` and returns an `APIRouter` whose handlers close
over those deps — a 1:1 extraction of the route closures that used to live inside
`create_app` (ZERO behaviour change: identical paths/methods/responses)."""

from __future__ import annotations

from .activity import make_activity_router
from .conversations import make_conversation_library_router, make_conversations_router
from .debug import make_debug_router
from .deck_editor import make_deck_editor_router
from .export import make_export_router
from .files import make_files_router
from .health import make_health_router
from .mcp import make_mcp_router
from .models import make_models_router
from .preview import make_preview_router
from .preview_edit import make_preview_edit_router
from .probes import make_probes_router
from .projects import make_projects_router
from .release import make_release_router
from .report import make_report_router
from .sandbox import make_sandbox_router
from .schedules import make_schedules_router
from .sessions import make_sessions_router
from .share import make_share_router
from .spaces import make_spaces_router
from .storage import make_storage_router
from .suggestions import make_suggestions_router
from .workflows import make_workflows_router
from .ws import make_ws_router

__all__ = [
    "make_activity_router",
    "make_conversation_library_router",
    "make_conversations_router",
    "make_debug_router",
    "make_deck_editor_router",
    "make_export_router",
    "make_files_router",
    "make_health_router",
    "make_mcp_router",
    "make_models_router",
    "make_preview_router",
    "make_preview_edit_router",
    "make_probes_router",
    "make_projects_router",
    "make_release_router",
    "make_report_router",
    "make_sandbox_router",
    "make_schedules_router",
    "make_sessions_router",
    "make_share_router",
    "make_spaces_router",
    "make_storage_router",
    "make_suggestions_router",
    "make_workflows_router",
    "make_ws_router",
]

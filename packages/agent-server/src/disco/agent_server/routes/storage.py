"""Server-side directory picker route (settings projects_root selector)."""

from __future__ import annotations

from disco.core.store.sqlite import SqliteEventStore
from fastapi import APIRouter, Query, Request

from ..runtime import ConversationRuntime
from .storage_support import (
    _audit_browse as _audit_browse,
)
from .storage_support import (
    _browse_allowed_roots as _browse_allowed_roots,
)
from .storage_support import (
    _browse_path_allowed as _browse_path_allowed,
)
from .storage_support import (
    _ensure_allowed_browse_path as _ensure_allowed_browse_path,
)
from .storage_support import (
    _is_forbidden_root as _is_forbidden_root,
)
from .storage_support import (
    browse_storage as _browse_storage,
)


def make_storage_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/storage/browse")
    async def browse_storage(
        request: Request,
        path: str = Query(default=""),
    ) -> dict:
        return _browse_storage(request, path)

    return router

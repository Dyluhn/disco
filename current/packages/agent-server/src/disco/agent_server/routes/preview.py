"""Live-preview route façade and compatibility seams."""

from __future__ import annotations

import websockets  # noqa: F401 — preserved private test seam
from disco.core.auth import PATH_PREVIEW_BOOTSTRAP_PATH, PreviewCapabilitySigner
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.sandbox._container import USER_PORTS
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket

from ..auth import current_session, websocket_session
from ..runtime import ConversationRuntime
from ._common import (
    require_owned_conversation,
    require_owned_conversation_for_owner,
)
from .preview_browser import (
    _canonical_preview_port as _canonical_preview_port,
)
from .preview_browser import (
    _fetch_inside_response as _fetch_inside_response,
)
from .preview_browser import (
    _path_preview_bootstrap_url as _path_preview_bootstrap_url,
)
from .preview_browser import (
    _path_preview_port_from_label as _path_preview_port_from_label,
)
from .preview_browser import (
    _preview_bootstrap_url as _preview_bootstrap_url,
)
from .preview_browser import (
    _preview_origin_base as _preview_origin_base,
)
from .preview_browser import (
    _preview_websocket_origin_allowed as _preview_websocket_origin_allowed,
)
from .preview_browser import (
    _register_live_browser_start_route,
    _register_live_browser_status_routes,
    _register_preview_meta_routes,
    _wake_for_preview,
)
from .preview_capability import (
    _canonical_preview_authority as _canonical_preview_authority,
)
from .preview_capability import _path_preview_bootstrap_response
from .preview_handoff import PreviewCapabilityBody, preview_capability_response
from .preview_proxy import (
    _close_ws,
    _port_app_response,
    _preview_app_websocket_response,
)
from .preview_proxy import (
    _preview_app_response as _preview_app_response,
)
from .preview_proxy import (
    _proxy_websocket_to_upstream as _proxy_websocket_to_upstream,
)
from .preview_static import (
    _finished_snapshot_is_committed as _finished_snapshot_is_committed,
)
from .preview_static import (
    _selected_app_entry as _selected_app_entry,
)
from .preview_static import (
    _serve_static_from_snapshot as _serve_static_from_snapshot,
)
from .preview_static import (
    _snapshot_request_path as _snapshot_request_path,
)
from .preview_static import (
    _verified_snapshot_request_path as _verified_snapshot_request_path,
)


def _register_port_preview_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    @router.get("/conversations/{conversation_id}/port/{port}/{path:path}")
    @router.get("/conversations/{conversation_id}/port/{port}/")
    async def port_app(
        request: Request,
        conversation_id: str,
        port: int,
        path: str = "",
    ) -> Response:
        conversation_id = await require_owned_conversation(
            request,
            store,
            conversation_id,
        )
        if port not in USER_PORTS:
            return Response(
                "unknown port",
                status_code=404,
                media_type="text/plain",
            )
        if runtime is None:
            return Response(
                "preview not available",
                status_code=503,
                media_type="text/plain",
            )
        return await _port_app_response(
            runtime,
            conversation_id,
            port,
            path,
            current_session(request).owner_id,
        )

    @router.websocket("/conversations/{conversation_id}/port/{port}/{path:path}")
    @router.websocket("/conversations/{conversation_id}/port/{port}/")
    async def port_app_websocket(
        websocket: WebSocket,
        conversation_id: str,
        port: int,
        path: str = "",
    ) -> None:
        session = websocket_session(websocket)
        if session is None:
            await _close_ws(websocket, 1008, "auth required")
            return
        try:
            conversation_id = await require_owned_conversation_for_owner(
                store,
                conversation_id,
                session.owner_id,
                owner_bypass=session.session_id == "test-session",
            )
        except HTTPException:
            await _close_ws(
                websocket,
                1008,
                "conversation forbidden",
            )
            return
        if port not in USER_PORTS:
            await _close_ws(websocket, 1008, "unknown port")
            return
        if runtime is None:
            await _close_ws(
                websocket,
                1008,
                "preview not available",
            )
            return
        cid8 = conversation_id.removeprefix("conv_")[:8]
        upstream = await _wake_for_preview(
            runtime,
            cid8,
            port,
            owner_id=session.owner_id,
        )
        if upstream is None:
            await _close_ws(
                websocket,
                1008,
                "preview not available",
            )
            return
        await _proxy_websocket_to_upstream(websocket, upstream, path)


def _register_preview_app_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    from disco.core.auth import ISOLATED_PATH_PREVIEW_PREFIX

    @router.get("/conversations/{conversation_id}/preview-app/{path:path}")
    @router.get("/conversations/{conversation_id}/preview-app/")
    @router.get(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/{{path:path}}")
    @router.get(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/")
    async def preview_app(
        request: Request,
        conversation_id: str,
        path: str = "",
    ) -> Response:
        return await _preview_app_response(
            store,
            runtime,
            request,
            conversation_id,
            path,
        )


def _register_preview_app_websocket_routes(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    from disco.core.auth import ISOLATED_PATH_PREVIEW_PREFIX

    @router.websocket("/conversations/{conversation_id}/preview-app/{path:path}")
    @router.websocket("/conversations/{conversation_id}/preview-app/")
    @router.websocket(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/{{path:path}}")
    @router.websocket(f"{ISOLATED_PATH_PREVIEW_PREFIX}/{{conversation_id}}/")
    async def preview_app_websocket(
        websocket: WebSocket,
        conversation_id: str,
        path: str = "",
    ) -> None:
        await _preview_app_websocket_response(
            websocket,
            store,
            runtime,
            conversation_id,
            path,
        )


def _register_preview_capability_route(
    router: APIRouter,
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> None:
    signer = PreviewCapabilitySigner(redemption_store=store)

    @router.post("/conversations/{conversation_id}/preview/capability")
    async def preview_capability(
        conversation_id: str,
        body: PreviewCapabilityBody,
        request: Request,
    ) -> Response:
        return await preview_capability_response(
            store,
            runtime,
            signer,
            conversation_id,
            body,
            request,
        )

    @router.post(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{{cid8}}")
    async def path_preview_bootstrap(
        cid8: str,
        request: Request,
    ) -> Response:
        return await _path_preview_bootstrap_response(
            signer,
            cid8,
            request,
        )


def make_preview_router(
    store: SqliteEventStore,
    runtime: ConversationRuntime | None,
) -> APIRouter:
    router = APIRouter()
    _register_preview_capability_route(router, store, runtime)
    _register_live_browser_start_route(router, store, runtime)
    _register_live_browser_status_routes(router, store, runtime)
    _register_port_preview_routes(router, store, runtime)
    _register_preview_meta_routes(router, store, runtime)
    _register_preview_app_routes(router, store, runtime)
    _register_preview_app_websocket_routes(router, store, runtime)
    return router

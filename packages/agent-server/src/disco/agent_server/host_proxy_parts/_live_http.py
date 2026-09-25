"""Select the existing isolated LIVE transport before any application dispatch."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING

from disco.core.auth import PreviewCapability
from disco.tools import SandboxSession
from disco.tools.sandbox._container import NOVNC_PORT
from disco.tools.sandbox.session_parts.request_inside import request_inside
from fastapi import Request, Response
from starlette.types import Receive, Scope, Send

from ..preview_manager import PreviewManager
from ..preview_models import PreviewSession
from ._application_http import _application_response, _hop_headers, _request_body
from ._capability import _authority_changed
from ._forwarding import _build_upstream_request_headers

if TYPE_CHECKING:
    from ..host_proxy import HostPreviewProxyMiddleware
    from ._route_resolution import _PreviewRoute


@dataclass(frozen=True)
class _LiveBinding:
    sandbox: SandboxSession
    manager: PreviewManager
    selected: PreviewSession
    projection_id: str
    instance_id: str
    generation: int

    def valid(self, port: int) -> bool:
        return (
            self.sandbox._preview_manager is self.manager
            and self.manager._sandbox is self.sandbox
            and self.manager.canonical_session() is self.selected
            and self.selected.port == port
            and self.selected.projection_id == self.projection_id
            and self.selected.sandbox_instance_id == self.instance_id == self.sandbox.id
            and self.selected.sandbox_generation == self.generation == self.sandbox.generation
        )


def _live_binding(sandbox: SandboxSession, port: int) -> _LiveBinding | None:
    manager = sandbox._preview_manager
    if not isinstance(manager, PreviewManager):
        return None
    selected = manager.canonical_session()
    if selected is None:
        return None
    binding = _LiveBinding(
        sandbox, manager, selected, selected.projection_id, sandbox.id, sandbox.generation
    )
    return binding if binding.valid(port) else None


async def _owned_session(
    proxy: HostPreviewProxyMiddleware, cap: PreviewCapability, cid8: str
) -> SandboxSession | None:
    resolver = proxy.session_resolver
    if resolver is None:
        return None
    try:
        resolved = resolver(cid8, cap.owner_id)
        return await resolved if inspect.isawaitable(resolved) else resolved
    except Exception:  # noqa: BLE001 — never downgrade an owner lookup
        return None


async def _live_response(
    proxy: HostPreviewProxyMiddleware,
    request: Request,
    cap: PreviewCapability,
    cid8: str,
    binding: _LiveBinding,
    send: Send,
) -> Response | None:
    headers, declared = _build_upstream_request_headers(
        request.scope,
        _hop_headers(",".join(request.headers.getlist("connection"))),
        local_gateway=True,
    )
    body = await _request_body(request, declared)
    if isinstance(body, Response):
        return body
    # Both body collection and authority resolution may yield. Reject detach,
    # restart, or selection replacement before the single transport dispatch.
    current = await _owned_session(proxy, cap, cid8)
    if await _authority_changed(proxy, request.scope, send, cap):
        return None
    if current is not binding.sandbox or not binding.valid(cap.port):
        return Response("preview generation changed", status_code=409)
    headers = [(key, value) for key, value in headers if key.lower() != "content-length"]
    target = request.scope.get("raw_path", request.url.path.encode()).decode("latin1")
    query = request.scope.get("query_string", b"").decode("latin1")
    if query:
        target += "?" + query
    try:
        result = await request_inside(
            binding.sandbox, cap.port, request.method, target, headers, body
        )
    except Exception:  # noqa: BLE001 — unknown mutation outcome; never replay
        result = None
    if result is None:
        return Response("preview not available", status_code=503)
    return _application_response(
        request, f"http://127.0.0.1:{cap.port}", result, inject_selection=True
    )


async def _try_live_http_via_session(
    proxy: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    route: _PreviewRoute,
    cap: PreviewCapability | None,
) -> bool:
    if (
        cap is None
        or cap.authority_id is None
        or not cap.authority_id.startswith("live:")
        or cap.immutable_version is not None
        or route.port == NOVNC_PORT
        or proxy.session_resolver is None
    ):
        return False
    sandbox = await _owned_session(proxy, cap, route.cid8)
    # A host-shared sandbox has a directly routable listener; retain its existing
    # streaming transport. Isolated namespaces use their approved exec channel.
    if sandbox is not None and sandbox.shares_host_network:
        return False
    binding = _live_binding(sandbox, cap.port) if sandbox is not None else None
    if binding is None:
        response = Response("preview not available", status_code=503)
    else:
        response = await _live_response(
            proxy, Request(scope, receive), cap, route.cid8, binding, send
        )
    if response is not None:
        await response(scope, receive, send)
    return True

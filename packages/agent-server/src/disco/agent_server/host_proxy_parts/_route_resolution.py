"""Host/port -> preview route resolution, and the pre-capability admission gate.

Extracted from ``HostPreviewProxyMiddleware.__call__`` (PKG-10-SANDBOX), which
exceeded the per-callable logical-line and McCabe budgets as one flat method.
Split into two responsibilities:

- route resolution: the local-preview-gateway lease lookup OR the
  ``p2-<cid8>-<port>``/path-preview hostname regex match, producing one
  ``_PreviewRoute`` (or fully handling the request itself — an error
  response, or a passthrough to the wrapped app — and returning ``None``);
- the admission gate: the unmanaged-port / service-worker / isolated-asset
  passthrough / p3s-deny / WS-origin checks that run before any capability is
  even looked up.

Every status code and response body string is reproduced byte-for-byte from
the original method.
"""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PATH_PREVIEW_HOST_PREFIX,
    PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS,
    PREVIEW_COOKIE,
    local_preview_cookie_name,
    local_preview_gateway_host,
    local_preview_gateway_ports,
)
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.tools.sandbox._container import PREVIEW_PORT
from starlette.types import Receive, Scope, Send

from ._request_checks import _preview_service_worker_request, _preview_ws_origin_allowed
from ._responses import _deny_static_preview_route

if TYPE_CHECKING:
    from disco.agent_server.host_proxy import HostPreviewProxyMiddleware

# Versioned live (p2) and path/static (p3s) labels work beneath localhost or a
# deployment wildcard domain. p3s includes a full-conversation digest so the
# browser origin is never keyed only by the ambiguous cid8 routing prefix. The
# capability cookie/intent still gates every request; host matching only selects
# the proxy family.
PREVIEW_HOST_RE = re.compile(
    r"^(?P<family>p2)-"
    r"(?P<cid8>[0-9a-f]{8})-(?P<port>\d{2,5})\.[A-Za-z0-9.-]+?(?::\d+)?$"
)
PATH_PREVIEW_HOST_RE = re.compile(
    rf"^{PATH_PREVIEW_HOST_PREFIX}-(?P<cid8>[0-9a-f]{{8}})-"
    rf"[0-9a-f]{{{PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS}}}-"
    r"(?P<port>\d{2,5})\.[A-Za-z0-9.-]+?(?::\d+)?$"
)


@dataclass
class _PreviewRoute:
    """The (cid8, port, family) a preview request resolved to, plus whatever
    the local-preview-gateway lease established about the expected principal.
    """

    cid8: str
    port: int
    cookie_name: str
    preview_family: str
    local_gateway: bool
    expected_conversation_id: str | None
    expected_owner_id: str | None
    expected_authority_id: str | None
    storage_reset_required: bool
    listener_port: int


def _extract_host(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() == b"host":
            return value.decode("latin1")
    return ""


def _extract_listener_port(scope: Scope) -> int:
    raw_server = scope.get("server")
    return (
        raw_server[1]
        if isinstance(raw_server, (tuple, list))
        and len(raw_server) == 2
        and type(raw_server[1]) is int
        else 0
    )


def _is_local_listener(listener_port: int) -> bool:
    try:
        return listener_port in local_preview_gateway_ports()
    except ValueError:
        return False


async def _resolve_local_route(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    host: str,
    listener_port: int,
) -> _PreviewRoute | None:
    lease = None
    resolver = self.local_lease_resolver
    if resolver is not None:
        lease_result = resolver(listener_port, now=int(time.time()))
        lease = await lease_result if inspect.isawaitable(lease_result) else lease_result
    if lease is None:
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"preview origin expired"})
        else:
            await send({"type": "websocket.close", "code": 1008, "reason": "preview expired"})
        return None
    expected_host = f"{local_preview_gateway_host(listener_port)}:{listener_port}"
    if host.lower() != expected_host:
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"preview origin required"})
        else:
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "preview origin required",
                }
            )
        return None
    expected_conversation_id = str(getattr(lease, "conversation_id", "") or "")
    expected_owner_id = str(getattr(lease, "owner_id", "") or "")
    expected_authority_id = str(getattr(lease, "authority_id", "") or "")
    storage_reset_required = bool(getattr(lease, "storage_reset_required", True))
    cid8 = expected_conversation_id.removeprefix("conv_")[:8]
    port = int(getattr(lease, "target_port", 0) or 0)
    cookie_name = local_preview_cookie_name(listener_port)
    return _PreviewRoute(
        cid8=cid8,
        port=port,
        cookie_name=cookie_name,
        preview_family="local",
        local_gateway=True,
        expected_conversation_id=expected_conversation_id,
        expected_owner_id=expected_owner_id,
        expected_authority_id=expected_authority_id,
        storage_reset_required=storage_reset_required,
        listener_port=listener_port,
    )


def _resolve_hostname_route(host: str, listener_port: int) -> _PreviewRoute | None:
    path_preview_host = PATH_PREVIEW_HOST_RE.match(host)
    match = path_preview_host or PREVIEW_HOST_RE.match(host)
    if not match:
        return None
    cid8 = match.group("cid8")
    preview_family = (
        PATH_PREVIEW_HOST_PREFIX if path_preview_host is not None else match.group("family")
    )
    try:
        port = int(match.group("port"))
    except ValueError:
        port = 0
    return _PreviewRoute(
        cid8=cid8,
        port=port,
        cookie_name=PREVIEW_COOKIE,
        preview_family=preview_family,
        local_gateway=False,
        expected_conversation_id=None,
        expected_owner_id=None,
        expected_authority_id=None,
        storage_reset_required=False,
        listener_port=listener_port,
    )


async def _resolve_route(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    host: str,
    listener_port: int,
) -> _PreviewRoute | None:
    """Resolve the local-lease or hostname-regex route.

    Returns None in every case where the request is already fully handled:
    an error response was sent (local lease expired/origin mismatch), or —
    for a hostname that is not a preview host at all — the request was
    forwarded to the wrapped app. The caller must return either way.
    """
    if _is_local_listener(listener_port):
        return await _resolve_local_route(self, scope, send, host, listener_port)
    route = _resolve_hostname_route(host, listener_port)
    if route is None:
        await self.app(scope, receive, send)
        return None
    return route


def _port_allowed(self: HostPreviewProxyMiddleware, route: _PreviewRoute) -> bool:
    from disco.tools.sandbox._container import USER_PORTS

    managed_canonical_port = (
        self.require_capability
        and (route.local_gateway or route.preview_family == "p2")
        and is_managed_host_preview_port(route.port)
    )
    return route.port in USER_PORTS or managed_canonical_port


def _is_isolated_asset_passthrough(route: _PreviewRoute, path: str) -> bool:
    """H079: committed static previews use the same isolated wildcard host on
    remote deployments, but their bytes come from the selected snapshot
    route rather than the live port proxy. Pass only those two narrow path
    families to the application, whose middleware requires the separate,
    path-scoped preview capability on every asset request.
    """
    return (
        route.preview_family == PATH_PREVIEW_HOST_PREFIX
        and (
            path.startswith(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/")
            or path.startswith(f"{ISOLATED_PATH_PREVIEW_PREFIX}/")
        )
    ) or (
        route.port == PREVIEW_PORT
        and path.startswith("/conversations/")
        and "/preview-app/" in path
    )


async def _gate_and_maybe_dispatch(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    route: _PreviewRoute,
    host: str,
    path: str,
) -> bool:
    """Run every check that precedes capability verification.

    Returns True iff the request is already fully handled (an error
    response was sent, or it was dispatched/denied) and the caller must
    return.
    """
    if not _port_allowed(self, route):
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"unknown port"})
        else:
            await send({"type": "websocket.close", "code": 1008, "reason": "unknown port"})
        return True

    if (
        self.require_capability
        and scope["type"] == "http"
        and _preview_service_worker_request(scope)
    ):
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview service workers disabled"})
        return True

    if _is_isolated_asset_passthrough(route, path):
        await self.app(scope, receive, send)
        return True

    if route.preview_family == PATH_PREVIEW_HOST_PREFIX:
        # p3s is a static/path-only origin. Never let a host capability or
        # an unscoped request turn it back into the generic live-port proxy.
        await _deny_static_preview_route(scope, send)
        return True

    if (
        self.require_capability
        and scope["type"] == "websocket"
        and not _preview_ws_origin_allowed(scope, host)
    ):
        await send(
            {"type": "websocket.close", "code": 1008, "reason": "preview origin required"}
        )
        return True

    return False

"""Capability verification, authority-generation checks, and canonical dispatch.

Extracted from ``HostPreviewProxyMiddleware.__call__`` (PKG-10-SANDBOX), which
exceeded the per-callable logical-line and McCabe budgets as one flat method.
The authority-generation check runs BOTH before and after the (possibly
awaited) upstream resolution — a real race-condition guard, not duplication —
so it is its own reusable function called from both places. Every status
code and response body string is reproduced byte-for-byte from the original
method.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from disco.agent_server.preview_paths import safe_capability_target_path
from disco.core.auth import ISOLATED_PATH_PREVIEW_PREFIX, PreviewCapability
from disco.tools.projects import is_runtime_secret_path
from starlette.types import Receive, Scope, Send

from ._request_checks import _preview_ws_origin_allowed, _verified_preview_capability

if TYPE_CHECKING:
    from disco.agent_server.host_proxy import HostPreviewProxyMiddleware

    from ._route_resolution import _PreviewRoute


async def _verify_capability(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    host: str,
    *,
    cid8: str,
    port: int,
    cookie_name: str,
    local_gateway: bool,
    expected_conversation_id: str | None,
    expected_owner_id: str | None,
    expected_authority_id: str | None,
    path: str,
) -> tuple[PreviewCapability | None, str | None] | None:
    """Verify the signed preview capability, if this proxy requires one.

    Returns None iff the request is already fully handled (an error
    response was sent) and the caller must return. Otherwise returns
    ``(cap, cap_owner_id)`` — both None when capability enforcement is off.
    """
    if not self.require_capability:
        return None, None
    method = (
        "WEBSOCKET" if scope["type"] == "websocket" else str(scope.get("method") or "GET").upper()
    )
    cap = _verified_preview_capability(
        self.capability_signer,
        scope,
        cid8=cid8,
        port=port,
        method=method,
        path=path,
        cookie_name=cookie_name,
    )
    if cap is None or (
        local_gateway
        and (
            cap.conversation_id != expected_conversation_id
            or cap.owner_id != expected_owner_id
            or cap.authority_id != expected_authority_id
        )
    ):
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"preview capability required",
                }
            )
        else:
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "preview capability required",
                }
            )
        return None
    if (
        scope["type"] == "http"
        and method in {"POST", "PUT", "PATCH", "DELETE"}
        and not _preview_ws_origin_allowed(scope, host)
    ):
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview origin required"})
        return None
    return cap, cap.owner_id


async def _resolve_canonical_authority(
    self: HostPreviewProxyMiddleware, cap: PreviewCapability
) -> object:
    resolver = self.canonical_authority_resolver
    if resolver is None:
        return None
    resolved = resolver(cap.conversation_id, cap.port, cap.immutable_version)
    return await resolved if inspect.isawaitable(resolved) else resolved


async def _authority_changed(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    cap: PreviewCapability | None,
) -> bool:
    """Return True (having sent a 409/ws-close) iff ``cap``'s authority is stale.

    A capability with no authority (``cap is None`` or ``cap.authority_id is
    None``) is never considered changed — this call is then a no-op.
    """
    if cap is None or cap.authority_id is None:
        return False
    current_authority = await _resolve_canonical_authority(self, cap)
    if current_authority == cap.authority_id:
        return False
    if scope["type"] == "http":
        await send(
            {
                "type": "http.response.start",
                "status": 409,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview generation changed"})
    else:
        await send(
            {
                "type": "websocket.close",
                "code": 1008,
                "reason": "preview generation changed",
            }
        )
    return True


async def _apply_canonical_routing(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    cap: PreviewCapability | None,
    path: str,
) -> bool:
    """Authority check + the canonical internal-rewrite dispatch + immutable gate.

    Returns True iff the request is already fully handled (dispatched
    internally, or an error/read-only response was sent) and the caller must
    return.
    """
    if cap is None or cap.authority_id is None:
        return False
    if await _authority_changed(self, scope, send, cap):
        return True
    safe_path = safe_capability_target_path(path)
    if safe_path is None or is_runtime_secret_path(safe_path):
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"preview path not found"})
        else:
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "preview path not found",
                }
            )
        return True
    # Canonical Preview browser GETs and HMR sockets enter the existing
    # lifecycle dispatcher while retaining the browser's root-relative
    # URL. This is an internal ASGI rewrite only; generated code never
    # sees Disco's authenticated origin or session cookie.
    if scope["type"] == "websocket" or (
        scope["type"] == "http"
        and str(scope.get("method") or "GET").upper() == "GET"
        and not cap.authority_id.startswith("live:")
    ):
        internal_path = f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cap.conversation_id}/{path.lstrip('/')}"
        internal_scope = dict(scope)
        internal_scope["path"] = internal_path
        internal_scope["raw_path"] = internal_path.encode("utf-8")
        state = dict(scope.get("state") or {})
        state["canonical_preview_capability"] = cap
        state["canonical_preview_original_path"] = path
        internal_scope["state"] = state
        await self.app(internal_scope, receive, send)
        return True
    if cap.authority_id.startswith(("version:", "committed:")):
        await send(
            {
                "type": "http.response.start",
                "status": 405,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"immutable preview is read-only"})
        return True
    return False


def _is_canonical_gateway(route: _PreviewRoute, cap: PreviewCapability | None) -> bool:
    return route.local_gateway or bool(cap and cap.authority_id)

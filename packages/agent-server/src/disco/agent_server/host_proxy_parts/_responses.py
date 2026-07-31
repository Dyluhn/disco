"""Small response-shaping helpers used before any upstream is resolved."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.types import Message, Scope, Send

if TYPE_CHECKING:
    from disco.agent_server.host_proxy import HostPreviewProxyMiddleware


def _force_private_no_store(send: Send) -> Send:
    async def send_no_store(message: Message) -> None:
        if message.get("type") == "http.response.start":
            blocked = {
                b"cache-control",
                b"pragma",
                b"expires",
                b"etag",
                b"last-modified",
                b"surrogate-control",
                b"cdn-cache-control",
                b"cloudflare-cdn-cache-control",
            }
            headers = [
                (name, value)
                for name, value in message.get("headers", [])
                if name.lower() not in blocked
            ]
            headers.extend([(b"cache-control", b"private, no-store"), (b"pragma", b"no-cache")])
            message = {**message, "headers": headers}
        await send(message)

    return send_no_store


def _maybe_force_private_no_store(
    self: HostPreviewProxyMiddleware, scope: Scope, send: Send
) -> Send:
    """Wrap ``send`` so a capability-gated preview never leaves cacheable bytes.

    A generated app must never retain capability-protected bytes in a
    browser/shared cache. This wrapper also covers pass-through path
    previews and every gated error response on the wildcard origin.
    """
    if self.require_capability and scope["type"] == "http":
        return _force_private_no_store(send)
    return send


async def _deny_static_preview_route(scope: Scope, send: Send) -> None:
    """Keep the dedicated p3s origin out of the generic live-port proxy."""

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
                "body": b"isolated preview route required",
            }
        )
        return
    await send({"type": "websocket.close", "code": 1008, "reason": "unknown preview route"})

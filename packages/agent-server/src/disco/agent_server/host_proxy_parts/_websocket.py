"""WebSocket proxying to the upstream preview dev server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
from typing import TYPE_CHECKING

import websockets
from starlette.types import Receive, Scope, Send
from websockets.typing import Subprotocol

if TYPE_CHECKING:
    from disco.agent_server.host_proxy import HostPreviewProxyMiddleware

_LOG = logging.getLogger("disco.agent_server.host_proxy")


def _negotiate_subprotocols(scope: Scope) -> list[Subprotocol]:
    """Return the client's requested WS subprotocols, scope-list first."""
    subprotocols = [
        Subprotocol(str(protocol).strip()) for protocol in scope.get("subprotocols", [])
    ]
    subprotocols = [protocol for protocol in subprotocols if protocol]
    if not subprotocols:
        for name, value in scope.get("headers", []):
            if name.lower() == b"sec-websocket-protocol":
                subprotocols = [
                    Subprotocol(p.strip()) for p in value.decode("latin1").split(",") if p.strip()
                ]
                break
    return subprotocols


async def _proxy_websocket(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    upstream: str,
) -> None:
    message = await receive()
    if message["type"] != "websocket.connect":
        return

    path = scope.get("path", "")
    query_string = scope.get("query_string", b"").decode("latin1")
    upstream_parsed = urllib.parse.urlparse(upstream)
    ws_scheme = "wss" if upstream_parsed.scheme == "https" else "ws"
    url = f"{ws_scheme}://{upstream_parsed.netloc}{path}"
    if query_string:
        url = f"{url}?{query_string}"

    subprotocols = _negotiate_subprotocols(scope)

    try:
        if subprotocols:
            ws_client = await websockets.connect(url, subprotocols=subprotocols)
        else:
            # websockets 16 serializes an explicit empty sequence as an
            # invalid `Sec-WebSocket-Protocol:` header. Omit it entirely.
            ws_client = await websockets.connect(url)
    except Exception as e:
        _LOG.warning("ws upstream connect error: %s", e)
        await send({"type": "websocket.close", "code": 1006})
        return

    accept_msg = {"type": "websocket.accept"}
    if ws_client.subprotocol:
        accept_msg["subprotocol"] = ws_client.subprotocol
    await send(accept_msg)

    async def client_to_upstream():
        try:
            while True:
                message = await receive()
                if message["type"] == "websocket.receive":
                    if "text" in message:
                        await ws_client.send(message["text"])
                    elif "bytes" in message:
                        await ws_client.send(message["bytes"])
                elif message["type"] == "websocket.disconnect":
                    await ws_client.close(message.get("code", 1000))
                    break
        except Exception:
            await ws_client.close(1006)

    async def upstream_to_client():
        try:
            async for message in ws_client:
                if isinstance(message, str):
                    await send({"type": "websocket.send", "text": message})
                else:
                    await send({"type": "websocket.send", "bytes": message})
            await send({"type": "websocket.close", "code": 1000})
        except websockets.ConnectionClosed as e:
            await send({"type": "websocket.close", "code": e.code, "reason": e.reason})
        except Exception:
            await send({"type": "websocket.close", "code": 1006})

    t1 = asyncio.create_task(client_to_upstream())
    t2 = asyncio.create_task(upstream_to_client())

    done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

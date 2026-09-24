"""WebSocket proxying to the upstream preview dev server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import urllib.parse
from typing import TYPE_CHECKING

import websockets
from starlette.types import Receive, Scope, Send
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ProtocolError
from websockets.frames import Close
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
        await send({"type": "websocket.close", "code": 1011})
        return

    try:
        accept_msg = {"type": "websocket.accept"}
        if ws_client.subprotocol:
            accept_msg["subprotocol"] = ws_client.subprotocol
        await send(accept_msg)
        await _relay_websocket(ws_client, receive, send)
    finally:
        with contextlib.suppress(Exception):
            await ws_client.close()


def _wire_close_code(code: object) -> int:
    """ASGI reports reserved observation codes that cannot go on the wire."""
    if code == 1005:
        return 1000  # peer sent an empty, otherwise normal close frame
    if not isinstance(code, int) or isinstance(code, bool):
        return 1011
    try:
        Close(code, "").check()
    except ProtocolError:
        return 1011
    return code


def _wire_close_reason(reason: object) -> str:
    """A close control frame permits 123 UTF-8 bytes after its two-byte code."""
    if not isinstance(reason, str):
        return ""
    return reason.encode("utf-8", errors="replace")[:123].decode("utf-8", errors="ignore")


async def _relay_websocket(ws_client: ClientConnection, receive: Receive, send: Send) -> None:
    client_disconnected = False
    close_sent = False

    async def close_client(code: object, reason: str = "") -> None:
        nonlocal close_sent
        if client_disconnected or close_sent:
            return
        close_sent = True
        await send({"type": "websocket.close", "code": _wire_close_code(code), "reason": reason})

    async def client_to_upstream():
        nonlocal client_disconnected
        while True:
            message = await receive()
            if message["type"] == "websocket.receive":
                if message.get("text") is not None:
                    await ws_client.send(message["text"])
                elif message.get("bytes") is not None:
                    await ws_client.send(message["bytes"])
            elif message["type"] == "websocket.disconnect":
                client_disconnected = True
                await ws_client.close(
                    _wire_close_code(message.get("code", 1000)),
                    _wire_close_reason(message.get("reason", "")),
                )
                return

    async def upstream_to_client():
        try:
            async for message in ws_client:
                if isinstance(message, str):
                    await send({"type": "websocket.send", "text": message})
                else:
                    await send({"type": "websocket.send", "bytes": message})
            await close_client(ws_client.close_code or 1000, ws_client.close_reason or "")
        except websockets.ConnectionClosed as error:
            await close_client(
                error.rcvd.code if error.rcvd else 1011,
                error.rcvd.reason if error.rcvd else "",
            )

    tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if any(not task.cancelled() and task.exception() is not None for task in done):
            with contextlib.suppress(Exception):
                await close_client(1011)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Retrieve failures from completed pumps too, including when the caller
        # is cancelled. No detached receive task or unobserved exception survives.
        await asyncio.gather(*tasks, return_exceptions=True)

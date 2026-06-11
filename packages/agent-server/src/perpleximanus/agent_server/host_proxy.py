import asyncio
import contextlib
import logging
import re
import urllib.parse
from collections.abc import Callable

import httpx
import websockets
from starlette.types import ASGIApp, Receive, Scope, Send

_LOG = logging.getLogger(__name__)

PREVIEW_HOST_RE = re.compile(r"^(?P<cid8>[0-9a-f]{8})-(?P<port>\d{2,5})\.localhost(?::\d+)?$")

_client: httpx.AsyncClient | None = None

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0), follow_redirects=False)
    return _client

class HostPreviewProxyMiddleware:
    def __init__(
        self, app: ASGIApp, *, upstream_resolver: Callable[[str, int], str | None]
    ) -> None:
        self.app = app
        self.upstream_resolver = upstream_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        host = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"host":
                host = value.decode("latin1")
                break
        
        match = PREVIEW_HOST_RE.match(host)
        if not match:
            await self.app(scope, receive, send)
            return

        cid8 = match.group("cid8")
        try:
            port = int(match.group("port"))
        except ValueError:
            port = 0

        from perpleximanus.tools.sandbox._container import USER_PORTS
        if port not in USER_PORTS:
            if scope["type"] == "http":
                await send({
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"unknown port"})
            else:
                await send({"type": "websocket.close", "code": 1008, "reason": "unknown port"})
            return

        upstream = self.upstream_resolver(cid8, port)
        if not upstream:
            if scope["type"] == "http":
                await send({
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"preview not available"})
            else:
                await send(
                    {"type": "websocket.close", "code": 1008, "reason": "preview not available"}
                )
            return
            
        if scope["type"] == "http":
            await self._proxy_http(scope, receive, send, upstream)
        elif scope["type"] == "websocket":
            await self._proxy_websocket(scope, receive, send, upstream)

    async def _proxy_http(self, scope: Scope, receive: Receive, send: Send, upstream: str) -> None:
        client = _get_client()
        
        path = scope.get("path", "")
        query_string = scope.get("query_string", b"").decode("latin1")
        url = f"{upstream}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        method = scope.get("method", "GET")
        
        headers = []
        hop_by_hop = {
            "connection", "keep-alive", "transfer-encoding", "upgrade",
            "proxy-authenticate", "proxy-authorization", "te", "trailers",
        }
        for name, value in scope.get("headers", []):
            name_str = name.decode("latin1").lower()
            if name_str not in hop_by_hop and not name_str.startswith("proxy-"):
                if name_str != "host":
                    headers.append((name.decode("latin1"), value.decode("latin1")))

        upstream_parsed = urllib.parse.urlparse(upstream)
        headers.append(("host", upstream_parsed.netloc))

        async def body_iterator():
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] == "http.request":
                    yield message.get("body", b"")
                    more_body = message.get("more_body", False)
                elif message["type"] == "http.disconnect":
                    break

        req = client.build_request(method, url, headers=headers, content=body_iterator())
        try:
            res = await client.send(req, stream=True)
        except httpx.RequestError as e:
            _LOG.warning("upstream connect error: %s", e)
            await send({
                "type": "http.response.start",
                "status": 502,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({"type": "http.response.body", "body": b"preview upstream unreachable"})
            return

        res_headers = []
        for k, v in res.headers.multi_items():
            k_lower = k.lower()
            if k_lower not in hop_by_hop and not k_lower.startswith("proxy-"):
                res_headers.append((k.encode("latin1"), v.encode("latin1")))

        # aclose in finally: a client disconnect mid-stream raises out of send()
        # and must not leak the upstream connection (orchestrator hardening).
        try:
            await send({
                "type": "http.response.start",
                "status": res.status_code,
                "headers": res_headers,
            })
            async for chunk in res.aiter_raw():
                await send({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                })
            await send({
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            })
        finally:
            await res.aclose()

    async def _proxy_websocket(
        self, scope: Scope, receive: Receive, send: Send, upstream: str
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

        subprotocols = scope.get("subprotocols", [])
        if not subprotocols:
            for name, value in scope.get("headers", []):
                if name.lower() == b"sec-websocket-protocol":
                    subprotocols = [p.strip() for p in value.decode("latin1").split(",")]
                    break

        try:
            ws_client = await websockets.connect(url, subprotocols=subprotocols)
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

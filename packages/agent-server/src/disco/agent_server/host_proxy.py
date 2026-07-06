import asyncio
import contextlib
import inspect
import logging
import re
import urllib.parse
from collections.abc import Awaitable, Callable

import httpx
import websockets
from disco.core.auth import (
    PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_COOKIE,
    PreviewCapabilitySigner,
    preview_ttl_s,
)
from disco.agent_server.preview_inject import inject_element_mention_picker
from starlette.types import ASGIApp, Receive, Scope, Send

_LOG = logging.getLogger(__name__)

PREVIEW_HOST_RE = re.compile(r"^(?P<cid8>[0-9a-f]{8})-(?P<port>\d{2,5})\.localhost(?::\d+)?$")

# C2 (first-hit wake race): the preview upstream is bound lazily by the
# sandbox; the first proxy hit after wake can land BEFORE the dev server has
# finished binding its port, which surfaces as a connect failure (ECONNREFUSED
# or RST) rather than an HTTP response. To make the first hit wait for the
# bind instead of 502/503ing, we retry the connect a bounded number of times
# with a short exponential backoff. A real HTTP response — including any
# error status — is NEVER retried (the upstream has spoken, we just forward).
# Healthy upstreams add zero extra latency: the retry loop only sleeps on
# RequestError, and the first attempt that succeeds returns immediately.
_CONNECT_RETRY_ATTEMPTS = 5  # 1 initial + 4 retries
_CONNECT_BACKOFF_BASE = 0.05  # 50ms
_CONNECT_BACKOFF_FACTOR = 2.0
_CONNECT_BACKOFF_CAP = 0.4  # 400ms
# Total backoff across all failed attempts: 50+100+200+400 = 750ms (well under 2s).

_client: httpx.AsyncClient | None = None

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0), follow_redirects=False)
    return _client


def _cookie_value(scope: Scope, name: str) -> str | None:
    needle = name + "="
    for header_name, value in scope.get("headers", []):
        if header_name.lower() != b"cookie":
            continue
        for part in value.decode("latin1").split(";"):
            item = part.strip()
            if item.startswith(needle):
                return item[len(needle):]
    return None


def _preview_ws_origin_allowed(scope: Scope, host: str) -> bool:
    origin = ""
    for name, value in scope.get("headers", []):
        if name.lower() == b"origin":
            origin = value.decode("latin1")
            break
    if not origin:
        return False
    parsed = urllib.parse.urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc == host

class HostPreviewProxyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        upstream_resolver: Callable[..., str | None | Awaitable[str | None]],
        session_resolver: Callable[..., object | None | Awaitable[object | None]] | None = None,
        require_capability: bool = False,
    ) -> None:
        self.app = app
        self.upstream_resolver = upstream_resolver
        self.capability_signer = PreviewCapabilitySigner()
        self.require_capability = require_capability
        # Fix 2 (codex P1): cid8 -> live SandboxSession (or None). On sealed/filtered
        # backends the hostname proxy gets NO host upstream even while the dev server
        # is up, so the canonical in-app iframe 503s "available-then-broken". When a
        # live session exists we fall back to a liveness proxy (curl INSIDE the box)
        # so the iframe shows the built result on every backend. None ⇒ no fallback
        # (behaviour byte-identical to before this fix).
        self.session_resolver = session_resolver

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

        from disco.tools.sandbox._container import USER_PORTS
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

        if (
            self.require_capability
            and scope["type"] == "websocket"
            and not _preview_ws_origin_allowed(scope, host)
        ):
            await send(
                {"type": "websocket.close", "code": 1008, "reason": "preview origin required"}
            )
            return

        path = str(scope.get("path") or "/")
        if self.require_capability and scope["type"] == "http" and path == PREVIEW_BOOTSTRAP_PATH:
            await self._handle_preview_bootstrap(scope, send, cid8, port)
            return

        cap_owner_id = None
        if self.require_capability:
            method = "WEBSOCKET" if scope["type"] == "websocket" else str(
                scope.get("method") or "GET"
            ).upper()
            cap = self.capability_signer.verify(
                _cookie_value(scope, PREVIEW_COOKIE),
                cid8=cid8,
                port=port,
                method=method,
                path=path,
            )
            if cap is None:
                if scope["type"] == "http":
                    await send({
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"text/plain")],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b"preview capability required",
                    })
                else:
                    await send({
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "preview capability required",
                    })
                return
            cap_owner_id = cap.owner_id

        try:
            upstream_res = self.upstream_resolver(cid8, port, cap_owner_id)
        except TypeError:
            upstream_res = self.upstream_resolver(cid8, port)
        if inspect.isawaitable(upstream_res):
            upstream = await upstream_res
        else:
            upstream = upstream_res

        if not upstream:
            # Fix 2 (codex P1): no host upstream published, but a live session may be
            # serving the port INSIDE the box (sealed/filtered backend). For HTTP,
            # fall back to the in-sandbox liveness proxy so the canonical iframe still
            # renders the built result. Websocket/HMR upgrade still needs a published
            # port on open boxes (unchanged) — the fallback is for "view the result".
            if scope["type"] == "http" and await self._proxy_http_via_session(
                scope, send, cid8, port, cap_owner_id
            ):
                return
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

    async def _handle_preview_bootstrap(
        self, scope: Scope, send: Send, cid8: str, port: int
    ) -> None:
        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin1"))
        intent = (query.get("intent") or [""])[0]
        minted = self.capability_signer.mint_cookie_from_intent(intent)
        if minted is None:
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({"type": "http.response.body", "body": b"invalid preview intent"})
            return
        token, target = minted
        if self.capability_signer.verify(
            token, cid8=cid8, port=port, method="GET", path=target.split("?", 1)[0] or "/"
        ) is None:
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({"type": "http.response.body", "body": b"preview intent scope mismatch"})
            return
        cookie = (
            f"{PREVIEW_COOKIE}={token}; Path=/; Max-Age={preview_ttl_s()}; "
            "HttpOnly; SameSite=Strict"
        ).encode("latin1")
        await send({
            "type": "http.response.start",
            "status": 303,
            "headers": [
                (b"location", target.encode("latin1")),
                (b"set-cookie", cookie),
                (b"referrer-policy", b"no-referrer"),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    async def _send_with_connect_retry(
        self,
        client: httpx.AsyncClient,
        req: httpx.Request,
        send: Send,
    ) -> httpx.Response | None:
        """Send `req`, retrying on connect/transport errors (httpx.RequestError).

        A successful HTTP response — even one with a 5xx status — is returned
        on the first attempt that produces one, so the proxy adds ZERO extra
        latency to a healthy upstream and does NOT retry on a legitimate
        upstream error. Only transport-level failures (connect refused, RST,
        read timeout mid-handshake) trigger the bounded backoff loop.

        Returns the response on success, or None after sending a final 502 to
        the client when all attempts are exhausted.
        """
        last_err: Exception | None = None
        for attempt in range(_CONNECT_RETRY_ATTEMPTS):
            try:
                return await client.send(req, stream=True)
            except httpx.RequestError as e:
                last_err = e
                if attempt < _CONNECT_RETRY_ATTEMPTS - 1:
                    wait = min(
                        _CONNECT_BACKOFF_BASE * (_CONNECT_BACKOFF_FACTOR ** attempt),
                        _CONNECT_BACKOFF_CAP,
                    )
                    await asyncio.sleep(wait)
        _LOG.warning(
            "upstream connect failed after %d attempts: %s",
            _CONNECT_RETRY_ATTEMPTS,
            last_err,
        )
        await send({
            "type": "http.response.start",
            "status": 502,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"preview upstream unreachable"})
        return None

    async def _proxy_http_via_session(
        self,
        scope: Scope,
        send: Send,
        cid8: str,
        port: int,
        owner_id: str | None,
    ) -> bool:
        """Fix 2 (codex P1) — fall back to the in-sandbox liveness proxy when there's
        no published host upstream. Returns True iff the live in-box server answered
        (response already sent); False to let the caller emit the honest 503. GET only."""
        # SECURITY (noVNC gate-bypass fix): NOVNC_PORT is in USER_PORTS, so a hostname
        # like {cid8}-6080.localhost reaches here when the upstream resolver returns
        # None. For 6080 that None is the live-browser GATE (port_upstream refuses
        # NOVNC_PORT when the feature is disabled), NOT just "no host port published".
        # Curling the stale noVNC HTTP surface from inside the box would re-expose a
        # disabled live-browser surface, bypassing the gate. The noVNC surface is
        # served EXCLUSIVELY via the gated published-port proxy — never fetch_inside.
        from disco.tools.sandbox._container import NOVNC_PORT
        if port == NOVNC_PORT:
            return False
        resolver = self.session_resolver
        if resolver is None:
            return False
        try:
            try:
                session_res = resolver(cid8, owner_id)
            except TypeError:
                session_res = resolver(cid8)
            session = await session_res if inspect.isawaitable(session_res) else session_res
            if session is None:
                return False
            path = scope.get("path", "")
            query_string = scope.get("query_string", b"").decode("latin1")
            rel = path.lstrip("/")
            if query_string:
                rel = f"{rel}?{query_string}"
            got = await session.fetch_inside(port, rel)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a liveness probe must never 500 the iframe
            return False
        if got is None:
            return False
        status, body, ctype = got
        media_type = ctype or "application/octet-stream"
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", media_type.encode("latin1"))],
        })
        await send({
            "type": "http.response.body",
            "body": inject_element_mention_picker(body, media_type),
        })
        return True

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

        # Buffer the request body in memory so we can rebuild the request on
        # each retry attempt. The body iterator is a one-shot async generator
        # over the ASGI receive channel; if the first connect attempt fails we
        # cannot replay it. Buffering is bounded by request size; preview
        # upstreams see small bodies (form posts, hmr pings, asset fetches).
        body_chunks: list[bytes] = []
        async for chunk in body_iterator():
            body_chunks.append(chunk)
        body = b"".join(body_chunks)

        req = client.build_request(method, url, headers=headers, content=body)
        res = await self._send_with_connect_retry(client, req, send)
        if res is None:
            # All connect attempts failed; 502 already sent.
            return

        content_type = res.headers.get("content-type")
        buffer_html = (
            content_type is not None
            and content_type.split(";", 1)[0].strip().lower() == "text/html"
        )
        if buffer_html:
            body = await res.aread()
            injected = inject_element_mention_picker(body, content_type)
            changed = injected != body
            res_headers = []
            for k, v in res.headers.multi_items():
                k_lower = k.lower()
                if k_lower in hop_by_hop or k_lower.startswith("proxy-"):
                    continue
                if k_lower in {"content-length", "content-encoding", "etag"}:
                    continue
                if changed and k_lower in {
                    "content-security-policy",
                    "content-security-policy-report-only",
                }:
                    continue
                res_headers.append((k.encode("latin1"), v.encode("latin1")))
            res_headers.append((b"content-length", str(len(injected)).encode("latin1")))
            try:
                await send({
                    "type": "http.response.start",
                    "status": res.status_code,
                    "headers": res_headers,
                })
                await send({
                    "type": "http.response.body",
                    "body": injected,
                    "more_body": False,
                })
            finally:
                await res.aclose()
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


def make_preview_session_resolver(runtime: object | None) -> Callable[..., Awaitable[object | None]]:
    """Fix 2 (codex P1): build the `cid8 -> live SandboxSession | None` resolver the
    HostPreviewProxyMiddleware uses for its in-sandbox liveness fallback. Captures
    `runtime` (None in wire-only tests → always None). Read-only: resolves the full
    conversation id from the 8-char prefix, then the live session (no creation)."""

    async def _resolve(cid8: str, owner_id: str | None = None) -> object | None:
        if runtime is None:
            return None
        try:
            if owner_id is not None:
                cid = await runtime.resolve_owned_cid_prefix(cid8, owner_id)  # type: ignore[attr-defined]
            else:
                cid = runtime.resolve_cid_prefix(cid8)  # type: ignore[attr-defined]
            if not cid:
                return None
            return runtime.live_session(cid)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — resolver must never raise into the proxy
            return None

    return _resolve

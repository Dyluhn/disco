"""HTTP request/response proxying to the upstream preview dev server.

Extracted from ``host_proxy`` (PKG-10-SANDBOX): ``_forward_http_response`` and
``_proxy_http`` each exceeded the per-callable logical-line and McCabe
budgets as flat functions/methods. Split by phase (header filtering,
transport-failure fallback, streamed vs. buffered-HTML response bodies) so
each stays trivially under budget; the two orchestrators are now short
straight-line sequences.

``_get_client`` and ``_send_with_connect_retry`` stay defined on
``host_proxy`` itself (its test suite imports/monkeypatches them directly), so
every use here goes through the parent module object
(``host_proxy._get_client()``) rather than a direct import — the test suite's
``monkeypatch.setattr(host_proxy_module, "_get_client", ...)`` must be seen by
this call site, which a `from host_proxy import _get_client` binding would
silently defeat.
"""

from __future__ import annotations

import inspect
import logging
import re
import urllib.parse
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx
from disco.agent_server import host_proxy
from disco.agent_server.preview_inject import (
    MAX_ELEMENT_MENTION_HTML_BYTES,
    inject_element_mention_picker,
    inject_selection_agent,
)
from starlette.types import Receive, Scope, Send

from ._headers import _forwardable_response_header, _strip_reserved_preview_cookie
from ._redirects import _rewrite_canonical_upstream_location

if TYPE_CHECKING:
    from disco.agent_server.host_proxy import HostPreviewProxyMiddleware

_LOG = logging.getLogger("disco.agent_server.host_proxy")
_EXCEPTION_CLASS_MAX_CHARS = 64


def _sanitized_exception_class(error: BaseException | None) -> str:
    """Return a bounded identifier, never the exception message or request data."""

    if error is None:
        return "UnknownError"
    raw_name = type(error).__name__
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_name)
    return (safe_name or "UnknownError")[:_EXCEPTION_CLASS_MAX_CHARS]


def _exception_category(error: BaseException | None) -> str:
    """Classify failures without serializing potentially sensitive exceptions."""

    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(error, httpx.ConnectError):
        return "connect"
    if isinstance(error, httpx.ProtocolError):
        return "protocol"
    if isinstance(error, httpx.RequestError):
        return "request"
    if isinstance(error, (ConnectionError, OSError)):
        return "io"
    return "internal"


async def _read_bounded_request_body(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    max_bytes: int,
) -> bytes | None:
    """Read the ASGI request body, returning it if within ``max_bytes``.

    Sends a 413 response and returns ``None`` if the body exceeds the bound.
    The iterator is a one-shot async generator over the ASGI receive channel,
    so the body must be buffered in memory to support connect retries.
    """

    body_chunks: list[bytes] = []
    body_size = 0
    more_body = True
    while more_body:
        message = await receive()
        if message.get("type") != "http.request":
            # A disconnect is not an empty/complete body. Forwarding a
            # truncated mutation here could commit an unintended action.
            return None
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            return None
        body_size += len(chunk)
        if body_size > max_bytes:
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"preview request body too large",
                }
            )
            return None
        body_chunks.append(chunk)
        more_body = bool(message.get("more_body"))
    return b"".join(body_chunks)


async def _resolve_upstream(
    self: HostPreviewProxyMiddleware, cid8: str, port: int, owner_id: str | None
) -> str | None:
    try:
        upstream_res = self.upstream_resolver(cid8, port, owner_id)
    except TypeError:
        upstream_res = self.upstream_resolver(cid8, port)
    if inspect.isawaitable(upstream_res):
        return await upstream_res
    return upstream_res


async def _proxy_http_via_session(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    cid8: str,
    port: int,
    owner_id: str | None,
    *,
    local_gateway: bool = False,
) -> bool:
    """Use the in-sandbox liveness proxy when the published path is unavailable.

    Returns True iff the live in-box server answered (response already sent),
    otherwise False so the caller can emit its honest 502/503. GET only.
    """
    # SECURITY (noVNC gate-bypass fix): NOVNC_PORT is in USER_PORTS, so a hostname
    # like p2-{cid8}-6080.localhost reaches here when the upstream resolver returns
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
        if owner_id is not None:
            # A capability-gated fallback must preserve the authenticated
            # owner lookup. Never downgrade a resolver failure to an
            # unowned cid-prefix lookup.
            session_res = resolver(cid8, owner_id)
        else:
            try:
                inspect.signature(resolver).bind(cid8, owner_id)
            except (TypeError, ValueError):
                session_res = resolver(cid8)
            else:
                session_res = resolver(cid8, owner_id)
        session = await session_res if inspect.isawaitable(session_res) else session_res
        if session is None:
            return False
        path = scope.get("path", "")
        query_string = scope.get("query_string", b"").decode("latin1")
        rel = path.lstrip("/")
        if query_string:
            rel = f"{rel}?{query_string}"
        got = await session.fetch_inside(port, rel)
    except Exception as error:  # noqa: BLE001 — a liveness probe must never 500 the iframe
        _LOG.warning(
            "preview in-session fallback failed category=%s class=%s",
            _exception_category(error),
            _sanitized_exception_class(error),
        )
        return False
    if got is None:
        return False
    status, body, ctype = got
    media_type = ctype or "application/octet-stream"
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", media_type.encode("latin1"))],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": inject_element_mention_picker(
                inject_selection_agent(body, media_type) if local_gateway else body,
                media_type,
            ),
        }
    )
    return True


async def _respond_missing_upstream(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    cid8: str,
    port: int,
    owner_id: str | None,
    *,
    local_gateway: bool,
) -> None:
    """Handle "no published upstream": try the in-box liveness fallback (GET
    only), else emit the honest unavailable response.

    Fix 2 (codex P1): no host upstream published, but a live session may be
    serving the port INSIDE the box (sealed/filtered backend). For HTTP,
    fall back to the in-sandbox liveness proxy so the canonical iframe still
    renders the built result. Websocket/HMR upgrade still needs a published
    port on open boxes (unchanged) — the fallback is for "view the result".
    """
    if (
        scope["type"] == "http"
        and str(scope.get("method") or "GET").upper() == "GET"
        and await _proxy_http_via_session(
            self, scope, send, cid8, port, owner_id, local_gateway=local_gateway
        )
    ):
        return
    if scope["type"] == "http":
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview not available"})
    else:
        await send({"type": "websocket.close", "code": 1008, "reason": "preview not available"})


def _response_forward_headers(
    res: httpx.Response,
    hop_by_hop: set[str],
    *,
    local_gateway: bool,
    upstream: str,
    request_scope: Scope | None,
) -> list[tuple[bytes, bytes]]:
    return [
        (
            k.encode("latin1"),
            (
                _rewrite_canonical_upstream_location(v, upstream=upstream, scope=request_scope)
                if local_gateway and request_scope is not None and k.lower() == "location"
                else v
            ).encode("latin1"),
        )
        for k, v in res.headers.multi_items()
        if _forwardable_response_header(k, v, hop_by_hop, local_gateway=local_gateway)
    ]


async def _stream_raw_response(
    send: Send, res: httpx.Response, headers: list[tuple[bytes, bytes]]
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": res.status_code,
            "headers": headers,
        }
    )
    async for chunk in res.aiter_raw():
        await send(
            {
                "type": "http.response.body",
                "body": chunk,
                "more_body": True,
            }
        )
    await send(
        {
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        }
    )


async def _stream_buffered_html_overflow(
    send: Send,
    res: httpx.Response,
    headers: list[tuple[bytes, bytes]],
    html_chunks: list[bytes],
    html_stream: AsyncIterator[bytes],
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": res.status_code,
            "headers": headers,
        }
    )
    for chunk in html_chunks:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})
    async for chunk in html_stream:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _injected_html_headers(
    res: httpx.Response,
    hop_by_hop: set[str],
    *,
    local_gateway: bool,
    request_scope: Scope | None,
    upstream: str,
    changed: bool,
    injected: bytes,
) -> list[tuple[bytes, bytes]]:
    res_headers = []
    for k, v in res.headers.multi_items():
        k_lower = k.lower()
        if not _forwardable_response_header(k, v, hop_by_hop, local_gateway=local_gateway):
            continue
        if k_lower in {"content-length", "content-encoding", "etag"}:
            continue
        if changed and k_lower in {
            "content-security-policy",
            "content-security-policy-report-only",
        }:
            continue
        if local_gateway and request_scope is not None and k_lower == "location":
            v = _rewrite_canonical_upstream_location(v, upstream=upstream, scope=request_scope)
        res_headers.append((k.encode("latin1"), v.encode("latin1")))
    res_headers.append((b"content-length", str(len(injected)).encode("latin1")))
    return res_headers


async def _forward_http_response(
    self: HostPreviewProxyMiddleware,
    send: Send,
    res: httpx.Response,
    *,
    hop_by_hop: set[str],
    content_type: str | None,
    local_gateway: bool = False,
    upstream: str = "",
    request_scope: Scope | None = None,
) -> None:
    """Forward an upstream HTTP response to the ASGI client.

    Buffers ``text/html`` responses (when not content-encoded) so the
    element-mention picker can inject UI chrome; streams everything else.
    The caller is responsible for ``res.aclose()`` in a ``finally`` block.
    """
    content_encoding = (res.headers.get("content-encoding") or "").strip().lower()
    buffer_html = (
        content_type is not None
        and content_type.split(";", 1)[0].strip().lower() == "text/html"
        # Do not decompress an attacker-controlled encoded response into
        # memory for injection; encoded HTML stays on the raw streaming path.
        and content_encoding in {"", "identity"}
    )
    if buffer_html:
        html_chunks: list[bytes] = []
        html_size = 0
        html_overflow = False
        html_stream = res.aiter_bytes()
        async for chunk in html_stream:
            html_chunks.append(chunk)
            html_size += len(chunk)
            if html_size > MAX_ELEMENT_MENTION_HTML_BYTES:
                html_overflow = True
                break
        if html_overflow:
            headers = _response_forward_headers(
                res,
                hop_by_hop,
                local_gateway=local_gateway,
                upstream=upstream,
                request_scope=request_scope,
            )
            await _stream_buffered_html_overflow(send, res, headers, html_chunks, html_stream)
            return
        original_body = b"".join(html_chunks)
        body = original_body
        if local_gateway:
            body = inject_selection_agent(body, content_type)
        injected = inject_element_mention_picker(body, content_type)
        changed = injected != original_body
        headers = _injected_html_headers(
            res,
            hop_by_hop,
            local_gateway=local_gateway,
            request_scope=request_scope,
            upstream=upstream,
            changed=changed,
            injected=injected,
        )
        await send(
            {
                "type": "http.response.start",
                "status": res.status_code,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": injected,
                "more_body": False,
            }
        )
        return

    headers = _response_forward_headers(
        res, hop_by_hop, local_gateway=local_gateway, upstream=upstream, request_scope=request_scope
    )
    await _stream_raw_response(send, res, headers)


def _build_upstream_request_headers(
    scope: Scope, hop_by_hop: set[str], *, local_gateway: bool
) -> tuple[list[tuple[str, str]], int | None]:
    headers: list[tuple[str, str]] = []
    declared_length: int | None = None
    for name, value in scope.get("headers", []):
        name_str = name.decode("latin1").lower()
        if name_str not in hop_by_hop and not name_str.startswith("proxy-"):
            if name_str != "host":
                value_str = value.decode("latin1")
                if name_str == "cookie":
                    filtered_cookie = _strip_reserved_preview_cookie(
                        value_str, local_gateway=local_gateway
                    )
                    if filtered_cookie is not None:
                        headers.append((name.decode("latin1"), filtered_cookie))
                else:
                    headers.append((name.decode("latin1"), value_str))
        if name_str == "content-length":
            try:
                declared_length = int(value.decode("latin1"))
            except ValueError:
                declared_length = None
    return headers, declared_length


async def _respond_to_transport_failure(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    cid8: str,
    port: int,
    owner_id: str | None,
    *,
    local_gateway: bool,
    method: str,
    transport_error: httpx.RequestError | None,
    attempts: int,
) -> None:
    fallback_result = "not_attempted"
    if str(method).upper() == "GET":
        fallback_succeeded = await _proxy_http_via_session(
            self,
            scope,
            send,
            cid8,
            port,
            owner_id,
            local_gateway=local_gateway,
        )
        fallback_result = "success" if fallback_succeeded else "unavailable"
    _LOG.warning(
        "preview published upstream transport exhausted attempts=%d "
        "category=%s class=%s in_session_fallback=%s",
        attempts,
        _exception_category(transport_error),
        _sanitized_exception_class(transport_error),
        fallback_result,
    )
    if fallback_result == "success":
        return
    await send(
        {
            "type": "http.response.start",
            "status": 502,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"preview upstream unreachable"})


async def _proxy_http(
    self: HostPreviewProxyMiddleware,
    scope: Scope,
    receive: Receive,
    send: Send,
    upstream: str,
    *,
    cid8: str,
    port: int,
    owner_id: str | None,
    local_gateway: bool = False,
) -> None:
    client = host_proxy._get_client()

    path = scope.get("path", "")
    query_string = scope.get("query_string", b"").decode("latin1")
    url = f"{upstream}{path}"
    if query_string:
        url = f"{url}?{query_string}"

    method = scope.get("method", "GET")

    hop_by_hop = {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
    }
    headers, declared_length = _build_upstream_request_headers(
        scope, hop_by_hop, local_gateway=local_gateway
    )

    if declared_length is not None and declared_length > host_proxy.MAX_PREVIEW_REQUEST_BODY_BYTES:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview request body too large"})
        return

    upstream_parsed = urllib.parse.urlparse(upstream)
    headers.append(("host", upstream_parsed.netloc))

    body = await _read_bounded_request_body(
        self,
        scope,
        receive,
        send,
        max_bytes=host_proxy.MAX_PREVIEW_REQUEST_BODY_BYTES,
    )
    if body is None:
        return

    req = client.build_request(method, url, headers=headers, content=body)
    res, transport_error, attempts = await host_proxy._send_with_connect_retry(client, req)
    if res is None:
        await _respond_to_transport_failure(
            self,
            scope,
            send,
            cid8,
            port,
            owner_id,
            local_gateway=local_gateway,
            method=method,
            transport_error=transport_error,
            attempts=attempts,
        )
        return

    # aclose in finally: a client disconnect mid-stream raises out of send()
    # and must not leak the upstream connection (orchestrator hardening).
    try:
        await _forward_http_response(
            self,
            send,
            res,
            hop_by_hop=hop_by_hop,
            content_type=res.headers.get("content-type"),
            local_gateway=local_gateway,
            upstream=upstream,
            request_scope=scope,
        )
    finally:
        await res.aclose()

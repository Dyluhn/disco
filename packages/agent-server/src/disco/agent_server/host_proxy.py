import asyncio
import contextlib
import inspect
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable

import httpx
import websockets
from disco.agent_server.preview_bootstrap import (
    MAX_PREVIEW_REDEMPTION_BODY_BYTES,
    cross_site_iframe_headers,
    parse_preview_redemption,
    parse_preview_storage_handoff,
    preview_navigation_document,
    preview_redemption_content_type,
    preview_storage_reset_document,
)
from disco.agent_server.preview_inject import (
    MAX_ELEMENT_MENTION_HTML_BYTES,
    inject_element_mention_picker,
    inject_selection_agent,
)
from disco.agent_server.preview_paths import safe_capability_target_path
from disco.core.auth import (
    ISOLATED_PATH_PREVIEW_PREFIX,
    LOCAL_PREVIEW_COOKIE_PREFIX,
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PATH_PREVIEW_HOST_PREFIX,
    PATH_PREVIEW_ORIGIN_DIGEST_HEX_CHARS,
    PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_COOKIE,
    SESSION_COOKIE,
    PreviewCapability,
    PreviewCapabilitySigner,
    PreviewIntentRedemptionStore,
    local_preview_cookie_name,
    local_preview_gateway_host,
    local_preview_gateway_ports,
    preview_ttl_s,
)
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.tools.projects import is_runtime_secret_path
from disco.tools.sandbox._container import PREVIEW_PORT
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from websockets.typing import Subprotocol

_LOG = logging.getLogger(__name__)

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
MAX_PREVIEW_REQUEST_BODY_BYTES = 1024 * 1024
_EXCEPTION_CLASS_MAX_CHARS = 64
# Total backoff across all failed attempts: 50+100+200+400 = 750ms (well under 2s).

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, read=60.0),
            follow_redirects=False,
            trust_env=False,
        )
    return _client


async def _send_with_connect_retry(
    client: httpx.AsyncClient,
    req: httpx.Request,
) -> tuple[httpx.Response | None, httpx.RequestError | None, int]:
    """Send one request with bounded retries for transport failures only.

    A real HTTP response, including 5xx, is returned immediately. Mutations are
    never replayed. The caller owns final error emission so the host middleware
    can try its authenticated in-session GET path before producing one 502.
    """

    last_err: httpx.RequestError | None = None
    attempts = _CONNECT_RETRY_ATTEMPTS if req.method.upper() in {"GET", "HEAD", "OPTIONS"} else 1
    for attempt in range(attempts):
        try:
            return await client.send(req, stream=True), None, attempt + 1
        except httpx.RequestError as error:
            last_err = error
            if attempt < attempts - 1:
                wait = min(
                    _CONNECT_BACKOFF_BASE * (_CONNECT_BACKOFF_FACTOR**attempt),
                    _CONNECT_BACKOFF_CAP,
                )
                await asyncio.sleep(wait)
    return None, last_err, attempts


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


def _cookie_values(scope: Scope, name: str) -> tuple[str, ...]:
    needle = name + "="
    found: list[str] = []
    for header_name, value in scope.get("headers", []):
        if header_name.lower() != b"cookie":
            continue
        for part in value.decode("latin1").split(";"):
            item = part.strip()
            if item.startswith(needle):
                found.append(item[len(needle) :])
    return tuple(found)


def _local_reserved_cookie_name(name: str) -> bool:
    return bool(
        name in {PREVIEW_COOKIE, SESSION_COOKIE}
        or name.startswith("disco_path_preview_")
        or name.startswith(LOCAL_PREVIEW_COOKIE_PREFIX)
    )


def _strip_reserved_preview_cookie(value: str, *, local_gateway: bool = False) -> str | None:
    """Remove proxy credentials before forwarding a generated-app request."""

    kept: list[str] = []
    for raw_part in value.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        raw_name, separator, _cookie_value_text = part.partition("=")
        name = raw_name.strip()
        if separator and (
            name == PREVIEW_COOKIE or (local_gateway and _local_reserved_cookie_name(name))
        ):
            continue
        kept.append(part)
    return "; ".join(kept) or None


def _sets_reserved_preview_cookie(value: str, *, local_gateway: bool = False) -> bool:
    first_pair = value.split(";", 1)[0]
    raw_name, separator, _cookie_value_text = first_pair.partition("=")
    name = raw_name.strip()
    return bool(
        separator
        and (name == PREVIEW_COOKIE or (local_gateway and _local_reserved_cookie_name(name)))
    )


def _forwardable_response_header(
    name: str,
    value: str,
    hop_by_hop: set[str],
    *,
    local_gateway: bool = False,
) -> bool:
    name_lower = name.lower()
    if name_lower in hop_by_hop or name_lower.startswith("proxy-"):
        return False
    return not (
        name_lower == "set-cookie"
        and _sets_reserved_preview_cookie(value, local_gateway=local_gateway)
    )


def _rewrite_canonical_upstream_location(
    value: str,
    *,
    upstream: str,
    scope: Scope,
) -> str:
    """Keep exact-upstream absolute redirects on the canonical Preview origin.

    Relative and genuinely external redirects retain application semantics. An
    absolute redirect back to the managed runtime address would bypass the
    capability/generation boundary, so translate only that exact origin.
    """

    try:
        location = urllib.parse.urlsplit(value)
        source = urllib.parse.urlsplit(upstream)
        location_port = location.port or (443 if location.scheme.lower() == "https" else 80)
        source_port = source.port or (443 if source.scheme.lower() == "https" else 80)
        if (
            not location.scheme
            or not location.netloc
            or location.username is not None
            or location.password is not None
            or location.scheme.lower() != source.scheme.lower()
            or (location.hostname or "").lower() != (source.hostname or "").lower()
            or location_port != source_port
        ):
            return value
    except (ValueError, UnicodeError):
        return value

    request_host = ""
    forwarded_proto = ""
    for name, raw_value in scope.get("headers", []):
        if name.lower() == b"host":
            request_host = raw_value.decode("latin1")
        elif name.lower() == b"x-forwarded-proto":
            forwarded_proto = raw_value.decode("latin1").split(",", 1)[0].strip().lower()
    if not request_host:
        return value
    request_scheme = str(scope.get("scheme") or "http").lower()
    if forwarded_proto in {"http", "https"}:
        request_scheme = forwarded_proto
    if request_scheme not in {"http", "https"}:
        return value
    return urllib.parse.urlunsplit(
        (request_scheme, request_host, location.path, location.query, location.fragment)
    )


def _preview_ws_origin_allowed(scope: Scope, host: str) -> bool:
    origin = ""
    forwarded_proto = ""
    for name, value in scope.get("headers", []):
        if name.lower() == b"origin":
            origin = value.decode("latin1")
        elif name.lower() == b"x-forwarded-proto":
            forwarded_proto = value.decode("latin1").split(",", 1)[0].strip().lower()
    if not origin:
        return False
    parsed = urllib.parse.urlparse(origin)
    scope_scheme = str(scope.get("scheme") or "http").lower()
    expected_scheme = {"ws": "http", "wss": "https"}.get(scope_scheme, scope_scheme)
    if forwarded_proto in {"http", "https"}:
        expected_scheme = forwarded_proto
    return parsed.scheme == expected_scheme and parsed.netloc.lower() == host.lower()


def _preview_service_worker_request(scope: Scope) -> bool:
    for name, value in scope.get("headers", []):
        name_lower = name.lower()
        value_lower = value.decode("latin1").strip().lower()
        if name_lower == b"sec-fetch-dest" and value_lower == "serviceworker":
            return True
        if name_lower == b"service-worker" and value_lower == "script":
            return True
    return False


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


def _verified_preview_capability(
    signer: PreviewCapabilitySigner,
    scope: Scope,
    *,
    cid8: str,
    port: int,
    method: str,
    path: str,
    cookie_name: str = PREVIEW_COOKIE,
) -> PreviewCapability | None:
    """Select a valid signed capability without trusting duplicate order."""

    for token in _cookie_values(scope, cookie_name):
        cap = signer.verify(token, cid8=cid8, port=port, method=method, path=path)
        if cap is not None:
            return cap
    return None


class HostPreviewProxyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        upstream_resolver: Callable[..., str | None | Awaitable[str | None]],
        session_resolver: Callable[..., object | None | Awaitable[object | None]] | None = None,
        require_capability: bool = False,
        redemption_store: PreviewIntentRedemptionStore | None = None,
        local_lease_resolver: Callable[..., object | None] | None = None,
        local_storage_reset_committer: Callable[..., object] | None = None,
        canonical_authority_resolver: Callable[..., object | None] | None = None,
    ) -> None:
        self.app = app
        self.upstream_resolver = upstream_resolver
        if require_capability and redemption_store is None:
            raise ValueError("capability-gated preview proxy requires a durable redemption store")
        self.capability_signer = PreviewCapabilitySigner(redemption_store=redemption_store)
        self.require_capability = require_capability
        # Fix 2 (codex P1): cid8 -> live SandboxSession (or None). On sealed/filtered
        # backends the hostname proxy gets NO host upstream even while the dev server
        # is up, so the canonical in-app iframe 503s "available-then-broken". The same
        # authenticated liveness proxy (curl INSIDE the box) is also the final GET
        # path after a published upstream exhausts its bounded transport retries.
        # None ⇒ no fallback (behaviour byte-identical to before this fix).
        self.session_resolver = session_resolver
        self.local_lease_resolver = local_lease_resolver
        self.local_storage_reset_committer = local_storage_reset_committer
        self.canonical_authority_resolver = canonical_authority_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        host = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"host":
                host = value.decode("latin1")
                break

        raw_server = scope.get("server")
        listener_port = (
            raw_server[1]
            if isinstance(raw_server, (tuple, list))
            and len(raw_server) == 2
            and type(raw_server[1]) is int
            else 0
        )
        try:
            local_listener = listener_port in local_preview_gateway_ports()
        except ValueError:
            local_listener = False
        local_gateway = False
        expected_conversation_id: str | None = None
        expected_owner_id: str | None = None
        expected_authority_id: str | None = None
        storage_reset_required = False
        cookie_name = PREVIEW_COOKIE
        preview_family = ""
        path_preview_host = None

        if local_listener:
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
                    await send(
                        {"type": "websocket.close", "code": 1008, "reason": "preview expired"}
                    )
                return
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
                return
            expected_conversation_id = str(getattr(lease, "conversation_id", "") or "")
            expected_owner_id = str(getattr(lease, "owner_id", "") or "")
            expected_authority_id = str(getattr(lease, "authority_id", "") or "")
            storage_reset_required = bool(getattr(lease, "storage_reset_required", True))
            cid8 = expected_conversation_id.removeprefix("conv_")[:8]
            port = int(getattr(lease, "target_port", 0) or 0)
            cookie_name = local_preview_cookie_name(listener_port)
            preview_family = "local"
            local_gateway = True
        else:
            path_preview_host = PATH_PREVIEW_HOST_RE.match(host)
            match = path_preview_host or PREVIEW_HOST_RE.match(host)
            if not match:
                await self.app(scope, receive, send)
                return
            cid8 = match.group("cid8")
            preview_family = (
                PATH_PREVIEW_HOST_PREFIX if path_preview_host is not None else match.group("family")
            )
            try:
                port = int(match.group("port"))
            except ValueError:
                port = 0

        if self.require_capability and scope["type"] == "http":
            # A generated app must never retain capability-protected bytes in a
            # browser/shared cache. This wrapper also covers pass-through path
            # previews and every gated error response on the wildcard origin.
            send = _force_private_no_store(send)

        from disco.tools.sandbox._container import USER_PORTS

        managed_canonical_port = (
            self.require_capability
            and (local_gateway or preview_family == "p2")
            and is_managed_host_preview_port(port)
        )
        if port not in USER_PORTS and not managed_canonical_port:
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
            return

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
            return

        # H079: committed static previews use the same isolated wildcard host on
        # remote deployments, but their bytes come from the selected snapshot
        # route rather than the live port proxy. Pass only those two narrow path
        # families to the application, whose middleware requires the separate,
        # path-scoped preview capability on every asset request.
        path = str(scope.get("path") or "/")
        if (
            preview_family == PATH_PREVIEW_HOST_PREFIX
            and (
                path.startswith(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/")
                or path.startswith(f"{ISOLATED_PATH_PREVIEW_PREFIX}/")
            )
        ) or (
            port == PREVIEW_PORT and path.startswith("/conversations/") and "/preview-app/" in path
        ):
            await self.app(scope, receive, send)
            return

        if preview_family == PATH_PREVIEW_HOST_PREFIX:
            # p3s is a static/path-only origin. Never let a host capability or
            # an unscoped request turn it back into the generic live-port proxy.
            await _deny_static_preview_route(scope, send)
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

        if self.require_capability and scope["type"] == "http" and path == PREVIEW_BOOTSTRAP_PATH:
            await self._handle_preview_bootstrap(
                scope,
                receive,
                send,
                cid8,
                port,
                cookie_name=cookie_name,
                expected_conversation_id=expected_conversation_id,
                expected_owner_id=expected_owner_id,
                expected_authority_id=expected_authority_id,
                storage_reset_required=storage_reset_required,
                local_listener_port=listener_port if local_gateway else None,
            )
            return

        cap_owner_id = None
        cap: PreviewCapability | None = None
        if self.require_capability:
            method = (
                "WEBSOCKET"
                if scope["type"] == "websocket"
                else str(scope.get("method") or "GET").upper()
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
                return
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
                return
            cap_owner_id = cap.owner_id

        if cap is not None and cap.authority_id is not None:
            resolver = self.canonical_authority_resolver
            current_authority = None
            if resolver is not None:
                resolved = resolver(
                    cap.conversation_id,
                    cap.port,
                    cap.immutable_version,
                )
                current_authority = await resolved if inspect.isawaitable(resolved) else resolved
            if current_authority != cap.authority_id:
                if scope["type"] == "http":
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 409,
                            "headers": [(b"content-type", b"text/plain")],
                        }
                    )
                    await send(
                        {"type": "http.response.body", "body": b"preview generation changed"}
                    )
                else:
                    await send(
                        {
                            "type": "websocket.close",
                            "code": 1008,
                            "reason": "preview generation changed",
                        }
                    )
                return
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
                return
            # Canonical Preview browser GETs and HMR sockets enter the existing
            # lifecycle dispatcher while retaining the browser's root-relative
            # URL. This is an internal ASGI rewrite only; generated code never
            # sees Disco's authenticated origin or session cookie.
            if scope["type"] == "websocket" or (
                scope["type"] == "http"
                and str(scope.get("method") or "GET").upper() == "GET"
                and not cap.authority_id.startswith("live:")
            ):
                internal_path = (
                    f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cap.conversation_id}/{path.lstrip('/')}"
                )
                internal_scope = dict(scope)
                internal_scope["path"] = internal_path
                internal_scope["raw_path"] = internal_path.encode("utf-8")
                state = dict(scope.get("state") or {})
                state["canonical_preview_capability"] = cap
                state["canonical_preview_original_path"] = path
                internal_scope["state"] = state
                await self.app(internal_scope, receive, send)
                return
            if cap.authority_id.startswith(("version:", "committed:")):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 405,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b"immutable preview is read-only"}
                )
                return
        canonical_gateway = local_gateway or bool(cap and cap.authority_id)
        try:
            upstream_res = self.upstream_resolver(cid8, port, cap_owner_id)
        except TypeError:
            upstream_res = self.upstream_resolver(cid8, port)
        if inspect.isawaitable(upstream_res):
            upstream = await upstream_res
        else:
            upstream = upstream_res

        if cap is not None and cap.authority_id is not None:
            resolver = self.canonical_authority_resolver
            resolved = (
                resolver(
                    cap.conversation_id,
                    cap.port,
                    cap.immutable_version,
                )
                if resolver is not None
                else None
            )
            current_authority = await resolved if inspect.isawaitable(resolved) else resolved
            if current_authority != cap.authority_id:
                if scope["type"] == "http":
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 409,
                            "headers": [(b"content-type", b"text/plain")],
                        }
                    )
                    await send(
                        {"type": "http.response.body", "body": b"preview generation changed"}
                    )
                else:
                    await send(
                        {
                            "type": "websocket.close",
                            "code": 1008,
                            "reason": "preview generation changed",
                        }
                    )
                return

        if not upstream:
            # Fix 2 (codex P1): no host upstream published, but a live session may be
            # serving the port INSIDE the box (sealed/filtered backend). For HTTP,
            # fall back to the in-sandbox liveness proxy so the canonical iframe still
            # renders the built result. Websocket/HMR upgrade still needs a published
            # port on open boxes (unchanged) — the fallback is for "view the result".
            if (
                scope["type"] == "http"
                and str(scope.get("method") or "GET").upper() == "GET"
                and await self._proxy_http_via_session(
                    scope,
                    send,
                    cid8,
                    port,
                    cap_owner_id,
                    local_gateway=canonical_gateway,
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
                await send(
                    {"type": "websocket.close", "code": 1008, "reason": "preview not available"}
                )
            return

        if scope["type"] == "http":
            await _proxy_http(
                self,
                scope,
                receive,
                send,
                upstream,
                cid8=cid8,
                port=port,
                owner_id=cap_owner_id,
                local_gateway=canonical_gateway,
            )
        elif scope["type"] == "websocket":
            await _proxy_websocket(self, scope, receive, send, upstream)

    async def _handle_preview_bootstrap(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        cid8: str,
        port: int,
        *,
        cookie_name: str = PREVIEW_COOKIE,
        expected_conversation_id: str | None = None,
        expected_owner_id: str | None = None,
        expected_authority_id: str | None = None,
        storage_reset_required: bool = False,
        local_listener_port: int | None = None,
    ) -> None:
        method = str(scope.get("method") or "GET").upper()
        headers = {
            name.decode("latin1").lower(): value.decode("latin1")
            for name, value in scope.get("headers", [])
        }
        if method != "POST":
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"method not allowed"})
            return
        if not preview_redemption_content_type(headers.get("content-type")):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"invalid preview intent"})
            return

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") != "http.request":
                body.clear()
                break
            chunk = message.get("body", b"")
            if isinstance(chunk, bytes):
                body.extend(chunk)
            if len(body) > MAX_PREVIEW_REDEMPTION_BODY_BYTES:
                body.clear()
                break
            more_body = bool(message.get("more_body"))
        raw_body = bytes(body)
        handoff = parse_preview_storage_handoff(raw_body)
        if handoff is not None:
            await _complete_storage_handoff(
                self,
                scope,
                send,
                handoff=handoff,
                cid8=cid8,
                port=port,
                cookie_name=cookie_name,
                expected_conversation_id=expected_conversation_id,
                expected_owner_id=expected_owner_id,
                expected_authority_id=expected_authority_id,
                local_listener_port=local_listener_port,
            )
            return

        parsed = parse_preview_redemption(raw_body)
        if parsed is None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"invalid preview intent"})
            return
        intent = parsed
        redeemed = self.capability_signer.redeem_intent(
            intent,
            cid8=cid8,
            port=port,
            path_scope="host",
            expected_conversation_id=expected_conversation_id,
            expected_owner_id=expected_owner_id,
            expected_authority_id=expected_authority_id,
        )
        if redeemed is None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"invalid preview intent"})
            return
        token, target = redeemed
        cap = self.capability_signer.verify(
            token,
            cid8=cid8,
            port=port,
            method="GET",
            path=urllib.parse.urlsplit(target).path or "/",
        )
        if cap is None or (
            expected_conversation_id is not None
            and (
                cap.conversation_id != expected_conversation_id
                or cap.owner_id != expected_owner_id
                or cap.authority_id != expected_authority_id
            )
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"preview intent scope mismatch"})
            return
        partitioned = cross_site_iframe_headers(headers)
        if storage_reset_required and expected_authority_id is not None:
            handoff = self.capability_signer.mint_storage_handoff(
                cap,
                target,
                partitioned=partitioned,
            )
            response_body, reset_headers = preview_storage_reset_document(handoff)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (name.encode("latin1"), value.encode("latin1"))
                        for name, value in reset_headers.items()
                    ],
                }
            )
            await send({"type": "http.response.body", "body": response_body})
            return
        if partitioned:
            cookie = (
                f"{cookie_name}={token}; Path=/; Max-Age={preview_ttl_s()}; "
                "HttpOnly; Secure; SameSite=None; Partitioned"
            ).encode("latin1")
        else:
            forwarded_scheme = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
            secure = forwarded_scheme == "https" or scope.get("scheme") == "https"
            cookie = (
                f"{cookie_name}={token}; Path=/; Max-Age={preview_ttl_s()}; "
                f"HttpOnly; {'Secure; ' if secure else ''}SameSite=Strict"
            ).encode("latin1")
        response_body, navigation_headers = preview_navigation_document(
            target,
            clear_storage=expected_authority_id is None,
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    *[
                        (name.encode("latin1"), value.encode("latin1"))
                        for name, value in navigation_headers.items()
                    ],
                    (b"set-cookie", cookie),
                ],
            }
        )
        await send({"type": "http.response.body", "body": response_body})

    async def _proxy_http_via_session(
        self,
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
            got = await session.fetch_inside(port, rel)  # type: ignore[attr-defined]
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


async def _complete_storage_handoff(
    proxy: HostPreviewProxyMiddleware,
    scope: Scope,
    send: Send,
    *,
    handoff: str,
    cid8: str,
    port: int,
    cookie_name: str,
    expected_conversation_id: str | None,
    expected_owner_id: str | None,
    expected_authority_id: str | None,
    local_listener_port: int | None,
) -> None:
    if (
        expected_conversation_id is None
        or expected_owner_id is None
        or expected_authority_id is None
        or local_listener_port is None
    ):
        redeemed = None
    else:
        redeemed = proxy.capability_signer.redeem_storage_handoff(
            handoff,
            cid8=cid8,
            port=port,
            expected_conversation_id=expected_conversation_id,
            expected_owner_id=expected_owner_id,
            expected_authority_id=expected_authority_id,
        )
    if redeemed is None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"invalid preview handoff"})
        return
    token, target, partitioned = redeemed
    committer = proxy.local_storage_reset_committer
    committed = (
        committer(
            local_listener_port,
            authority_id=expected_authority_id,
            now=int(time.time()),
        )
        if committer is not None
        else False
    )
    if inspect.isawaitable(committed):
        committed = await committed
    if committed is not True:
        await send(
            {
                "type": "http.response.start",
                "status": 409,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"preview authority changed"})
        return
    secure = partitioned or scope.get("scheme") == "https"
    cookie_policy = (
        "Secure; SameSite=None; Partitioned"
        if partitioned
        else f"{'Secure; ' if secure else ''}SameSite=Strict"
    )
    cookie = (
        f"{cookie_name}={token}; Path=/; Max-Age={preview_ttl_s()}; HttpOnly; {cookie_policy}"
    ).encode("latin1")
    response_body = json.dumps({"target": target}, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"cache-control", b"private, no-store"),
                (b"x-content-type-options", b"nosniff"),
                (b"set-cookie", cookie),
            ],
        }
    )
    await send({"type": "http.response.body", "body": response_body})


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
            res_headers = [
                (
                    k.encode("latin1"),
                    (
                        _rewrite_canonical_upstream_location(
                            v, upstream=upstream, scope=request_scope
                        )
                        if local_gateway and request_scope is not None and k.lower() == "location"
                        else v
                    ).encode("latin1"),
                )
                for k, v in res.headers.multi_items()
                if _forwardable_response_header(k, v, hop_by_hop, local_gateway=local_gateway)
            ]
            await send(
                {
                    "type": "http.response.start",
                    "status": res.status_code,
                    "headers": res_headers,
                }
            )
            for chunk in html_chunks:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            async for chunk in html_stream:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        original_body = b"".join(html_chunks)
        body = original_body
        if local_gateway:
            body = inject_selection_agent(body, content_type)
        injected = inject_element_mention_picker(body, content_type)
        changed = injected != original_body
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
        await send(
            {
                "type": "http.response.start",
                "status": res.status_code,
                "headers": res_headers,
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

    res_headers = [
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
    await send(
        {
            "type": "http.response.start",
            "status": res.status_code,
            "headers": res_headers,
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
    client = _get_client()

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
    headers = []
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

    if declared_length is not None and declared_length > MAX_PREVIEW_REQUEST_BODY_BYTES:
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
        max_bytes=MAX_PREVIEW_REQUEST_BODY_BYTES,
    )
    if body is None:
        return

    req = client.build_request(method, url, headers=headers, content=body)
    res, transport_error, attempts = await _send_with_connect_retry(client, req)
    if res is None:
        fallback_result = "not_attempted"
        if str(method).upper() == "GET":
            fallback_succeeded = await self._proxy_http_via_session(
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


def make_preview_session_resolver(
    runtime: object | None,
) -> Callable[..., Awaitable[object | None]]:
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

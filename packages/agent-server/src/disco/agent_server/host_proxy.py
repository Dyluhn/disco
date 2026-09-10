import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from disco.core.auth import (
    PreviewCapabilitySigner,
    PreviewIntentRedemptionStore,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from . import preview_session_ports as pp
from .host_proxy_parts._bootstrap import _maybe_dispatch_bootstrap
from .host_proxy_parts._capability import (
    _apply_canonical_routing,
    _authority_changed,
    _is_canonical_gateway,
    _verify_capability,
)
from .host_proxy_parts._forwarding import (
    _proxy_http,
    _resolve_upstream,
    _respond_missing_upstream,
)
from .host_proxy_parts._redirects import (
    _rewrite_canonical_upstream_location as _rewrite_canonical_upstream_location,
)
from .host_proxy_parts._responses import _maybe_force_private_no_store
from .host_proxy_parts._route_resolution import PREVIEW_HOST_RE as PREVIEW_HOST_RE
from .host_proxy_parts._route_resolution import (
    _extract_host,
    _extract_listener_port,
    _gate_and_maybe_dispatch,
    _resolve_route,
)
from .host_proxy_parts._websocket import _proxy_websocket

_LOG = logging.getLogger(__name__)

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


class HostPreviewProxyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        upstream_resolver: Callable[..., str | None | Awaitable[str | None]],
        session_resolver: pp.PreviewSessionResolver | None = None,
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

        host = _extract_host(scope)
        listener_port = _extract_listener_port(scope)
        route = await _resolve_route(self, scope, receive, send, host, listener_port)
        if route is None:
            return

        # A generated app must never retain capability-protected bytes in a
        # browser/shared cache. This wrapper also covers pass-through path
        # previews and every gated error response on the wildcard origin.
        send = _maybe_force_private_no_store(self, scope, send)

        path = str(scope.get("path") or "/")
        if await _gate_and_maybe_dispatch(self, scope, receive, send, route, host, path):
            return

        if await _maybe_dispatch_bootstrap(self, scope, receive, send, route, path):
            return

        verified = await _verify_capability(
            self,
            scope,
            send,
            host,
            cid8=route.cid8,
            port=route.port,
            cookie_name=route.cookie_name,
            local_gateway=route.local_gateway,
            expected_conversation_id=route.expected_conversation_id,
            expected_owner_id=route.expected_owner_id,
            expected_authority_id=route.expected_authority_id,
            path=path,
        )
        if verified is None:
            return
        cap, cap_owner_id = verified

        if await _apply_canonical_routing(self, scope, receive, send, cap, path):
            return

        canonical_gateway = _is_canonical_gateway(route, cap)
        upstream = await _resolve_upstream(self, route.cid8, route.port, cap_owner_id)

        # Re-check after the (possibly awaited) upstream resolution: a
        # capability's authority can go stale WHILE that resolution is
        # in-flight, and this second check catches exactly that race.
        if await _authority_changed(self, scope, send, cap):
            return

        if not upstream:
            await _respond_missing_upstream(
                self,
                scope,
                send,
                route.cid8,
                route.port,
                cap_owner_id,
                local_gateway=canonical_gateway,
            )
            return

        if scope["type"] == "http":
            await _proxy_http(
                self,
                scope,
                receive,
                send,
                upstream,
                cid8=route.cid8,
                port=route.port,
                owner_id=cap_owner_id,
                local_gateway=canonical_gateway,
            )
        elif scope["type"] == "websocket":
            await _proxy_websocket(self, scope, receive, send, upstream)


def make_preview_session_resolver(
    preview: pp.PreviewSessionAccess | None,
) -> pp.AsyncPreviewSessionResolver:
    """Build the host proxy's read-only cid8 -> live SandboxSession resolver.
    It returns the existing session; None in wire-only tests and never creates one."""

    async def _resolve(cid8: str, owner_id: str | None = None) -> pp.PreviewSession | None:
        if preview is None:
            return None
        try:
            if owner_id is not None:
                cid = await preview.resolve_owned_cid_prefix(cid8, owner_id)
            else:
                cid = preview.resolve_cid_prefix(cid8)
            if not cid:
                return None
            return preview.live_session(cid)
        except Exception:  # noqa: BLE001 — resolver must never raise into the proxy
            return None

    return _resolve

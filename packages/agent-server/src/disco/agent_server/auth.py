"""HTTP/WebSocket auth adapter for the agent-server."""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from disco.core._auth_capability_policy import (
    authenticated_request_refusal,
    conversation_owner_refusal,
    origin_refusal,
    pairing_refusal,
)
from disco.core.auth import (
    CSRF_HEADER,
    ISOLATED_PATH_PREVIEW_PREFIX,
    PATH_PREVIEW_BOOTSTRAP_PATH,
    PREVIEW_BOOTSTRAP_PATH,
    SESSION_COOKIE,
    AuthSession,
    PreviewCapability,
    PreviewCapabilitySigner,
    SessionSigner,
    allowed_frontend_origins,
    configured_admin_token,
    cookie_header_from_headers,
    generated_preview_request_host,
    local_preview_origin_crosses_host,
    origin_allowed,
    origin_permitted,
    pairing_token,
    path_preview_cookie_name,
    path_preview_host_label,
    request_traversed_proxy,
)
from disco.core.env import disco_env
from disco.core.store.sqlite import DEFAULT_OWNER_ID, SqliteEventStore
from disco.tools.sandbox._container import NOVNC_PORT, USER_PORTS
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .host_service_bus import is_bus_route_raw

_LOG = logging.getLogger(__name__)
_CID_RE = re.compile(r"/(conv_[A-Za-z0-9_-]+)(?:/|$)")
_PATH_PREVIEW_RE = re.compile(
    rf"^{re.escape(ISOLATED_PATH_PREVIEW_PREFIX)}/(?P<cid>conv_[A-Za-z0-9_-]+)(?:/|$)"
)
_PATH_PREVIEW_BOOTSTRAP_RE = re.compile(
    rf"^{re.escape(PATH_PREVIEW_BOOTSTRAP_PATH)}/[0-9a-f]{{8}}$", re.IGNORECASE
)
_LEGACY_PATH_PREVIEW_RE = re.compile(
    r"^/conversations/(?P<cid>conv_[A-Za-z0-9_-]+)/preview-app(?:/|$)"
)


def _path_preview_port(conversation_id: str, request_label: str) -> int | None:
    """Return the exact curated port bound into a full-CID p3s host label."""
    for port in sorted(USER_PORTS - {NOVNC_PORT}):
        try:
            expected = path_preview_host_label(conversation_id, port)
        except ValueError:
            return None
        if secrets.compare_digest(expected, request_label.strip().lower()):
            return port
    return None


# Derived LIVE from the shared session secret at each use: identical to the
# app-server's token and stable across restarts, so ONE pasted token pairs both
# origins. See disco.core.auth.pairing_token. Re-usable (secret is the root of
# trust); computed live (not cached at import) so no import-order fragility.
_ADMIN_PREFIXES = (
    "/api/mcp",
    "/api/tts",
    "/api/image-gen",
    "/api/sandbox",
    "/api/encoders",
    "/api/data-sources",
    "/api/role-fallback",
    "/api/projects/storage",
    "/api/live-browser",
    "/api/build-kernel",
)


class MintSessionBody(BaseModel):
    pairing_token: str | None = None


@dataclass(frozen=True)
class _AuthRequestContext:
    path: str
    method: str
    host: str
    isolated_path: bool
    legacy_preview_path: bool
    isolated_preview_host: bool
    bootstrap_cross_host_exchange: bool


def _auth_request_context(request: Request) -> _AuthRequestContext:
    path = request.url.path
    method = request.method.upper()
    host = request.headers.get("host", "")
    return _AuthRequestContext(
        path=path,
        method=method,
        host=host,
        isolated_path=_PATH_PREVIEW_RE.match(path) is not None,
        legacy_preview_path=_LEGACY_PATH_PREVIEW_RE.match(path) is not None,
        isolated_preview_host=generated_preview_request_host(host),
        bootstrap_cross_host_exchange=(
            method == "POST" and _PATH_PREVIEW_BOOTSTRAP_RE.fullmatch(path) is not None
        ),
    )


def _auto_pair_enabled() -> bool:
    raw = disco_env("AUTH_DEV_AUTO_PAIR", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    return host == "testclient" or host == "::1" or host.startswith("127.")


def _is_testclient(request: Request) -> bool:
    if request.client and request.client.host == "testclient":
        return True
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    host = (request.headers.get("host") or "").split(":", 1)[0]
    return host in {"test", "testserver", "t"}


def _test_session(request: Request) -> AuthSession:
    owner_id = request.query_params.get("owner_id") or DEFAULT_OWNER_ID
    return AuthSession(
        owner_id=owner_id,
        csrf_token="test-csrf",
        session_id="test-session",
        expires_at=2**31,
        is_admin=True,
    )


def _is_public_http(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    if path in {
        "/health",
        "/api/auth/session",
        "/api/auth/mint",
        "/api/auth/origins",
        "/api/auth/pairing-token",
    }:
        return True
    if method == "GET" and path.startswith("/share/"):
        return True
    if method == "GET" and path.startswith("/api/share/") and path.endswith("/bundle"):
        return True
    if path.startswith("/api/appkit/cloudflare/"):
        return True
    if path == PREVIEW_BOOTSTRAP_PATH:
        return True
    # A form-navigation POST carries the short-lived signed intent in its body,
    # never its URL. The route atomically consumes that durable JTI before setting
    # a cookie, so it remains public without inheriting an application session.
    if method == "POST" and path.startswith(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/"):
        return True
    return False


def _private_no_store(response: Response) -> Response:
    for header in ("expires", "etag", "last-modified"):
        if header in response.headers:
            del response.headers[header]
    response.headers["cache-control"] = "private, no-store"
    response.headers["pragma"] = "no-cache"
    return response


def _is_admin_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _ADMIN_PREFIXES)


def current_session(request: Request) -> AuthSession:
    session = getattr(request.state, "auth_session", None)
    if isinstance(session, AuthSession):
        return session
    if _is_testclient(request):
        return _test_session(request)
    raise HTTPException(status_code=401, detail={"reason": "auth_required"})


def current_owner_id(request: Request) -> str:
    return current_session(request).owner_id


def require_admin_session(request: Request) -> AuthSession:
    session = current_session(request)
    if not session.is_admin:
        raise HTTPException(status_code=403, detail={"reason": "admin_required"})
    return session


def set_session_cookie(response: Response, token: str, session: AuthSession) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max(1, session.expires_at - int(time.time())),
        httponly=True,
        secure=False,
        samesite="strict",
        path="/",
    )


def _pairing_token_ok(presented: str | None) -> bool:
    # Constant-time compare against the derived token; re-usable by design.
    return bool(presented and secrets.compare_digest(presented, pairing_token()))


def _require_loopback_allowed_origin(request: Request) -> None:
    if not _is_loopback_client(request):
        raise HTTPException(status_code=403, detail={"reason": "loopback_required"})
    origin = request.headers.get("origin")
    if not origin_allowed(origin):
        raise HTTPException(status_code=403, detail={"reason": "origin_not_allowed"})


def _require_loopback_pairing_channel(request: Request) -> None:
    if not _is_loopback_client(request):
        raise HTTPException(status_code=403, detail={"reason": "loopback_required"})
    origin = request.headers.get("origin")
    if origin is not None and not origin_allowed(origin):
        raise HTTPException(status_code=403, detail={"reason": "origin_not_allowed"})


class AgentAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, store: SqliteEventStore) -> None:
        super().__init__(app)
        self._store = store
        self._signer = SessionSigner()
        self._preview_signer = PreviewCapabilitySigner()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        context = _auth_request_context(request)
        preview_refusal = self._preview_quarantine_refusal(request, context)
        if preview_refusal is not None:
            return preview_refusal
        canonical_response = await self._canonical_preview_response(
            request,
            context,
            call_next,
        )
        if canonical_response is not None:
            return canonical_response
        if _is_public_http(context.path, context.method):
            return await call_next(request)
        if is_bus_route_raw(request.scope.get("raw_path", b"")):
            return await call_next(request)
        return await self._dispatch_protected(request, context, call_next)

    def _preview_quarantine_refusal(
        self,
        request: Request,
        context: _AuthRequestContext,
    ) -> Response | None:
        if (
            local_preview_origin_crosses_host(request.headers.get("origin"), context.host)
            and not context.bootstrap_cross_host_exchange
        ):
            # The local path-preview bootstrap intentionally crosses once to its
            # dedicated p3s.*.localhost origin. No other generated-content
            # request may cross hostnames: Host-derived quarantine must still
            # deny it without trusting an attacker-settable marker cookie.
            # This guard is before public auth routes by design.
            return _private_no_store(Response("forbidden preview origin", status_code=403))
        if context.isolated_preview_host and not (
            context.isolated_path
            or context.path.startswith(f"{PATH_PREVIEW_BOOTSTRAP_PATH}/")
            or context.path == PREVIEW_BOOTSTRAP_PATH
        ):
            # Quarantine the versioned generated-content origin before public
            # auth routes or full session cookies are considered. Host proxy is
            # normally the outer gate; this keeps the boundary sound even if
            # middleware ordering changes.
            return _private_no_store(Response("isolated preview route required", status_code=403))
        if (context.isolated_path or context.legacy_preview_path) and (
            request.headers.get("sec-fetch-dest", "").strip().lower() == "serviceworker"
            or request.headers.get("service-worker", "").strip().lower() == "script"
        ):
            return _private_no_store(Response("preview service workers disabled", status_code=403))
        if context.legacy_preview_path:
            # Executable generated bytes must never inherit the application's
            # full session. Canonical callers use the isolated capability route;
            # the deprecated direct path now fails closed.
            return _private_no_store(Response("preview capability required", status_code=403))
        return None

    async def _canonical_preview_response(
        self,
        request: Request,
        context: _AuthRequestContext,
        call_next: RequestResponseEndpoint,
    ) -> Response | None:
        canonical_cap = request.scope.get("state", {}).get("canonical_preview_capability")
        if not context.isolated_path or not isinstance(canonical_cap, PreviewCapability):
            return None
        match = _PATH_PREVIEW_RE.match(context.path)
        assert match is not None
        if (
            canonical_cap.authority_id is None
            or canonical_cap.conversation_id != match.group("cid")
            or context.method not in canonical_cap.http_methods
        ):
            return _private_no_store(Response("preview capability required", status_code=403))
        owner = await self._store.conversation_owner_id(canonical_cap.conversation_id)
        if owner is None:
            return _private_no_store(Response("conversation not found", status_code=404))
        if owner != canonical_cap.owner_id:
            return _private_no_store(Response("conversation forbidden", status_code=403))
        request.state.preview_capability = canonical_cap
        return _private_no_store(await call_next(request))

    async def _dispatch_protected(
        self,
        request: Request,
        context: _AuthRequestContext,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        origin = request.headers.get("origin")
        if context.isolated_path:
            refusal = origin_refusal(origin, context.host)
            if refusal is not None:
                return Response(refusal.response_text, status_code=refusal.status_code)
            _is_path_preview, preview_error = await self._authenticate_path_preview(request)
            if preview_error is not None:
                return _private_no_store(preview_error)
            return _private_no_store(await call_next(request))
        session = self._authenticate_request(request)
        refusal = authenticated_request_refusal(
            session=session,
            origin=origin,
            request_host=context.host,
            method=context.method,
            csrf_token=request.headers.get(CSRF_HEADER),
            admin_required=_is_admin_path(context.path),
            test_client=_is_testclient(request),
        )
        if refusal is not None:
            return Response(refusal.response_text, status_code=refusal.status_code)
        assert session is not None
        request.state.auth_session = session
        owner_check = await self._authorize_conversation_path(request, session)
        if owner_check is not None:
            return owner_check
        return await call_next(request)

    async def _authenticate_path_preview(self, request: Request) -> tuple[bool, Response | None]:
        """Accept only a signed, path-scoped static-preview capability.

        The bool identifies this route family. A ``None`` response means its
        capability authenticated; otherwise the response is the exact refusal.
        """
        match = _PATH_PREVIEW_RE.match(request.url.path)
        if match is None:
            return False, None
        if (
            request.headers.get("sec-fetch-dest", "").strip().lower() == "serviceworker"
            or request.headers.get("service-worker", "").strip().lower() == "script"
        ):
            return True, Response("preview service workers disabled", status_code=403)
        if request.method.upper() != "GET":
            return True, Response("preview capability required", status_code=403)
        conversation_id = match.group("cid")
        cid8 = conversation_id.removeprefix("conv_")[:8]
        request_label = (request.url.hostname or "").split(".", 1)[0].lower()
        port = _path_preview_port(conversation_id, request_label)
        if port is None:
            return True, Response("preview capability required", status_code=403)
        try:
            cookie_name = path_preview_cookie_name(cid8)
        except ValueError:
            return True, Response("preview capability required", status_code=403)
        cap = self._preview_signer.verify_cookie_header(
            cookie_header_from_headers(request.headers),
            cookie_name,
            cid8=cid8,
            port=port,
            method="GET",
            path=request.url.path,
        )
        if cap is None or cap.conversation_id != conversation_id:
            return True, Response("preview capability required", status_code=403)
        owner = await self._store.conversation_owner_id(conversation_id)
        if owner is None:
            return True, Response("conversation not found", status_code=404)
        if owner != cap.owner_id:
            return True, Response("conversation forbidden", status_code=403)
        request.state.preview_capability = cap
        return True, None

    def _authenticate_request(self, request: Request) -> AuthSession | None:
        session = self._signer.verify_cookie_header(cookie_header_from_headers(request.headers))
        if session is not None:
            return session
        if _is_testclient(request):
            return _test_session(request)
        return None

    async def _authorize_conversation_path(
        self, request: Request, session: AuthSession
    ) -> Response | None:
        match = _CID_RE.search(request.url.path)
        if match is None:
            return None
        cid = match.group(1)
        owner = await self._store.conversation_owner_id(cid)
        refusal = conversation_owner_refusal(session, owner)
        if refusal is not None:
            return Response(refusal.response_text, status_code=refusal.status_code)
        return None


def make_auth_router() -> APIRouter:
    router = APIRouter()
    signer = SessionSigner()
    if _auto_pair_enabled():
        _LOG.info("Disco loopback auto-pair enabled; pairing token omitted from logs")
    else:
        _LOG.info("Disco pairing token (derived, shared across servers): %s", pairing_token())

    @router.post("/api/auth/mint")
    async def mint_session(body: MintSessionBody, request: Request, response: Response) -> dict:
        # See app_server.auth for the full rationale: origin must be allowed;
        # a valid token pairs from ANY allowed origin (remote self-host); a
        # tokenless client gets pairing_required unless it's a loopback dev
        # client with auto-pair on. (2026-07-09 remote fresh-install fix.)
        # Same-host origins (the front-door deploy) are permitted: minting there
        # still requires the operator's TOKEN, so a DNS-rebound page gains nothing.
        origin = request.headers.get("origin")
        refusal = pairing_refusal(
            token_valid=_pairing_token_ok(body.pairing_token),
            origin=origin,
            request_host=request.headers.get("host"),
            loopback_client=_is_loopback_client(request),
            auto_pair_enabled=_auto_pair_enabled(),
            traversed_proxy=request_traversed_proxy(request.headers),
        )
        if refusal is not None:
            raise HTTPException(
                status_code=refusal.status_code,
                detail={"reason": refusal.reason},
            )
        cookie, session = signer.mint(owner_id=DEFAULT_OWNER_ID, is_admin=True)
        set_session_cookie(response, cookie, session)
        return {
            "ok": True,
            "owner_id": session.owner_id,
            "csrf_token": session.csrf_token,
            "admin": session.is_admin,
        }

    @router.get("/api/auth/pairing-token")
    async def pairing_token_route(request: Request) -> dict:
        # Loopback-only convenience (see app_server.auth); remote browsers paste
        # the token from the server logs into the pairing prompt instead.
        _require_loopback_pairing_channel(request)
        return {"pairing_token": pairing_token()}

    @router.get("/api/auth/session")
    async def auth_session(request: Request) -> dict:
        session = signer.verify_cookie_header(cookie_header_from_headers(request.headers))
        if session is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "owner_id": session.owner_id,
            "csrf_token": session.csrf_token,
            "admin": session.is_admin,
        }

    @router.get("/api/auth/origins")
    async def auth_origins() -> dict:
        token = configured_admin_token()
        return {"origins": list(allowed_frontend_origins()), "admin_token_configured": bool(token)}

    return router


def websocket_session(websocket: WebSocket) -> AuthSession | None:
    client_host = websocket.client.host if websocket.client is not None else ""
    server_host = ""
    if websocket.scope.get("server"):
        server_host = str(websocket.scope["server"][0])
    origin = websocket.headers.get("origin")
    ws_host = websocket.headers.get("host")
    if local_preview_origin_crosses_host(origin, ws_host):
        return None
    if generated_preview_request_host(ws_host):
        # Generated hosts authenticate only with their conversation-scoped
        # preview capability below; child-domain cookies never select trust.
        return None
    # allowlist OR same-host (the single-front-door deploy: Origin == Host).
    if origin and not origin_permitted(origin, ws_host):
        return None
    signer = SessionSigner()
    session = signer.verify_cookie_header(cookie_header_from_headers(websocket.headers))
    if session is not None:
        if not origin and client_host != "testclient":
            return None
        return session
    if client_host == "testclient" or (
        os.environ.get("PYTEST_CURRENT_TEST") and server_host in {"testserver", "test", "t"}
    ):
        return AuthSession(DEFAULT_OWNER_ID, "test-csrf", "test-session", 2**31, True)
    if not origin_permitted(origin, ws_host):
        return None
    return signer.verify_cookie_header(cookie_header_from_headers(websocket.headers))

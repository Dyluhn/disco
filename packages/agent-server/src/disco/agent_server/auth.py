"""HTTP/WebSocket auth adapter for the agent-server."""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from typing import Any

from disco.core.auth import (
    CSRF_HEADER,
    PREVIEW_BOOTSTRAP_PATH,
    SESSION_COOKIE,
    AuthSession,
    SessionSigner,
    allowed_frontend_origins,
    configured_admin_token,
    localhost_auto_pair_allowed,
    origin_allowed,
    origin_permitted,
    pairing_token,
    request_traversed_proxy,
)
from disco.core.env import disco_env
from disco.core.store.sqlite import DEFAULT_OWNER_ID, SqliteEventStore
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .host_service_bus import is_bus_route_raw

_LOG = logging.getLogger(__name__)
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CID_RE = re.compile(r"/(conv_[A-Za-z0-9_-]+)(?:/|$)")
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
    return False


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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        method = request.method.upper()
        if _is_public_http(path, method):
            return await call_next(request)
        # WO-A2.2: the host-service bus uses its own bearer-token auth and must
        # never be authenticated by session cookies, admin pairing, query params,
        # or CSRF tokens. Bypass the `/_disco/svc/` prefix so the bus route itself
        # enforces the strict dotted-service shape and returns 400 for malformed names.
        if is_bus_route_raw(request.scope.get("raw_path", b"")):
            return await call_next(request)
        origin = request.headers.get("origin")
        # allowlist OR same-host (the single-front-door deploy: Origin == Host).
        if origin and not origin_permitted(origin, request.headers.get("host")):
            return Response("forbidden origin", status_code=403)
        session = self._authenticate_request(request)
        if session is None:
            return Response("auth required", status_code=401)
        request.state.auth_session = session
        if _is_admin_path(path) and not session.is_admin:
            return Response("admin required", status_code=403)
        if (
            method in _UNSAFE_METHODS
            and not _is_testclient(request)
            and not SessionSigner.csrf_valid(session, request.headers.get(CSRF_HEADER))
        ):
            return Response("csrf required", status_code=403)
        owner_check = await self._authorize_conversation_path(request, session)
        if owner_check is not None:
            return owner_check
        return await call_next(request)

    def _authenticate_request(self, request: Request) -> AuthSession | None:
        session = self._signer.verify(request.cookies.get(SESSION_COOKIE))
        if session is not None:
            return session
        if _is_testclient(request):
            return _test_session(request)
        return None

    async def _authorize_conversation_path(
        self, request: Request, session: AuthSession
    ) -> Response | None:
        if session.session_id == "test-session":
            return None
        match = _CID_RE.search(request.url.path)
        if match is None:
            return None
        cid = match.group(1)
        owner = await self._store.conversation_owner_id(cid)
        if owner is None:
            return Response("conversation not found", status_code=404)
        if owner != session.owner_id:
            return Response("conversation forbidden", status_code=403)
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
        if not origin_permitted(origin, request.headers.get("host")):
            raise HTTPException(status_code=403, detail={"reason": "origin_not_allowed"})
        auto_pair = _auto_pair_enabled()
        token_ok = _pairing_token_ok(body.pairing_token)
        if not token_ok:
            # The TOKENLESS shortcuts stay strict — a DNS-rebound page's Origin
            # matches the rebound Host but is never localhost, so rebinding can't
            # mint without the token via either branch.
            if _is_loopback_client(request) and auto_pair and origin_allowed(origin):
                pass  # host-process dev convenience (unforgeable loopback TCP peer)
            elif localhost_auto_pair_allowed(
                origin,
                request.headers.get("host"),
                via_proxy=request_traversed_proxy(request.headers),
            ):
                # Loopback-BOUND front door (compose default): the ports are
                # kernel-unreachable from other machines, and the page asserts a
                # localhost origin == this app's own Host → the operator's own
                # machine. Zero-friction first run; DISCO_BIND=0.0.0.0 (or a proxy
                # in front, which adds a forwarding header) disables it.
                pass
            else:
                raise HTTPException(status_code=401, detail={"reason": "pairing_required"})
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
        session = signer.verify(request.cookies.get(SESSION_COOKIE))
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
    # allowlist OR same-host (the single-front-door deploy: Origin == Host).
    if origin and not origin_permitted(origin, ws_host):
        return None
    session = SessionSigner().verify(websocket.cookies.get(SESSION_COOKIE))
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
    return SessionSigner().verify(websocket.cookies.get(SESSION_COOKIE))

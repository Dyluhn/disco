from __future__ import annotations

import pytest
from disco.agent_server import auth as agent_auth
from disco.agent_server.app import create_app as create_agent_app
from disco.agent_server.auth import AgentAuthMiddleware
from disco.app_server import auth as app_auth
from disco.app_server.app import create_app as create_app_server
from disco.app_server.auth import _ADMIN_PREFIXES, AppAuthMiddleware, _is_admin_path
from disco.core.auth import (
    CSRF_HEADER,
    PATH_PREVIEW_BOOTSTRAP_PATH,
    SESSION_COOKIE,
    SessionSigner,
)
from disco.core.store.sqlite import SqliteEventStore
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FRONTEND_ORIGIN = "http://localhost:5173"
EVIL_ORIGIN = "https://evil.example"
pytestmark = pytest.mark.integration


def _routes(app) -> list[APIRoute | APIWebSocketRoute]:
    return [r for r in app.routes if isinstance(r, (APIRoute, APIWebSocketRoute))]


def _middleware_classes(app) -> set[type]:
    return {mw.cls for mw in app.user_middleware}


def _cors_kwargs(app) -> dict:
    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:
            return dict(mw.kwargs)
    raise AssertionError("CORSMiddleware is not installed")


def _agent_public(path: str, methods: set[str]) -> bool:
    if path in {
        "/health",
        "/api/auth/session",
        "/api/auth/mint",
        "/api/auth/origins",
        "/api/auth/pairing-token",
    }:
        return True
    if "GET" in methods and path == "/share/{token}":
        return True
    if "GET" in methods and path == "/api/share/{token}/bundle":
        return True
    if path.startswith("/api/appkit/cloudflare/"):
        return True
    # Token-public, not anonymous access: the isolated preview origin cannot
    # carry the application session. The route exchanges a signed one-use
    # intent for a narrowly scoped preview cookie and rejects missing/invalid
    # intents. Its capability enforcement is asserted explicitly below.
    if "GET" in methods and path == f"{PATH_PREVIEW_BOOTSTRAP_PATH}/{{cid8}}":
        return True
    return False


def _app_public(path: str) -> bool:
    return path in {
        "/api/health",
        "/api/auth/session",
        "/api/auth/mint",
        "/api/auth/origins",
        "/api/auth/pairing-token",
    }


def _agent_capability_or_session(path: str, methods: set[str]) -> bool:
    return "GET" in methods and path.startswith(
        "/conversations/{conversation_id}/preview-app/"
    )


def _route_class(path: str, methods: set[str]) -> str:
    if "conv_" in path or "{conversation_id}" in path:
        return "owner"
    if methods & _UNSAFE:
        return "authenticated+csrf"
    return "authenticated"


def _install_session(client: TestClient, *, owner_id: str, admin: bool) -> str:
    token, session = SessionSigner().mint(owner_id=owner_id, is_admin=admin)
    client.cookies.set(SESSION_COOKIE, token)
    client.headers.update({"Cookie": f"{SESSION_COOKIE}={token}"})
    return session.csrf_token


def _sample_path(path: str) -> str:
    replacements = {
        "{conversation_id}": "conv_owner_a",
        "{path:path}": "x",
        "{port}": "8000",
        "{seq}": "1",
        "{schedule_id}": "sched_missing",
        "{instance_id}": "wf_missing",
        "{token}": "tok_missing",
        "{name}": "main",
        "{kernel_id}": "kernel_missing",
        "{secret_name}": "secret_missing",
        "{model_key}": "model_missing",
        "{server_name}": "server_missing",
        "{service:path}": "svc.ping",
        "{space_id}": "space_missing",
    }
    concrete = path
    for pattern, value in replacements.items():
        concrete = concrete.replace(pattern, value)
    return concrete


def _request(client: TestClient, method: str, path: str, *, headers: dict | None = None):
    body = {} if method in _UNSAFE else None
    return client.request(method, path, headers=headers, json=body)


def test_agent_route_inventory_is_globally_auth_gated() -> None:
    app = create_agent_app(SqliteEventStore(":memory:"), runtime=None)
    assert AgentAuthMiddleware in _middleware_classes(app)
    cors = _cors_kwargs(app)
    assert cors["allow_credentials"] is True
    assert "*" not in cors["allow_origins"]

    inventory: list[tuple[str, tuple[str, ...], str]] = []
    for route in _routes(app):
        path = route.path
        methods = set(getattr(route, "methods", set()) or set())
        if isinstance(route, APIWebSocketRoute):
            continue
        if not _agent_public(path, methods):
            inventory.append((path, tuple(sorted(methods)), _route_class(path, methods)))
    assert inventory, "inventory must enumerate protected routes"
    assert not [row for row in inventory if row[2] == "public"]
    assert any(path == "/conversations/{conversation_id}/events" for path, _, _ in inventory)
    assert any(
        path == "/api/projects/{conversation_id}/manifest" and klass == "owner"
        for path, _, klass in inventory
    )
    assert not [
        path
        for path, methods, _ in inventory
        if path.endswith("/browser/live-url") and "GET" in methods
    ]


def test_agent_route_inventory_actual_enforcement(monkeypatch) -> None:
    monkeypatch.setattr(agent_auth, "_is_testclient", lambda _request: False)
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_owner_a", owner_id="owner-a")
    app = create_agent_app(store, runtime=None)
    unauth = TestClient(app)
    owner_a = TestClient(app)
    owner_b = TestClient(app)
    _csrf_a = _install_session(owner_a, owner_id="owner-a", admin=True)
    csrf_b = _install_session(owner_b, owner_id="owner-b", admin=True)

    checked = 0
    for route in _routes(app):
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        methods = set(getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        if not methods or _agent_public(path, methods):
            continue
        method = sorted(methods)[0]
        concrete = _sample_path(path)
        if path == "/_disco/svc/{service:path}":
            missing = _request(unauth, method, concrete)
            evil_origin = _request(unauth, method, concrete, headers={"Origin": EVIL_ORIGIN})
            assert missing.status_code == evil_origin.status_code == 401
            checked += 1
            continue

        unauth_resp = _request(unauth, method, concrete)
        expected_unauth = 403 if _agent_capability_or_session(path, methods) else 401
        assert unauth_resp.status_code == expected_unauth, (
            method,
            path,
            unauth_resp.status_code,
        )

        bad_origin = _request(unauth, method, concrete, headers={"Origin": EVIL_ORIGIN})
        assert bad_origin.status_code == 403, (method, path, bad_origin.status_code)

        if method in _UNSAFE:
            no_csrf = _request(owner_a, method, concrete)
            assert no_csrf.status_code == 403, (method, path, no_csrf.status_code)

        if "{conversation_id}" in path:
            headers = {"Origin": FRONTEND_ORIGIN}
            if method in _UNSAFE:
                headers[CSRF_HEADER] = csrf_b
            cross_owner = _request(owner_b, method, concrete, headers=headers)
            assert cross_owner.status_code in {403, 404}, (
                method,
                path,
                cross_owner.status_code,
            )
        checked += 1

    assert checked > 20

    host_proxy = unauth.get("/", headers={"host": "abcdef12-8000.localhost"})
    assert host_proxy.status_code == 403
    raw_prefix = owner_b.get("/conversations/abcdef12/preview-app/")
    assert raw_prefix.status_code == 404

    # Public in the session inventory does not mean open: without a valid
    # signed intent, the alternate-origin bootstrap must fail closed and must
    # not set either an app session or preview capability cookie.
    invalid_bootstrap = unauth.get(
        f"{PATH_PREVIEW_BOOTSTRAP_PATH}/deadbeef", follow_redirects=False
    )
    assert invalid_bootstrap.status_code == 403
    assert "location" not in invalid_bootstrap.headers
    assert "set-cookie" not in invalid_bootstrap.headers


def test_agent_websocket_inventory_has_handshake_auth() -> None:
    app = create_agent_app(SqliteEventStore(":memory:"), runtime=None)
    ws_paths = sorted(route.path for route in _routes(app) if isinstance(route, APIWebSocketRoute))
    assert "/ws/conversations/{conversation_id}" in ws_paths
    assert "/ws/research" in ws_paths
    assert "/conversations/{conversation_id}/preview-app/{path:path}" in ws_paths
    assert "/conversations/{conversation_id}/port/{port}/{path:path}" in ws_paths


def test_agent_websocket_inventory_actual_enforcement() -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_owner_a", owner_id="owner-a")
    app = create_agent_app(store, runtime=None)
    owner_b = TestClient(app)
    _install_session(owner_b, owner_id="owner-b", admin=True)

    with pytest.raises(WebSocketDisconnect):
        with owner_b.websocket_connect(
            "/ws/conversations/conv_owner_a",
            headers={"Origin": FRONTEND_ORIGIN},
        ):
            pass

    with pytest.raises(WebSocketDisconnect):
        with owner_b.websocket_connect(
            "/conversations/abcdef12/preview-app/",
            headers={"Origin": FRONTEND_ORIGIN},
        ):
            pass

    with pytest.raises(WebSocketDisconnect):
        with owner_b.websocket_connect(
            "/conversations/conv_owner_a/port/8000/",
            headers={"Origin": FRONTEND_ORIGIN},
        ):
            pass


def test_app_route_inventory_is_auth_gated_and_admin_classified() -> None:
    app = create_app_server(SqliteEventStore(":memory:"))
    assert AppAuthMiddleware in _middleware_classes(app)
    cors = _cors_kwargs(app)
    assert cors["allow_credentials"] is True
    assert "*" not in cors["allow_origins"]

    route_paths = [route.path for route in _routes(app)]
    protected = [p for p in route_paths if not _app_public(p)]
    assert protected, "inventory must enumerate protected app routes"
    admin_routes = [p for p in protected if _is_admin_path(p)]
    assert admin_routes, "inventory must classify admin-global settings routes"
    assert all(
        any(p == prefix or p.startswith(prefix + "/") for prefix in _ADMIN_PREFIXES)
        for p in admin_routes
    )
    assert "/api/skills" in admin_routes
    assert "/api/mcp" in admin_routes


def test_app_route_inventory_actual_enforcement(monkeypatch) -> None:
    monkeypatch.setattr(app_auth, "_is_testclient", lambda _request: False)
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_owner_a", owner_id="owner-a")
    app = create_app_server(store)
    unauth = TestClient(app)
    owner_a = TestClient(app)
    owner_b = TestClient(app)
    non_admin = TestClient(app)
    _csrf_a = _install_session(owner_a, owner_id="owner-a", admin=True)
    csrf_b = _install_session(owner_b, owner_id="owner-b", admin=True)
    _install_session(non_admin, owner_id="owner-a", admin=False)

    checked = 0
    for route in _routes(app):
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        methods = set(getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
        if not methods or _app_public(path):
            continue
        method = sorted(methods)[0]
        concrete = _sample_path(path)

        unauth_resp = _request(unauth, method, concrete)
        assert unauth_resp.status_code == 401, (method, path, unauth_resp.status_code)

        bad_origin = _request(unauth, method, concrete, headers={"Origin": EVIL_ORIGIN})
        assert bad_origin.status_code == 403, (method, path, bad_origin.status_code)

        if method in _UNSAFE:
            no_csrf = _request(owner_a, method, concrete)
            assert no_csrf.status_code == 403, (method, path, no_csrf.status_code)

        if _is_admin_path(path):
            forbidden = _request(non_admin, method, concrete)
            assert forbidden.status_code == 403, (method, path, forbidden.status_code)

        if "{conversation_id}" in path:
            headers = {"Origin": FRONTEND_ORIGIN}
            if method in _UNSAFE:
                headers[CSRF_HEADER] = csrf_b
            cross_owner = _request(owner_b, method, concrete, headers=headers)
            assert cross_owner.status_code in {403, 404}, (
                method,
                path,
                cross_owner.status_code,
            )
        checked += 1

    assert checked > 10


def test_app_global_settings_are_admin_only() -> None:
    app = create_app_server(SqliteEventStore(":memory:"))
    client = TestClient(app)
    csrf = _install_session(client, owner_id="owner-b", admin=False)

    read = client.get("/api/skills")
    write = client.post(
        "/api/skills",
        headers={"X-Disco-CSRF": csrf},
        json={"name": "global", "description": "x", "instructions": "x"},
    )

    assert read.status_code == 403
    assert write.status_code == 403

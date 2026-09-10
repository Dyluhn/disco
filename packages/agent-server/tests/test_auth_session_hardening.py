"""Agent-server session-boundary hardening.

SEC-C1  the in-process TestClient shim is gated on an actual pytest run, so a
        forwarding header cannot conjure an admin session.
SEC-C2  the public /api/auth/session hands the CSRF token only to a same-origin
        or permitted-origin caller.
SEC-6a  the session cookie's Secure flag follows the https signal.
"""

from __future__ import annotations

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server import auth as agent_auth
from disco.core.auth import SESSION_COOKIE, SessionSigner
from disco.core.llm import ConfigStore, RouterConfig
from disco.core.store.sqlite import SqliteEventStore
from fastapi.testclient import TestClient

FRONT_DOOR = "http://localhost:8088"


@pytest.fixture
def client() -> TestClient:
    store = SqliteEventStore(":memory:")
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    from pathlib import Path

    cfg_store = ConfigStore(path=Path("/dev/null"))
    cfg_store.load = lambda: cfg  # type: ignore[method-assign]
    runtime = ConversationRuntime(store, config=cfg, config_store=cfg_store)
    return TestClient(create_app(store, runtime=runtime))


# ---- C1: the testclient shim is not reachable from the wire ------------------


def test_testclient_shim_requires_a_real_pytest_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`request.client.host` is scope["client"], which uvicorn rewrites from
    X-Forwarded-For without checking it is an IP. Sending
    `X-Forwarded-For: testclient` therefore used to satisfy the shim — which
    mints `is_admin=True` with an attacker-chosen owner_id and a session_id that
    short-circuits the owner, CSRF and conversation-ownership checks."""
    with_shim = client.get("/api/conversations")
    assert with_shim.status_code == 200  # the shim works inside the test run

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    spoofed = client.get("/api/conversations?owner_id=victim")
    assert spoofed.status_code == 401


def test_loopback_shortcut_also_requires_a_real_pytest_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same spoofable value satisfied `_is_loopback_client`, which un-gates
    the pairing-token route — a DURABLE admin credential."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    fake = type("_C", (), {"host": "testclient"})()
    request = type("_R", (), {"client": fake})()
    assert not agent_auth._is_loopback_client(request)  # type: ignore[arg-type]
    assert not agent_auth._is_testclient(request)  # type: ignore[arg-type]


def test_real_loopback_peer_is_still_recognised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    for host in ("127.0.0.1", "127.0.0.53", "::1"):
        fake = type("_C", (), {"host": host})()
        request = type("_R", (), {"client": fake})()
        assert agent_auth._is_loopback_client(request)  # type: ignore[arg-type]


# ---- C2: the CSRF token is not readable by any allowlisted local page --------


def test_auth_session_returns_csrf_to_the_front_door(client: TestClient) -> None:
    token, session = SessionSigner().mint(owner_id="local", is_admin=True)
    client.cookies.set(SESSION_COOKIE, token)

    same_origin = client.get("/api/auth/session", headers={"Host": "localhost:8088"})
    assert same_origin.json()["csrf_token"] == session.csrf_token

    front_door = client.get(
        "/api/auth/session",
        headers={"Origin": FRONT_DOOR, "Host": "localhost:8088"},
    )
    assert front_door.json()["csrf_token"] == session.csrf_token


def test_auth_session_withholds_csrf_from_an_unpermitted_origin(client: TestClient) -> None:
    """Layer under CORS: SameSite is site-scoped, not port-scoped, so any page
    on another localhost port is same-site and its credentialed fetch carries
    the cookie. It must still learn nothing it could sign a write with."""
    token, _session = SessionSigner().mint(owner_id="local", is_admin=True)
    client.cookies.set(SESSION_COOKIE, token)

    res = client.get(
        "/api/auth/session",
        headers={"Origin": "http://localhost:5173", "Host": "localhost:8000"},
    )

    body = res.json()
    assert body["authenticated"] is True
    assert "csrf_token" not in body


# ---- 6a: the Secure flag follows the https signal ----------------------------


def _set_cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


def test_mint_cookie_is_not_secure_on_plain_localhost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCO_PUBLIC_UI_URL", raising=False)
    monkeypatch.setenv("DISCO_BIND", "127.0.0.1")
    res = client.post(
        "/api/auth/mint",
        headers={"Origin": FRONT_DOOR, "Host": "localhost:8088"},
        json={},
    )
    assert res.status_code == 200
    assert "secure" not in _set_cookie_header(res).lower()


def test_mint_cookie_is_secure_behind_a_tls_front_door(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCO_PUBLIC_UI_URL", raising=False)
    monkeypatch.setenv("DISCO_BIND", "127.0.0.1")
    res = client.post(
        "/api/auth/mint",
        headers={
            "Origin": FRONT_DOOR,
            "Host": "localhost:8088",
            "X-Forwarded-Proto": "https",
        },
        json={},
    )
    assert res.status_code == 200
    assert "secure" in _set_cookie_header(res).lower()

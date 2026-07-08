from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from disco.agent_server import auth as agent_auth
from disco.agent_server.app import create_app
from disco.core.auth import SESSION_COOKIE
from disco.core.store.sqlite import DEFAULT_OWNER_ID, SqliteEventStore


ALLOWED_ORIGIN = "http://remote.example:8088"
OTHER_ALLOWED_ORIGIN = "http://tailnet.example:8088"
EVIL_ORIGIN = "https://evil.example"
REMOTE_HOST = "172.18.0.1"
LOOPBACK_HOST = "127.0.0.1"


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("DISCO_AUTH_SECRET", "agent-remote-pairing-test-secret")
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DISCO_FRONTEND_ORIGINS", ALLOWED_ORIGIN)
    monkeypatch.delenv("DISCO_AUTH_DEV_AUTO_PAIR", raising=False)
    monkeypatch.setattr(agent_auth, "_PAIRING_TOKEN_CONSUMED", False)
    yield
    monkeypatch.setattr(agent_auth, "_PAIRING_TOKEN_CONSUMED", False)


def _client(host: str) -> TestClient:
    return TestClient(
        create_app(SqliteEventStore(":memory:"), runtime=None),
        client=(host, 50000),
    )


def _reason(response) -> str:
    return response.json()["detail"]["reason"]


def test_remote_valid_pairing_token_mints_admin_session_and_consumes_token() -> None:
    client = _client(REMOTE_HOST)
    token = agent_auth._PAIRING_TOKEN

    minted = client.post(
        "/api/auth/mint",
        headers={"Origin": ALLOWED_ORIGIN},
        json={"pairing_token": token},
    )

    assert minted.status_code == 200
    assert minted.json()["owner_id"] == DEFAULT_OWNER_ID
    assert minted.json()["admin"] is True
    assert SESSION_COOKIE in minted.headers.get("set-cookie", "")
    assert agent_auth._PAIRING_TOKEN_CONSUMED is True

    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert session.json()["admin"] is True

    second = client.post(
        "/api/auth/mint",
        headers={"Origin": ALLOWED_ORIGIN},
        json={"pairing_token": token},
    )
    assert second.status_code == 403
    assert _reason(second) == "loopback_required"


def test_remote_no_token_auto_pair_is_still_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_AUTH_DEV_AUTO_PAIR", "1")
    client = _client(REMOTE_HOST)

    response = client.post(
        "/api/auth/mint",
        headers={"Origin": ALLOWED_ORIGIN},
        json={},
    )

    assert response.status_code == 403
    assert _reason(response) == "loopback_required"


def test_remote_valid_token_with_disallowed_origin_is_rejected_without_consuming() -> None:
    client = _client(REMOTE_HOST)

    response = client.post(
        "/api/auth/mint",
        headers={"Origin": EVIL_ORIGIN},
        json={"pairing_token": agent_auth._PAIRING_TOKEN},
    )

    assert response.status_code == 403
    assert _reason(response) == "origin_not_allowed"
    assert agent_auth._PAIRING_TOKEN_CONSUMED is False


def test_loopback_auto_pair_no_token_still_mints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_AUTH_DEV_AUTO_PAIR", "1")
    client = _client(LOOPBACK_HOST)

    response = client.post("/api/auth/mint", json={})

    assert response.status_code == 200
    assert response.json()["admin"] is True


def test_loopback_no_token_without_auto_pair_still_requires_pairing() -> None:
    client = _client(LOOPBACK_HOST)

    response = client.post("/api/auth/mint", json={})

    assert response.status_code == 401
    assert _reason(response) == "pairing_required"


def test_pairing_token_endpoint_stays_loopback_only() -> None:
    client = _client(REMOTE_HOST)

    response = client.get("/api/auth/pairing-token")

    assert response.status_code == 403
    assert _reason(response) == "loopback_required"


def test_frontend_origins_round_trip_allows_listed_origin_and_rejects_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DISCO_FRONTEND_ORIGINS",
        f"{ALLOWED_ORIGIN}, {OTHER_ALLOWED_ORIGIN}/",
    )
    client = _client(REMOTE_HOST)

    origins = client.get("/api/auth/origins")
    assert origins.status_code == 200
    assert origins.json()["origins"] == [ALLOWED_ORIGIN, OTHER_ALLOWED_ORIGIN]

    rejected = client.post(
        "/api/auth/mint",
        headers={"Origin": EVIL_ORIGIN},
        json={"pairing_token": agent_auth._PAIRING_TOKEN},
    )
    assert rejected.status_code == 403
    assert _reason(rejected) == "origin_not_allowed"
    assert agent_auth._PAIRING_TOKEN_CONSUMED is False

    accepted = client.post(
        "/api/auth/mint",
        headers={"Origin": OTHER_ALLOWED_ORIGIN},
        json={"pairing_token": agent_auth._PAIRING_TOKEN},
    )
    assert accepted.status_code == 200

"""Generated path previews cannot regain App credentials across loopback aliases."""

from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.core.auth import (
    PATH_PREVIEW_ISOLATION_COOKIE,
    SESSION_COOKIE,
    SessionSigner,
)
from disco.core.store.sqlite import SqliteEventStore
from fastapi.testclient import TestClient


def _owner_client(app, *, base_url: str) -> TestClient:
    client = TestClient(app, base_url=base_url)
    token, _session = SessionSigner().mint(owner_id="owner-a", is_admin=True)
    client.cookies.set(SESSION_COOKIE, token)
    client.headers.update({"Cookie": f"{SESSION_COOKIE}={token}"})
    return client


def test_app_auth_blocks_same_host_marker_and_cross_alias_session_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "preview-app-quarantine-secret")
    monkeypatch.setattr("disco.app_server.auth._is_testclient", lambda _request: False)
    store = SqliteEventStore(":memory:")
    app = create_app(store)
    owner = _owner_client(app, base_url="http://127.0.0.1:8800")

    # Generated content on localhost cannot target the original 127 hostname,
    # even when that request carries the operator's valid admin session.
    for request_host in ("127.0.0.1:8800", "lvh.me:8800"):
        headers = {"Origin": "http://localhost:8088", "Host": request_host}
        for path in (
            "/api/auth/session",
            "/api/auth/pairing-token",
            "/api/models",
            "/api/secrets",
        ):
            denied = owner.get(path, headers=headers)
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == "private, no-store"
        denied_mint = owner.post(
            "/api/auth/mint",
            headers=headers,
            json={"pairing_token": None},
        )
        assert denied_mint.status_code == 403

    # Same hostname on the normal split frontend/server ports remains valid.
    same_host = {"Origin": "http://127.0.0.1:5173", "Host": "127.0.0.1:8800"}
    session = owner.get("/api/auth/session", headers=same_host)
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert owner.get("/api/models", headers=same_host).status_code == 200

    # On the preview hostname itself, the durable quarantine marker blocks all
    # App APIs, including public credential grants and admin/settings routes.
    marked = _owner_client(app, base_url="http://localhost:8800")
    marked.headers.update(
        {
            "Cookie": (
                f"{SESSION_COOKIE}={marked.cookies.get(SESSION_COOKIE)}; "
                f"{PATH_PREVIEW_ISOLATION_COOKIE}=1"
            )
        }
    )
    for path in (
        "/api/auth/session",
        "/api/auth/pairing-token",
        "/api/models",
        "/api/secrets",
    ):
        assert marked.get(path, headers={"Origin": "http://localhost:8088"}).status_code == 403
    assert (
        marked.post(
            "/api/auth/mint",
            headers={"Origin": "http://localhost:8088"},
            json={"pairing_token": None},
        ).status_code
        == 403
    )
    store.close()

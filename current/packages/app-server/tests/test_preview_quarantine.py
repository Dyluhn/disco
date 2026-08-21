"""Generated path previews cannot regain App credentials across loopback aliases."""

from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.core.auth import (
    SESSION_COOKIE,
    SessionSigner,
)
from disco.core.store.sqlite import SqliteEventStore
from fastapi.testclient import TestClient

_TOSSED_MARKER_COOKIE = "disco_path_preview_isolated"


def _owner_client(app, *, base_url: str) -> TestClient:
    client = TestClient(app, base_url=base_url)
    token, _session = SessionSigner().mint(owner_id="owner-a", is_admin=True)
    client.cookies.set(SESSION_COOKIE, token)
    client.headers.update({"Cookie": f"{SESSION_COOKIE}={token}"})
    return client


def test_app_auth_ignores_tossed_marker_and_blocks_generated_host_or_cross_alias(
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

    # Same hostname on the normal split current/frontend/server ports remains valid.
    same_host = {"Origin": "http://127.0.0.1:8088", "Host": "127.0.0.1:8800"}
    session = owner.get("/api/auth/session", headers=same_host)
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert owner.get("/api/models", headers=same_host).status_code == 200

    # H148: a child-domain marker is not a trust signal, and an invalid duplicate
    # session cannot shadow the valid HostOnly signed session on the parent app.
    marked = _owner_client(app, base_url="http://localhost:8800")
    valid_session = marked.cookies.get(SESSION_COOKIE)
    assert valid_session
    for cookie_header in (
        (
            f"{SESSION_COOKIE}=child-domain-invalid; {_TOSSED_MARKER_COOKIE}=1; "
            f"{SESSION_COOKIE}={valid_session}"
        ),
        (
            f"{SESSION_COOKIE}={valid_session}; {_TOSSED_MARKER_COOKIE}=1; "
            f"{SESSION_COOKIE}=child-domain-invalid"
        ),
    ):
        marked.headers.update({"Cookie": cookie_header})
        tossed_session = marked.get(
            "/api/auth/session", headers={"Origin": "http://localhost:8088"}
        )
        assert tossed_session.status_code == 200
        assert tossed_session.json()["authenticated"] is True
        assert (
            marked.get("/api/models", headers={"Origin": "http://localhost:8088"}).status_code
            == 200
        )

    # Exact legacy/p2/p3s generated Hosts remain quarantined without the removed
    # marker, while malformed lookalikes retain the normal app session.
    for generated_host in (
        "a1b2c3d4-8000.example",
        "p2-a1b2c3d4-8000.example",
        "p3s-a1b2c3d4-" + "b" * 40 + "-8000.example",
    ):
        generated = _owner_client(app, base_url=f"https://{generated_host}")
        for path in (
            "/api/auth/session",
            "/api/auth/pairing-token",
            "/api/models",
            "/api/secrets",
        ):
            denied = generated.get(path)
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == "private, no-store"
    for normal_host in (
        "p2-a1b2c3d4-0.example",
        "p2-nothex123-8000.example",
        "p3s-a1b2c3d4-short-8000.example",
    ):
        normal = _owner_client(app, base_url=f"https://{normal_host}")
        normal_session = normal.get("/api/auth/session")
        assert normal_session.status_code == 200
        assert normal_session.json()["authenticated"] is True
    store.close()

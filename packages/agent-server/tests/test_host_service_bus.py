"""Route tests for the WO-A2.2 authenticated host-service bus."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from disco.agent_server import create_app
from disco.agent_server.host_service_bus import (
    _MAX_BODY_BYTES,
    _MAX_RESPONSE_BYTES,
)
from disco.agent_server.host_token_store import HostTokenStore, TokenStoreClosed
from disco.core import SqliteEventStore
from disco.core.auth import CSRF_HEADER, SESSION_COOKIE, SessionSigner
from disco.core.host_services import HostServiceDefinition, register_host_service
from fastapi.testclient import TestClient
from pydantic import BaseModel


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "events.db"
    s = SqliteEventStore(path)
    yield s
    s.close()


@pytest.fixture
def token_store(tmp_path):
    path = tmp_path / "tokens.db"
    s = HostTokenStore(path)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def reset_host_service_registry():
    """Restore the host-service registry after each test so custom handlers do
    not leak between tests."""
    from disco.core import host_services as hs

    original = dict(hs._REGISTRY)
    yield
    hs._REGISTRY.clear()
    hs._REGISTRY.update(original)


def _make_client(store, token_store, runtime=None):
    return TestClient(create_app(store, runtime=runtime, host_token_store=token_store))


def _create_conversation(store, conversation_id: str, owner_id: str = "owner_a"):
    store.create_conversation(conversation_id, owner_id=owner_id)


def _mint(token_store, conversation_id: str, owner_id: str = "owner_a", **kwargs):
    kwargs.setdefault(
        "allowed_services",
        frozenset(
            {
                "svc.ping",
                "svc.echo",
                "svc.fail",
                "svc.boom",
                "svc.slow",
                "svc.big",
                "svc.unknown",
                "svc.definitely_not_registered",
            }
        ),
    )
    return token_store.mint(conversation_id, owner_id, "app:demo", **kwargs)


def _bus(client: TestClient, service: str, token: str | None, payload: Any = None, **extra):
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return client.post(
        f"/_disco/svc/{service}",
        content=json.dumps(payload if payload is not None else {}),
        headers=headers,
        **extra,
    )


def test_bus_ping_authenticated(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.ping", token, {"hello": "world"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["service"] == "svc.ping"


def test_bus_missing_token(store, token_store):
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.ping", None, {})
    assert resp.status_code == 401


def test_bus_malformed_token(store, token_store):
    client = _make_client(store, token_store)
    for bad in ["", "not.bearer", "a2v0.short", "a2v0..x"]:
        resp = _bus(client, "svc.ping", bad, {})
        assert resp.status_code == 401, f"failed for {bad!r}"


def test_bus_unknown_selector(store, token_store):
    client = _make_client(store, token_store)
    token = "a2v0.AAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    resp = _bus(client, "svc.ping", token, {})
    assert resp.status_code == 401


def test_bus_wrong_verifier(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    parts = token.split(".")
    wrong = f"{parts[0]}.{parts[1]}.CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.ping", wrong, {})
    assert resp.status_code == 401


def test_bus_revoked_token(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    record = token_store.verify(token)
    assert token_store.revoke(record.selector) is True
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.ping", token, {})
    assert resp.status_code == 401


def test_bus_expired_token(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1", expires_in=timedelta(seconds=-1))
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.ping", token, {})
    assert resp.status_code == 401


def test_bus_deleted_conversation(store, token_store):
    import asyncio

    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    asyncio.run(store.delete_conversation("conv_1", owner_id="owner_a"))
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.ping", token, {})
    assert resp.status_code == 401


def test_bus_owner_mismatch(store, token_store):
    _create_conversation(store, "conv_1", owner_id="owner_a")
    token = _mint(token_store, "conv_1", owner_id="owner_b")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.ping", token, {})
    assert resp.status_code == 401


def test_bus_cross_conversation_isolation(store, token_store):
    _create_conversation(store, "conv_a", owner_id="owner_a")
    _create_conversation(store, "conv_b", owner_id="owner_b")
    token_a = _mint(token_store, "conv_a", owner_id="owner_a")
    token_b = _mint(token_store, "conv_b", owner_id="owner_b")
    client = _make_client(store, token_store)
    assert _bus(client, "svc.ping", token_a).status_code == 200
    assert _bus(client, "svc.ping", token_b).status_code == 200
    # Each token record carries its own conversation.
    assert token_store.verify(token_a).conversation_id == "conv_a"
    assert token_store.verify(token_b).conversation_id == "conv_b"


def test_bus_unknown_service_authenticated(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.definitely_not_registered", token, {})
    assert resp.status_code == 404


def test_bus_service_enumeration_resistance(store, token_store):
    """Unauthenticated callers learn nothing from the status code about whether
    a service exists."""
    client = _make_client(store, token_store)
    existing = _bus(client, "svc.ping", None, {}).status_code
    missing = _bus(client, "svc.not_real", None, {}).status_code
    assert existing == missing == 401


def test_bus_authenticates_before_content_and_body_validation(store, token_store):
    client = _make_client(store, token_store)
    response = client.post(
        "/_disco/svc/svc.ping",
        content=b"not-json",
        headers={"content-type": "text/plain", "authorization": "Bearer bad"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "auth_required"}


def test_bus_refuses_service_outside_credential_scope(store, token_store):
    _create_conversation(store, "conv_1")
    token = token_store.mint(
        "conv_1",
        "owner_a",
        "app:demo",
        allowed_services=frozenset({"svc.ping"}),
    )
    response = _bus(_make_client(store, token_store), "svc.unknown", token, {})
    assert response.status_code == 403
    assert response.json() == {"error": "service_not_allowed"}


def test_bus_context_cannot_broaden_app_service_or_origin_scope(store, token_store):
    class _EmptyPayload(BaseModel):
        pass

    async def _scope(payload, ctx):
        del payload
        return {
            "app_id": ctx.app_id,
            "services": sorted(ctx.allowed_services),
            "origins": sorted(ctx.allowed_origins),
        }

    register_host_service(
        HostServiceDefinition(
            name="svc.scope",
            handler=_scope,
            description="scope proof",
            payload_schema=_EmptyPayload,
        )
    )
    _create_conversation(store, "conv_1")
    token_a = token_store.mint(
        "conv_1",
        "owner_a",
        "app:a",
        allowed_services=frozenset({"svc.scope"}),
        allowed_origins=frozenset({"https://a.example"}),
    )
    client = _make_client(store, token_store)
    response = _bus(client, "svc.scope", token_a, {})
    assert response.status_code == 200
    assert response.json() == {
        "app_id": "app:a",
        "services": ["svc.scope"],
        "origins": ["https://a.example"],
    }
    assert _bus(client, "svc.ping", token_a, {}).status_code == 403


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("allowed_services", '["Svc-Ping"]'),
        ("allowed_origins", '["https://a.example/path"]'),
        ("version", 99),
        ("verifier_digest", b"short"),
        ("created_at", "2026-07-11T00:00:00"),
        ("owner_id", ""),
        ("generation", -1),
    ],
)
def test_bus_malformed_persisted_credential_fails_auth(store, token_store, column, value):
    _create_conversation(store, "conv_1")
    token = token_store.mint(
        "conv_1",
        "owner_a",
        "app:a",
        allowed_services=frozenset({"svc.ping"}),
        allowed_origins=frozenset({"https://a.example"}),
    )
    selector = token.split(".")[1]
    conn = sqlite3.connect(token_store.db_path)
    conn.execute(
        f"UPDATE host_service_tokens SET {column} = ? WHERE selector = ?",
        (value, selector),
    )
    conn.commit()
    conn.close()

    response = _bus(_make_client(store, token_store), "svc.ping", token, {})
    assert response.status_code == 401
    assert response.json() == {"error": "auth_required"}


def test_bus_payload_validation_error(store, token_store):
    class _EchoPayload(BaseModel):
        message: str

    async def _echo(payload, ctx):
        return {"ok": True, "message": payload["message"]}

    register_host_service(
        HostServiceDefinition(
            name="svc.echo",
            handler=_echo,
            description="echo",
            payload_schema=_EchoPayload,
        )
    )
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.echo", token, {"message": 123})
    assert resp.status_code == 422


def test_bus_handler_returns_ok_false(store, token_store):
    async def _fail(_payload, _ctx):
        return {"ok": False, "error": "nope"}

    register_host_service(
        HostServiceDefinition(
            name="svc.fail",
            handler=_fail,
            description="fails soft",
        )
    )
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.fail", token, {})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_bus_handler_exception_is_sanitized(store, token_store):
    async def _boom(_payload, _ctx):
        raise RuntimeError("secret details")

    register_host_service(
        HostServiceDefinition(
            name="svc.boom",
            handler=_boom,
            description="boom",
        )
    )
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.boom", token, {})
    assert resp.status_code == 500
    assert "secret details" not in resp.text
    assert resp.json()["error"] == "handler_error"


def test_bus_handler_timeout(store, token_store, monkeypatch):
    async def _slow(_payload, _ctx):
        import asyncio

        await asyncio.sleep(60)
        return {"ok": True}

    register_host_service(HostServiceDefinition(name="svc.slow", handler=_slow, description="slow"))
    monkeypatch.setattr("disco.agent_server.host_service_bus._HANDLER_TIMEOUT_S", 0.01)
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.slow", token, {})
    assert resp.status_code == 504
    assert resp.json()["error"] == "handler_timeout"


def test_bus_response_too_large(store, token_store):
    async def _big(_payload, _ctx):
        big = "x" * (_MAX_RESPONSE_BYTES + 1)
        return {"ok": True, "data": big}

    register_host_service(
        HostServiceDefinition(name="svc.big", handler=_big, description="big response")
    )
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, "svc.big", token, {})
    assert resp.status_code == 500


def test_bus_oversize_body(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    big = {"x": "y" * _MAX_BODY_BYTES}
    resp = _bus(client, "svc.ping", token, big)
    assert resp.status_code == 413
    assert resp.json()["error"] == "oversize_body"


def test_bus_lying_content_length(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b'{"ok":true}',
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "content-length": str(_MAX_BODY_BYTES + 1),
        },
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "oversize_body"


def test_bus_compressed_body_rejected(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b'{"ok":true}',
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "content-encoding": "gzip",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_encoding"


def test_bus_non_json_content_type(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b'{"ok":true}',
        headers={
            "content-type": "text/plain",
            "authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 415
    assert resp.json()["error"] == "content_type"


def test_bus_duplicate_json_keys(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b'{"a":1,"a":2}',
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "duplicate_keys"


def test_bus_nan_in_json(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b'{"x":NaN}',
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_json"


def test_bus_non_object_json(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b"[1,2,3]",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "json_not_object"


@pytest.mark.parametrize(
    "body",
    [
        b'{"n":' + (b"9" * 5000) + b"}",
        b'{"x":' + (b"[" * 15000) + (b"]" * 15000) + b"}",
    ],
)
def test_bus_pathological_json_is_sanitized_400(store, token_store, body):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    response = _make_client(store, token_store).post(
        "/_disco/svc/svc.ping",
        content=body,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_json"}


@pytest.mark.parametrize(
    "service,expected_status",
    [
        ("Svc.Ping", 400),  # mixed case
        ("svc/pong", 400),  # slash
        ("a" * 129, 400),  # overlong
        ("svc..ping", 400),  # empty segment
    ],
)
def test_bus_malformed_or_overlong_service(store, token_store, service, expected_status):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = _bus(client, service, token, {})
    assert resp.status_code == expected_status


def test_bus_traversal_service_rejected():
    from disco.agent_server.host_service_bus import _extract_service_name

    assert _extract_service_name(b"/_disco/svc/../etc/passwd") is None
    assert _extract_service_name(b"/_disco/svc/svc..ping") is None
    assert _extract_service_name(b"/_disco/svc/svc.ping") == "svc.ping"


def test_bus_encoded_slash_rejected(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    resp = client.post(
        "/_disco/svc/svc%2Fping",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 400


def test_bus_session_cookie_does_not_authenticate(store, token_store):
    client = _make_client(store, token_store)
    # Set a session cookie (valid or not) and hit the bus WITHOUT a bearer token.
    # The bus must never accept browser-session authentication.
    client.cookies.set("disco_session", "fake-session-cookie")
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401


def test_bus_csrf_not_required(store, token_store):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    # POST without the CSRF header that the rest of the API requires.
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 200


def test_bus_rotation(store, token_store):
    _create_conversation(store, "conv_1")
    old = _mint(token_store, "conv_1")
    new = token_store.rotate("conv_1", "owner_a", "app:demo")
    client = _make_client(store, token_store)
    assert _bus(client, "svc.ping", old).status_code == 200
    assert _bus(client, "svc.ping", new).status_code == 200
    token_store.finish_rotation(
        "conv_1", "app:demo", keep_selector=token_store.verify(new).selector
    )
    assert _bus(client, "svc.ping", old).status_code == 401


def test_bus_restart_durability(tmp_path):
    events_path = tmp_path / "events.db"
    tokens_path = tmp_path / "tokens.db"
    store = SqliteEventStore(events_path)
    token_store = HostTokenStore(tokens_path)
    store.create_conversation("conv_1", owner_id="owner_a")
    token = token_store.mint("conv_1", "owner_a", "app:demo")
    store.close()
    token_store.close()

    store2 = SqliteEventStore(events_path)
    token_store2 = HostTokenStore(tokens_path)
    client = TestClient(create_app(store2, host_token_store=token_store2))
    resp = client.post(
        "/_disco/svc/svc.ping",
        content=b"{}",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )
    assert resp.status_code == 200
    store2.close()
    token_store2.close()


def test_kill_switch_revokes_all_conversation_credentials(store, token_store):
    _create_conversation(store, "conv_1")
    bus_token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    session_token, session = SessionSigner().mint(owner_id="owner_a", is_admin=True)
    client.cookies.set(SESSION_COOKIE, session_token)
    response = client.post(
        "/conversations/conv_1/kill",
        headers={
            "Host": "localhost:8000",
            "Origin": "http://localhost:5173",
            CSRF_HEADER: session.csrf_token,
        },
    )
    assert response.status_code == 200, response.text
    assert _bus(client, "svc.ping", bus_token, {}).status_code == 401


def test_app_lifespan_closes_owned_token_store(tmp_path):
    event_store = SqliteEventStore(tmp_path / "events.db")
    app = create_app(event_store)
    owned = app.state.host_token_store
    with TestClient(app):
        assert owned.list_for_conversation("none") == []
    with pytest.raises(TokenStoreClosed):
        owned.verify("a2v0.AAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
    event_store.close()


def test_bus_plaintext_not_logged(store, token_store, caplog):
    _create_conversation(store, "conv_1")
    token = _mint(token_store, "conv_1")
    client = _make_client(store, token_store)
    with caplog.at_level("DEBUG"):
        _bus(client, "svc.ping", token, {})
    combined = caplog.text
    # Neither the full token nor the verifier should appear in logs.
    assert token not in combined
    assert token.split(".")[-1] not in combined

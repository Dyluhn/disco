"""Library deletion must be authorized and drained by the live runtime owner."""

import httpx
import pytest
from disco.app_server.routes import conversations
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent
from disco.core.auth import CSRF_HEADER, SESSION_COOKIE, SessionSigner
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("DISCO_AGENT_BASE", "http://agent.test")
    store = SqliteEventStore(":memory:")
    store.create_conversation("conv_delete", owner_id="local", surface="deep_research")
    token, session = SessionSigner().mint(owner_id="local")
    app = FastAPI()

    @app.middleware("http")
    async def authenticated(request: Request, call_next):
        request.state.auth_session = session
        return await call_next(request)

    app.include_router(conversations.make_conversations_router(store))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        client.headers[CSRF_HEADER] = session.csrf_token
        yield store, client, session
    store.close()


def transport(monkeypatch, handle):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        conversations.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )


def test_agent_receives_authorized_delete_before_database_removal(context, monkeypatch):
    store, client, session = context
    observed = []

    async def handle(request):
        observed.append(await store.conversation_owner_id("conv_delete"))
        forwarded = SessionSigner().verify_cookie_header(request.headers.get("cookie"))
        assert forwarded == session
        assert request.headers[CSRF_HEADER] == session.csrf_token
        await store.delete_conversation("conv_delete", owner_id=session.owner_id)
        return httpx.Response(200, json={"id": "conv_delete", "deleted": True})

    transport(monkeypatch, handle)
    response = client.delete("/api/conversations/conv_delete")
    assert response.status_code == 200
    assert observed == ["local"]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_unconfirmed_runtime_delete_preserves_database(context, monkeypatch, status):
    store, client, _session = context
    transport(monkeypatch, lambda request: httpx.Response(status))
    response = client.delete("/api/conversations/conv_delete")
    assert response.status_code == 503
    assert store.conversation_owner_id_sync("conv_delete") == "local"


async def test_no_agent_configuration_cannot_delete_an_active_run(context, monkeypatch):
    store, client, _session = context
    monkeypatch.delenv("DISCO_AGENT_BASE")
    await store.append("conv_delete", StatusEvent(status=ConversationStatus.RUNNING))
    response = client.delete("/api/conversations/conv_delete")
    assert response.status_code == 409
    assert store.conversation_owner_id_sync("conv_delete") == "local"

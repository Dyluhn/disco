"""Adversarial integration tests for WO-A4 bus metering and ``ai.chat``."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from disco.agent_server import create_app
from disco.agent_server.host_token_store import HostTokenStore
from disco.core import SqliteEventStore
from disco.core.llm import CompletionResponse, TokenUsage
from disco.core.quota import QuotaConfig, SqliteQuotaStore
from fastapi.testclient import TestClient


class _ApprovalsConfig:
    def approval_store(self, *, secret_store: object) -> None:
        del secret_store
        return None


class _Router:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def complete(self, request: object, *, context: object) -> CompletionResponse:
        self.calls.append((request, context))
        return CompletionResponse(
            text="safe answer",
            finish_reason="stop",
            model_used="host-selected",
            usage=TokenUsage(input_tokens=7, output_tokens=3),
        )


class _Runtime:
    def __init__(self, router: _Router) -> None:
        self.router = router
        self.conversation_ids: list[str] = []
        self._secret_store = None
        self._config_store = _ApprovalsConfig()

    def _router_now(self, *, conversation_id: str) -> _Router:
        self.conversation_ids.append(conversation_id)
        return self.router


@pytest.fixture
def a4(tmp_path):
    events = SqliteEventStore(tmp_path / "events.db")
    tokens = HostTokenStore(tmp_path / "tokens.db")
    quotas = SqliteQuotaStore(
        tmp_path / "quotas.db",
        default_config=QuotaConfig(window_seconds=60, max_requests=1000),
    )
    router = _Router()
    runtime = _Runtime(router)
    yield SimpleNamespace(
        events=events,
        tokens=tokens,
        quotas=quotas,
        router=router,
        runtime=runtime,
    )
    quotas.close()
    tokens.close()
    events.close()


def _principal(a4: Any, conversation: str, owner: str, app: str, services: set[str]) -> str:
    a4.events.create_conversation(conversation, owner_id=owner)
    return a4.tokens.mint(
        conversation,
        owner,
        app,
        allowed_services=frozenset(services),
    )


def _client(a4: Any) -> TestClient:
    return TestClient(
        create_app(
            a4.events,
            runtime=a4.runtime,
            host_token_store=a4.tokens,
            quota_store=a4.quotas,
        )
    )


def _call(client: TestClient, token: str, payload: dict[str, Any], service: str = "ai.chat"):
    return client.post(
        f"/_disco/svc/{service}",
        content=json.dumps(payload),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def _chat(**extra: object) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": "hello"}], **extra}


def test_quota_identity_is_exact_owner_and_audience(a4: Any) -> None:
    a4.quotas.configure(
        owner_id="owner-a",
        audience="app-a",
        limits=QuotaConfig(window_seconds=60, max_requests=1),
    )
    token_a = _principal(a4, "conv-a", "owner-a", "app-a", {"ai.chat"})
    token_b = _principal(a4, "conv-b", "owner-b", "app-b", {"ai.chat"})
    client = _client(a4)

    assert _call(client, token_a, _chat()).status_code == 200
    assert _call(client, token_a, _chat()).status_code == 429
    assert _call(client, token_b, _chat()).status_code == 200


def test_exact_service_and_aggregate_limits_both_apply_with_retry_after(a4: Any) -> None:
    a4.quotas.configure(
        owner_id="owner-a",
        audience="app-a",
        limits=QuotaConfig(window_seconds=60, max_requests=2),
    )
    a4.quotas.configure(
        owner_id="owner-a",
        audience="app-a",
        service="ai.chat",
        limits=QuotaConfig(window_seconds=60, max_requests=1),
    )
    token = _principal(a4, "conv-a", "owner-a", "app-a", {"ai.chat", "svc.ping"})
    client = _client(a4)

    assert _call(client, token, _chat()).status_code == 200
    denied = _call(client, token, _chat())
    assert denied.status_code == 429
    assert denied.json()["error"] == "quota_exceeded"
    assert denied.headers["retry-after"].isdigit()
    assert int(denied.headers["retry-after"]) >= 1
    assert _call(client, token, {}, "svc.ping").status_code == 200
    aggregate = _call(client, token, {}, "svc.ping")
    assert aggregate.status_code == 429
    assert aggregate.headers["retry-after"].isdigit()


@pytest.mark.parametrize("payload", [_chat(model="attacker"), _chat(tools=[]), {}])
def test_authenticated_malformed_ai_chat_counts_request_but_zero_tokens(
    a4: Any, payload: dict[str, Any]
) -> None:
    token = _principal(a4, "conv-a", "owner-a", "app-a", {"ai.chat"})
    response = _call(_client(a4), token, payload)
    assert response.status_code == 422
    usage = a4.quotas.get_usage(owner_id="owner-a", audience="app-a")
    assert usage.request_count == 1
    assert usage.input_tokens == usage.output_tokens == 0


def test_ai_chat_requires_explicit_scope_and_binds_authenticated_conversation(a4: Any) -> None:
    denied = _principal(a4, "conv-denied", "owner-a", "app-denied", {"svc.ping"})
    allowed = _principal(a4, "conv-authenticated", "owner-a", "app-ok", {"ai.chat"})
    client = _client(a4)

    assert _call(client, denied, _chat()).status_code == 403
    response = _call(client, allowed, _chat())
    assert response.status_code == 200
    assert a4.runtime.conversation_ids == ["conv-authenticated"]
    request, context = a4.router.calls[0]
    assert request.tools is None
    assert request.enable_thinking is False
    assert context.conversation_id == "conv-authenticated"


def test_estimate_is_reconciled_to_exact_actual_usage(a4: Any) -> None:
    token = _principal(a4, "conv-a", "owner-a", "app-a", {"ai.chat"})
    assert _call(_client(a4), token, _chat(max_tokens=2000)).status_code == 200
    usage = a4.quotas.get_usage(owner_id="owner-a", audience="app-a")
    assert (usage.request_count, usage.input_tokens, usage.output_tokens) == (1, 7, 3)


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_oversubscribe_request_limit(a4: Any) -> None:
    import httpx

    a4.quotas.configure(
        owner_id="owner-a",
        audience="app-a",
        limits=QuotaConfig(window_seconds=60, max_requests=1),
    )
    token = _principal(a4, "conv-a", "owner-a", "app-a", {"ai.chat"})
    app = create_app(
        a4.events,
        runtime=a4.runtime,
        host_token_store=a4.tokens,
        quota_store=a4.quotas,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/_disco/svc/ai.chat",
                    json=_chat(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                for _index in range(8)
            )
        )
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1
    assert statuses.count(429) == 7
    assert a4.quotas.get_usage(owner_id="owner-a", audience="app-a").request_count == 1


def test_handler_error_and_timeout_settle_zero_tokens(
    a4: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _principal(a4, "conv-a", "owner-a", "app-a", {"ai.chat"})

    async def boom(_service: str, _payload: object, _ctx: object) -> object:
        raise RuntimeError("downstream exploded")

    monkeypatch.setattr("disco.agent_server.host_service_bus.call_host_service", boom)
    assert _call(_client(a4), token, _chat()).status_code == 500
    first = a4.quotas.get_usage(owner_id="owner-a", audience="app-a")
    assert (first.request_count, first.input_tokens, first.output_tokens) == (1, 0, 0)

    async def slow(_service: str, _payload: object, _ctx: object) -> object:
        await asyncio.sleep(10)
        return {"ok": True}

    monkeypatch.setattr("disco.agent_server.host_service_bus.call_host_service", slow)
    monkeypatch.setattr("disco.agent_server.host_service_bus._HANDLER_TIMEOUT_S", 0.001)
    assert _call(_client(a4), token, _chat()).status_code == 504
    second = a4.quotas.get_usage(owner_id="owner-a", audience="app-a")
    assert (second.request_count, second.input_tokens, second.output_tokens) == (2, 0, 0)


@pytest.mark.asyncio
async def test_cancelled_request_settles_zero_tokens(
    a4: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    token = _principal(a4, "conv-a", "owner-a", "app-a", {"ai.chat"})
    entered = asyncio.Event()

    async def blocked(_service: str, _payload: object, _ctx: object) -> object:
        entered.set()
        await asyncio.Event().wait()
        return {"ok": True}

    monkeypatch.setattr("disco.agent_server.host_service_bus.call_host_service", blocked)
    app = create_app(
        a4.events,
        runtime=a4.runtime,
        host_token_store=a4.tokens,
        quota_store=a4.quotas,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(
                "/_disco/svc/ai.chat",
                json=_chat(),
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    usage = a4.quotas.get_usage(owner_id="owner-a", audience="app-a")
    assert (usage.request_count, usage.input_tokens, usage.output_tokens) == (1, 0, 0)

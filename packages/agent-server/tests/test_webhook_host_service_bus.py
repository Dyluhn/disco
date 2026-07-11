"""Production app wiring injects the host-owned webhook configuration store."""

from __future__ import annotations

from typing import Any

from disco.agent_server import create_app
from disco.agent_server.host_token_store import HostTokenStore
from disco.core import SqliteEventStore
from disco.core.host_services import HostServiceContext
from disco.core.webhook_host_service import WEBHOOK_EMIT_SERVICE_NAME, WebhookAppConfigStore
from fastapi.testclient import TestClient


def test_authenticated_bus_injects_host_owned_webhook_config_store(tmp_path, monkeypatch) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    store.create_conversation("conv_webhook", owner_id="owner-1")
    tokens = HostTokenStore(tmp_path / "tokens.db")
    configs = WebhookAppConfigStore(tmp_path / "webhook.db")
    token = tokens.mint(
        "conv_webhook",
        "owner-1",
        "app_0123456789abcdef0123456789abcdef",
        allowed_services=frozenset({WEBHOOK_EMIT_SERVICE_NAME}),
    )
    captured: list[HostServiceContext] = []

    async def capture_context(
        name: str,
        payload: dict[str, Any],
        ctx: HostServiceContext,
    ) -> dict[str, Any]:
        assert name == WEBHOOK_EMIT_SERVICE_NAME
        assert payload["endpoint_id"] == "order_events"
        captured.append(ctx)
        return {"ok": False, "error": "captured"}

    monkeypatch.setattr("disco.agent_server.host_service_bus.call_host_service", capture_context)
    try:
        with TestClient(
            create_app(
                store,
                host_token_store=tokens,
                webhook_config_store=configs,
            )
        ) as client:
            assert client.app.state.webhook_config_store is configs
            response = client.post(
                f"/_disco/svc/{WEBHOOK_EMIT_SERVICE_NAME}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "app_binding": "app_0123456789abcdef0123456789abcdef",
                    "endpoint_id": "order_events",
                    "event_type": "order.created",
                    "data": {"order_id": 7},
                },
            )
        assert response.status_code == 200
        assert response.json() == {"ok": False, "error": "captured"}
        assert len(captured) == 1
        assert captured[0].webhook_config_store is configs
        assert captured[0].owner_id == "owner-1"
        assert captured[0].app_id == "app_0123456789abcdef0123456789abcdef"
        assert configs.get("owner-1", "app_1", "order_events") is None
    finally:
        configs.close()
        tokens.close()
        store.close()


def test_agent_app_closes_only_its_owned_webhook_store(tmp_path) -> None:
    store = SqliteEventStore(tmp_path / "events.db")
    try:
        app = create_app(store)
        configs = app.state.webhook_config_store
        with TestClient(app):
            assert configs.get("owner", "app", "endpoint") is None
        try:
            configs.get("owner", "app", "endpoint")
        except RuntimeError as exc:
            assert str(exc) == "webhook config store is closed"
        else:
            raise AssertionError("agent-server leaked its owned webhook config store")
    finally:
        store.close()

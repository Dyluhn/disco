"""Owner-admin webhook configuration is durable, scoped, and write-only."""

from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.core.webhook_host_service import (
    WEBHOOK_PURPOSE,
    WebhookAppConfigStore,
    webhook_inbound_secret_ref,
    webhook_secret_ref,
)
from fastapi.testclient import TestClient

_MASTER_KEY = "strong-webhook-route-test-key-0123456789-ABCDE"
_INBOUND_SECRET = "inbound_webhook_signing_secret_0123456789"
_OUTBOUND_SECRET = "outbound_webhook_signing_secret_0123456789"


@pytest.fixture
def configured_client(tmp_path):
    config_store = ConfigStore(tmp_path / "config.json")
    secrets = SecretStore(tmp_path / "secrets.json", box=SecretBox(_MASTER_KEY))
    webhook_configs = WebhookAppConfigStore(tmp_path / "webhook.db")
    state = ConfigState(
        store=config_store,
        secrets=secrets,
        skills=SkillStore(tmp_path / "skills"),
        webhook_configs=webhook_configs,
    )
    event_store = SqliteEventStore(":memory:")
    client = TestClient(create_app(event_store, state))
    yield client, config_store, secrets, webhook_configs, tmp_path
    webhook_configs.close()
    event_store.close()


def test_inbound_secret_is_owner_scoped_and_never_returned(configured_client) -> None:
    client, _config_store, secrets, _configs, tmp_path = configured_client
    response = client.put(
        "/api/webhooks/config/app-1/inbound?owner_id=owner-a",
        json={"signing_secret": _INBOUND_SECRET},
    )

    assert response.status_code == 200
    assert response.json() == {"audience": "app-1", "inbound_configured": True}
    assert _INBOUND_SECRET not in response.text
    ref = webhook_inbound_secret_ref("owner-a", "app-1")
    assert secrets.get_secret(ref, strong_required=True) == _INBOUND_SECRET
    assert not secrets.has_secret(webhook_inbound_secret_ref("owner-b", "app-1"))
    assert _INBOUND_SECRET not in (tmp_path / "secrets.json").read_text()


def test_outbound_target_is_owner_scoped_approved_and_secret_is_write_only(
    configured_client,
) -> None:
    client, config_store, secrets, configs, tmp_path = configured_client
    response = client.put(
        "/api/webhooks/config/app-1/outbound/order_events?owner_id=owner-a",
        json={
            "target_url": "https://hooks.example.com/disco",
            "signing_secret": _OUTBOUND_SECRET,
            "event_types": ["order.created", "order.cancelled"],
            "enabled": True,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "audience": "app-1",
        "endpoint_id": "order_events",
        "enabled": True,
        "credential_configured": True,
    }
    assert _OUTBOUND_SECRET not in response.text
    assert "hooks.example.com" not in response.text
    config = configs.get("owner-a", "app-1", "order_events")
    assert config is not None
    assert configs.get("owner-b", "app-1", "order_events") is None
    ref = webhook_secret_ref("owner-a", "app-1", "order_events")
    assert config.secret_ref == ref
    assert config.event_types == frozenset({"order.created", "order.cancelled"})
    assert secrets.get_secret(ref, strong_required=True) == _OUTBOUND_SECRET
    assert config_store.approvals.approval_store(secret_store=secrets).is_approved(
        "https://hooks.example.com/disco",
        WEBHOOK_PURPOSE,
        ref,
    )
    assert _OUTBOUND_SECRET not in (tmp_path / "secrets.json").read_text()
    assert _OUTBOUND_SECRET.encode() not in (tmp_path / "webhook.db").read_bytes()


def test_invalid_or_extra_secret_input_is_redacted_and_not_persisted(
    configured_client,
    caplog,
) -> None:
    client, _config_store, secrets, configs, _tmp_path = configured_client
    submitted = "S" * 513
    response = client.put(
        "/api/webhooks/config/app-1/outbound/order_events?owner_id=owner-a",
        json={
            "target_url": "https://hooks.example.com/disco",
            "signing_secret": submitted,
            "event_types": ["order.created"],
            "enabled": True,
            "secret": "attacker-extra-field",
        },
    )

    assert response.status_code == 422
    assert submitted not in response.text
    assert "attacker-extra-field" not in response.text
    assert submitted not in caplog.text
    assert "attacker-extra-field" not in caplog.text
    assert configs.get("owner-a", "app-1", "order_events") is None
    assert not secrets.has_secret(webhook_secret_ref("owner-a", "app-1", "order_events"))


def test_default_app_wiring_persists_config_for_the_agent_process(tmp_path, monkeypatch) -> None:
    event_path = tmp_path / "events.db"
    monkeypatch.setenv("DISCO_SECRET_KEY", _MASTER_KEY)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "default-secrets.json"))
    monkeypatch.setenv("DISCO_CONFIG", str(tmp_path / "default-config.json"))
    events = SqliteEventStore(event_path)
    try:
        with TestClient(create_app(events)) as client:
            response = client.put(
                "/api/webhooks/config/app-1/outbound/order_events?owner_id=owner-a",
                json={
                    "target_url": "https://hooks.example.com/disco",
                    "signing_secret": _OUTBOUND_SECRET,
                    "event_types": ["order.created"],
                    "enabled": True,
                },
            )
            assert response.status_code == 200

        agent_configs = WebhookAppConfigStore(event_path)
        try:
            config = agent_configs.get("owner-a", "app-1", "order_events")
            assert config is not None
            assert config.target_url == "https://hooks.example.com/disco"
            stored_secret = SecretStore().get_secret(config.secret_ref, strong_required=True)
            assert stored_secret == _OUTBOUND_SECRET
        finally:
            agent_configs.close()
    finally:
        events.close()

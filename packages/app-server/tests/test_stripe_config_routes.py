"""Owner-admin Stripe configuration is real, durable, and write-only."""

from __future__ import annotations

import asyncio

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.host_egress import GuardedResponse
from disco.core.host_services import HostServiceContext, call_host_service
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    STRIPE_API_URL,
    STRIPE_SECRET_REF,
    StripeAppConfigStore,
)
from fastapi.testclient import TestClient

_MASTER_KEY = "strong-stripe-route-test-key-0123456789-ABCDE"
_RESTRICTED_KEY = "rk_test_route_abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def configured_client(tmp_path):
    config_store = ConfigStore(tmp_path / "config.json")
    secrets = SecretStore(tmp_path / "secrets.json", box=SecretBox(_MASTER_KEY))
    stripe_configs = StripeAppConfigStore(tmp_path / "stripe.db")
    state = ConfigState(
        store=config_store,
        secrets=secrets,
        skills=SkillStore(tmp_path / "skills"),
        stripe_configs=stripe_configs,
    )
    event_store = SqliteEventStore(":memory:")
    client = TestClient(create_app(event_store, state))
    yield client, config_store, secrets, stripe_configs, tmp_path
    stripe_configs.close()
    event_store.close()


def _body(**overrides):
    body = {
        "restricted_key": _RESTRICTED_KEY,
        "plan_selector": "pro",
        "stripe_price_id": "price_ABCdef123456",
        "allowed_return_origins": ["https://app.example.com"],
        "enabled": True,
    }
    body.update(overrides)
    return body


def test_owner_can_configure_app_without_secret_ever_returning(configured_client) -> None:
    client, config_store, secrets, stripe_configs, tmp_path = configured_client
    response = client.put("/api/stripe/config/app-1?owner_id=owner-a", json=_body())
    assert response.status_code == 200
    assert response.json() == {
        "audience": "app-1",
        "plan_selector": "pro",
        "allowed_return_origins": ["https://app.example.com"],
        "enabled": True,
        "credential_configured": True,
    }
    assert _RESTRICTED_KEY not in response.text
    assert "price_ABCdef123456" not in response.text
    assert secrets.get_secret(STRIPE_SECRET_REF) == _RESTRICTED_KEY
    assert _RESTRICTED_KEY not in (tmp_path / "secrets.json").read_text()
    assert _RESTRICTED_KEY.encode() not in (tmp_path / "stripe.db").read_bytes()
    config = stripe_configs.get("owner-a", "app-1")
    assert config is not None and config.enabled
    assert config.stripe_price_id == "price_ABCdef123456"
    assert config_store.approval_store(secret_store=secrets).is_approved(
        STRIPE_API_URL,
        PAYMENTS_CHECKOUT_SERVICE_NAME,
        STRIPE_SECRET_REF,
    )


def test_full_access_key_and_extra_fields_are_refused_without_persistence(
    configured_client,
) -> None:
    client, _config_store, secrets, stripe_configs, _tmp_path = configured_client
    full_access = client.put(
        "/api/stripe/config/app-1?owner_id=owner-a",
        json=_body(restricted_key="sk_live_full_access_forbidden"),
    )
    assert full_access.status_code == 400
    assert "full-access" in full_access.text
    assert "sk_live_full_access_forbidden" not in full_access.text
    assert not secrets.has_secret(STRIPE_SECRET_REF)
    assert stripe_configs.get("owner-a", "app-1") is None

    extra = _body()
    extra["amount"] = 1
    assert client.put("/api/stripe/config/app-1?owner_id=owner-a", json=extra).status_code == 422
    assert not secrets.has_secret(STRIPE_SECRET_REF)


def test_invalid_config_does_not_store_the_credential(configured_client) -> None:
    client, _config_store, secrets, stripe_configs, _tmp_path = configured_client
    response = client.put(
        "/api/stripe/config/app-1?owner_id=owner-a",
        json=_body(allowed_return_origins=["https://evil.example/path"]),
    )
    assert response.status_code == 400
    assert not secrets.has_secret(STRIPE_SECRET_REF)
    assert stripe_configs.get("owner-a", "app-1") is None


def test_overlong_key_validation_never_echoes_or_logs_secret(configured_client, caplog) -> None:
    client, _config_store, secrets, _stripe_configs, _tmp_path = configured_client
    submitted = "rk_" + "S" * 300
    response = client.put(
        "/api/stripe/config/app-1?owner_id=owner-a",
        json=_body(restricted_key=submitted),
    )
    assert response.status_code == 422
    assert submitted not in response.text
    assert submitted not in caplog.text
    assert not secrets.has_secret(STRIPE_SECRET_REF)


def test_default_app_config_is_visible_to_separate_agent_store_and_handler(
    tmp_path, monkeypatch
) -> None:
    event_path = tmp_path / "events.db"
    monkeypatch.setenv("DISCO_SECRET_KEY", _MASTER_KEY)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "default-secrets.json"))
    monkeypatch.setenv("DISCO_CONFIG", str(tmp_path / "default-config.json"))
    events = SqliteEventStore(event_path)
    try:
        with TestClient(create_app(events)) as client:
            response = client.put(
                "/api/stripe/config/app-1?owner_id=owner-a",
                json=_body(),
            )
            assert response.status_code == 200

        # A distinct connection models the agent-server process after restart.
        agent_configs = StripeAppConfigStore(event_path)
        try:
            config = agent_configs.get("owner-a", "app-1")
            assert config is not None
            secrets = SecretStore()
            approvals = ConfigStore().approval_store(secret_store=secrets)

            def fake_stripe(*_args, **_kwargs):
                return GuardedResponse(
                    url=STRIPE_API_URL,
                    status_code=200,
                    headers={"content-type": "application/json"},
                    content=b'{"url":"https://checkout.stripe.com/c/pay/cs_test_cross_process"}',
                )

            monkeypatch.setattr(
                "disco.core.stripe_host_service.guarded_request",
                fake_stripe,
            )
            result = asyncio.run(
                call_host_service(
                    PAYMENTS_CHECKOUT_SERVICE_NAME,
                    {
                        "plan_selector": "pro",
                        "user_id": 42,
                        "success_path": "/billing/success",
                        "cancel_path": "/billing/cancel",
                    },
                    HostServiceContext(
                        secret_store=secrets,
                        approvals=approvals,
                        allow_hosts=frozenset({"api.stripe.com"}),
                        app_id="app-1",
                        owner_id="owner-a",
                        allowed_origins=frozenset({"https://app.example.com"}),
                        stripe_config_store=agent_configs,
                    ),
                )
            )
            assert result == {"url": "https://checkout.stripe.com/c/pay/cs_test_cross_process"}
        finally:
            agent_configs.close()
    finally:
        events.close()

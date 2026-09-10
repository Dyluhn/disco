"""Authenticated production-import proof for the Stripe host-service seam."""

from __future__ import annotations

from typing import Any

from disco.agent_server import create_app
from disco.agent_server.host_token_store import HostTokenStore
from disco.core import SqliteEventStore
from disco.core.host_services import HostServiceContext, get_host_service
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    StripeAppConfigStore,
)
from fastapi.testclient import TestClient


def _payload(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "plan_selector": "pro",
        "user_id": 42,
        "success_path": "/billing/success",
        "cancel_path": "/billing/cancel",
        "binding_proof": "0" * 64,
        "webhook_proof": "0" * 64,
    }
    payload.update(extra)
    return payload


def _setup(tmp_path):
    store = SqliteEventStore(tmp_path / "events.db")
    store.create_conversation("conv_stripe", owner_id="owner-1")
    tokens = HostTokenStore(tmp_path / "tokens.db")
    configs = StripeAppConfigStore(tmp_path / "stripe.db")
    token = tokens.mint(
        "conv_stripe",
        "owner-1",
        "app-1",
        allowed_services=frozenset({PAYMENTS_CHECKOUT_SERVICE_NAME}),
        allowed_origins=frozenset({"https://app.example.com"}),
    )
    return store, tokens, configs, token


def test_production_bus_registers_checkout_and_refuses_amount_before_egress(
    tmp_path, monkeypatch
) -> None:
    store, tokens, configs, token = _setup(tmp_path)
    assert get_host_service(PAYMENTS_CHECKOUT_SERVICE_NAME) is not None
    called = False

    def must_not_egress(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True
        raise AssertionError("client amount reached Stripe egress")

    monkeypatch.setattr("disco.core.stripe_host_service.guarded_request", must_not_egress)
    try:
        with TestClient(
            create_app(
                store,
                host_token_store=tokens,
                stripe_config_store=configs,
            )
        ) as client:
            response = client.post(
                f"/_disco/svc/{PAYMENTS_CHECKOUT_SERVICE_NAME}",
                headers={"Authorization": f"Bearer {token}"},
                json=_payload(amount=1, currency="usd", price="price_attacker"),
            )
        assert response.status_code == 422
        assert response.json() == {"error": "payload_error"}
        assert not called
    finally:
        configs.close()
        tokens.close()
        store.close()


def test_authenticated_bus_injects_host_owned_stripe_config_store(tmp_path, monkeypatch) -> None:
    store, tokens, configs, token = _setup(tmp_path)
    captured: list[HostServiceContext] = []

    async def capture_context(
        name: str,
        payload: dict[str, Any],
        ctx: HostServiceContext,
    ) -> dict[str, Any]:
        assert name == PAYMENTS_CHECKOUT_SERVICE_NAME
        assert payload == _payload()
        captured.append(ctx)
        return {"ok": False, "error": "captured"}

    monkeypatch.setattr("disco.agent_server.host_service_bus.call_host_service", capture_context)
    try:
        with TestClient(
            create_app(
                store,
                host_token_store=tokens,
                stripe_config_store=configs,
            )
        ) as client:
            response = client.post(
                f"/_disco/svc/{PAYMENTS_CHECKOUT_SERVICE_NAME}",
                headers={"Authorization": f"Bearer {token}"},
                json=_payload(),
            )
        assert response.status_code == 200
        assert response.json() == {"ok": False, "error": "captured"}
        assert len(captured) == 1
        assert captured[0].stripe_config_store is configs
        assert captured[0].owner_id == "owner-1"
        assert captured[0].app_id == "app-1"
        assert captured[0].allowed_origins == frozenset({"https://app.example.com"})
    finally:
        configs.close()
        tokens.close()
        store.close()

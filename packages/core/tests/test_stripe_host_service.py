from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
from disco.core.host_egress import GuardedResponse
from disco.core.host_services import (
    HostServiceContext,
    HostServicePayloadError,
    call_host_service,
)
from disco.core.llm.secret_refs import secret_ref_allowed_for_origin
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.origin_approvals import OriginApprovalStore
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    PAYMENTS_READY_SERVICE_NAME,
    STRIPE_API_URL,
    STRIPE_SECRET_REF,
    StripeAppConfigStore,
    StripeConfigurationError,
    configure_stripe_restricted_key,
    ensure_stripe_binding_secret,
    stripe_correlation_tag,
)

_MASTER_KEY = "strong-test-master-key-0123456789-ABCDE"
_RESTRICTED_KEY = "rk_test_abcdefghijklmnopqrstuvwxyz"
_PRICE_ID = "price_ABCdef123456"
_ORIGIN = "https://app.example.com"


def _secret_store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets.json", box=SecretBox(_MASTER_KEY))


def _configured(
    tmp_path: Path,
    *,
    origins: frozenset[str] = frozenset({_ORIGIN}),
    enabled: bool = True,
) -> tuple[StripeAppConfigStore, SecretStore, OriginApprovalStore]:
    secrets = _secret_store(tmp_path)
    configure_stripe_restricted_key(secrets, _RESTRICTED_KEY)
    ensure_stripe_binding_secret(secrets, "owner-1", "app-1")
    configs = StripeAppConfigStore(tmp_path / "state.db")
    configs.configure(
        owner_id="owner-1",
        audience="app-1",
        plan_selector="pro",
        stripe_price_id=_PRICE_ID,
        allowed_return_origins=origins,
        enabled=enabled,
        secret_store=secrets,
    )
    approvals = OriginApprovalStore(tmp_path / "approvals.json", secret_store=secrets)
    approvals.approve(STRIPE_API_URL, PAYMENTS_CHECKOUT_SERVICE_NAME, STRIPE_SECRET_REF)
    return configs, secrets, approvals


def _ctx(
    configs: StripeAppConfigStore,
    secrets: SecretStore,
    approvals: OriginApprovalStore,
    *,
    allowed_origins: frozenset[str] = frozenset({_ORIGIN}),
) -> HostServiceContext:
    return HostServiceContext(
        secret_store=secrets,
        approvals=approvals,
        allow_hosts=frozenset({"api.stripe.com"}),
        app_id="app-1",
        owner_id="owner-1",
        conversation_id="conversation-1",
        allowed_services=frozenset({PAYMENTS_CHECKOUT_SERVICE_NAME}),
        allowed_origins=allowed_origins,
        request_timeout_s=3.25,
        stripe_config_store=configs,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "plan_selector": "pro",
        "user_id": 42,
        "success_path": "/billing/success",
        "cancel_path": "/billing/cancel",
    }
    payload.update(overrides)
    return payload


def _stripe_response(url: str = "https://checkout.stripe.com/c/pay/cs_test_123#fragment"):
    return GuardedResponse(
        url=STRIPE_API_URL,
        status_code=200,
        headers={"content-type": "application/json; charset=utf-8"},
        content=json.dumps({"url": url}).encode(),
    )


def test_configuration_is_durable_and_never_persists_plaintext_key(tmp_path: Path) -> None:
    configs, secrets, _approvals = _configured(tmp_path)
    configs.close()

    reopened = StripeAppConfigStore(tmp_path / "state.db")
    config = reopened.get("owner-1", "app-1")
    assert config is not None
    assert config.plan_selector == "pro"
    assert config.stripe_price_id == _PRICE_ID
    assert config.allowed_return_origins == frozenset({_ORIGIN})
    assert _RESTRICTED_KEY.encode() not in (tmp_path / "state.db").read_bytes()
    assert _RESTRICTED_KEY not in (tmp_path / "secrets.json").read_text()
    assert secrets.get_secret(STRIPE_SECRET_REF) == _RESTRICTED_KEY


def test_configuration_loudly_refuses_full_access_or_wrong_ref_keys(tmp_path: Path) -> None:
    secrets = _secret_store(tmp_path)
    with pytest.raises(StripeConfigurationError, match="full-access.*sk_.*restricted rk_"):
        configure_stripe_restricted_key(secrets, "sk_live_do_not_accept_this")
    with pytest.raises(StripeConfigurationError, match="restricted rk_"):
        configure_stripe_restricted_key(secrets, "pk_test_public")
    assert not secrets.has_secret(STRIPE_SECRET_REF)

    secrets.set_secret(STRIPE_SECRET_REF, "sk_test_bypassed_api", strong_required=True)
    configs = StripeAppConfigStore(tmp_path / "state.db")
    with pytest.raises(StripeConfigurationError, match="full-access"):
        configs.configure(
            owner_id="owner-1",
            audience="app-1",
            plan_selector="pro",
            stripe_price_id=_PRICE_ID,
            allowed_return_origins=frozenset({_ORIGIN}),
            enabled=True,
            secret_store=secrets,
        )


def test_stripe_secret_ref_is_pinned_to_exact_api_origin() -> None:
    assert secret_ref_allowed_for_origin(STRIPE_SECRET_REF, STRIPE_API_URL)
    assert not secret_ref_allowed_for_origin(
        STRIPE_SECRET_REF, "https://api.stripe.com.evil.example/v1/checkout/sessions"
    )
    assert not secret_ref_allowed_for_origin(STRIPE_SECRET_REF, "https://checkout.stripe.com")


@pytest.mark.parametrize(
    "origin",
    [
        "http://app.example.com",
        "https://user@app.example.com",
        "https://app.example.com/path",
        "https://app.example.com?next=evil",
        "https://app.example.com#fragment",
    ],
)
def test_configuration_rejects_unsafe_return_origins(tmp_path: Path, origin: str) -> None:
    secrets = _secret_store(tmp_path)
    configure_stripe_restricted_key(secrets, _RESTRICTED_KEY)
    configs = StripeAppConfigStore(tmp_path / "state.db")
    with pytest.raises(StripeConfigurationError):
        configs.configure(
            owner_id="owner-1",
            audience="app-1",
            plan_selector="pro",
            stripe_price_id=_PRICE_ID,
            allowed_return_origins=frozenset({origin}),
            enabled=True,
            secret_store=secrets,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", ["price", "amount", "currency", "id", "metadata"])
async def test_client_chosen_commerce_fields_are_rejected_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injected: str
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    called = False

    def must_not_egress(*_args: Any, **_kwargs: Any) -> GuardedResponse:
        nonlocal called
        called = True
        raise AssertionError("injected commerce data reached egress")

    monkeypatch.setattr("disco.core.stripe_host_service.guarded_request", must_not_egress)
    with pytest.raises(HostServicePayloadError):
        await call_host_service(
            PAYMENTS_CHECKOUT_SERVICE_NAME,
            _payload(**{injected: "attacker-controlled"}),
            _ctx(configs, secrets, approvals),
        )
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/steal",
        "https://evil.example/steal",
        "/safe?next=https://evil.example",
        "/../admin",
        "/%2f%2fevil.example",
        "/safe\\evil",
    ],
)
async def test_return_path_tricks_are_rejected_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("bad return path reached egress"),
    )
    with pytest.raises(HostServicePayloadError):
        await call_host_service(
            PAYMENTS_CHECKOUT_SERVICE_NAME,
            _payload(success_path=path),
            _ctx(configs, secrets, approvals),
        )


@pytest.mark.asyncio
async def test_success_uses_trusted_price_origin_timeout_and_per_call_stripe_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_request(*args: Any, **kwargs: Any) -> GuardedResponse:
        calls.append((args, kwargs))
        return _stripe_response()

    monkeypatch.setattr("disco.core.stripe_host_service.guarded_request", fake_request)
    ctx = _ctx(configs, secrets, approvals)
    first = await call_host_service(PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), ctx)
    second = await call_host_service(PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), ctx)

    assert first == second == {"url": "https://checkout.stripe.com/c/pay/cs_test_123#fragment"}
    assert len(calls) == 2
    keys: list[str] = []
    for args, kwargs in calls:
        assert args == ("POST", STRIPE_API_URL)
        assert kwargs["allow_hosts"] == frozenset({"api.stripe.com"})
        assert kwargs["timeout_s"] == 3.25
        assert kwargs["max_redirects"] == 0
        headers = kwargs["headers"]
        assert headers["Authorization"] == f"Bearer {_RESTRICTED_KEY}"
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert headers["Idempotency-Key"].startswith("disco-checkout-")
        keys.append(headers["Idempotency-Key"])
        form = parse_qs(kwargs["body"].decode("ascii"), strict_parsing=True)
        binding = ensure_stripe_binding_secret(secrets, "owner-1", "app-1")
        assert form == {
            "mode": ["payment"],
            "line_items[0][price]": [_PRICE_ID],
            "line_items[0][quantity]": ["1"],
            "client_reference_id": ["42"],
            "metadata[disco_app_binding]": ["app-1"],
            "metadata[disco_plan_selector]": ["pro"],
            "metadata[disco_correlation]": [stripe_correlation_tag(binding, "app-1", "pro", 42)],
            "success_url": [f"{_ORIGIN}/billing/success"],
            "cancel_url": [f"{_ORIGIN}/billing/cancel"],
        }
    assert keys[0] != keys[1]


@pytest.mark.asyncio
async def test_readiness_is_dynamic_and_performs_no_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("readiness must not reach Stripe"),
    )
    ctx = _ctx(configs, secrets, approvals)
    ctx = replace(ctx, allowed_services=frozenset({PAYMENTS_READY_SERVICE_NAME}))
    assert await call_host_service(PAYMENTS_READY_SERVICE_NAME, {"plan_selector": "pro"}, ctx) == {
        "ready": True
    }
    configs.disable("owner-1", "app-1")
    assert await call_host_service(PAYMENTS_READY_SERVICE_NAME, {"plan_selector": "pro"}, ctx) == {
        "ready": False
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [0, -1, "42", True, 1.5])
async def test_invalid_user_binding_is_rejected_before_egress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    user_id: object,
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("invalid user binding reached egress"),
    )
    with pytest.raises(HostServicePayloadError):
        await call_host_service(
            PAYMENTS_CHECKOUT_SERVICE_NAME,
            _payload(user_id=user_id),
            _ctx(configs, secrets, approvals),
        )


@pytest.mark.asyncio
async def test_wrong_plan_or_credential_origin_cannot_reach_stripe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("refused checkout reached egress"),
    )
    ctx = _ctx(configs, secrets, approvals)
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(plan_selector="basic"), ctx
    ) == {"ok": False, "error": "unknown_plan"}
    wrong_origin_ctx = _ctx(
        configs,
        secrets,
        approvals,
        allowed_origins=frozenset({"https://attacker.example"}),
    )
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), wrong_origin_ctx
    ) == {"ok": False, "error": "return_origin_not_allowed"}


@pytest.mark.asyncio
async def test_multiple_matching_credential_origins_fail_closed_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_origin = "https://preview.example.com"
    configs, secrets, approvals = _configured(
        tmp_path,
        origins=frozenset({_ORIGIN, second_origin}),
    )
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("ambiguous origin reached egress"),
    )
    ctx = _ctx(
        configs,
        secrets,
        approvals,
        allowed_origins=frozenset({_ORIGIN, second_origin}),
    )
    assert await call_host_service(PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), ctx) == {
        "ok": False,
        "error": "return_origin_not_allowed",
    }


@pytest.mark.asyncio
async def test_missing_secret_or_signed_approval_fails_closed_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("unapproved checkout reached egress"),
    )
    secrets.clear_secret(STRIPE_SECRET_REF)
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), _ctx(configs, secrets, approvals)
    ) == {"ok": False, "error": "stripe_not_configured"}

    configure_stripe_restricted_key(secrets, _RESTRICTED_KEY)
    unapproved = OriginApprovalStore(tmp_path / "empty-approvals.json", secret_store=secrets)
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), _ctx(configs, secrets, unapproved)
    ) == {"ok": False, "error": "stripe_origin_not_approved"}


@pytest.mark.asyncio
async def test_directly_seeded_stripe_ref_under_weak_master_is_refused_at_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, _strong_secrets, approvals = _configured(tmp_path)
    weak_secrets = SecretStore(
        tmp_path / "weak-secrets.json",
        box=SecretBox("weak-password"),
    )
    # Generic SecretStore remains backward-compatible and permits weak masters;
    # the Stripe use path must independently enforce its high-value custody gate.
    weak_secrets.set_secret(STRIPE_SECRET_REF, _RESTRICTED_KEY)
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("weakly protected credential reached egress"),
    )
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME,
        _payload(),
        _ctx(configs, weak_secrets, approvals),
    ) == {"ok": False, "error": "stripe_credential_refused"}


@pytest.mark.asyncio
async def test_corrupt_host_configuration_fails_closed_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute(
            "UPDATE stripe_app_configs SET allowed_return_origins = ?",
            ('["https://app.example.com", 7]',),
        )
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("corrupt config reached egress"),
    )
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), _ctx(configs, secrets, approvals)
    ) == {"ok": False, "error": "stripe_not_configured"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_stripe_response("http://checkout.stripe.com/c/pay/cs_test"), "stripe_invalid_response"),
        (
            _stripe_response("https://checkout.stripe.com.evil/c/pay/cs_test"),
            "stripe_invalid_response",
        ),
        (
            _stripe_response("https://user@checkout.stripe.com/c/pay/cs_test"),
            "stripe_invalid_response",
        ),
        (
            _stripe_response("https://checkout.stripe.com:444/c/pay/cs_test"),
            "stripe_invalid_response",
        ),
        (
            _stripe_response("https://checkout.stripe.com/c/pay/cs_test\nignored"),
            "stripe_invalid_response",
        ),
        (_stripe_response("https://stripe.com/c/pay/cs_test"), "stripe_invalid_response"),
        (
            GuardedResponse(
                url=STRIPE_API_URL,
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"url":"https://checkout.stripe.com/a","url":"https://evil.example"}',
            ),
            "stripe_invalid_response",
        ),
        (
            GuardedResponse(
                url=STRIPE_API_URL,
                status_code=500,
                headers={"content-type": "application/json"},
                content=b'{"url":"https://checkout.stripe.com/a"}',
            ),
            "stripe_invalid_response",
        ),
    ],
)
async def test_malicious_or_failed_stripe_response_never_becomes_a_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: GuardedResponse,
    expected: str,
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request", lambda *_args, **_kwargs: response
    )
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), _ctx(configs, secrets, approvals)
    ) == {"ok": False, "error": expected}


@pytest.mark.asyncio
async def test_exact_checkout_host_query_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals = _configured(tmp_path)
    checkout_url = "https://checkout.stripe.com/c/pay/cs_test?prefilled_email=user%40example.com"
    monkeypatch.setattr(
        "disco.core.stripe_host_service.guarded_request",
        lambda *_args, **_kwargs: _stripe_response(checkout_url),
    )
    assert await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), _ctx(configs, secrets, approvals)
    ) == {"url": checkout_url}


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_secret_is_not_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals = _configured(tmp_path)

    def timed_out(*_args: Any, **kwargs: Any) -> GuardedResponse:
        assert kwargs["timeout_s"] == 3.25
        raise TimeoutError("network failure containing no response")

    monkeypatch.setattr("disco.core.stripe_host_service.guarded_request", timed_out)
    result = await call_host_service(
        PAYMENTS_CHECKOUT_SERVICE_NAME, _payload(), _ctx(configs, secrets, approvals)
    )
    assert result == {"ok": False, "error": "stripe_unavailable"}
    assert _RESTRICTED_KEY not in json.dumps(result)

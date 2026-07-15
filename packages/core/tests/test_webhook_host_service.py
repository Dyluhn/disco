"""Unit-level abuse tests for the host-owned ``webhook.emit`` adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from disco.core.host_egress import GuardedResponse
from disco.core.host_services import HostServiceContext, call_host_service
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.origin_approvals import OriginApprovalStore
from disco.core.webhook_host_service import (
    WEBHOOK_EMIT_SERVICE_NAME,
    WEBHOOK_PURPOSE,
    WebhookAppConfigStore,
)

_MASTER_KEY = "strong-webhook-test-master-key-0123456789-ABCDE"
_APP_ID = "app_" + "a" * 32
_OTHER_APP_ID = "app_" + "b" * 32
_OWNER_ID = "owner-1"
_ENDPOINT_ID = "fulfillment_events"
_TARGET_URL = "https://hooks.example.net/events"
_SIGNING_SECRET = "webhook-test-signing-key-with-ample-entropy"


def _secret_store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets.json", box=SecretBox(_MASTER_KEY))


def _configured(
    tmp_path: Path,
    *,
    target_url: str = _TARGET_URL,
) -> tuple[WebhookAppConfigStore, SecretStore, OriginApprovalStore, str]:
    secret_store = _secret_store(tmp_path)
    configs = WebhookAppConfigStore(tmp_path / "webhooks.db")
    config = configs.configure(
        owner_id=_OWNER_ID,
        audience=_APP_ID,
        endpoint_id=_ENDPOINT_ID,
        target_url=target_url,
        signing_secret=_SIGNING_SECRET,
        event_types=frozenset({"order.shipped"}),
        enabled=True,
        secret_store=secret_store,
    )
    approvals = OriginApprovalStore(tmp_path / "approvals.json", secret_store=secret_store)
    return configs, secret_store, approvals, config.secret_ref


def _ctx(
    configs: WebhookAppConfigStore | None,
    secrets: SecretStore | None,
    approvals: OriginApprovalStore | None,
    *,
    app_id: str = _APP_ID,
    allow_hosts: frozenset[str] | None = frozenset({"hooks.example.net"}),
) -> HostServiceContext:
    return HostServiceContext(
        secret_store=secrets,
        approvals=approvals,
        allow_hosts=allow_hosts,
        app_id=app_id,
        owner_id=_OWNER_ID,
        conversation_id="conversation-1",
        allowed_services=frozenset({WEBHOOK_EMIT_SERVICE_NAME}),
        request_timeout_s=2.75,
        webhook_config_store=configs,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "app_binding": _APP_ID,
        "endpoint_id": _ENDPOINT_ID,
        "event_type": "order.shipped",
        "data": {"order_id": 42, "carrier": "postal"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_missing_host_dependencies_or_endpoint_config_fail_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "disco.core.webhook_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("unconfigured webhook reached egress"),
    )
    secrets = _secret_store(tmp_path)
    approvals = OriginApprovalStore(tmp_path / "approvals.json", secret_store=secrets)
    empty = WebhookAppConfigStore(tmp_path / "empty.db")

    for ctx in (
        _ctx(None, secrets, approvals),
        _ctx(empty, secrets, approvals),
        _ctx(empty, None, approvals),
        _ctx(empty, secrets, None),
    ):
        assert await call_host_service(WEBHOOK_EMIT_SERVICE_NAME, _payload(), ctx) == {
            "ok": False,
            "error": "webhook_not_configured",
        }


@pytest.mark.asyncio
async def test_app_binding_mismatch_cannot_select_another_apps_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals, _ref = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.webhook_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("cross-app webhook reached egress"),
    )
    assert await call_host_service(
        WEBHOOK_EMIT_SERVICE_NAME,
        _payload(app_binding=_OTHER_APP_ID),
        _ctx(configs, secrets, approvals),
    ) == {"ok": False, "error": "app_binding_refused"}

    # Even a self-consistent credential for a different app cannot read app A's
    # owner/app/endpoint row.
    assert await call_host_service(
        WEBHOOK_EMIT_SERVICE_NAME,
        _payload(app_binding=_OTHER_APP_ID),
        _ctx(configs, secrets, approvals, app_id=_OTHER_APP_ID),
    ) == {"ok": False, "error": "webhook_not_configured"}


@pytest.mark.asyncio
async def test_missing_signed_origin_approval_fails_before_secret_use_or_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals, _ref = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.webhook_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("unapproved webhook reached egress"),
    )
    result = await call_host_service(
        WEBHOOK_EMIT_SERVICE_NAME, _payload(), _ctx(configs, secrets, approvals)
    )
    assert result == {"ok": False, "error": "webhook_origin_not_approved"}
    assert _SIGNING_SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_undeclared_event_type_is_refused_before_secret_use_or_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals, _ref = _configured(tmp_path)
    monkeypatch.setattr(
        "disco.core.webhook_host_service.guarded_request",
        lambda *_args, **_kwargs: pytest.fail("undeclared webhook event reached egress"),
    )
    result = await call_host_service(
        WEBHOOK_EMIT_SERVICE_NAME,
        _payload(event_type="admin.credentials_exported"),
        _ctx(configs, secrets, approvals),
    )
    assert result == {"ok": False, "error": "webhook_event_type_refused"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_url",
    [
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data",
    ],
)
async def test_internal_and_metadata_destinations_are_blocked_by_real_guarded_egress(
    tmp_path: Path, target_url: str
) -> None:
    configs, secrets, approvals, ref = _configured(tmp_path, target_url=target_url)
    approvals.approve(target_url, WEBHOOK_PURPOSE, ref)
    # allow_hosts=None removes only the optional hostname allowlist.  The S-W2
    # class-1 public-IP policy remains mandatory and rejects these literals
    # before opening a socket.
    result = await call_host_service(
        WEBHOOK_EMIT_SERVICE_NAME,
        _payload(),
        _ctx(configs, secrets, approvals, allow_hosts=None),
    )
    assert result == {"ok": False, "error": "webhook_egress_denied"}
    assert _SIGNING_SECRET not in json.dumps(result)


@pytest.mark.asyncio
async def test_approved_delivery_is_canonically_signed_without_leaking_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs, secrets, approvals, ref = _configured(tmp_path)
    approvals.approve(_TARGET_URL, WEBHOOK_PURPOSE, ref)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_request(*args: Any, **kwargs: Any) -> GuardedResponse:
        calls.append((args, kwargs))
        return GuardedResponse(
            url=_TARGET_URL,
            status_code=204,
            headers={"content-type": "text/plain"},
            content=b"",
        )

    monkeypatch.setattr("disco.core.webhook_host_service.guarded_request", fake_request)
    monkeypatch.setattr("disco.core.webhook_host_service.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr(
        "disco.core.webhook_host_service.secrets.token_urlsafe", lambda _n: "fixed_event_id"
    )

    result = await call_host_service(
        WEBHOOK_EMIT_SERVICE_NAME, _payload(), _ctx(configs, secrets, approvals)
    )
    assert result == {"ok": True, "status": 204}
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("POST", _TARGET_URL)
    assert kwargs["allow_hosts"] == frozenset({"hooks.example.net"})
    assert kwargs["timeout_s"] == 2.75
    assert kwargs["max_redirects"] == 0

    body = kwargs["body"]
    assert isinstance(body, bytes)
    assert json.loads(body) == {
        "data": {"carrier": "postal", "order_id": 42},
        "id": "dwh_fixed_event_id",
        "type": "order.shipped",
    }
    headers = kwargs["headers"]
    assert headers["Disco-Webhook-Id"] == "dwh_fixed_event_id"
    assert headers["Disco-Webhook-Event"] == "order.shipped"
    expected = hmac.new(
        _SIGNING_SECRET.encode(),
        f"1700000000.{_ENDPOINT_ID}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    assert headers["Disco-Webhook-Signature"] == f"t=1700000000,v1={expected}"

    observable = json.dumps(result) + json.dumps(headers) + body.decode()
    assert _SIGNING_SECRET not in observable
    assert _SIGNING_SECRET.encode() not in (tmp_path / "webhooks.db").read_bytes()
    assert _SIGNING_SECRET not in (tmp_path / "secrets.json").read_text()

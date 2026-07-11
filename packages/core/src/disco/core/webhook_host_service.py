"""Host-owned generic webhook configuration and ``webhook.emit`` adapter."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .host_egress import EgressDenied, guarded_request
from .host_services import HostServiceContext, HostServiceDefinition, register_host_service
from .llm.secret_refs import resolve_provider_secret, secret_ref_allowed_for_origin

if TYPE_CHECKING:
    from .llm.secrets import SecretStore

WEBHOOK_EMIT_SERVICE_NAME = "webhook.emit"
WEBHOOK_PURPOSE = WEBHOOK_EMIT_SERVICE_NAME
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_APP_BINDING_RE = re.compile(r"^app_[0-9a-f]{32}$")
_MAX_RESPONSE_BYTES = 64 * 1024


class WebhookConfigurationError(ValueError):
    """Host-owned webhook configuration is absent or unsafe."""


def _identity(value: str, label: str) -> str:
    result = value.strip()
    if not result or len(result.encode()) > 256:
        raise WebhookConfigurationError(f"invalid webhook {label}")
    return result


def _target_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise WebhookConfigurationError("invalid webhook target URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or len(value) > 2048
    ):
        raise WebhookConfigurationError("webhook targets require a credential-free HTTPS URL")
    return value


def webhook_secret_ref(owner_id: str, audience: str, endpoint_id: str) -> str:
    owner = _identity(owner_id, "owner")
    app = _identity(audience, "audience")
    if _IDENT_RE.fullmatch(endpoint_id) is None:
        raise WebhookConfigurationError("invalid webhook endpoint id")
    digest = hashlib.sha256(f"{owner}\0{app}\0{endpoint_id}".encode()).hexdigest()
    return f"webhook.outbound.{digest}"


def webhook_inbound_secret_ref(owner_id: str, audience: str) -> str:
    owner = _identity(owner_id, "owner")
    app = _identity(audience, "audience")
    digest = hashlib.sha256(f"{owner}\0{app}".encode()).hexdigest()
    return f"webhook.inbound.{digest}"


def _validate_signing_secret(secret: str) -> str:
    if not 32 <= len(secret) <= 512 or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in secret):
        raise WebhookConfigurationError(
            "webhook signing secret must be 32-512 printable non-whitespace characters"
        )
    return secret


def configure_webhook_inbound_secret(
    store: SecretStore, owner_id: str, audience: str, secret: str
) -> None:
    store.set_secret(
        webhook_inbound_secret_ref(owner_id, audience),
        _validate_signing_secret(secret),
        strong_required=True,
    )


def resolve_webhook_inbound_secret(store: SecretStore, owner_id: str, audience: str) -> str | None:
    value = store.get_secret(webhook_inbound_secret_ref(owner_id, audience), strong_required=True)
    try:
        return _validate_signing_secret(value) if value is not None else None
    except WebhookConfigurationError:
        return None


@dataclass(frozen=True)
class WebhookTargetConfig:
    owner_id: str
    audience: str
    endpoint_id: str
    target_url: str
    secret_ref: str
    event_types: frozenset[str]
    enabled: bool


class WebhookAppConfigStore:
    """Durable non-secret outbound targets keyed by owner/app/endpoint."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        resolved = Path(path)
        self.db_path = str(resolved)
        if self.db_path != ":memory:":
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                resolved.parent.chmod(0o700)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=5.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS webhook_app_configs (
              owner_id TEXT NOT NULL, audience TEXT NOT NULL, endpoint_id TEXT NOT NULL,
              target_url TEXT NOT NULL, secret_ref TEXT NOT NULL,
              event_types TEXT NOT NULL, enabled INTEGER NOT NULL,
              PRIMARY KEY(owner_id, audience, endpoint_id)
            )"""
        )
        self._conn.commit()

    def configure(
        self,
        *,
        owner_id: str,
        audience: str,
        endpoint_id: str,
        target_url: str,
        signing_secret: str,
        event_types: frozenset[str],
        enabled: bool,
        secret_store: SecretStore,
    ) -> WebhookTargetConfig:
        owner = _identity(owner_id, "owner")
        app = _identity(audience, "audience")
        if _IDENT_RE.fullmatch(endpoint_id) is None:
            raise WebhookConfigurationError("invalid webhook endpoint id")
        target = _target_url(target_url)
        if (
            not event_types
            or len(event_types) > 12
            or any(len(event) > 64 or _EVENT_RE.fullmatch(event) is None for event in event_types)
        ):
            raise WebhookConfigurationError(
                "webhook event_types must contain 1-12 bounded dotted snake_case values"
            )
        if not isinstance(enabled, bool):
            raise WebhookConfigurationError("webhook enabled state must be boolean")
        ref = webhook_secret_ref(owner, app, endpoint_id)
        secret_store.set_secret(ref, _validate_signing_secret(signing_secret), strong_required=True)
        with self._lock:
            conn = self._check_open()
            conn.execute(
                """INSERT INTO webhook_app_configs
                (owner_id, audience, endpoint_id, target_url, secret_ref, event_types, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, audience, endpoint_id) DO UPDATE SET
                target_url=excluded.target_url, secret_ref=excluded.secret_ref,
                event_types=excluded.event_types, enabled=excluded.enabled""",
                (
                    owner,
                    app,
                    endpoint_id,
                    target,
                    ref,
                    json.dumps(sorted(event_types), separators=(",", ":")),
                    int(enabled),
                ),
            )
            conn.commit()
        return WebhookTargetConfig(owner, app, endpoint_id, target, ref, event_types, enabled)

    def get(self, owner_id: str, audience: str, endpoint_id: str) -> WebhookTargetConfig | None:
        with self._lock:
            row = (
                self._check_open()
                .execute(
                    "SELECT * FROM webhook_app_configs "
                    "WHERE owner_id=? AND audience=? AND endpoint_id=?",
                    (owner_id, audience, endpoint_id),
                )
                .fetchone()
            )
        if row is None:
            return None
        raw_event_types = json.loads(str(row["event_types"]))
        if not isinstance(raw_event_types, list) or any(
            not isinstance(event, str) for event in raw_event_types
        ):
            raise RuntimeError("stored webhook event types are invalid")
        return WebhookTargetConfig(
            str(row["owner_id"]),
            str(row["audience"]),
            str(row["endpoint_id"]),
            str(row["target_url"]),
            str(row["secret_ref"]),
            frozenset(raw_event_types),
            bool(row["enabled"]),
        )

    def _check_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("webhook config store is closed")
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class WebhookEmitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    app_binding: str = Field(min_length=36, max_length=36)
    endpoint_id: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64)
    data: Any

    @field_validator("app_binding")
    @classmethod
    def _binding(cls, value: str) -> str:
        if _APP_BINDING_RE.fullmatch(value) is None:
            raise ValueError("invalid app binding")
        return value

    @field_validator("endpoint_id")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        if _IDENT_RE.fullmatch(value) is None:
            raise ValueError("invalid endpoint id")
        return value

    @field_validator("event_type")
    @classmethod
    def _event(cls, value: str) -> str:
        if _EVENT_RE.fullmatch(value) is None:
            raise ValueError("invalid event type")
        return value


def _failure(reason: str) -> dict[str, Any]:
    return {"ok": False, "error": reason}


async def _webhook_emit_handler(payload: dict[str, Any], ctx: HostServiceContext) -> dict[str, Any]:
    if payload["app_binding"] != ctx.app_id:
        return _failure("app_binding_refused")
    configs = ctx.webhook_config_store
    if configs is None or ctx.secret_store is None or ctx.approvals is None:
        return _failure("webhook_not_configured")
    config = configs.get(ctx.owner_id, ctx.app_id, payload["endpoint_id"])
    if config is None or not config.enabled:
        return _failure("webhook_not_configured")
    if payload["event_type"] not in config.event_types:
        return _failure("webhook_event_type_refused")
    if not secret_ref_allowed_for_origin(config.secret_ref, config.target_url):
        return _failure("webhook_origin_refused")
    if not ctx.approvals.is_approved(config.target_url, WEBHOOK_PURPOSE, config.secret_ref):
        return _failure("webhook_origin_not_approved")
    try:
        secret = resolve_provider_secret(config.secret_ref, ctx.secret_store, strong_required=True)
    except RuntimeError:
        return _failure("webhook_credential_refused")
    if secret is None:
        return _failure("webhook_not_configured")
    try:
        _validate_signing_secret(secret)
        event_id = "dwh_" + secrets.token_urlsafe(18)
        body = json.dumps(
            {"id": event_id, "type": payload["event_type"], "data": payload["data"]},
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, WebhookConfigurationError):
        return _failure("invalid_webhook_payload")
    if len(body) > 64 * 1024:
        return _failure("webhook_payload_too_large")
    timestamp = int(time.time())
    signed = f"{timestamp}.{config.endpoint_id}.".encode() + body
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    try:
        response = await asyncio.to_thread(
            guarded_request,
            "POST",
            config.target_url,
            headers={
                "Content-Type": "application/json",
                "Disco-Webhook-Id": event_id,
                "Disco-Webhook-Event": payload["event_type"],
                "Disco-Webhook-Signature": f"t={timestamp},v1={signature}",
            },
            body=body,
            allow_hosts=ctx.allow_hosts,
            timeout_s=ctx.request_timeout_s,
            max_redirects=0,
            max_bytes=_MAX_RESPONSE_BYTES,
        )
    except (EgressDenied, OSError, TimeoutError):
        return _failure("webhook_egress_denied")
    return {"ok": 200 <= response.status_code < 300, "status": response.status_code}


WEBHOOK_EMIT_SERVICE = HostServiceDefinition(
    name=WEBHOOK_EMIT_SERVICE_NAME,
    handler=_webhook_emit_handler,
    description="Deliver a signed event to an operator-approved external webhook target.",
    payload_schema=WebhookEmitPayload,
)
register_host_service(WEBHOOK_EMIT_SERVICE)


__all__ = [
    "WEBHOOK_EMIT_SERVICE",
    "WEBHOOK_EMIT_SERVICE_NAME",
    "WEBHOOK_PURPOSE",
    "WebhookAppConfigStore",
    "WebhookConfigurationError",
    "WebhookEmitPayload",
    "WebhookTargetConfig",
    "configure_webhook_inbound_secret",
    "resolve_webhook_inbound_secret",
    "webhook_inbound_secret_ref",
    "webhook_secret_ref",
]

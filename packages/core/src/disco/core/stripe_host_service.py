"""Host-owned Stripe configuration and the ``payments.checkout`` adapter.

The generated app supplies only a stable plan selector and relative return
paths.  Stripe price ids, credentials, approved origins, and idempotency keys
remain in the trusted host plane.  This module deliberately exposes no API for
reading a Stripe credential back out of ``SecretStore``.

Operators configure the slice through the app-server's admin+CSRF-protected
``PUT /api/stripe/config/{audience}`` route.  That route writes the restricted
key to ``SecretStore`` and the non-secret mapping to this store in the shared
event database.  The agent-server opens a distinct connection to the same DB;
the plaintext key is write-only and never appears in the route response.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .host_egress import EgressDenied, GuardedResponse, guarded_request, origin_for_url
from .host_services import (
    HostServiceContext,
    HostServiceDefinition,
    register_host_service,
    return_url_allowed,
)
from .llm.secret_refs import resolve_provider_secret, secret_ref_allowed_for_origin

if TYPE_CHECKING:
    from .llm.secrets import SecretStore

PAYMENTS_CHECKOUT_SERVICE_NAME = "payments.checkout"
PAYMENTS_READY_SERVICE_NAME = "payments.ready"
STRIPE_SECRET_REF = "stripe"
STRIPE_API_URL = "https://api.stripe.com/v1/checkout/sessions"
STRIPE_API_HOSTS = frozenset({"api.stripe.com"})

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PRICE_ID_RE = re.compile(r"^price_[A-Za-z0-9]{6,200}$")
_RETURN_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._~-]+/?)*$")
_BINDING_SECRET_RE = re.compile(r"^stb_[A-Za-z0-9_-]{43}$")
_MAX_STRIPE_RESPONSE_BYTES = 256 * 1024


class StripeConfigurationError(ValueError):
    """Operator-owned Stripe configuration is absent or unsafe."""


def validate_stripe_restricted_key(api_key: str) -> None:
    """Accept restricted ``rk_`` credentials and loudly refuse full ``sk_`` keys."""
    if api_key.startswith("sk_"):
        raise StripeConfigurationError(
            "refusing a full-access Stripe sk_ credential; configure a restricted rk_ key"
        )
    if (
        not api_key.startswith("rk_")
        or not 8 <= len(api_key) <= 256
        or any(ch.isspace() or ord(ch) < 0x21 or ord(ch) > 0x7E for ch in api_key)
    ):
        raise StripeConfigurationError("Stripe requires a restricted rk_ credential")


def configure_stripe_restricted_key(secret_store: SecretStore, api_key: str) -> None:
    """Validate, then persist the credential encrypted under the fixed ``stripe`` ref."""
    validate_stripe_restricted_key(api_key)
    secret_store.set_secret(STRIPE_SECRET_REF, api_key, strong_required=True)


def stripe_binding_secret_ref(owner_id: str, audience: str) -> str:
    """Opaque per-owner/app ref; raw tenant identifiers never become secret names."""
    owner = _validate_identity("owner", owner_id)
    app = _validate_identity("audience", audience)
    digest = hashlib.sha256(f"{owner}\0{app}".encode()).hexdigest()
    return f"stripe.binding.{digest}"


def ensure_stripe_binding_secret(
    secret_store: SecretStore,
    owner_id: str,
    audience: str,
) -> str:
    """Return the per-app correlation key, creating it encrypted when absent."""
    ref = stripe_binding_secret_ref(owner_id, audience)
    existing = secret_store.get_secret(ref, strong_required=True)
    if existing is not None:
        if _BINDING_SECRET_RE.fullmatch(existing) is None:
            raise StripeConfigurationError("Stripe app binding secret is invalid")
        return existing
    value = "stb_" + secrets.token_urlsafe(32)
    secret_store.set_secret(ref, value, strong_required=True)
    return value


def resolve_stripe_binding_secret(
    secret_store: SecretStore,
    owner_id: str,
    audience: str,
) -> str | None:
    value = secret_store.get_secret(
        stripe_binding_secret_ref(owner_id, audience),
        strong_required=True,
    )
    return value if value is not None and _BINDING_SECRET_RE.fullmatch(value) else None


def stripe_correlation_tag(
    binding_secret: str,
    app_id: str,
    plan_selector: str,
    user_id: int,
) -> str:
    """Authenticate host-created Session metadata without sending the key to Stripe."""
    if _BINDING_SECRET_RE.fullmatch(binding_secret) is None:
        raise StripeConfigurationError("Stripe app binding secret is invalid")
    message = f"{app_id}\0{plan_selector}\0{user_id}".encode()
    return hmac.new(binding_secret.encode(), message, hashlib.sha256).hexdigest()


def _validate_identity(label: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized.encode("utf-8")) > 256:
        raise StripeConfigurationError(f"Stripe {label} must be a non-empty trusted identifier")
    return normalized


def _canonical_return_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise StripeConfigurationError("invalid Stripe return origin") from exc
    origin = origin_for_url(value)
    if (
        origin is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise StripeConfigurationError("Stripe return origins must be origin-only URLs")
    if parsed.scheme == "http":
        host = parsed.hostname or ""
        try:
            is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.lower() == "localhost"
        if not is_loopback:
            raise StripeConfigurationError("plaintext Stripe return origins must be loopback")
    if parsed.scheme == "https" and port not in {None, 443}:
        # Non-default HTTPS ports are valid origins and are intentionally kept.
        return origin
    return origin


@dataclass(frozen=True)
class StripeAppConfig:
    owner_id: str
    audience: str
    plan_selector: str
    stripe_price_id: str
    allowed_return_origins: frozenset[str]
    enabled: bool
    updated_at: datetime


class StripeAppConfigStore:
    """Durable, host-only Stripe app configuration keyed by owner + audience."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        resolved = Path(path)
        self.db_path = str(resolved)
        if self.db_path != ":memory:":
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                resolved.parent.chmod(0o700)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            with contextlib.suppress(OSError):
                resolved.chmod(0o600)
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stripe_app_configs (
                    owner_id               TEXT NOT NULL,
                    audience               TEXT NOT NULL,
                    plan_selector          TEXT NOT NULL,
                    stripe_price_id        TEXT NOT NULL,
                    allowed_return_origins TEXT NOT NULL,
                    enabled                INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at             TEXT NOT NULL,
                    PRIMARY KEY (owner_id, audience)
                )
                """
            )

    def configure(
        self,
        *,
        owner_id: str,
        audience: str,
        plan_selector: str,
        stripe_price_id: str,
        allowed_return_origins: frozenset[str],
        enabled: bool,
        secret_store: SecretStore,
    ) -> StripeAppConfig:
        """Validate all trust inputs and atomically upsert non-secret app config."""
        self.validate_configuration(
            owner_id=owner_id,
            audience=audience,
            plan_selector=plan_selector,
            stripe_price_id=stripe_price_id,
            allowed_return_origins=allowed_return_origins,
            enabled=enabled,
        )
        owner = _validate_identity("owner", owner_id)
        app = _validate_identity("audience", audience)
        if _IDENTIFIER_RE.fullmatch(plan_selector) is None:
            raise StripeConfigurationError("invalid Stripe plan selector")
        if _PRICE_ID_RE.fullmatch(stripe_price_id) is None:
            raise StripeConfigurationError("invalid Stripe price id")
        if not isinstance(enabled, bool):
            raise StripeConfigurationError("Stripe enabled state must be boolean")
        if not allowed_return_origins:
            raise StripeConfigurationError("Stripe requires at least one return origin")
        origins = frozenset(_canonical_return_origin(value) for value in allowed_return_origins)
        secret = secret_store.get_secret(STRIPE_SECRET_REF, strong_required=True)
        if secret is None:
            raise StripeConfigurationError("Stripe restricted credential is not configured")
        validate_stripe_restricted_key(secret)
        updated_at = datetime.now(UTC)
        with self._lock:
            conn = self._check_open()
            with conn:
                conn.execute(
                    """
                    INSERT INTO stripe_app_configs
                        (owner_id, audience, plan_selector, stripe_price_id,
                         allowed_return_origins, enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id, audience) DO UPDATE SET
                        plan_selector = excluded.plan_selector,
                        stripe_price_id = excluded.stripe_price_id,
                        allowed_return_origins = excluded.allowed_return_origins,
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (
                        owner,
                        app,
                        plan_selector,
                        stripe_price_id,
                        json.dumps(sorted(origins), separators=(",", ":")),
                        int(enabled),
                        updated_at.isoformat(),
                    ),
                )
        return StripeAppConfig(
            owner_id=owner,
            audience=app,
            plan_selector=plan_selector,
            stripe_price_id=stripe_price_id,
            allowed_return_origins=origins,
            enabled=enabled,
            updated_at=updated_at,
        )

    @staticmethod
    def validate_configuration(
        *,
        owner_id: str,
        audience: str,
        plan_selector: str,
        stripe_price_id: str,
        allowed_return_origins: frozenset[str],
        enabled: bool,
    ) -> None:
        """Validate non-secret operator input without changing either store."""
        _validate_identity("owner", owner_id)
        _validate_identity("audience", audience)
        if _IDENTIFIER_RE.fullmatch(plan_selector) is None:
            raise StripeConfigurationError("invalid Stripe plan selector")
        if _PRICE_ID_RE.fullmatch(stripe_price_id) is None:
            raise StripeConfigurationError("invalid Stripe price id")
        if not isinstance(enabled, bool):
            raise StripeConfigurationError("Stripe enabled state must be boolean")
        if not allowed_return_origins:
            raise StripeConfigurationError("Stripe requires at least one return origin")
        for value in allowed_return_origins:
            _canonical_return_origin(value)

    def get(self, owner_id: str, audience: str) -> StripeAppConfig | None:
        owner = _validate_identity("owner", owner_id)
        app = _validate_identity("audience", audience)
        with self._lock:
            row = (
                self._check_open()
                .execute(
                    "SELECT * FROM stripe_app_configs WHERE owner_id = ? AND audience = ?",
                    (owner, app),
                )
                .fetchone()
            )
        if row is None:
            return None
        try:
            raw_origins = json.loads(str(row["allowed_return_origins"]))
            if not isinstance(raw_origins, list) or not all(
                isinstance(value, str) for value in raw_origins
            ):
                return None
            origins = frozenset(_canonical_return_origin(value) for value in raw_origins)
            enabled_raw = row["enabled"]
            plan_selector = str(row["plan_selector"])
            price_id = str(row["stripe_price_id"])
            if (
                not origins
                or enabled_raw not in {0, 1}
                or _IDENTIFIER_RE.fullmatch(plan_selector) is None
                or _PRICE_ID_RE.fullmatch(price_id) is None
            ):
                return None
            return StripeAppConfig(
                owner_id=str(row["owner_id"]),
                audience=str(row["audience"]),
                plan_selector=plan_selector,
                stripe_price_id=price_id,
                allowed_return_origins=origins,
                enabled=bool(enabled_raw),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def disable(self, owner_id: str, audience: str) -> bool:
        owner = _validate_identity("owner", owner_id)
        app = _validate_identity("audience", audience)
        with self._lock, self._check_open() as conn:
            cur = conn.execute(
                "UPDATE stripe_app_configs SET enabled = 0, updated_at = ? "
                "WHERE owner_id = ? AND audience = ?",
                (datetime.now(UTC).isoformat(), owner, app),
            )
        return cur.rowcount > 0

    def delete(self, owner_id: str, audience: str) -> bool:
        owner = _validate_identity("owner", owner_id)
        app = _validate_identity("audience", audience)
        with self._lock, self._check_open() as conn:
            cur = conn.execute(
                "DELETE FROM stripe_app_configs WHERE owner_id = ? AND audience = ?",
                (owner, app),
            )
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _check_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Stripe app configuration store is closed")
        return self._conn


class StripeCheckoutPayload(BaseModel):
    """The complete sandbox-controlled checkout payload; all extras are refused."""

    model_config = ConfigDict(extra="forbid", strict=True)

    plan_selector: str
    # The canonical Worker derives this from its verified records session.  The
    # current A2 bearer is app-scoped, so the Worker verifier remains the trust
    # boundary until A2 grows a host-verifiable end-user principal assertion.
    user_id: int = Field(gt=0)
    success_path: str
    cancel_path: str

    @field_validator("plan_selector")
    @classmethod
    def _valid_selector(cls, value: str) -> str:
        if _IDENTIFIER_RE.fullmatch(value) is None:
            raise ValueError("invalid plan selector")
        return value

    @field_validator("success_path", "cancel_path")
    @classmethod
    def _valid_return_path(cls, value: str) -> str:
        if _RETURN_PATH_RE.fullmatch(value) is None:
            raise ValueError("return path must be a strict origin-relative path")
        segments = value.split("/")
        if any(segment in {".", ".."} for segment in segments):
            raise ValueError("return path cannot contain dot segments")
        return value


class StripeReadyPayload(BaseModel):
    """Selector-only readiness probe; it performs no Stripe egress."""

    model_config = ConfigDict(extra="forbid", strict=True)

    plan_selector: str

    @field_validator("plan_selector")
    @classmethod
    def _valid_selector(cls, value: str) -> str:
        if _IDENTIFIER_RE.fullmatch(value) is None:
            raise ValueError("invalid plan selector")
        return value


def _safe_failure(reason: str) -> dict[str, Any]:
    return {"ok": False, "error": reason}


def _return_origin(ctx: HostServiceContext, config: StripeAppConfig) -> str | None:
    candidates = config.allowed_return_origins.intersection(ctx.allowed_origins)
    if len(candidates) != 1:
        return None
    origin = next(iter(candidates))
    return origin if return_url_allowed(ctx, f"{origin}/") else None


def _new_stripe_request_key() -> str:
    """Per-call Stripe request key; webhook replay idempotency is handled separately."""
    return "disco-checkout-" + secrets.token_urlsafe(24)


@dataclass(frozen=True)
class _StripeRuntimeInputs:
    config: StripeAppConfig
    restricted_key: str
    return_origin: str
    binding_secret: str


def _runtime_inputs(
    ctx: HostServiceContext,
    plan_selector: str,
) -> tuple[_StripeRuntimeInputs | None, str]:
    config_store = ctx.stripe_config_store
    if config_store is None or ctx.secret_store is None or ctx.approvals is None:
        return None, "stripe_not_configured"
    config = config_store.get(ctx.owner_id, ctx.app_id)
    if config is None or not config.enabled:
        return None, "stripe_not_configured"
    if plan_selector != config.plan_selector:
        return None, "unknown_plan"
    origin = _return_origin(ctx, config)
    if origin is None:
        return None, "return_origin_not_allowed"
    try:
        secret = resolve_provider_secret(
            STRIPE_SECRET_REF,
            ctx.secret_store,
            strong_required=True,
        )
        binding_secret = resolve_stripe_binding_secret(
            ctx.secret_store,
            ctx.owner_id,
            ctx.app_id,
        )
    except RuntimeError:
        return None, "stripe_credential_refused"
    if secret is None or binding_secret is None:
        return None, "stripe_not_configured"
    try:
        validate_stripe_restricted_key(secret)
    except StripeConfigurationError:
        return None, "stripe_credential_refused"
    if not secret_ref_allowed_for_origin(STRIPE_SECRET_REF, STRIPE_API_URL):
        return None, "stripe_origin_refused"
    if not ctx.approvals.is_approved(
        STRIPE_API_URL,
        PAYMENTS_CHECKOUT_SERVICE_NAME,
        STRIPE_SECRET_REF,
    ):
        return None, "stripe_origin_not_approved"
    return _StripeRuntimeInputs(config, secret, origin, binding_secret), ""


def _parse_stripe_checkout_url(response: GuardedResponse) -> str | None:
    if not 200 <= response.status_code < 300:
        return None
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return None

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Stripe response key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise ValueError("non-finite JSON")

    try:
        body = json.loads(
            response.content.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        return None
    checkout_url = body["url"]
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in checkout_url):
        return None
    try:
        parsed = urlsplit(checkout_url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "checkout.stripe.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
    ):
        return None
    return checkout_url


async def _payments_checkout_handler(
    payload: dict[str, Any], ctx: HostServiceContext
) -> dict[str, Any]:
    runtime, error = _runtime_inputs(ctx, payload["plan_selector"])
    if runtime is None:
        return _safe_failure(error)
    success_url = runtime.return_origin + payload["success_path"]
    cancel_url = runtime.return_origin + payload["cancel_path"]
    if not return_url_allowed(ctx, success_url) or not return_url_allowed(ctx, cancel_url):
        return _safe_failure("return_url_not_allowed")
    user_id = int(payload["user_id"])
    correlation = stripe_correlation_tag(
        runtime.binding_secret,
        ctx.app_id,
        runtime.config.plan_selector,
        user_id,
    )

    form = urlencode(
        {
            "mode": "payment",
            "line_items[0][price]": runtime.config.stripe_price_id,
            "line_items[0][quantity]": "1",
            "client_reference_id": str(user_id),
            "metadata[disco_app_binding]": ctx.app_id,
            "metadata[disco_plan_selector]": runtime.config.plan_selector,
            "metadata[disco_correlation]": correlation,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
    ).encode("ascii")
    try:
        response = await asyncio.to_thread(
            guarded_request,
            "POST",
            STRIPE_API_URL,
            headers={
                "Authorization": f"Bearer {runtime.restricted_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Idempotency-Key": _new_stripe_request_key(),
            },
            body=form,
            allow_hosts=ctx.allow_hosts,
            timeout_s=ctx.request_timeout_s,
            max_redirects=0,
            max_bytes=_MAX_STRIPE_RESPONSE_BYTES,
        )
    except (EgressDenied, OSError, TimeoutError):
        return _safe_failure("stripe_unavailable")
    checkout_url = _parse_stripe_checkout_url(response)
    if checkout_url is None:
        return _safe_failure("stripe_invalid_response")
    return {"url": checkout_url}


async def _payments_ready_handler(
    payload: dict[str, Any], ctx: HostServiceContext
) -> dict[str, Any]:
    runtime, _error = _runtime_inputs(ctx, payload["plan_selector"])
    return {"ready": runtime is not None}


PAYMENTS_READY_SERVICE = HostServiceDefinition(
    name=PAYMENTS_READY_SERVICE_NAME,
    handler=_payments_ready_handler,
    description="Check host-owned Stripe configuration without creating a Session.",
    payload_schema=StripeReadyPayload,
)
register_host_service(PAYMENTS_READY_SERVICE)


PAYMENTS_CHECKOUT_SERVICE = HostServiceDefinition(
    name=PAYMENTS_CHECKOUT_SERVICE_NAME,
    handler=_payments_checkout_handler,
    description="Create a Stripe Checkout Session from host-owned plan configuration.",
    payload_schema=StripeCheckoutPayload,
)
register_host_service(PAYMENTS_CHECKOUT_SERVICE)


__all__ = [
    "PAYMENTS_CHECKOUT_SERVICE",
    "PAYMENTS_CHECKOUT_SERVICE_NAME",
    "PAYMENTS_READY_SERVICE",
    "PAYMENTS_READY_SERVICE_NAME",
    "STRIPE_API_HOSTS",
    "STRIPE_API_URL",
    "STRIPE_SECRET_REF",
    "StripeAppConfig",
    "StripeAppConfigStore",
    "StripeCheckoutPayload",
    "StripeConfigurationError",
    "StripeReadyPayload",
    "configure_stripe_restricted_key",
    "ensure_stripe_binding_secret",
    "resolve_stripe_binding_secret",
    "stripe_binding_secret_ref",
    "stripe_correlation_tag",
    "validate_stripe_restricted_key",
]

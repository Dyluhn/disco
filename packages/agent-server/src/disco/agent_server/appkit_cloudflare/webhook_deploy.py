"""Fail-closed generic-webhook bindings for Cloudflare deployments.

Runtime signing keys, outbound destinations, and host-service capabilities are
host-owned.  This module snapshots them before the untrusted build, rechecks
them afterwards, and sends secret values to wrangler only through the caller's
stdin-backed secret writer.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from disco.core.appkit.spec import AppSpec, WebhookMeta
from disco.core.env import disco_env
from disco.core.host_egress import origin_for_url
from disco.core.llm.secret_refs import secret_ref_allowed_for_origin
from disco.core.llm.secrets import SecretStore
from disco.core.origin_approvals import OriginApprovalStore
from disco.core.webhook_host_service import (
    WEBHOOK_EMIT_SERVICE_NAME,
    WEBHOOK_PURPOSE,
    WebhookAppConfigStore,
    WebhookTargetConfig,
    resolve_webhook_inbound_secret,
    webhook_secret_ref,
)

from ..host_token_store import HostTokenStore

SecretPut = Callable[[str, str], Awaitable[bool]]
MutationRecord = Callable[[str], None]


def _discard_mutation(_name: str) -> None:
    return None


class WebhookDeployError(RuntimeError):
    """A webhook deployment precondition or binding step failed safely."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step = step
        self.detail = detail


def _fingerprint(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _valid_signing_secret(value: str) -> bool:
    return 32 <= len(value) <= 512 and all(0x21 <= ord(ch) <= 0x7E for ch in value)


def _valid_target_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and len(value) <= 2048
    )


def _canonical_https_origin(value: str, *, label: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise WebhookDeployError(label, f"{label} is not a valid URL") from exc
    origin = origin_for_url(value)
    if (
        origin is None
        or parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise WebhookDeployError(label, f"{label} must be a canonical HTTPS origin")
    return origin


def configured_webhook_bus_origin() -> str:
    value = disco_env("SVC_BUS_PUBLIC_URL")
    if value is None or not value.strip():
        raise WebhookDeployError(
            "webhook_runtime_config",
            "DISCO_SVC_BUS_PUBLIC_URL is required for an outbound webhook deployment",
        )
    return _canonical_https_origin(value.strip(), label="Webhook public host-service bus URL")


@dataclass(frozen=True)
class _OutboundSnapshot:
    config: WebhookTargetConfig
    secret_fingerprint: bytes


@dataclass(frozen=True)
class _WebhookSnapshot:
    meta: WebhookMeta
    inbound_fingerprint: bytes | None
    outbound: tuple[_OutboundSnapshot, ...]


@dataclass(frozen=True)
class WebhookDeployDependencies:
    """Host services required only by webhook-bearing deployments."""

    token_store: HostTokenStore
    config_store: WebhookAppConfigStore
    approvals: OriginApprovalStore | None = None


@dataclass(frozen=True)
class WebhookDeployContext:
    """Trusted request identity bound to webhook deployment services."""

    dependencies: WebhookDeployDependencies
    owner_id: str | None
    conversation_id: str


class WebhookDeploymentLifecycle:
    """One deployment's write-only webhook binding and rotation state."""

    def __init__(
        self,
        *,
        app_spec: AppSpec,
        owner_id: str,
        conversation_id: str,
        secret_store: SecretStore,
        config_store: WebhookAppConfigStore,
        token_store: HostTokenStore,
        approvals: OriginApprovalStore | None = None,
    ) -> None:
        meta = app_spec.webhooks
        if meta is None:
            raise ValueError("WebhookDeploymentLifecycle requires webhook metadata")
        if app_spec.app_kind != "records" or not app_spec.roles:
            raise WebhookDeployError(
                "webhook_runtime_config",
                "Webhook deployment requires a session-authenticated records app",
            )
        if not owner_id or not conversation_id:
            raise WebhookDeployError(
                "webhook_runtime_config",
                "Webhook deployment requires a trusted conversation owner and id",
            )
        self._owner_id = owner_id
        self._conversation_id = conversation_id
        self._secret_store = secret_store
        self._config_store = config_store
        self._token_store = token_store
        self._approvals = approvals
        self._audience = meta.app_binding
        self._has_inbound = any(endpoint.direction == "inbound" for endpoint in meta.endpoints)
        self._has_outbound = any(endpoint.direction == "outbound" for endpoint in meta.endpoints)
        if self._has_outbound:
            try:
                self._token_store.list_for_conversation(self._conversation_id)
            except Exception as exc:
                raise WebhookDeployError(
                    "webhook_runtime_config",
                    "Webhook deployed-token store is unavailable",
                ) from exc
        self._bus_origin = configured_webhook_bus_origin() if self._has_outbound else None
        self._snapshot = self._read_snapshot(meta)
        self._candidate_selector: str | None = None
        self._rotation_finished = False
        self._worker_active = False
        self._record_mutation: MutationRecord = _discard_mutation

    @property
    def audience(self) -> str:
        return self._audience

    @property
    def required_services(self) -> frozenset[str]:
        return frozenset({WEBHOOK_EMIT_SERVICE_NAME}) if self._has_outbound else frozenset()

    def _read_snapshot(self, meta: WebhookMeta) -> _WebhookSnapshot:
        inbound_fingerprint: bytes | None = None
        if any(endpoint.direction == "inbound" for endpoint in meta.endpoints):
            try:
                inbound = resolve_webhook_inbound_secret(
                    self._secret_store, self._owner_id, self._audience
                )
            except Exception as exc:
                raise WebhookDeployError(
                    "webhook_runtime_config", "Webhook inbound secret is unavailable"
                ) from exc
            if inbound is None:
                raise WebhookDeployError(
                    "webhook_runtime_config", "Webhook inbound secret is not configured"
                )
            inbound_fingerprint = _fingerprint(inbound)

        outbound: list[_OutboundSnapshot] = []
        for endpoint in meta.endpoints:
            if endpoint.direction != "outbound":
                continue
            try:
                config = self._config_store.get(
                    self._owner_id, self._audience, endpoint.endpoint_id
                )
            except Exception as exc:
                raise WebhookDeployError(
                    "webhook_runtime_config",
                    "Webhook outbound operator configuration is unavailable",
                ) from exc
            if config is None or not config.enabled:
                raise WebhookDeployError(
                    "webhook_runtime_config",
                    f"Webhook outbound endpoint {endpoint.endpoint_id!r} is missing or disabled",
                )
            expected_ref = webhook_secret_ref(self._owner_id, self._audience, endpoint.endpoint_id)
            if (
                config.owner_id != self._owner_id
                or config.audience != self._audience
                or config.endpoint_id != endpoint.endpoint_id
                or config.event_types != frozenset(endpoint.event_types)
                or config.secret_ref != expected_ref
                or not _valid_target_url(config.target_url)
                or not secret_ref_allowed_for_origin(config.secret_ref, config.target_url)
            ):
                raise WebhookDeployError(
                    "webhook_runtime_config",
                    f"Webhook outbound endpoint {endpoint.endpoint_id!r} does not match "
                    "the generated app",
                )
            try:
                secret = self._secret_store.get_secret(config.secret_ref, strong_required=True)
            except Exception as exc:
                raise WebhookDeployError(
                    "webhook_runtime_config",
                    f"Webhook outbound endpoint {endpoint.endpoint_id!r} secret is unavailable",
                ) from exc
            if secret is None or not _valid_signing_secret(secret):
                raise WebhookDeployError(
                    "webhook_runtime_config",
                    f"Webhook outbound endpoint {endpoint.endpoint_id!r} secret is not "
                    "configured safely",
                )
            if self._approvals is None or not self._approvals.is_approved(
                config.target_url, WEBHOOK_PURPOSE, config.secret_ref
            ):
                raise WebhookDeployError(
                    "webhook_runtime_config",
                    f"Webhook outbound endpoint {endpoint.endpoint_id!r} origin is not approved",
                )
            outbound.append(_OutboundSnapshot(config, _fingerprint(secret)))

        return _WebhookSnapshot(meta, inbound_fingerprint, tuple(outbound))

    def recheck_after_build(self, app_spec: AppSpec) -> None:
        if self._has_outbound:
            try:
                self._token_store.list_for_conversation(self._conversation_id)
            except Exception as exc:
                raise WebhookDeployError(
                    "webhook_runtime_drift",
                    "Webhook deployed-token store became unavailable during deployment",
                ) from exc
        meta = app_spec.webhooks
        if meta is None or meta != self._snapshot.meta:
            raise WebhookDeployError(
                "webhook_runtime_drift", "Webhook app metadata changed during deployment"
            )
        current = self._read_snapshot(meta)
        if current.meta != self._snapshot.meta or len(current.outbound) != len(
            self._snapshot.outbound
        ):
            raise WebhookDeployError(
                "webhook_runtime_drift",
                "Webhook operator configuration changed during deployment",
            )
        if (current.inbound_fingerprint is None) != (self._snapshot.inbound_fingerprint is None):
            raise WebhookDeployError(
                "webhook_runtime_drift",
                "Webhook operator configuration changed during deployment",
            )
        if (
            current.inbound_fingerprint is not None
            and self._snapshot.inbound_fingerprint is not None
        ):
            if not hmac.compare_digest(
                current.inbound_fingerprint, self._snapshot.inbound_fingerprint
            ):
                raise WebhookDeployError(
                    "webhook_runtime_drift",
                    "Webhook operator configuration changed during deployment",
                )
        for before, after in zip(self._snapshot.outbound, current.outbound, strict=True):
            if before.config != after.config or not hmac.compare_digest(
                before.secret_fingerprint, after.secret_fingerprint
            ):
                raise WebhookDeployError(
                    "webhook_runtime_drift",
                    "Webhook operator configuration changed during deployment",
                )

    def _inbound_secret(self) -> str | None:
        if not self._has_inbound:
            return None
        secret = resolve_webhook_inbound_secret(self._secret_store, self._owner_id, self._audience)
        fingerprint = self._snapshot.inbound_fingerprint
        if (
            secret is None
            or fingerprint is None
            or not hmac.compare_digest(_fingerprint(secret), fingerprint)
        ):
            raise WebhookDeployError(
                "webhook_runtime_drift", "Webhook inbound secret changed during deployment"
            )
        return secret

    async def install_fixed_bindings(
        self, put_secret: SecretPut, *, include_bus: bool = True
    ) -> None:
        inbound = self._inbound_secret()
        if inbound is not None and not await put_secret("WEBHOOK_SIGNING_SECRET", inbound):
            raise WebhookDeployError(
                "wrangler secret put WEBHOOK_SIGNING_SECRET",
                "could not install the webhook inbound signing binding",
            )
        if include_bus and self._bus_origin is not None:
            if not await put_secret("DISCO_SVC_BUS", self._bus_origin):
                raise WebhookDeployError(
                    "wrangler secret put DISCO_SVC_BUS",
                    "could not install the webhook host-service bus binding",
                )

    async def establish_disabled(self, put_secret: SecretPut) -> None:
        """Explicitly latch a fresh or existing Worker closed before activation."""
        self._worker_active = True
        if not await put_secret("WEBHOOK_RUNTIME_READY", "0"):
            raise WebhookDeployError(
                "wrangler secret put WEBHOOK_RUNTIME_READY",
                "could not establish the Worker's disabled webhook state",
            )

    async def enable(self, put_secret: SecretPut) -> None:
        self._worker_active = True
        if not await put_secret("WEBHOOK_RUNTIME_READY", "1"):
            raise WebhookDeployError(
                "wrangler secret put WEBHOOK_RUNTIME_READY",
                "the configured webhook Worker could not be enabled",
            )

    async def rotate_outbound_token(
        self,
        *,
        deployed_url: str | None,
        put_secret: SecretPut,
        record_mutation: MutationRecord = _discard_mutation,
    ) -> None:
        if not self._has_outbound:
            return
        if not deployed_url:
            raise WebhookDeployError(
                "webhook_runtime_config", "wrangler did not report a deployed Worker URL"
            )
        origin = _canonical_https_origin(deployed_url, label="Webhook deployed Worker origin")
        self._record_mutation = record_mutation
        self._record_mutation("host_token_candidate_mint_attempted")
        candidate = self._token_store.rotate(
            self._conversation_id,
            self._owner_id,
            self._audience,
            allowed_services=self.required_services,
            allowed_origins=frozenset({origin}),
            kind="deployed",
        )
        record = self._token_store.verify(candidate)
        if record is None:
            raise WebhookDeployError(
                "webhook_token_rotation", "new webhook host-service capability is not active"
            )
        self._candidate_selector = record.selector
        self._record_mutation("host_token_candidate_minted")
        if not await put_secret("DISCO_SVC_TOKEN", candidate):
            raise WebhookDeployError(
                "wrangler secret put DISCO_SVC_TOKEN",
                "could not install the new webhook host-service capability",
            )
        self._record_mutation("host_token_rotation_finish_attempted")
        self._token_store.finish_rotation(
            self._conversation_id, self._audience, keep_selector=record.selector
        )
        self._rotation_finished = True
        self._record_mutation("host_token_rotation_finished")

    async def fail_closed(self, put_secret: SecretPut) -> bool:
        ready_zero_confirmed = not self._worker_active
        if self._worker_active:
            try:
                ready_zero_confirmed = await put_secret("WEBHOOK_RUNTIME_READY", "0")
            except Exception:
                ready_zero_confirmed = False
        candidate_safe = True
        if self._candidate_selector is None or self._rotation_finished:
            return ready_zero_confirmed
        try:
            self._record_mutation("host_token_candidate_revoke_attempted")
            if self._token_store.revoke(self._candidate_selector):
                self._record_mutation("host_token_candidate_revoked")
            candidate_safe = self._token_store.verify(self._candidate_selector) is None
        except Exception:
            candidate_safe = False
        return ready_zero_confirmed and candidate_safe


def webhook_lifecycle_for(
    app_spec: AppSpec,
    *,
    owner_id: str | None,
    conversation_id: str | None,
    secret_store: SecretStore,
    dependencies: WebhookDeployDependencies | None,
) -> WebhookDeploymentLifecycle | None:
    """Return ``None`` for non-webhook specs; otherwise require all host wiring."""
    if app_spec.webhooks is None:
        return None
    if dependencies is None or owner_id is None or conversation_id is None:
        raise WebhookDeployError(
            "webhook_runtime_config", "Webhook deployment services are not wired on this host"
        )
    return WebhookDeploymentLifecycle(
        app_spec=app_spec,
        owner_id=owner_id,
        conversation_id=conversation_id,
        secret_store=secret_store,
        config_store=dependencies.config_store,
        token_store=dependencies.token_store,
        approvals=dependencies.approvals,
    )


__all__ = [
    "WebhookDeployContext",
    "WebhookDeployDependencies",
    "WebhookDeployError",
    "WebhookDeploymentLifecycle",
    "configured_webhook_bus_origin",
    "webhook_lifecycle_for",
]

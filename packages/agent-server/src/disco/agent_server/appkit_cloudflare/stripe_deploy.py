"""Fail-closed Stripe lifecycle for a Cloudflare Worker deployment.

The generated tree never receives these values.  They move only from the host
``SecretStore`` to ``wrangler secret put`` over stdin, then the newly active
Worker proves the complete binding through its owner-authenticated runtime
probe before checkout is enabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from disco.core.appkit.spec import AppSpec, StripeMeta
from disco.core.env import disco_env
from disco.core.host_egress import origin_for_url
from disco.core.llm.secrets import SecretStore
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    PAYMENTS_READY_SERVICE_NAME,
    StripeAppConfig,
    StripeAppConfigStore,
    resolve_stripe_binding_secret,
    resolve_stripe_webhook_secret,
)

from ..host_token_store import HostTokenStore

_RUNTIME_PROBE_PATH = "/api/stripe/runtime-probe"
_MAX_PROBE_BODY_BYTES = 4096
_DEPLOYED_SERVICES = frozenset({PAYMENTS_READY_SERVICE_NAME, PAYMENTS_CHECKOUT_SERVICE_NAME})


class StripeDeployError(RuntimeError):
    """A Stripe deployment precondition or activation step failed safely."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step = step
        self.detail = detail


class StripeWorkerProbe(Protocol):
    """Prove the active Worker can traverse its newly installed A2 capability."""

    async def payments_ready(self, deployed_origin: str, admin_token: str) -> bool: ...


class HttpStripeWorkerProbe:
    """HTTPS-only production probe for the canonical admin-protected Worker seam."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def payments_ready(self, deployed_origin: str, admin_token: str) -> bool:
        try:
            origin = _canonical_https_origin(deployed_origin, label="Stripe deployed Worker origin")
        except StripeDeployError:
            return False
        url = origin + _RUNTIME_PROBE_PATH
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "Authorization": f"Bearer {admin_token}",
                        "Accept": "application/json",
                        "Cache-Control": "no-store",
                    },
                ) as response:
                    if response.status_code != 200:
                        return False
                    media_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if media_type.strip().lower() != "application/json":
                        return False
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_PROBE_BODY_BYTES:
                            return False
                        body.extend(chunk)
        except (httpx.HTTPError, ValueError):
            return False

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate runtime-probe key")
                result[key] = value
            return result

        try:
            decoded = json.loads(
                bytes(body).decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant {value!r}")
                ),
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return False
        return decoded == {"ready": True}


SecretPut = Callable[[str, str], Awaitable[bool]]
MutationRecord = Callable[[str], None]


def _discard_mutation(_name: str) -> None:
    return None


def _canonical_https_origin(value: str, *, label: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise StripeDeployError(label, f"{label} is not a valid URL") from exc
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
        raise StripeDeployError(label, f"{label} must be a canonical HTTPS origin")
    return origin


def configured_public_bus_origin() -> str:
    """Read the public A2 bus origin through the project env compatibility shim."""
    value = disco_env("SVC_BUS_PUBLIC_URL")
    if value is None or not value.strip():
        raise StripeDeployError(
            "stripe_runtime_config",
            "DISCO_SVC_BUS_PUBLIC_URL is required for a Stripe deployment",
        )
    return _canonical_https_origin(value.strip(), label="Stripe public host-service bus URL")


def _secret_fingerprint(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


@dataclass(frozen=True)
class _StripeSnapshot:
    meta: StripeMeta
    config: StripeAppConfig
    webhook_fingerprint: bytes
    binding_fingerprint: bytes


@dataclass(frozen=True)
class StripeDeployDependencies:
    """Host-only services needed solely by Stripe-bearing deployments."""

    token_store: HostTokenStore
    config_store: StripeAppConfigStore
    probe: StripeWorkerProbe | None = None


@dataclass(frozen=True)
class StripeDeployContext:
    """Trusted request identity bound to the host deployment services."""

    dependencies: StripeDeployDependencies
    owner_id: str | None
    conversation_id: str


class StripeDeploymentLifecycle:
    """One deployment's in-memory, write-only Stripe activation state."""

    def __init__(
        self,
        *,
        app_spec: AppSpec,
        owner_id: str,
        conversation_id: str,
        secret_store: SecretStore,
        config_store: StripeAppConfigStore,
        token_store: HostTokenStore,
        probe: StripeWorkerProbe,
    ) -> None:
        meta = app_spec.stripe
        if meta is None:
            raise ValueError("StripeDeploymentLifecycle requires Stripe metadata")
        if not owner_id or not conversation_id:
            raise StripeDeployError(
                "stripe_runtime_config",
                "Stripe deployment requires a trusted conversation owner and id",
            )
        self._owner_id = owner_id
        self._conversation_id = conversation_id
        self._secret_store = secret_store
        self._config_store = config_store
        self._token_store = token_store
        self._probe = probe
        self._audience = meta.app_binding
        self._bus_origin = configured_public_bus_origin()
        self._snapshot = self._read_snapshot(meta)
        self._candidate_selector: str | None = None
        self._rotation_finished = False
        self._worker_active = False
        self._record_mutation: MutationRecord = _discard_mutation

    @property
    def audience(self) -> str:
        return self._audience

    def _read_snapshot(self, meta: StripeMeta) -> _StripeSnapshot:
        try:
            config = self._config_store.get(self._owner_id, self._audience)
        except Exception as exc:
            raise StripeDeployError(
                "stripe_runtime_config", "Stripe operator configuration is unavailable"
            ) from exc
        if config is None or not config.enabled:
            raise StripeDeployError(
                "stripe_runtime_config", "Stripe operator configuration is missing or disabled"
            )
        if config.plan_selector != meta.plan_selector:
            raise StripeDeployError(
                "stripe_runtime_config",
                "Stripe operator plan selector does not match the generated app",
            )
        try:
            binding = resolve_stripe_binding_secret(
                self._secret_store, self._owner_id, self._audience
            )
            webhook = resolve_stripe_webhook_secret(
                self._secret_store, self._owner_id, self._audience
            )
        except Exception as exc:
            raise StripeDeployError(
                "stripe_runtime_config", "Stripe deployment secrets are unavailable"
            ) from exc
        if binding is None or webhook is None:
            raise StripeDeployError(
                "stripe_runtime_config", "Stripe deployment secrets are not configured"
            )
        return _StripeSnapshot(
            meta=meta,
            config=config,
            webhook_fingerprint=_secret_fingerprint(webhook),
            binding_fingerprint=_secret_fingerprint(binding),
        )

    def recheck_after_build(self, app_spec: AppSpec) -> None:
        """Refuse config/spec/secret drift after untrusted build execution."""
        meta = app_spec.stripe
        if meta is None or meta != self._snapshot.meta:
            raise StripeDeployError(
                "stripe_runtime_drift", "Stripe app metadata changed during deployment"
            )
        current = self._read_snapshot(meta)
        if current.config != self._snapshot.config or not (
            hmac.compare_digest(current.webhook_fingerprint, self._snapshot.webhook_fingerprint)
            and hmac.compare_digest(current.binding_fingerprint, self._snapshot.binding_fingerprint)
        ):
            raise StripeDeployError(
                "stripe_runtime_drift", "Stripe operator configuration changed during deployment"
            )

    async def quiesce_existing_worker(self, put_secret: SecretPut) -> None:
        self._worker_active = True
        if not await put_secret("STRIPE_RUNTIME_READY", "0"):
            raise StripeDeployError(
                "wrangler secret put STRIPE_RUNTIME_READY",
                "could not disable Stripe before replacing the existing Worker",
            )

    def _deployed_origin(self, deployed_url: str | None) -> str:
        if not deployed_url:
            raise StripeDeployError(
                "stripe_runtime_config", "wrangler did not report a deployed Worker URL"
            )
        origin = _canonical_https_origin(deployed_url, label="Stripe deployed Worker origin")
        if origin not in self._snapshot.config.allowed_return_origins:
            raise StripeDeployError(
                "stripe_runtime_config",
                "Stripe operator configuration does not allow the deployed Worker origin",
            )
        return origin

    def _secret_values(self) -> tuple[str, str]:
        binding = resolve_stripe_binding_secret(self._secret_store, self._owner_id, self._audience)
        webhook = resolve_stripe_webhook_secret(self._secret_store, self._owner_id, self._audience)
        if binding is None or webhook is None:
            raise StripeDeployError(
                "stripe_runtime_drift", "Stripe deployment secrets changed during deployment"
            )
        if not (
            hmac.compare_digest(_secret_fingerprint(binding), self._snapshot.binding_fingerprint)
            and hmac.compare_digest(
                _secret_fingerprint(webhook), self._snapshot.webhook_fingerprint
            )
        ):
            raise StripeDeployError(
                "stripe_runtime_drift", "Stripe deployment secrets changed during deployment"
            )
        return binding, webhook

    async def activate(
        self,
        *,
        deployed_url: str | None,
        admin_token: str,
        fresh_worker: bool,
        put_secret: SecretPut,
        record_mutation: MutationRecord = _discard_mutation,
        additional_services: frozenset[str] = frozenset(),
    ) -> None:
        """Install bindings, prove the active Worker, rotate, then enable last."""
        self._worker_active = True
        self._record_mutation = record_mutation
        origin = self._deployed_origin(deployed_url)
        binding, webhook = self._secret_values()
        if fresh_worker and not await put_secret("STRIPE_RUNTIME_READY", "0"):
            raise StripeDeployError(
                "wrangler secret put STRIPE_RUNTIME_READY",
                "could not establish the fresh Worker's disabled Stripe state",
            )
        fixed_secrets = (
            ("ADMIN_TOKEN", admin_token),
            ("DISCO_SVC_BUS", self._bus_origin),
            ("STRIPE_WEBHOOK_SECRET", webhook),
            ("STRIPE_APP_BINDING_SECRET", binding),
        )
        for name, value in fixed_secrets:
            if not await put_secret(name, value):
                raise StripeDeployError(
                    f"wrangler secret put {name}",
                    f"could not install required Worker binding {name}",
                )
        self._record_mutation("host_token_candidate_mint_attempted")
        candidate = self._token_store.rotate(
            self._conversation_id,
            self._owner_id,
            self._audience,
            allowed_services=_DEPLOYED_SERVICES | additional_services,
            allowed_origins=frozenset({origin}),
            kind="deployed",
        )
        record = self._token_store.verify(candidate)
        if record is None:
            raise StripeDeployError(
                "stripe_token_rotation", "new Stripe host-service capability is not active"
            )
        self._candidate_selector = record.selector
        self._record_mutation("host_token_candidate_minted")
        if not await put_secret("DISCO_SVC_TOKEN", candidate):
            raise StripeDeployError(
                "wrangler secret put DISCO_SVC_TOKEN",
                "could not install the new Stripe host-service capability",
            )
        if not await self._probe.payments_ready(origin, admin_token):
            raise StripeDeployError(
                "stripe_runtime_probe",
                "the active Worker could not prove payments.ready through its new capability",
            )
        self._record_mutation("host_token_rotation_finish_attempted")
        self._token_store.finish_rotation(
            self._conversation_id,
            self._audience,
            keep_selector=record.selector,
        )
        self._rotation_finished = True
        self._record_mutation("host_token_rotation_finished")
        if not await put_secret("STRIPE_RUNTIME_READY", "1"):
            raise StripeDeployError(
                "wrangler secret put STRIPE_RUNTIME_READY",
                "the verified Stripe Worker could not be enabled",
            )

    async def fail_closed(self, put_secret: SecretPut) -> bool:
        """Best-effort disable; return whether READY=0 was positively installed."""
        ready_zero_confirmed = not self._worker_active
        candidate_safe = True
        if self._worker_active:
            try:
                ready_zero_confirmed = await put_secret("STRIPE_RUNTIME_READY", "0")
            except Exception:
                ready_zero_confirmed = False
        if self._candidate_selector is not None and not self._rotation_finished:
            try:
                self._record_mutation("host_token_candidate_revoke_attempted")
                if self._token_store.revoke(self._candidate_selector):
                    self._record_mutation("host_token_candidate_revoked")
                candidate_safe = self._token_store.verify(self._candidate_selector) is None
            except Exception:
                candidate_safe = False
        return ready_zero_confirmed and candidate_safe


def stripe_lifecycle_for(
    app_spec: AppSpec,
    *,
    owner_id: str | None,
    conversation_id: str | None,
    secret_store: SecretStore,
    dependencies: StripeDeployDependencies | None,
) -> StripeDeploymentLifecycle | None:
    """Return ``None`` byte-for-byte for non-Stripe specs; otherwise fail closed."""
    if app_spec.stripe is None:
        return None
    if dependencies is None or owner_id is None or conversation_id is None:
        raise StripeDeployError(
            "stripe_runtime_config", "Stripe deployment services are not wired on this host"
        )
    return StripeDeploymentLifecycle(
        app_spec=app_spec,
        owner_id=owner_id,
        conversation_id=conversation_id,
        secret_store=secret_store,
        config_store=dependencies.config_store,
        token_store=dependencies.token_store,
        probe=dependencies.probe or HttpStripeWorkerProbe(),
    )


__all__ = [
    "HttpStripeWorkerProbe",
    "StripeDeployError",
    "StripeDeployDependencies",
    "StripeDeployContext",
    "StripeDeploymentLifecycle",
    "StripeWorkerProbe",
    "configured_public_bus_origin",
    "stripe_lifecycle_for",
]

"""Integrations repository split out of ConfigState (PY-0365): owner-scoped
Stripe checkout + generic webhook configuration, each writing an encrypted
credential and approving its exact origin. `ConfigState` exposes an instance
of this class as the plain `integrations` attribute.
"""

from __future__ import annotations

from disco.core.llm import ConfigStore, SecretStore
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    STRIPE_API_URL,
    STRIPE_SECRET_REF,
    StripeAppConfig,
    StripeAppConfigStore,
    configure_stripe_restricted_key,
    configure_stripe_webhook_secret,
    ensure_stripe_binding_secret,
)
from disco.core.webhook_host_service import (
    WEBHOOK_PURPOSE,
    WebhookAppConfigStore,
    WebhookTargetConfig,
    configure_webhook_inbound_secret,
)

from . import origin_approval_wiring as _origin_wiring


class ConfigIntegrations:
    """Owner-admin Stripe + webhook configuration over encrypted secrets."""

    def __init__(
        self,
        store: ConfigStore,
        secrets: SecretStore,
        *,
        stripe_configs: StripeAppConfigStore | None,
        webhook_configs: WebhookAppConfigStore | None,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._stripe_configs = stripe_configs
        self._webhook_configs = webhook_configs

    def _approve_origin(self, url: str, purpose: str, secret_ref: str | None = "") -> None:
        _origin_wiring.sign_origin(self._store, self._secrets, url, purpose, secret_ref)

    def configure_stripe_app(
        self,
        *,
        owner_id: str,
        audience: str,
        restricted_key: str,
        webhook_secret: str,
        plan_selector: str,
        stripe_price_id: str,
        allowed_return_origins: frozenset[str],
        enabled: bool,
    ) -> StripeAppConfig:
        """Owner-only settings seam; the key is write-only and separately encrypted."""
        if self._stripe_configs is None:
            raise RuntimeError("Stripe configuration store is not wired to shared host state")
        self._stripe_configs.validate_configuration(
            owner_id=owner_id,
            audience=audience,
            plan_selector=plan_selector,
            stripe_price_id=stripe_price_id,
            allowed_return_origins=allowed_return_origins,
            enabled=enabled,
        )
        configure_stripe_restricted_key(self._secrets, restricted_key)
        configure_stripe_webhook_secret(
            self._secrets,
            owner_id,
            audience,
            webhook_secret,
        )
        ensure_stripe_binding_secret(self._secrets, owner_id, audience)
        config = self._stripe_configs.configure(
            owner_id=owner_id,
            audience=audience,
            plan_selector=plan_selector,
            stripe_price_id=stripe_price_id,
            allowed_return_origins=allowed_return_origins,
            enabled=enabled,
            secret_store=self._secrets,
        )
        self._approve_origin(
            STRIPE_API_URL,
            PAYMENTS_CHECKOUT_SERVICE_NAME,
            STRIPE_SECRET_REF,
        )
        return config

    def configure_webhook_inbound(
        self,
        *,
        owner_id: str,
        audience: str,
        signing_secret: str,
    ) -> None:
        """Store an app-scoped inbound verifier secret without exposing it."""
        configure_webhook_inbound_secret(
            self._secrets,
            owner_id,
            audience,
            signing_secret,
        )

    def configure_webhook_outbound(
        self,
        *,
        owner_id: str,
        audience: str,
        endpoint_id: str,
        target_url: str,
        signing_secret: str,
        event_types: frozenset[str],
        enabled: bool,
    ) -> WebhookTargetConfig:
        """Store one owner/app-scoped outbound target and approve its exact origin."""
        if self._webhook_configs is None:
            raise RuntimeError("Webhook configuration store is not wired to shared host state")
        config = self._webhook_configs.configure(
            owner_id=owner_id,
            audience=audience,
            endpoint_id=endpoint_id,
            target_url=target_url,
            signing_secret=signing_secret,
            event_types=event_types,
            enabled=enabled,
            secret_store=self._secrets,
        )
        self._approve_origin(config.target_url, WEBHOOK_PURPOSE, config.secret_ref)
        return config

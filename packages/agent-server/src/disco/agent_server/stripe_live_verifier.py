"""Host-owned live verifier for the AppKit ``stripe`` security primitive.

The callback is wired into every ``DefaultToolExecutor`` created by
``ConversationRuntime`` (see ``runtime.py``).  When a ``verify_appkit_app`` run
hits a primitive whose ``live_verify_id`` is ``stripe.security.v1``, this
runner stands up a real generated Worker + local D1 + authenticated host-service
bus in an isolated temporary root, exercises the five mandatory adversarial
checks, and tears everything down.

All runtime secrets stay in a sealed inherited memory descriptor with no
filesystem name and are never written to the project tree. The host stores
used by the bus are temporary, so a live verification run never mutates the
server's production Stripe configuration.

The implementation owners live in the ``stripe_verification`` package:
``evidence`` (immutable evidence helpers), ``workerd_lifecycle`` (Worker
lifecycle), ``probe_script`` (Miniflare runtime probe), ``webhook_lifecycle``
(webhook lifecycle check), and ``checks`` (the five mandatory checks).  This
module retains the run orchestrator and the public verifier entry point.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets as _py_secrets
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from disco.core.appkit.primitives import PrimitiveVerifyResult, VerifyCheck
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.store.sqlite import SqliteEventStore
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    PAYMENTS_READY_SERVICE_NAME,
    STRIPE_API_URL,
    STRIPE_SECRET_REF,
    StripeAppConfigStore,
    StripeConfigurationError,
    configure_stripe_restricted_key,
    configure_stripe_webhook_secret,
    ensure_stripe_binding_secret,
)

from .host_token_store import HostTokenStore
from .stripe_verification.checks import (
    _check_forged_signature_rejected,
    _check_replay_deduped,
    _check_secret_absence,
)
from .stripe_verification.evidence import (
    StripeLiveVerifierError,
    _check_result,
    _find_wrangler,
    _free_port,
    _HostBusServer,
    _make_loopback_tls_pair,
    _scan_for_secrets,
)
from .stripe_verification.probe_script import _MiniflareRuntimeProbe
from .stripe_verification.webhook_lifecycle import _check_webhook_lifecycle
from .stripe_verification.workerd_lifecycle import _WorkerdApp

_STRIPE_LIVE_VERIFY_ID = "stripe.security.v1"

_LOG = logging.getLogger(__name__)

_REQUIRED_CHECKS = (
    "forged_signature_rejected",
    "replay_deduped",
    "secret_absence",
    "restricted_key_only",
    "price_injection_refused",
)


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    by_name = {check.name: check for check in checks}
    ordered = [
        by_name.get(name, _check_result(name, False, "check missing")) for name in _REQUIRED_CHECKS
    ]
    passed = all(check.passed for check in ordered)
    return PrimitiveVerifyResult(
        ok=passed,
        detail=f"{sum(c.passed for c in ordered)}/{len(ordered)} checks passed",
        checks=tuple(ordered),
    )


async def _check_restricted_key_only(secret_store: SecretStore) -> VerifyCheck:
    """The host must accept only restricted ``rk_`` keys, never full ``sk_``."""
    sk_attempt = "sk_test_liveverifier_rejected_" + _py_secrets.token_urlsafe(16)
    try:
        configure_stripe_restricted_key(secret_store, sk_attempt)
    except StripeConfigurationError:
        return _check_result(
            "restricted_key_only",
            True,
            "full-access sk_ credential refused; restricted rk_ credential accepted",
        )
    return _check_result(
        "restricted_key_only",
        False,
        "a full-access sk_ credential was accepted (blast radius too large)",
    )


async def _check_price_injection_refused(bus: _HostBusServer, token: str) -> VerifyCheck:
    """The host handler must reject client-chosed price/amount before egress."""
    status, _body = await bus._post(
        PAYMENTS_CHECKOUT_SERVICE_NAME,
        {
            "plan_selector": "pro",
            "user_id": 1,
            "success_path": "/",
            "cancel_path": "/",
            "binding_proof": "0" * 64,
            "webhook_proof": "0" * 64,
            "amount": 999,
            "currency": "usd",
            "price": "price_attacker",
        },
        token=token,
    )
    if status == 422:
        return _check_result(
            "price_injection_refused",
            True,
            "client-chosen price/amount rejected with 422 before Stripe egress",
        )
    return _check_result(
        "price_injection_refused",
        False,
        f"client-chosen price/amount was not refused (status {status})",
    )


class _StripeVerifyRun:
    """One complete isolated live-verification run."""

    def __init__(self, app: AppSpec, design: DesignSpec, tree: Mapping[str, str]) -> None:
        self.app = app
        self.design = design
        self.tree = tree
        self.tmp_root: Path | None = None
        self.secret_store: SecretStore | None = None
        self.config_store: ConfigStore | None = None
        self.event_store: SqliteEventStore | None = None
        self.token_store: HostTokenStore | None = None
        self.stripe_config_store: StripeAppConfigStore | None = None
        self.bus: _HostBusServer | None = None
        self.bus_cert: Path | None = None
        self.bus_key: Path | None = None
        self.worker: _WorkerdApp | None = None
        self.token: str | None = None
        self.token_selector: str | None = None
        self.admin_token: str = ""
        self.d1_control_token: str = ""
        self.owner_id: str = "stripe-live-owner"
        self.conversation_id: str = "stripe-live-verify"
        self.audience: str = ""
        self.origin: str = ""
        self.worker_port: int | None = None
        self.webhook_secret: str = ""
        self.binding_secret: str = ""
        self.restricted_key: str = ""
        self.price_id: str = ""
        self.exact_secrets: list[str] = []

    async def run(self) -> PrimitiveVerifyResult:
        checks: list[VerifyCheck] = []
        try:
            self._ensure_prerequisites()
            stripe = self.app.stripe
            assert stripe is not None
            self._setup_temp_stores()
            restricted_check = await self._configure_host_plane()
            checks.append(restricted_check)

            # Price injection is exercised against the host bus directly.
            assert self.bus is not None
            assert self.token is not None
            price_check = await _check_price_injection_refused(self.bus, self.token)
            checks.append(price_check)

            # Worker-level checks require wrangler dev to be running.
            assert self.tmp_root is not None
            if self.worker_port is None:
                raise StripeLiveVerifierError("Worker port was not reserved")
            if self.bus_cert is None:
                raise StripeLiveVerifierError("loopback bus certificate was not initialized")
            self.worker = _WorkerdApp(
                self.tree,
                self._env_bindings(),
                self.tmp_root,
                self.bus_cert,
            )
            self.worker.__enter__()
            try:
                self.worker.boot(self.worker_port)
                with _MiniflareRuntimeProbe(
                    self.worker, self.tmp_root, self.d1_control_token
                ) as runtime_probe:
                    runtime_ready = await asyncio.to_thread(
                        runtime_probe.payments_ready, self.admin_token
                    )
                    checks.append(
                        await _check_forged_signature_rejected(
                            runtime_probe,
                            self.webhook_secret,
                            stripe.app_binding,
                            stripe.plan_selector,
                            self.binding_secret,
                        )
                    )
                    replay = await _check_replay_deduped(
                        runtime_probe,
                        self.webhook_secret,
                        stripe.app_binding,
                        stripe.plan_selector,
                        self.binding_secret,
                        self.admin_token,
                    )
                    if not runtime_ready:
                        replay = _check_result(
                            "replay_deduped",
                            False,
                            "the deployed Worker bundle could not prove payments.ready through "
                            "the authenticated loopback host bus",
                        )
                    checks.append(replay)
                    if runtime_ready:
                        lifecycle = await _check_webhook_lifecycle(
                            runtime_probe,
                            self.webhook_secret,
                            stripe.app_binding,
                            stripe.plan_selector,
                            self.binding_secret,
                        )
                        if not lifecycle.passed:
                            raise StripeLiveVerifierError(lifecycle.evidence)
                    checks.append(
                        await _check_secret_absence(
                            self.tree, self.worker, self.exact_secrets, self.tmp_root
                        )
                    )
            finally:
                try:
                    self.worker.__exit__(None, None, None)
                except Exception:
                    # Keep the handle for _cleanup() to make a second process
                    # group termination attempt before revoking credentials.
                    raise
                else:
                    self.worker = None
        except StripeLiveVerifierError as exc:
            _LOG.warning("Stripe live verifier failed: %s", exc)
            checks.extend(_check_result(name, False, str(exc)) for name in _REQUIRED_CHECKS)
        finally:
            await self._cleanup()

        return _result(checks)

    def _ensure_prerequisites(self) -> None:
        if self.app is None or self.app.stripe is None:
            raise StripeLiveVerifierError("AppSpec.stripe metadata is missing")
        if self.app.app_kind != "records":
            raise StripeLiveVerifierError("Stripe live verifier requires a records app")
        if not _find_wrangler(Path.cwd()):
            raise StripeLiveVerifierError("wrangler executable is not available")

    def _setup_temp_stores(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="disco-stripe-live-"))
        self.bus_cert, self.bus_key = _make_loopback_tls_pair(self.tmp_root)
        app_secret = base64.b64encode(_py_secrets.token_bytes(32)).decode("ascii")
        self.secret_store = SecretStore(
            path=self.tmp_root / "secrets.json",
            box=SecretBox(app_secret),
        )
        self.config_store = ConfigStore(path=self.tmp_root / "disco-config.json")
        self.event_store = SqliteEventStore(self.tmp_root / "events.db")
        self.event_store.create_conversation(self.conversation_id, owner_id=self.owner_id)
        self.token_store = HostTokenStore(self.tmp_root / "tokens.db")
        self.stripe_config_store = StripeAppConfigStore(self.tmp_root / "stripe.db")
        stripe = self.app.stripe
        if stripe is None:
            raise StripeLiveVerifierError("AppSpec.stripe metadata is missing")
        self.audience = stripe.app_binding
        self.worker_port = _free_port()
        self.origin = f"http://127.0.0.1:{self.worker_port}"
        self.admin_token = "adm_" + _py_secrets.token_urlsafe(24)
        self.d1_control_token = "d1ctl_" + _py_secrets.token_urlsafe(24)

    async def _configure_host_plane(self) -> VerifyCheck:
        assert self.secret_store is not None
        assert self.config_store is not None
        assert self.event_store is not None
        assert self.token_store is not None
        assert self.stripe_config_store is not None
        assert self.bus_cert is not None and self.bus_key is not None
        stripe = self.app.stripe
        assert stripe is not None

        # Restricted-key-only gate: set the real key after proving sk_ is refused.
        restricted_check = await _check_restricted_key_only(self.secret_store)
        if not restricted_check.passed:
            raise StripeLiveVerifierError(restricted_check.evidence)
        self.restricted_key = "rk_test_liveverifier_" + _py_secrets.token_urlsafe(16)
        configure_stripe_restricted_key(self.secret_store, self.restricted_key)

        self.webhook_secret = "whsec_" + _py_secrets.token_urlsafe(32)
        configure_stripe_webhook_secret(
            self.secret_store, self.owner_id, self.audience, self.webhook_secret
        )
        self.binding_secret = ensure_stripe_binding_secret(
            self.secret_store, self.owner_id, self.audience
        )

        self.price_id = (
            "price_" + _py_secrets.token_urlsafe(16).replace("_", "").replace("-", "")[:24]
        )

        # Origin approval must exist before the checkout handler will egress.
        approval_store = self.config_store.approval_store(secret_store=self.secret_store)
        approval_store.approve(
            STRIPE_API_URL,
            PAYMENTS_CHECKOUT_SERVICE_NAME,
            STRIPE_SECRET_REF,
        )

        bus_port = _free_port()

        self.token = self.token_store.mint(
            self.conversation_id,
            self.owner_id,
            self.audience,
            allowed_services=frozenset(
                {PAYMENTS_CHECKOUT_SERVICE_NAME, PAYMENTS_READY_SERVICE_NAME}
            ),
            allowed_origins=frozenset({self.origin}),
            kind="probe",
        )
        parsed = self.token_store._parse(self.token)
        self.token_selector = parsed[0] if parsed else None

        self.bus = _HostBusServer(
            self.event_store,
            self.secret_store,
            self.config_store,
            self.token_store,
            self.stripe_config_store,
            bus_port,
            self.bus_cert,
            self.bus_key,
        )
        await self.bus.__aenter__()

        self.stripe_config_store.configure(
            owner_id=self.owner_id,
            audience=self.audience,
            plan_selector=stripe.plan_selector,
            stripe_price_id=self.price_id,
            allowed_return_origins=frozenset({self.origin}),
            enabled=True,
            secret_store=self.secret_store,
        )

        self.exact_secrets = [
            self.restricted_key,
            self.webhook_secret,
            self.binding_secret,
            self.token,
            self.admin_token,
            self.d1_control_token,
            self.secret_store.signing_secret or "",
        ]
        return restricted_check

    def _env_bindings(self) -> dict[str, str]:
        assert self.token is not None
        assert self.bus is not None
        return {
            "STRIPE_WEBHOOK_SECRET": self.webhook_secret,
            "STRIPE_APP_BINDING_SECRET": self.binding_secret,
            "STRIPE_RUNTIME_READY": "1",
            "DISCO_SVC_BUS": self.bus.origin,
            "DISCO_SVC_TOKEN": self.token,
            "ADMIN_TOKEN": self.admin_token,
            "DISCO_LIVE_D1_CONTROL_TOKEN": self.d1_control_token,
        }

    async def _cleanup(self) -> None:
        failures: list[str] = []
        if self.bus is not None:
            try:
                await self.bus.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - keep revocation and deletion running
                failures.append(type(exc).__name__)
            self.bus = None
        if self.worker is not None:
            try:
                self.worker.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - continue the security cleanup
                failures.append(type(exc).__name__)
            self.worker = None
        if self.token_store is not None and self.token_selector is not None:
            try:
                self.token_store.revoke(self.token_selector)
            except Exception as exc:  # noqa: BLE001 - report after remaining cleanup
                failures.append(type(exc).__name__)
        for store in (self.token_store, self.stripe_config_store, self.event_store):
            if store is not None:
                try:
                    store.close()
                except Exception as exc:  # noqa: BLE001 - report after remaining cleanup
                    failures.append(type(exc).__name__)
        if self.tmp_root is not None:
            try:
                shutil.rmtree(self.tmp_root)
            except OSError as exc:
                failures.append(type(exc).__name__)
            self.tmp_root = None
        if failures:
            raise StripeLiveVerifierError(
                "live verifier cleanup was incomplete (" + ", ".join(sorted(set(failures))) + ")"
            )


async def _stripe_live_verify(
    app: AppSpec,
    design: DesignSpec,
    tree: Mapping[str, str],
) -> PrimitiveVerifyResult:
    run = _StripeVerifyRun(app, design, tree)
    try:
        return await run.run()
    except Exception as exc:
        _LOG.exception("Stripe live verifier raised unexpectedly")
        return _result(
            [
                _check_result(
                    name, False, f"host live verifier raised {type(exc).__name__} (fail-closed)"
                )
                for name in _REQUIRED_CHECKS
            ]
        )


def make_stripe_live_verifier() -> Any:
    """Factory for the ``stripe.security.v1`` host-owned live verifier callback.

    Returns a ``PrimitiveLiveVerifier`` callable that the tools layer dispatches
    through ``ToolContext.primitive_live_verifier``.
    """

    async def verifier(
        live_id: str,
        app: AppSpec,
        design: DesignSpec,
        tree: Mapping[str, str],
    ) -> PrimitiveVerifyResult:
        if live_id != _STRIPE_LIVE_VERIFY_ID:
            return _result(
                [
                    _check_result(name, False, f"unknown live_verify_id {live_id!r}")
                    for name in _REQUIRED_CHECKS
                ]
            )
        return await _stripe_live_verify(app, design, tree)

    return verifier


# Reusable, host-owned exploit-harness pieces. They remain implementation
# details of the agent-server (never part of core or generated apps), but F3.3
# shares the exact materialize/build/workerd and secret-scan machinery so the
# two security primitives cannot drift onto subtly different proof paths.
LiveWorkerdApp = _WorkerdApp
find_wrangler = _find_wrangler
free_loopback_port = _free_port
make_loopback_tls_pair = _make_loopback_tls_pair
scan_live_paths_for_secrets = _scan_for_secrets

__all__ = [
    "LiveWorkerdApp",
    "StripeLiveVerifierError",
    "find_wrangler",
    "free_loopback_port",
    "make_loopback_tls_pair",
    "make_stripe_live_verifier",
    "scan_live_paths_for_secrets",
]

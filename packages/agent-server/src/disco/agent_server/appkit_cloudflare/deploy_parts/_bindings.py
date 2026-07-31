"""Worker secret/binding installation for the Stripe + webhook security primitives:
the redacting secret writer, the post-build binding recheck, and the fail-closed
activation/quiescence orchestration.

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports these names unchanged. ``_install_worker_bindings`` in particular was
split into several small, single-purpose helpers (one per original branch) so each
piece stays independently under the cyclomatic-complexity budget — the original
was one long function interleaving four largely-independent activation paths
(fresh-webhook establish, combined Stripe+webhook, legacy admin-only, webhook-only)
with a repeated "fail both lifecycles closed, else report which" pattern.
"""

from __future__ import annotations

from pathlib import Path

from disco.core.llm.secrets import SecretStore

from ...redaction import redact_text
from ..models import DeployPlan, DeployRefused, RefusalReason
from ..stripe_deploy import StripeDeployError, StripeDeploymentLifecycle
from ..webhook_deploy import WebhookDeployError, WebhookDeploymentLifecycle
from ..wrangler import BuildResult, CommandResult, CommandRunner
from ._constants import _HOST_TOKEN_LIFECYCLE_MUTATIONS
from ._deploy_record import _persist_record
from ._worker import (
    _assert_stripe_trusted_tree,
    _assert_webhook_trusted_tree,
    _load_guarded_app_spec,
)


def _record_deploy_step(
    transcript: list[str],
    label: str,
    res: CommandResult | BuildResult,
    secret_literals: tuple[str, ...],
    *,
    capture_output: bool = True,
) -> None:
    # ``capture_output=False`` records ONLY the label + returncode — used for the
    # `wrangler secret put ADMIN_TOKEN` step, whose stdout/stderr could contain
    # the admin token verbatim and must NEVER land in the transcript/record.
    if not capture_output:
        transcript.append(f"$ {label}\n[exit {res.returncode}]")
        return

    # Scrub literal secret values, then redact secret-shaped patterns, before
    # anything enters the transcript. wrangler may echo ids/urls too.
    out = res.stdout.strip()
    err = res.stderr.strip()
    for secret in secret_literals:
        out = out.replace(secret, "[REDACTED]")
        err = err.replace(secret, "[REDACTED]")
    out = redact_text(out)
    err = redact_text(err)
    transcript.append(f"$ {label}\n[exit {res.returncode}]\n" + out + ("\n" + err if err else ""))


class _DeploySecretWriter:
    """Write one Worker secret and durably record its NAME, never its value/output."""

    def __init__(
        self,
        runner: CommandRunner,
        wrangler_bin: str,
        staged: Path,
        deploy_env: dict[str, str],
        transcript: list[str],
        live_workspace: Path,
        record_rel: Path,
        plan: DeployPlan,
        mutations: list[str],
        store: SecretStore,
    ) -> None:
        self._runner = runner
        self._wrangler_bin = wrangler_bin
        self._staged = staged
        self._deploy_env = deploy_env
        self._transcript = transcript
        self._live_workspace = live_workspace
        self._record_rel = record_rel
        self._plan = plan
        self._mutations = mutations
        self._store = store
        self.effective_digest: str | None = None

    async def __call__(self, name: str, value: str) -> bool:
        self._record_named_mutation(f"secret_put_attempt:{name}")
        result = await self._runner.run(
            [self._wrangler_bin, "secret", "put", name],
            cwd=self._staged,
            env=self._deploy_env,
            stdin=value,
        )
        _record_deploy_step(
            self._transcript,
            f"wrangler secret put {name}",
            result,
            (),
            capture_output=False,
        )
        if not result.ok:
            return False
        self._record_named_mutation(f"secret_put:{name}")
        return True

    def _record_named_mutation(self, name: str) -> None:
        self._mutations.append(name)
        _persist_record(
            self._live_workspace,
            self._record_rel,
            self._plan,
            status="in_progress",
            mutations=self._mutations,
            effective_digest=self.effective_digest,
            store=self._store,
        )

    async def put_legacy_admin(self, value: str) -> CommandResult:
        """Preserve the pre-Stripe ADMIN_TOKEN behavior and mutation label."""
        result = await self._runner.run(
            [self._wrangler_bin, "secret", "put", "ADMIN_TOKEN"],
            cwd=self._staged,
            env=self._deploy_env,
            stdin=value,
        )
        _record_deploy_step(
            self._transcript,
            "wrangler secret put ADMIN_TOKEN",
            result,
            (),
            capture_output=False,
        )
        if result.ok:
            self._mutations.append("secret_put")
        return result

    def record_host_token_mutation(self, name: str) -> None:
        if name not in _HOST_TOKEN_LIFECYCLE_MUTATIONS:
            raise ValueError("invalid host-token lifecycle mutation name")
        self._record_named_mutation(name)


def _recheck_security_bindings_after_build(
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    staged: Path,
) -> None:
    app_spec = _load_guarded_app_spec(staged)
    if stripe_lifecycle is not None:
        _assert_stripe_trusted_tree(staged)
        try:
            stripe_lifecycle.recheck_after_build(app_spec)
        except StripeDeployError as exc:
            raise DeployRefused(RefusalReason.STRIPE_RUNTIME_CONFIG, exc.detail) from exc
    if webhook_lifecycle is not None:
        _assert_webhook_trusted_tree(staged)
        try:
            webhook_lifecycle.recheck_after_build(app_spec)
        except WebhookDeployError as exc:
            raise DeployRefused(RefusalReason.WEBHOOK_RUNTIME_CONFIG, exc.detail) from exc


async def _activate_stripe_worker(
    lifecycle: StripeDeploymentLifecycle,
    *,
    deployed_url: str | None,
    admin_token: str,
    fresh_worker: bool,
    writer: _DeploySecretWriter,
    additional_services: frozenset[str] = frozenset(),
) -> StripeDeployError | None:
    try:
        await lifecycle.activate(
            deployed_url=deployed_url,
            admin_token=admin_token,
            fresh_worker=fresh_worker,
            put_secret=writer,
            record_mutation=writer.record_host_token_mutation,
            additional_services=additional_services,
        )
    except StripeDeployError as exc:
        if await lifecycle.fail_closed(writer):
            return exc
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    except Exception:
        if await lifecycle.fail_closed(writer):
            return StripeDeployError(
                "stripe_runtime_activation",
                "Stripe activation failed unexpectedly and was disabled",
            )
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    return None


# ---- _install_worker_bindings, decomposed ------------------------------------


async def _fail_closed_both(
    stripe_lifecycle: StripeDeploymentLifecycle,
    webhook_lifecycle: WebhookDeploymentLifecycle,
    writer: _DeploySecretWriter,
) -> bool:
    """Fail-close BOTH lifecycles; True only if both report a safe (disabled) state."""
    stripe_safe = await stripe_lifecycle.fail_closed(writer)
    webhook_safe = await webhook_lifecycle.fail_closed(writer)
    return stripe_safe and webhook_safe


async def _establish_webhook_disabled_if_fresh(
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    fresh_worker: bool,
    writer: _DeploySecretWriter,
) -> WebhookDeployError | None:
    if webhook_lifecycle is None or not fresh_worker:
        return None
    try:
        await webhook_lifecycle.establish_disabled(writer)
    except WebhookDeployError as exc:
        if await webhook_lifecycle.fail_closed(writer):
            return exc
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    return None


async def _install_combo_shared_bindings(
    stripe_lifecycle: StripeDeploymentLifecycle,
    webhook_lifecycle: WebhookDeploymentLifecycle,
    writer: _DeploySecretWriter,
) -> WebhookDeployError | None:
    """Stripe owns the shared bus binding and the one candidate token; webhook
    contributes only its inbound key and service scope."""
    try:
        await webhook_lifecycle.install_fixed_bindings(writer, include_bus=False)
        return None
    except WebhookDeployError as exc:
        if await _fail_closed_both(stripe_lifecycle, webhook_lifecycle, writer):
            return exc
    except Exception:
        if await _fail_closed_both(stripe_lifecycle, webhook_lifecycle, writer):
            return WebhookDeployError(
                "webhook_runtime_activation",
                "Webhook activation failed unexpectedly and Stripe remained disabled",
            )
    return WebhookDeployError(
        "webhook_runtime_state_uncertain",
        "Webhook/Stripe activation state is uncertain; manual intervention is required.",
    )


async def _activate_combo_stripe(
    stripe_lifecycle: StripeDeploymentLifecycle,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    deployed_url: str | None,
    admin_token: str,
    fresh_worker: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    stripe_error = await _activate_stripe_worker(
        stripe_lifecycle,
        deployed_url=deployed_url,
        admin_token=admin_token,
        fresh_worker=fresh_worker,
        writer=writer,
        additional_services=(
            webhook_lifecycle.required_services if webhook_lifecycle is not None else frozenset()
        ),
    )
    if stripe_error is None:
        return None
    if webhook_lifecycle is not None and not await webhook_lifecycle.fail_closed(writer):
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    return stripe_error


async def _enable_combo_webhook(
    stripe_lifecycle: StripeDeploymentLifecycle,
    webhook_lifecycle: WebhookDeploymentLifecycle,
    writer: _DeploySecretWriter,
) -> WebhookDeployError | None:
    try:
        await webhook_lifecycle.enable(writer)
        return None
    except Exception:
        if await _fail_closed_both(stripe_lifecycle, webhook_lifecycle, writer):
            return WebhookDeployError(
                "wrangler secret put WEBHOOK_RUNTIME_READY",
                "the verified combined Worker could not enable webhook delivery",
            )
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook/Stripe activation state is uncertain; manual intervention is required.",
        )


async def _install_combined_stripe_webhook_bindings(
    stripe_lifecycle: StripeDeploymentLifecycle,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    deployed_url: str | None,
    admin_token: str,
    fresh_worker: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    if webhook_lifecycle is not None:
        shared_error = await _install_combo_shared_bindings(
            stripe_lifecycle, webhook_lifecycle, writer
        )
        if shared_error is not None:
            return shared_error
    stripe_error = await _activate_combo_stripe(
        stripe_lifecycle, webhook_lifecycle, deployed_url, admin_token, fresh_worker, writer
    )
    if stripe_error is not None:
        return stripe_error
    if webhook_lifecycle is not None:
        return await _enable_combo_webhook(stripe_lifecycle, webhook_lifecycle, writer)
    return None


async def _legacy_admin_secret_put(
    admin_token: str,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    result = await writer.put_legacy_admin(admin_token)
    if result.ok:
        return None
    if webhook_lifecycle is not None and not await webhook_lifecycle.fail_closed(writer):
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    return StripeDeployError(
        "wrangler secret put ADMIN_TOKEN",
        f"secret put failed (exit {result.returncode})",
    )


async def _webhook_solo_bindings(
    webhook_lifecycle: WebhookDeploymentLifecycle,
    deployed_url: str | None,
    writer: _DeploySecretWriter,
) -> WebhookDeployError | None:
    try:
        await webhook_lifecycle.install_fixed_bindings(writer)
        await webhook_lifecycle.rotate_outbound_token(
            deployed_url=deployed_url,
            put_secret=writer,
            record_mutation=writer.record_host_token_mutation,
        )
        await webhook_lifecycle.enable(writer)
        return None
    except WebhookDeployError as exc:
        if await webhook_lifecycle.fail_closed(writer):
            return exc
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    except Exception:
        if await webhook_lifecycle.fail_closed(writer):
            return WebhookDeployError(
                "webhook_runtime_activation", "Webhook activation failed unexpectedly"
            )
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )


async def _install_solo_worker_bindings(
    admin_token: str | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    deployed_url: str | None,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    if admin_token is not None:
        legacy_error = await _legacy_admin_secret_put(admin_token, webhook_lifecycle, writer)
        if legacy_error is not None:
            return legacy_error
    if webhook_lifecycle is None:
        return None
    return await _webhook_solo_bindings(webhook_lifecycle, deployed_url, writer)


async def _install_worker_bindings(
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    deployed_url: str | None,
    admin_token: str | None,
    fresh_worker: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    fresh_error = await _establish_webhook_disabled_if_fresh(
        webhook_lifecycle, fresh_worker, writer
    )
    if fresh_error is not None:
        return fresh_error
    if stripe_lifecycle is not None and admin_token is not None:
        return await _install_combined_stripe_webhook_bindings(
            stripe_lifecycle, webhook_lifecycle, deployed_url, admin_token, fresh_worker, writer
        )
    return await _install_solo_worker_bindings(admin_token, webhook_lifecycle, deployed_url, writer)


# ---- pre-deploy quiescence of an EXISTING worker's security primitives -------


async def _quiesce_stripe_worker(
    lifecycle: StripeDeploymentLifecycle | None,
    worker_exists: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | None:
    if lifecycle is None or not worker_exists:
        return None
    try:
        await lifecycle.quiesce_existing_worker(writer)
    except StripeDeployError as exc:
        if await lifecycle.fail_closed(writer):
            return exc
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    except Exception:
        if await lifecycle.fail_closed(writer):
            return StripeDeployError(
                "stripe_runtime_quiesce", "Stripe could not be disabled before deployment"
            )
        return StripeDeployError(
            "stripe_runtime_state_uncertain",
            "Stripe activation state is uncertain; manual intervention is required.",
        )
    return None


async def _quiesce_security_workers(
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
    worker_exists: bool,
    writer: _DeploySecretWriter,
) -> StripeDeployError | WebhookDeployError | None:
    stripe_error = await _quiesce_stripe_worker(stripe_lifecycle, worker_exists, writer)
    if stripe_error is not None:
        return stripe_error
    if webhook_lifecycle is None or not worker_exists:
        return None
    try:
        await webhook_lifecycle.establish_disabled(writer)
    except WebhookDeployError as exc:
        if await webhook_lifecycle.fail_closed(writer):
            return exc
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    except Exception:
        if await webhook_lifecycle.fail_closed(writer):
            return WebhookDeployError(
                "webhook_runtime_quiesce", "Webhook could not be disabled before deployment"
            )
        return WebhookDeployError(
            "webhook_runtime_state_uncertain",
            "Webhook activation state is uncertain; manual intervention is required.",
        )
    return None

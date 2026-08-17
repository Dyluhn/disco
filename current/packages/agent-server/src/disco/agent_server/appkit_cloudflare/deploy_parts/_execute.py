"""The HARD-GATED executor's outer shell: ``execute_deploy`` (the gate chain) and
the deploy-lock/record-scope orchestration that stages the tree, runs the
pre-build guards, and hands off to :func:`_execute_phases._run_real_deploy` (the
actual wrangler mutation sequence — kept in a sibling module since together the
two exceeded the module logical-line budget).

Extracted from ``deploy.py`` to reduce module complexity; the public facade
re-imports ``execute_deploy`` unchanged. ``execute_deploy`` was split into small,
single-purpose gate helpers (one per original numbered GATE) so each piece stays
independently under the cyclomatic-complexity budget — merely moving the original
body here, unchanged, would not have reduced its complexity.

MONKEYPATCH NOTE: ``build_plan`` is one of the four names tests monkeypatch
directly on the ``deploy`` facade module. :func:`execute_deploy` therefore calls it
through the parent module's OWN binding at call time (mirroring the pattern in
``core/release/local_compose_parts``).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from disco.core.llm.secrets import SecretStore, WeakSecretError

from ..models import DeployExecutionResult, DeployPlan, DeployRefused, RefusalReason
from ..stripe_deploy import StripeDeployContext
from ..webhook_deploy import WebhookDeployContext
from ..wrangler import BuildBackend, CommandRunner
from ._constants import CF_ACCOUNT_SECRET, CF_TOKEN_SECRET, DeployMutationHook
from ._execute_phases import _run_real_deploy
from ._guards import (
    _assert_no_ancestor_wrangler_config,
    _assert_no_plaintext_secrets,
    _assert_no_secret_shaped_files,
    _assert_sole_wrangler_config,
    _assert_wrangler_allowlisted,
)
from ._locks import _cross_process_deploy_lock, _deploy_lock_for
from ._plan import _security_lifecycles_for_deploy, connection_status
from ._worker import _admin_token_strong, _assert_worker_auth_verified

# ---- execute_deploy: the gate chain, decomposed ------------------------------


def _require_mutation_hook_pair(
    on_mutation_start: DeployMutationHook | None, on_mutation_finish: DeployMutationHook | None
) -> None:
    if (on_mutation_start is None) != (on_mutation_finish is None):
        raise ValueError("deploy mutation hooks must be supplied as a start/finish pair")


def _refuse_if_autonomous(autonomous: bool) -> None:
    # Like request_custom_build, a real deploy needs a human.
    if autonomous:
        raise DeployRefused(
            RefusalReason.AUTONOMOUS,
            "A real Cloudflare deploy is never auto-approved in an autonomous run. "
            "An owner must trigger and confirm it explicitly.",
        )


def _require_workspace_dir(workspace: Path) -> None:
    if not (workspace.exists() and workspace.is_dir()):
        raise DeployRefused(
            RefusalReason.NO_WORKSPACE,
            f"Workspace {workspace} does not exist or is not a directory.",
        )


def _require_export_ready(plan: DeployPlan) -> None:
    # GATE 1: Epic I export-readiness must PASS.
    if not plan.export_ready:
        raise DeployRefused(
            RefusalReason.EXPORT_NOT_READY,
            f"cloudflare_export_ready did not pass: {plan.export_detail}",
        )


def _require_connected_account(store: SecretStore) -> None:
    # GATE 2: a connected, decryptable account.
    status = connection_status(store)
    if not status.connected:
        raise DeployRefused(RefusalReason.NO_CONNECTED_ACCOUNT, status.detail)


def _require_valid_confirmation(plan: DeployPlan, confirmation: str | None) -> None:
    # GATE 3: exact owner confirmation, bound to this fresh plan_hash. A stale
    # phrase (tree/account changed since it was shown) won't match → refuse.
    if not confirmation or confirmation.strip() != plan.confirmation_phrase:
        raise DeployRefused(
            RefusalReason.CONFIRMATION_REQUIRED,
            "A real deploy requires the exact owner confirmation phrase for this "
            "plan. Re-fetch the plan and echo its confirmation_phrase verbatim.",
        )


def _require_runner(runner: CommandRunner | None) -> None:
    if runner is None:
        # CORR-15: the owner DID confirm — the server is just missing its runner. Surface
        # a distinct RUNNER_UNAVAILABLE (an operational fault), not confirmation_required.
        raise DeployRefused(
            RefusalReason.RUNNER_UNAVAILABLE,
            "No command runner is wired for real execution.",
        )


def _require_sandboxed_build(build_backend: BuildBackend | None) -> None:
    # GATE 5 (P0-1): the untrusted, workspace-controlled `npm run build` must run
    # in an ISOLATING sandbox. With no build backend wired, or a non-isolating
    # (same-user `process`) backend, REFUSE — never run workspace build code
    # unsandboxed with host-secret access on the deploy path.
    if build_backend is None or not build_backend.isolates:
        raise DeployRefused(
            RefusalReason.BUILD_NOT_SANDBOXED,
            "The workspace-controlled build (npm run build) can only run inside an "
            "isolating sandbox (gVisor/container) so it has no access to host "
            "secrets or the host filesystem. No isolating sandbox backend is "
            "available, so a real deploy is refused.",
        )


def _require_admin_token(admin_token: str | None) -> None:
    # GATE 6 (P1): a real deploy MUST set the deployed app's ADMIN_TOKEN so its
    # admin endpoint is actually protected. Without an admin_token we cannot run
    # `wrangler secret put ADMIN_TOKEN`, and the deployed Worker would ship an
    # UNAUTHENTICATED admin route. FAIL CLOSED — require it. (The token rides only
    # on stdin at execute time; it never enters the plan_hash, a record, or a log.)
    if not (admin_token and admin_token.strip()):
        raise DeployRefused(
            RefusalReason.ADMIN_TOKEN_REQUIRED,
            "A real deploy requires an admin_token so the deployed app's admin "
            "endpoint is protected via `wrangler secret put ADMIN_TOKEN`. Provide "
            "an admin_token to authorize a real deploy.",
        )
    # GATE 7 (SEC-4): the admin_token must be HIGH-ENTROPY — a short/low-variety token
    # would leave the deployed admin endpoint trivially guessable even with rate
    # limiting. Reject a weak one (fail closed).
    if not _admin_token_strong(admin_token):
        raise DeployRefused(
            RefusalReason.ADMIN_TOKEN_WEAK,
            "The admin_token is too weak to protect the deployed admin endpoint "
            "(need at least 16 characters and at least 8 distinct characters). "
            "Provide a high-entropy admin_token.",
        )


def _snapshot_confirmed_credentials(store: SecretStore, plan: DeployPlan) -> tuple[str, str | None]:
    """P1 (CORR-5): SNAPSHOT the credentials bound to THIS confirmed plan. A
    ``/connect`` account/token swap between the confirmation and the actual deploy
    must not be able to redirect the deploy to a DIFFERENT Cloudflare account — the
    deploy uses the snapshot and refuses on drift. (The account_id is already part
    of plan_hash, so an account swap also fails GATE 3; this is defence in depth and
    also covers a token rotation that leaves account_id unchanged.)
    SEC-30: refuse to USE the deploy credentials under a weak app secret too
    (defence in depth — a credential stored before the gate, or a key weakened
    since, must not feed a real Cloudflare mutation)."""
    try:
        snap_token = store.get_secret(CF_TOKEN_SECRET, strong_required=True)
        snap_account = store.get_secret(CF_ACCOUNT_SECRET, strong_required=True)
    except WeakSecretError as exc:
        raise DeployRefused(RefusalReason.NO_CONNECTED_ACCOUNT, str(exc)) from exc
    if not snap_token:
        raise DeployRefused(
            RefusalReason.NO_CONNECTED_ACCOUNT, "The Cloudflare token vanished before deploy."
        )
    if snap_account != plan.account_id:
        raise DeployRefused(
            RefusalReason.NO_CONNECTED_ACCOUNT,
            "The connected account changed between the plan and the deploy. Re-fetch "
            "the plan and re-confirm.",
        )
    return snap_token, snap_account


async def execute_deploy(
    workspace: Path,
    store: SecretStore,
    *,
    dry_run: bool = True,
    confirmation: str | None = None,
    autonomous: bool = False,
    runner: CommandRunner | None = None,
    build_backend: BuildBackend | None = None,
    admin_token: str | None = None,
    stripe_context: StripeDeployContext | None = None,
    webhook_context: WebhookDeployContext | None = None,
    on_mutation_start: DeployMutationHook | None = None,
    on_mutation_finish: DeployMutationHook | None = None,
) -> DeployExecutionResult:
    """The single entry point for a (possibly real) deploy. Enforces the four
    hard gates IN ORDER, then either returns the dry-run plan (default, no side
    effects) or — only when every gate passes and ``dry_run`` is False — drives
    the injected ``runner`` through the real wrangler sequence.

    Raises :class:`DeployRefused` (mapped to a 4xx at the route) on any gate.
    """
    _require_mutation_hook_pair(on_mutation_start, on_mutation_finish)
    _refuse_if_autonomous(autonomous)
    _require_workspace_dir(workspace)

    # Build the plan fresh so every gate judges the current tree. Resolved through
    # the parent facade's OWN binding (not a direct import) so a test/operator patch
    # of ``deploy.build_plan`` is actually observed here.
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    plan = _deploy.build_plan(workspace, store)

    _require_export_ready(plan)
    _require_connected_account(store)

    # DRY-RUN (default): return the plan with ZERO side effects. The runner is
    # never touched; no Cloudflare API is contacted.
    if dry_run:
        return DeployExecutionResult(executed=False, dry_run=True, plan=plan)

    _require_valid_confirmation(plan, confirmation)
    _require_runner(runner)
    _require_sandboxed_build(build_backend)
    _require_admin_token(admin_token)
    snap_token, snap_account = _snapshot_confirmed_credentials(store, plan)

    assert runner is not None
    assert build_backend is not None
    return await _execute_under_deploy_locks(
        workspace,
        store,
        plan,
        runner,
        build_backend,
        admin_token,
        snap_token=snap_token,
        snap_account=snap_account,
        stripe_context=stripe_context,
        webhook_context=webhook_context,
        on_mutation_start=on_mutation_start,
        on_mutation_finish=on_mutation_finish,
    )


async def _execute_under_deploy_locks(
    workspace: Path,
    store: SecretStore,
    plan: DeployPlan,
    runner: CommandRunner,
    build_backend: BuildBackend,
    admin_token: str | None,
    *,
    snap_token: str,
    snap_account: str | None,
    stripe_context: StripeDeployContext | None,
    webhook_context: WebhookDeployContext | None,
    on_mutation_start: DeployMutationHook | None,
    on_mutation_finish: DeployMutationHook | None,
) -> DeployExecutionResult:
    """Serialize, stage, validate, execute, and durably close one real deploy."""
    # ``_stage_deploy_tree`` is DEFINED on the ``deploy`` facade itself (see
    # ``_workspace.py``'s docstring for why), so it is reached the same way the
    # four monkeypatch-critical names are: through the parent module's OWN
    # binding, resolved lazily to avoid a circular import at module load time.
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    # The asyncio lock queues local peers; the inner flock refuses a different
    # process. Wrangler only sees the immutable, symlink-free staged copy.
    async with _deploy_lock_for(workspace):
        with _cross_process_deploy_lock(workspace):
            staged = _deploy._stage_deploy_tree(workspace)
            deploy_home = Path(tempfile.mkdtemp(prefix="disco-deploy-home-"))
            try:
                _assert_no_plaintext_secrets(staged)
                _assert_no_secret_shaped_files(staged)
                _assert_worker_auth_verified(staged)
                _assert_wrangler_allowlisted(staged)
                _assert_sole_wrangler_config(staged, workspace)
                _assert_no_ancestor_wrangler_config(staged)
                stripe_lifecycle, webhook_lifecycle = _security_lifecycles_for_deploy(
                    staged, store, stripe_context, webhook_context
                )
                mutation_scope_entered = False

                async def begin_mutation() -> None:
                    nonlocal mutation_scope_entered
                    if on_mutation_start is not None:
                        await on_mutation_start()
                    # The successful start callback is the mutation-scope commit
                    # point. A rejected guard must never trigger finalization.
                    mutation_scope_entered = True

                original_escape: BaseException | None = None
                try:
                    return await _run_real_deploy(
                        workspace,
                        staged,
                        store,
                        plan,
                        runner,
                        build_backend,
                        admin_token,
                        snap_token=snap_token,
                        snap_account=snap_account,
                        deploy_home=str(deploy_home),
                        stripe_lifecycle=stripe_lifecycle,
                        webhook_lifecycle=webhook_lifecycle,
                        on_mutation_start=begin_mutation,
                    )
                except BaseException as exc:
                    original_escape = exc
                    raise
                finally:
                    if mutation_scope_entered and on_mutation_finish is not None:
                        try:
                            await _drain_deploy_mutation_finish(on_mutation_finish)
                        except BaseException as finalizer_error:
                            if original_escape is None:
                                raise
                            original_escape.add_note(
                                "A later cancellation or mutation-finalizer failure was "
                                "drained without replacing this original deploy failure."
                            )
                            raise original_escape from finalizer_error
            finally:
                shutil.rmtree(staged, ignore_errors=True)
                shutil.rmtree(deploy_home, ignore_errors=True)


async def _drain_deploy_mutation_finish(callback: DeployMutationHook) -> None:
    """Drain a host-mirror reseal through any number of caller cancellations."""
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    task = asyncio.ensure_future(callback())
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = cancellation or exc
    try:
        task.result()
    except Exception:
        _deploy._log.exception("deploy mutation finalizer failed while draining cancellation")
        raise
    if cancellation is not None:
        raise cancellation

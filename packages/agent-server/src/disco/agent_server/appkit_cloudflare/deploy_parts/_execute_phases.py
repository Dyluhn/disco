"""``_run_real_deploy``: the actual (idempotent) wrangler mutation sequence run
against the immutable staged tree, plus the trusted-wrangler-binary + minimised
token-bearing-env resolution it depends on.

Extracted from ``deploy.py`` (via ``_execute.py``, which now holds only the outer
gate chain + lock/record orchestration) to reduce module complexity. ``_run_real_deploy``
was split into small, single-purpose phase helpers (one per original deploy leg:
build+reverify, D1 probe, Worker probe, D1 provision, schema migration, Worker
deploy, binding install, finalize) so each piece stays independently under the
cyclomatic-complexity budget — merely moving the original body here, unchanged,
would not have reduced its complexity.

MONKEYPATCH NOTE: ``_deploy_asset_dir_rel`` is one of the four names tests
monkeypatch directly on the ``deploy`` facade module. :func:`_prepare_real_deploy_context`
therefore resolves it through the parent module's OWN binding at call time
(mirroring the pattern in ``core/release/local_compose_parts``).

``read_workspace_file`` is likewise reached through the ``deploy`` facade (a
lazy, function-local import): it is DEFINED on ``deploy.py`` itself (see
``_workspace.py``'s docstring for why), and deploy.py imports FROM this module
at its own top level, so a module-level reverse import here would be circular.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from disco.core.llm.secrets import SecretStore

from ..models import DeployExecutionResult, DeployPlan, DeployRefused, RefusalReason
from ..stripe_deploy import StripeDeploymentLifecycle
from ..webhook_deploy import WebhookDeploymentLifecycle
from ..wrangler import BuildBackend, BuildResult, CommandResult, CommandRunner
from ._bindings import (
    _DeploySecretWriter,
    _install_worker_bindings,
    _quiesce_security_workers,
    _recheck_security_bindings_after_build,
    _record_deploy_step,
)
from ._constants import (
    _D1_ID_PLACEHOLDER,
    _WRANGLER_ACCOUNT_ENV,
    _WRANGLER_BIN_ENV,
    _WRANGLER_ENV_ALLOWLIST,
    _WRANGLER_TOKEN_ENV,
    CF_ACCOUNT_SECRET,
    CF_TOKEN_SECRET,
    DeployMutationHook,
)
from ._deploy_record import (
    _begin_deploy_record_scope,
    _close_deploy_record_on_escape,
    _failed_deploy_result,
    _persist_record,
)
from ._guards import (
    _assert_no_plaintext_secrets,
    _assert_no_secret_shaped_files,
    _assert_schema_safe_for_adopt,
    _assert_sole_wrangler_config,
)
from ._ownership import _has_ownership_record
from ._staging import _tree_digest, assert_deploy_tree_symlink_free
from ._worker import _assert_worker_is_canonical
from ._wrangler_output import (
    _aborted,
    _aborted_msg,
    _d1_entry_from_list,
    _database_id_from_list,
    _extract_database_id,
    _extract_url,
    _ListParseError,
    _preflight_signals_worker_absent,
    _substitute_database_id,
    _worker_exists_from_deployments,
)

# ---- SEC-3/SEC-32: trusted wrangler resolution + the minimised token-bearing env --


def _sanitized_path(*unsafe_roots: Path) -> str | None:
    """The host ``PATH`` with EVERY entry that lives inside an untrusted tree
    (the workspace, the staged copy) removed — so a planted ``node_modules/.bin``
    on ``PATH`` can never be used to resolve a tool for the token-bearing wrangler
    process. Returns ``None`` when no ``PATH`` is set."""
    raw = os.environ.get("PATH")
    if not raw:
        return None
    resolved_roots = []
    for r in unsafe_roots:
        try:
            resolved_roots.append(r.resolve())
        except OSError:
            continue
    kept: list[str] = []
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        # SEC-3/SEC-32: DROP every RELATIVE PATH entry (``.``, ``./x``, ``bin``) — a
        # relative entry is resolved against the (untrusted, workspace) cwd of the
        # wrangler subprocess, so a build-planted ``./node_modules/.bin`` could shadow a
        # real tool. Only ABSOLUTE, outside-the-untrusted-tree entries survive.
        if not Path(entry).is_absolute():
            continue
        try:
            ep = Path(entry).resolve()
        except OSError:
            continue
        if any(ep == root or ep.is_relative_to(root) for root in resolved_roots):
            continue
        kept.append(entry)
    return os.pathsep.join(kept) if kept else None


def _resolve_trusted_wrangler(*unsafe_roots: Path) -> str:
    """Resolve the wrangler invocation from a TRUSTED location (SEC-3/CORR-3), NEVER
    the untrusted workspace. We do NOT use ``npx wrangler`` (npx resolves
    ``./node_modules/.bin/wrangler`` FIRST — an attacker-planted binary would steal
    the CF token + ADMIN_TOKEN). Resolution order:

      1. ``DISCO_WRANGLER_BIN`` — an operator-pinned ABSOLUTE path (must exist and
         lie OUTSIDE every untrusted root);
      2. ``shutil.which("wrangler")`` against a SANITISED ``PATH`` (relative entries +
         any entry inside an untrusted tree stripped), accepted only if it resolves to
         an ABSOLUTE path OUTSIDE every untrusted root.

    WAVE 2 (SEC-3/CORR-3/SEC-32): there is NO bare-``wrangler`` fallback. A bare name is
    resolved by the OS from the subprocess ``PATH`` at exec time, and a relative/unsafe
    ``PATH`` entry (or a build-planted ``dist/wrangler``) could be selected — so if no
    TRUSTED absolute binary outside the workspace/staging tree is found, this FAILS
    CLOSED (:class:`DeployRefused` ``WRANGLER_NOT_TRUSTED``) rather than risk running a
    workspace-controlled wrangler with the CF token + ADMIN_TOKEN. The deploy NEVER
    prefixes it with ``npx`` and NEVER runs it through the workspace ``node_modules``."""
    resolved_roots = []
    for r in unsafe_roots:
        try:
            resolved_roots.append(r.resolve())
        except OSError:
            continue

    def _outside(p: Path) -> bool:
        if not p.is_absolute():
            return False
        try:
            rp = p.resolve()
        except OSError:
            return False
        return not any(rp == root or rp.is_relative_to(root) for root in resolved_roots)

    pinned = os.environ.get(_WRANGLER_BIN_ENV, "").strip()
    if pinned:
        pp = Path(pinned)
        if pp.is_absolute() and pp.exists() and _outside(pp):
            return str(pp)
    # Resolve only against the SANITISED PATH (relative + in-workspace entries already
    # stripped) so ``which`` can never return a workspace-resolved binary.
    sanitized = _sanitized_path(*unsafe_roots)
    found = shutil.which("wrangler", path=sanitized) if sanitized else None
    if found and _outside(Path(found)):
        return found
    raise DeployRefused(
        RefusalReason.WRANGLER_NOT_TRUSTED,
        "No TRUSTED wrangler binary could be resolved from outside the workspace/"
        "staging tree. Pin one via DISCO_WRANGLER_BIN (an absolute path) — refusing to "
        "deploy with a possibly workspace-controlled wrangler (fail closed).",
    )


def _deploy_env(
    token: str, account_id: str | None, *, home: str, unsafe_roots: tuple[Path, ...] = ()
) -> dict[str, str]:
    """The env for the TRUSTED wrangler steps (SEC-32 minimised): a tiny non-sensitive
    allowlist, a SANITISED ``PATH`` (workspace/staging entries stripped), a throwaway
    ``HOME`` (so a planted ``~/.npmrc``/``~/.wrangler`` can't redirect to host creds),
    PLUS only the Cloudflare credentials wrangler reads. ``NODE_OPTIONS`` and
    ``npm_config_*`` are deliberately NOT forwarded (preload / registry-redirect
    vectors). The token rides ONLY here — never in the build env, never argv, never
    logged."""
    src = os.environ
    env = {k: src[k] for k in _WRANGLER_ENV_ALLOWLIST if k in src}
    sanitized_path = _sanitized_path(*unsafe_roots)
    if sanitized_path is not None:
        env["PATH"] = sanitized_path
    env["HOME"] = home
    env[_WRANGLER_TOKEN_ENV] = token
    if account_id:
        env[_WRANGLER_ACCOUNT_ENV] = account_id
    return env


# ---- _run_real_deploy, decomposed into phases --------------------------------


@dataclass
class _RealDeployContext:
    """Bundles the per-run wrangler invocation state that used to live as local
    variables + closures (``_wr``, ``record``) inside ``_run_real_deploy``."""

    runner: CommandRunner
    wrangler_bin: str
    staged: Path
    deploy_env: dict[str, str]
    asset_dir_rel: str
    secret_literals: tuple[str, ...]
    transcript: list[str] = field(default_factory=list)

    def wr(self, *args: str) -> list[str]:
        return [self.wrangler_bin, *args]

    def record(
        self, label: str, res: CommandResult | BuildResult, *, capture_output: bool = True
    ) -> None:
        _record_deploy_step(
            self.transcript, label, res, self.secret_literals, capture_output=capture_output
        )


def _prepare_real_deploy_context(
    live_workspace: Path,
    staged: Path,
    runner: CommandRunner,
    deploy_home: str,
    token: str,
    account_id: str | None,
    admin_token: str | None,
) -> _RealDeployContext:
    # SEC-3/CORR-3: resolve a TRUSTED wrangler binary (NOT `npx wrangler`, which would
    # run a workspace ``node_modules/.bin/wrangler``). The staged tree carries no
    # node_modules, and both the live workspace + staged copy are passed as unsafe
    # roots so neither PATH resolution nor the binary itself can come from them.
    wrangler_bin = _resolve_trusted_wrangler(live_workspace, staged)
    # Trusted wrangler receives the token only in its minimized, sanitized environment.
    # NEVER reaches the build: the build runs in the sandbox, which has no host env /
    # host FS at all.
    deploy_env = _deploy_env(
        token, account_id, home=deploy_home, unsafe_roots=(live_workspace, staged)
    )
    # Scrub literals before pattern redaction and never record secret-put output.
    secret_literals = tuple(s for s in (token, admin_token) if s and s.strip())
    # Validate [assets].directory early, before any Cloudflare mutation. Resolved
    # through the parent facade's OWN binding (not a direct import) so a
    # test/operator patch of ``deploy._deploy_asset_dir_rel`` is actually observed.
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    asset_dir_rel = _deploy._deploy_asset_dir_rel(staged)
    return _RealDeployContext(
        runner=runner,
        wrangler_bin=wrangler_bin,
        staged=staged,
        deploy_env=deploy_env,
        asset_dir_rel=asset_dir_rel,
        secret_literals=secret_literals,
    )


async def _build_and_reverify_staged_tree(
    build_backend: BuildBackend,
    context: _RealDeployContext,
    plan: DeployPlan,
    store: SecretStore,
    snap_account: str | None,
    snap_token: str,
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
) -> DeployExecutionResult | None:
    """Run the sandboxed build, then re-validate everything that could have
    changed as a result: the snapshotted credentials, the plaintext/secret-shaped
    file scans, the canonical-Worker match, and the sole-config-source guard —
    ALL on the POST-build staged tree, BEFORE any Cloudflare mutation. Returns an
    abort result on a build failure, else ``None`` (continue)."""
    # 1. Build the UNTRUSTED, workspace-controlled app INSIDE an isolating sandbox
    #    (P0-1 — no host-secret/filesystem access) with a DETERMINISTIC `npm ci`
    #    from the hash-covered lockfile (P0-2). The built ./dist is synced back into
    #    the STAGED copy (never the live tree). ABORT on failure — never mutate
    #    Cloudflare off a broken (or un-isolated) build.
    # CORR-16/SEC-1: build + sync the EXACT `[assets].directory` wrangler will publish
    # (a non-`dist` app would otherwise sync the wrong dir).
    build = await build_backend.build(
        context.staged,
        install_cmd="npm ci",
        build_cmd="npm run build",
        asset_dir=context.asset_dir_rel,
    )
    context.record("npm ci && npm run build  (sandboxed)", build)
    if not build.ok:
        return _aborted(plan, "npm ci && npm run build", build, context.transcript)

    # CORR-5: re-validate the snapshotted credentials AFTER the build — a `/connect`
    # account/token swap during the deploy must not redirect to a different account.
    if (
        store.get_secret(CF_ACCOUNT_SECRET) != snap_account
        or store.get_secret(CF_TOKEN_SECRET) != snap_token
    ):
        raise DeployRefused(
            RefusalReason.NO_CONNECTED_ACCOUNT,
            "The connected Cloudflare credentials changed mid-deploy. Refusing to "
            "continue with drifted credentials (fail closed); re-fetch the plan and "
            "re-confirm.",
        )

    # SEC-17/18 (POST-BUILD rescan): the pre-build scans (in `execute_deploy`) ran on the
    # AUTHORED tree, BEFORE the build regenerated the output dir. The sandbox build can
    # EMIT a secret into the synced-back ``./dist`` (a leaked .env baked into a bundle, a
    # key written into a generated config). Re-run the deployable-file denylist + the
    # secret-SHAPED VALUE scan on the STAGED post-build tree, AFTER sync-back and BEFORE any
    # Cloudflare mutation — fail closed (no mutation runs) if the build emitted a secret.
    _assert_no_plaintext_secrets(context.staged)
    _assert_no_secret_shaped_files(context.staged)

    # SEC-4/BUILD-1 (POST-build canonical match): REQUIRE the staged worker/index.ts to BE
    # the canonical Worker the AppKit generator emits from this app's spec. The pre-build
    # heuristic auth gate judged the AUTHORED Worker; the sandbox build could EMIT/rewrite
    # worker/index.ts and the sync-back replaces it. This gate runs HERE — POST sync-back,
    # BEFORE the first Cloudflare mutation — and refuses (WORKER_NOT_CANONICAL) any Worker
    # that is not the pristine generated one, ending the heuristic-taint arms race and
    # closing the BUILD-1 TOCTOU (the deployed Worker == the regenerated canonical Worker).
    _assert_worker_is_canonical(context.staged)

    # SEC (config-source bypass, POST-build): the pre-build alt-config gate scanned the
    # AUTHORED tree; the sandbox build could EMIT a wrangler.json/.jsonc or a
    # .wrangler/deploy/config.json into the synced-back staged tree (the bytes wrangler runs
    # against). Re-scan the staged tree HERE — AFTER sync-back, BEFORE the first Cloudflare
    # mutation — and refuse (ALT_WRANGLER_CONFIG) so a build-emitted alt config never deploys.
    _assert_sole_wrangler_config(context.staged)
    _recheck_security_bindings_after_build(stripe_lifecycle, webhook_lifecycle, context.staged)
    return None


@dataclass
class _D1Probe:
    creating: bool
    listing: CommandResult


async def _probe_d1_state(
    context: _RealDeployContext, plan: DeployPlan, live_workspace: Path, store: SecretStore
) -> DeployExecutionResult | _D1Probe:
    """Decide D1 provisioning from the EXACT-parsed `d1 list` (SEC-15/CORR-8): match
    the database by EXACT name (no substring, no first-UUID fallback). The `d1 list`
    itself is READ-ONLY — no mutation yet. Adopting an EXISTING database additionally
    requires a signed ownership record + a schema-safety check (SEC-10/SEC-11)."""
    listing = await context.runner.run(
        context.wr("d1", "list", "--json"), cwd=context.staged, env=context.deploy_env
    )
    context.record("wrangler d1 list --json", listing)
    if not listing.ok:
        return _aborted(plan, "wrangler d1 list", listing, context.transcript)
    # SEC-15/CORR-8: a SUCCESSFUL list whose body is not a JSON array must ABORT — never
    # fall open to creating a fresh DB off unparseable output.
    try:
        existing_entry = _d1_entry_from_list(listing.stdout, plan.db_name)
    except _ListParseError as exc:
        return _aborted_msg(
            plan,
            "wrangler d1 list",
            context.transcript,
            f"malformed `wrangler d1 list --json` output ({exc}); refusing to fall open "
            "to creating a fresh database (fail closed).",
        )
    creating = existing_entry is None
    if not creating:
        # SEC-10: the D1 already exists by name. ADOPT it ONLY if a SIGNED, server-trusted
        # prior Disco deploy record proves we own it (SEC-10-B) — otherwise it is an
        # UNRELATED resource and migrating our schema into it could corrupt someone else's
        # data. Refuse (fail closed).
        if not _has_ownership_record(live_workspace, plan, store, kind="d1"):
            raise DeployRefused(
                RefusalReason.UNRELATED_RESOURCE,
                f"A D1 database named {plan.db_name!r} already exists on this account "
                "but no prior Disco deploy record proves we own it. Refusing to adopt/"
                "migrate an unrelated database (fail closed).",
            )
        # SEC-11/CORR-9: never run destructive/non-CREATE schema SQL against an adopted
        # (owner-owned, pre-existing) database.
        _assert_schema_safe_for_adopt(context.staged)
    return _D1Probe(creating=creating, listing=listing)


async def _probe_worker_state(
    context: _RealDeployContext, plan: DeployPlan, live_workspace: Path, store: SecretStore
) -> DeployExecutionResult | bool:
    """SEC-10: the read-only Worker-existence preflight may proceed only on a parsed
    existence verdict or Cloudflare's documented script-not-found signal. Ambiguity
    fails closed; an existing Worker additionally requires a signed ownership record."""
    preflight = await context.runner.run(
        context.wr("deployments", "list", "--name", plan.worker_name, "--json"),
        cwd=context.staged,
        env=context.deploy_env,
    )
    context.record(f"wrangler deployments list --name {plan.worker_name} --json", preflight)
    if preflight.ok:
        try:
            worker_exists = _worker_exists_from_deployments(preflight.stdout)
        except _ListParseError as exc:
            return _aborted_msg(
                plan,
                "wrangler deployments list",
                context.transcript,
                f"malformed `wrangler deployments list --json` output ({exc}); refusing "
                "to fall open and overwrite a possibly-unrelated Worker (fail closed).",
            )
    elif _preflight_signals_worker_absent(preflight):
        # The ONLY nonzero outcome we accept as trustworthy: a documented not-found result.
        worker_exists = False
    else:
        # SEC-10-A: any OTHER nonzero/errored preflight is AMBIGUOUS — we cannot prove the
        # Worker is absent, and proceeding could OVERWRITE an existing Worker. Fail closed
        # BEFORE any mutation (the preflight is still read-only at this point).
        raise DeployRefused(
            RefusalReason.WORKER_PREFLIGHT_FAILED,
            f"The Worker-existence preflight (`wrangler deployments list --name "
            f"{plan.worker_name}`) failed (exit {preflight.returncode}) without a "
            "documented 'script not found' result. Refusing to deploy — cannot prove the "
            "Worker is absent, and proceeding could overwrite an existing (possibly "
            "unrelated) Worker (fail closed). Check the wrangler version / API token "
            "permissions and retry.",
        )
    if worker_exists and not _has_ownership_record(live_workspace, plan, store, kind="worker"):
        raise DeployRefused(
            RefusalReason.UNRELATED_RESOURCE,
            f"A Worker named {plan.worker_name!r} already exists on this account but no "
            "signed prior Disco deploy record proves we own it. Refusing to overwrite an "
            "unrelated Worker (fail closed).",
        )
    return worker_exists


@dataclass
class _LegFailure:
    step: str
    detail: str


async def _provision_d1_database(
    context: _RealDeployContext,
    plan: DeployPlan,
    d1_probe: _D1Probe,
    live_workspace: Path,
    record_rel: Path,
    store: SecretStore,
    mutations: list[str],
) -> _LegFailure | None:
    database_id: str | None
    if d1_probe.creating:
        created = await context.runner.run(
            context.wr("d1", "create", plan.db_name), cwd=context.staged, env=context.deploy_env
        )
        context.record(f"wrangler d1 create {plan.db_name}", created)
        if not created.ok:
            return _LegFailure(
                "wrangler d1 create", f"d1 create failed (exit {created.returncode})"
            )
        mutations.append(f"d1_create:{plan.db_name}")
        _persist_record(
            live_workspace,
            record_rel,
            plan,
            status="in_progress",
            mutations=mutations,
            store=store,
        )
        database_id = _extract_database_id(created.stdout)
    else:
        database_id = _database_id_from_list(d1_probe.listing.stdout, plan.db_name)

    # P1-3: substitute the REAL database_id into the STAGED wrangler.toml BEFORE deploy,
    # so the D1 binding is actually wired (a fresh deploy ships the placeholder
    # otherwise). FAIL CLOSED if we cannot resolve it — never deploy a Worker with a
    # non-functional D1 binding.
    if database_id:
        _substitute_database_id(context.staged, database_id)
    from disco.agent_server.appkit_cloudflare import deploy as _deploy

    if _D1_ID_PLACEHOLDER in (
        _deploy.read_workspace_file(context.staged / "wrangler.toml", context.staged.resolve())
        or ""
    ):
        return _LegFailure(
            "resolve d1 database_id",
            "could not resolve a valid (UUID) D1 database_id from wrangler output; "
            "refusing to deploy a Worker with an unbound (placeholder) D1 database.",
        )
    return None


async def _apply_schema_migration(
    context: _RealDeployContext,
    plan: DeployPlan,
    live_workspace: Path,
    record_rel: Path,
    store: SecretStore,
    mutations: list[str],
) -> _LegFailure | None:
    # 3. Apply schema to the remote D1. ABORT before deploy if the migration fails.
    migrate = await context.runner.run(
        context.wr("d1", "execute", plan.db_name, "--remote", "--file=./schema.sql"),
        cwd=context.staged,
        env=context.deploy_env,
    )
    context.record(f"wrangler d1 execute {plan.db_name} --remote", migrate)
    if not migrate.ok:
        return _LegFailure(
            "wrangler d1 execute (migrate)", f"migration failed (exit {migrate.returncode})"
        )
    mutations.append("d1_migrate")
    _persist_record(
        live_workspace, record_rel, plan, status="in_progress", mutations=mutations, store=store
    )
    return None


@dataclass
class _WorkerDeployOutcome:
    deployed_url: str | None
    effective_digest: str


async def _deploy_worker(
    context: _RealDeployContext,
    plan: DeployPlan,
    live_workspace: Path,
    record_rel: Path,
    store: SecretStore,
    mutations: list[str],
) -> _LegFailure | _WorkerDeployOutcome:
    # P0 (round 9): IMMEDIATELY before `wrangler deploy` — which FOLLOWS symlinks
    # when it uploads ./dist — scan the EXACT asset tree wrangler will publish and
    # REFUSE if ANY entry (dir or file, any depth) is a symlink. The build regenerates
    # dist AFTER staging, so a symlink the build emits is caught only here. Fail closed
    # BEFORE the deploy command runs (no upload on refusal).
    assert_deploy_tree_symlink_free(context.staged, context.asset_dir_rel)

    # CORR-7: bind the audit record to the EFFECTIVE deployed artifact — the digest of
    # the staged tree POST-build and POST-D1-substitution (what wrangler actually
    # uploads), not just the plan-time pre-build digest.
    effective_digest = _tree_digest(context.staged)

    # 4. Deploy the Worker + assets. ABORT before the secret-put on failure.
    # SEC (config-source bypass): PIN the config explicitly to the staged wrangler.toml so
    # wrangler can NEVER pick an alt source by precedence (wrangler.json/.jsonc) at exec
    # time. Belt-and-suspenders alongside the alt-config REFUSAL (the primary guard — a
    # .wrangler/deploy/config.json redirect may not be overridden by --config) + the staging
    # exclusion of .wrangler; together they guarantee wrangler.toml is the sole config source.
    deployed = await context.runner.run(
        context.wr("deploy", "--config", str(context.staged / "wrangler.toml")),
        cwd=context.staged,
        env=context.deploy_env,
    )
    context.record("wrangler deploy", deployed)
    if not deployed.ok:
        return _LegFailure("wrangler deploy", f"worker deploy failed (exit {deployed.returncode})")
    mutations.append("worker_deploy")
    deployed_url = _extract_url(deployed.stdout)
    # CORR-7: persist the effective digest WITH the worker_deploy step, so a partial
    # failure AFTER the deploy (e.g. secret put) still records what was published.
    _persist_record(
        live_workspace,
        record_rel,
        plan,
        status="in_progress",
        mutations=mutations,
        deployed_url=deployed_url,
        effective_digest=effective_digest,
        store=store,
    )
    return _WorkerDeployOutcome(deployed_url=deployed_url, effective_digest=effective_digest)


def _finalize_deploy_success(
    live_workspace: Path,
    record_rel: Path,
    plan: DeployPlan,
    mutations: list[str],
    store: SecretStore,
    transcript: list[str],
    attempt_record_path: str,
    deployed_url: str | None,
    effective_digest: str | None,
) -> DeployExecutionResult:
    """Finalize the SAME durable record (persists for idempotency/audit); the staged
    copy is torn down by the caller."""
    record_path = _persist_record(
        live_workspace,
        record_rel,
        plan,
        status="succeeded",
        mutations=mutations,
        deployed_url=deployed_url,
        effective_digest=effective_digest,
        store=store,
    )
    return DeployExecutionResult(
        executed=True,
        dry_run=False,
        plan=plan,
        deployed_url=deployed_url,
        record_path=record_path,
        transcript=transcript,
        succeeded=True,
        mutations=list(mutations),
        attempt_record_path=attempt_record_path,
    )


async def _run_mutating_deploy_sequence(
    context: _RealDeployContext,
    live_workspace: Path,
    store: SecretStore,
    plan: DeployPlan,
    admin_token: str | None,
    record_rel: Path,
    mutations: list[str],
    attempt_record_path: str,
    d1_probe: _D1Probe,
    worker_exists: bool,
    stripe_lifecycle: StripeDeploymentLifecycle | None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None,
) -> DeployExecutionResult:
    """SEC-12/SEC-13/CORR-10/CORR-28: everything from the first Cloudflare mutation
    through the finalized durable record — quiesce, provision/adopt D1, migrate,
    deploy the Worker, install bindings, and close the record on success or
    failure."""
    effective_digest: str | None = None
    deployed_url: str | None = None

    def _fail(step: str, detail: str) -> DeployExecutionResult:
        return _failed_deploy_result(
            live_workspace,
            record_rel,
            plan,
            mutations,
            context.transcript,
            effective_digest,
            store=store,
            step=step,
            detail=detail,
        )

    with _close_deploy_record_on_escape(
        live_workspace,
        record_rel,
        plan,
        mutations,
        store,
        effective_digest=lambda: effective_digest,
        deployed_url=lambda: deployed_url,
    ):
        secret_writer = _DeploySecretWriter(
            context.runner,
            context.wrangler_bin,
            context.staged,
            context.deploy_env,
            context.transcript,
            live_workspace,
            record_rel,
            plan,
            mutations,
            store,
        )
        quiesce_error = await _quiesce_security_workers(
            stripe_lifecycle, webhook_lifecycle, worker_exists, secret_writer
        )
        if quiesce_error is not None:
            return _fail(quiesce_error.step, quiesce_error.detail)

        d1_failure = await _provision_d1_database(
            context, plan, d1_probe, live_workspace, record_rel, store, mutations
        )
        if d1_failure is not None:
            return _fail(d1_failure.step, d1_failure.detail)

        migration_failure = await _apply_schema_migration(
            context, plan, live_workspace, record_rel, store, mutations
        )
        if migration_failure is not None:
            return _fail(migration_failure.step, migration_failure.detail)

        deploy_outcome = await _deploy_worker(
            context, plan, live_workspace, record_rel, store, mutations
        )
        if isinstance(deploy_outcome, _LegFailure):
            return _fail(deploy_outcome.step, deploy_outcome.detail)
        effective_digest = deploy_outcome.effective_digest
        secret_writer.effective_digest = effective_digest
        deployed_url = deploy_outcome.deployed_url

        binding_error = await _install_worker_bindings(
            stripe_lifecycle,
            webhook_lifecycle,
            deployed_url,
            admin_token,
            not worker_exists,
            secret_writer,
        )
        if binding_error is not None:
            return _fail(binding_error.step, binding_error.detail)

        return _finalize_deploy_success(
            live_workspace,
            record_rel,
            plan,
            mutations,
            store,
            context.transcript,
            attempt_record_path,
            deployed_url,
            effective_digest,
        )


async def _run_real_deploy(
    live_workspace: Path,
    staged: Path,
    store: SecretStore,
    plan: DeployPlan,
    runner: CommandRunner,
    build_backend: BuildBackend,
    admin_token: str | None,
    *,
    snap_token: str,
    snap_account: str | None,
    deploy_home: str,
    stripe_lifecycle: StripeDeploymentLifecycle | None = None,
    webhook_lifecycle: WebhookDeploymentLifecycle | None = None,
    on_mutation_start: DeployMutationHook | None = None,
) -> DeployExecutionResult:
    """Drive the deploy sequence (idempotent) against the IMMUTABLE *staged* copy —
    NEVER the live mutable workspace (SEC-6/SEC-7/CORR-2). The UNTRUSTED build runs in
    an isolating SANDBOX (no host-secret access); the trusted wrangler steps run on the
    host with a TRUSTED wrangler binary (SEC-3), the SNAPSHOTTED token via a MINIMISED
    ENV only (SEC-32), and ADMIN_TOKEN via stdin only. Every captured output is redacted
    before it enters the transcript or the record. The deployment record is written to
    the LIVE workspace so it persists for idempotency/audit."""
    context = _prepare_real_deploy_context(
        live_workspace, staged, runner, deploy_home, snap_token, snap_account, admin_token
    )

    build_failure = await _build_and_reverify_staged_tree(
        build_backend,
        context,
        plan,
        store,
        snap_account,
        snap_token,
        stripe_lifecycle,
        webhook_lifecycle,
    )
    if build_failure is not None:
        return build_failure

    d1_probe = await _probe_d1_state(context, plan, live_workspace, store)
    if isinstance(d1_probe, DeployExecutionResult):
        return d1_probe

    worker_probe = await _probe_worker_state(context, plan, live_workspace, store)
    if isinstance(worker_probe, DeployExecutionResult):
        return worker_probe

    # SEC-12/SEC-13/CORR-10/CORR-28: write a DURABLE pre-mutation 'attempt' record BEFORE
    # the first Cloudflare mutation, then UPDATE it after each mutating leg, so a failure
    # part-way through is persisted (the partial side effects are never lost). Collision-
    # proof filename (SEC-16): timestamp + random suffix.
    record_rel, mutations, attempt_record_path = await _begin_deploy_record_scope(
        on_mutation_start, live_workspace, plan, store
    )
    return await _run_mutating_deploy_sequence(
        context,
        live_workspace,
        store,
        plan,
        admin_token,
        record_rel,
        mutations,
        attempt_record_path,
        d1_probe,
        worker_probe,
        stripe_lifecycle,
        webhook_lifecycle,
    )

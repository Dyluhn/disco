"""AppKit EPIC O -- Cloudflare deploy capability tests (split 1 of 5).

Split from the original test_appkit_cloudflare.py during its PY-0360
module-size decomposition (pure mechanical split, no test logic changed).
Shared fixtures/fakes/constants live in _appkit_cloudflare_support.py.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from _appkit_cloudflare_support import (
    _ACCOUNT as _ACCOUNT,
)
from _appkit_cloudflare_support import (
    _APP_ADMIN_TOKEN as _APP_ADMIN_TOKEN,
)
from _appkit_cloudflare_support import (
    _CF_TOKEN as _CF_TOKEN,
)
from _appkit_cloudflare_support import (
    _DB_ID as _DB_ID,
)
from _appkit_cloudflare_support import (
    FakeBuildBackend as FakeBuildBackend,
)
from _appkit_cloudflare_support import (
    FakeRunner as FakeRunner,
)
from _appkit_cloudflare_support import (
    _is_d1_create as _is_d1_create,
)
from _appkit_cloudflare_support import (
    _is_deploy as _is_deploy,
)
from _appkit_cloudflare_support import (
    _is_wrangler as _is_wrangler,
)
from _appkit_cloudflare_support import (
    _isolated_deploy_lock_dir as _isolated_deploy_lock_dir,
)
from _appkit_cloudflare_support import (
    _isolated_deploy_stage_dir as _isolated_deploy_stage_dir,
)
from _appkit_cloudflare_support import (
    _refusal as _refusal,
)
from _appkit_cloudflare_support import (
    _seed_ownership_record as _seed_ownership_record,
)
from _appkit_cloudflare_support import (
    _trusted_wrangler as _trusted_wrangler,
)
from _appkit_cloudflare_support import (
    connected as connected,
)
from _appkit_cloudflare_support import (
    store as store,
)
from _appkit_cloudflare_support import (
    workspace as workspace,
)
from disco.agent_server.appkit_cloudflare import deploy as cf
from disco.agent_server.appkit_cloudflare.models import RefusalReason
from disco.agent_server.appkit_cloudflare.wrangler import CommandResult
from disco.agent_server.workspace_commit import WorkspaceCommitUnavailable
from disco.core.llm.secrets import SecretBox, SecretStore

# ---- O1: account connection (token via the encrypted SecretStore) -----------


def test_connect_stores_token_encrypted_never_plaintext(store: SecretStore, tmp_path: Path):
    st = cf.connect_account(store, token=_CF_TOKEN, account_id=_ACCOUNT)
    assert st.connected and st.account_id == _ACCOUNT
    # The raw token must NEVER appear in the on-disk secrets file (ciphertext only).
    raw = (tmp_path / "secrets.json").read_text()
    assert _CF_TOKEN not in raw
    # Round-trips through decryption only.
    assert store.get_secret(cf.CF_TOKEN_SECRET) == _CF_TOKEN


def test_status_not_connected_without_token(store: SecretStore):
    st = cf.connection_status(store)
    assert not st.connected and not st.token_present


def test_status_locked_when_undecryptable(connected: SecretStore, tmp_path: Path):
    # A different app secret cannot decrypt the stored token → not connected.
    wrong = SecretStore(
        tmp_path / "secrets.json", box=SecretBox("a-totally-different-secret-key-999")
    )
    st = cf.connection_status(wrong)
    assert st.token_present and not st.decryptable and not st.connected


def test_disconnect_clears(connected: SecretStore):
    st = cf.disconnect_account(connected)
    assert not st.connected and not st.token_present


# ---- plan construction (side-effect-free) -----------------------------------


def test_build_plan_ready_and_steps(workspace: Path, connected: SecretStore):
    plan = cf.build_plan(workspace, connected)
    assert plan.export_ready
    assert plan.worker_name and plan.db_name
    assert plan.connected and plan.account_id == _ACCOUNT
    # The mutating legs incl. the secret-put (deploy-class) are present.
    titles = [s.title for s in plan.steps]
    assert "Deploy the Worker" in titles
    assert any("secret" in s.command.lower() and s.mutating for s in plan.steps)
    # Confirmation phrase is bound to worker + account + plan_hash slice.
    assert plan.plan_hash[:16] in plan.confirmation_phrase
    assert plan.worker_name in plan.confirmation_phrase


def test_build_plan_not_ready_on_incomplete_export(tmp_path: Path, connected: SecretStore):
    ws = tmp_path / "empty"
    ws.mkdir()
    plan = cf.build_plan(ws, connected)
    assert not plan.export_ready


def test_plan_hash_changes_when_tree_changes(workspace: Path, connected: SecretStore):
    before = cf.build_plan(workspace, connected).plan_hash
    (workspace / "schema.sql").write_text(
        (workspace / "schema.sql").read_text() + "\n-- drift\n", encoding="utf-8"
    )
    after = cf.build_plan(workspace, connected).plan_hash
    assert before != after  # a stale confirmation phrase can never match a changed tree


# ---- P0-1: plan_hash covers the FULL deploy tree, not just CF_EXPORT_FILES ----


def test_plan_hash_changes_on_build_script_drift(workspace: Path, connected: SecretStore):
    # package.json (the workspace-controlled `npm run build` script) is NOT in
    # CF_EXPORT_FILES, yet it drives what actually deploys → must change plan_hash.
    before = cf.build_plan(workspace, connected).plan_hash
    pkg = workspace / "package.json"
    pkg.write_text(pkg.read_text().replace('"vite build"', '"vite build && evil"'), "utf-8")
    after = cf.build_plan(workspace, connected).plan_hash
    assert before != after


def test_plan_hash_changes_on_client_source_drift(workspace: Path, connected: SecretStore):
    # src/App.tsx is compiled by the build but is NOT a CF_EXPORT_FILES deliverable.
    before = cf.build_plan(workspace, connected).plan_hash
    app_tsx = workspace / "src" / "App.tsx"
    app_tsx.write_text(app_tsx.read_text() + "\n// drift\n", "utf-8")
    after = cf.build_plan(workspace, connected).plan_hash
    assert before != after


def test_plan_hash_changes_on_lockfile_change(workspace: Path, connected: SecretStore):
    before = cf.build_plan(workspace, connected).plan_hash
    (workspace / "package-lock.json").write_text('{"lockfileVersion": 3}', "utf-8")
    after = cf.build_plan(workspace, connected).plan_hash
    assert before != after


def test_plan_hash_ignores_internal_disco_state(workspace: Path, connected: SecretStore):
    # The internal .disco state (deployment records) must NOT perturb the digest,
    # else idempotent re-deploys would never match their own confirmation.
    before = cf.build_plan(workspace, connected).plan_hash
    rec = workspace / ".disco" / "cloudflare" / "deployments"
    rec.mkdir(parents=True)
    (rec / "20260101T000000Z.json").write_text("{}", "utf-8")
    after = cf.build_plan(workspace, connected).plan_hash
    assert before == after


async def test_stale_confirmation_after_build_script_drift_refused(
    workspace: Path, connected: SecretStore
):
    # The security property: a confirmation bound to the OLD hash is refused once
    # the (non-CF_EXPORT_FILES) build script drifts.
    plan = cf.build_plan(workspace, connected)
    phrase = plan.confirmation_phrase
    pkg = workspace / "package.json"
    pkg.write_text(pkg.read_text() + "\n", "utf-8")  # build-affecting drift
    reason = await _refusal(
        cf.execute_deploy(
            workspace, connected, dry_run=False, confirmation=phrase, runner=FakeRunner()
        )
    )
    assert reason == RefusalReason.CONFIRMATION_REQUIRED


async def test_gate_autonomous_refused(workspace: Path, connected: SecretStore):
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation="anything",
            autonomous=True,
            runner=runner,
        )
    )
    assert reason == RefusalReason.AUTONOMOUS
    assert runner.calls == []  # never touched the runner


async def test_gate_export_not_ready(tmp_path: Path, connected: SecretStore):
    ws = tmp_path / "empty"
    ws.mkdir()
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(ws, connected, dry_run=False, confirmation="x", runner=runner)
    )
    assert reason == RefusalReason.EXPORT_NOT_READY
    assert runner.calls == []


async def test_gate_no_connected_account(workspace: Path, store: SecretStore):
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(workspace, store, dry_run=False, confirmation="x", runner=runner)
    )
    assert reason == RefusalReason.NO_CONNECTED_ACCOUNT
    assert runner.calls == []


async def test_gate_confirmation_required_when_missing(workspace: Path, connected: SecretStore):
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(workspace, connected, dry_run=False, confirmation=None, runner=runner)
    )
    assert reason == RefusalReason.CONFIRMATION_REQUIRED
    assert runner.calls == []


async def test_gate_stale_confirmation_refused(workspace: Path, connected: SecretStore):
    plan = cf.build_plan(workspace, connected)
    phrase = plan.confirmation_phrase
    # Tree drifts AFTER the phrase was shown → the fresh plan_hash differs → refuse.
    (workspace / "schema.sql").write_text(
        (workspace / "schema.sql").read_text() + "\n-- drift\n", encoding="utf-8"
    )
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(workspace, connected, dry_run=False, confirmation=phrase, runner=runner)
    )
    assert reason == RefusalReason.CONFIRMATION_REQUIRED
    assert runner.calls == []


# ---- dry-run is the DEFAULT and has ZERO side effects -----------------------


async def test_dry_run_default_no_side_effects(workspace: Path, connected: SecretStore):
    runner = FakeRunner()
    result = await cf.execute_deploy(workspace, connected, runner=runner)  # dry_run defaults True
    assert result.dry_run and not result.executed
    assert result.plan.export_ready
    assert runner.calls == []  # the runner is NEVER called in dry-run
    # No deployment record was written.
    assert not (workspace / ".disco" / "cloudflare" / "deployments").exists()


async def test_deploy_mutation_hooks_begin_at_first_record_and_finish_afterward(
    workspace: Path, connected: SecretStore
):
    transitions: list[tuple[str, int]] = []
    records = workspace / ".disco" / "cloudflare" / "deployments"

    async def on_start() -> None:
        transitions.append(("start", len(list(records.glob("*.json"))) if records.exists() else 0))

    async def on_finish() -> None:
        transitions.append(("finish", len(list(records.glob("*.json")))))

    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=FakeRunner(),
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
        on_mutation_start=on_start,
        on_mutation_finish=on_finish,
    )

    assert result.succeeded
    assert transitions == [("start", 0), ("finish", 1)]


async def test_readonly_deploy_abort_never_enters_mutation_scope(
    workspace: Path, connected: SecretStore
):
    transitions: list[str] = []

    async def on_start() -> None:
        transitions.append("start")

    async def on_finish() -> None:
        transitions.append("finish")

    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=FakeRunner(fail_on="d1 list"),
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
        on_mutation_start=on_start,
        on_mutation_finish=on_finish,
    )

    assert not result.succeeded
    assert transitions == []


async def test_rejected_mutation_start_does_not_run_finish(workspace: Path, connected: SecretStore):
    transitions: list[str] = []

    async def on_start() -> None:
        transitions.append("start")
        raise WorkspaceCommitUnavailable("committed head changed before mutation")

    async def on_finish() -> None:
        transitions.append("finish")

    plan = cf.build_plan(workspace, connected)
    with pytest.raises(WorkspaceCommitUnavailable, match="committed head changed"):
        await cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=FakeRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
            on_mutation_start=on_start,
            on_mutation_finish=on_finish,
        )

    assert transitions == ["start"]
    assert not (workspace / ".disco" / "cloudflare" / "deployments").exists()


async def test_repeatedly_cancelled_deploy_drains_mutation_finish_before_returning(
    workspace: Path, connected: SecretStore
):
    mutation_reached = asyncio.Event()
    release_runner = asyncio.Event()
    finish_started = asyncio.Event()
    finish_release = asyncio.Event()
    finish_done = asyncio.Event()
    finish_cancelled = asyncio.Event()
    statuses_seen_at_finish: list[str] = []

    class BlockingRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            if _is_d1_create(argv):
                mutation_reached.set()
                await release_runner.wait()
            return await super().run(argv, cwd=cwd, env=env, stdin=stdin)

    async def on_start() -> None:
        return None

    async def on_finish() -> None:
        records = workspace / ".disco" / "cloudflare" / "deployments"
        statuses_seen_at_finish.extend(
            json.loads(path.read_text(encoding="utf-8"))["status"]
            for path in records.glob("*.json")
        )
        finish_started.set()
        try:
            await finish_release.wait()
        except asyncio.CancelledError:
            finish_cancelled.set()
            raise
        finish_done.set()

    plan = cf.build_plan(workspace, connected)
    task = asyncio.create_task(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=BlockingRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
            on_mutation_start=on_start,
            on_mutation_finish=on_finish,
        )
    )
    await asyncio.wait_for(mutation_reached.wait(), timeout=10)
    task.cancel("first")
    await asyncio.wait_for(finish_started.wait(), timeout=10)
    assert not task.done()
    task.cancel("second")
    await asyncio.sleep(0)
    assert not task.done()
    assert not finish_cancelled.is_set()
    finish_release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("first",)
    assert finish_done.is_set()
    assert not finish_cancelled.is_set()
    assert statuses_seen_at_finish == ["failed"]
    records = list((workspace / ".disco/cloudflare/deployments").glob("*.json"))
    assert len(records) == 1
    closed = json.loads(records[0].read_text(encoding="utf-8"))
    assert closed["status"] == "failed"
    assert closed["attempted_step"] == "deploy interrupted"
    assert closed["error_detail"] == "Deploy execution was cancelled after the mutation fence."


async def test_runner_exception_closes_exact_attempt_before_finish_hook(
    workspace: Path,
    connected: SecretStore,
) -> None:
    planted_secret = f"{_CF_TOKEN}-must-not-enter-record"
    failure = RuntimeError(f"runner exploded with {planted_secret} at /host/private")
    status_at_finish: list[str] = []

    class RaisingRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            if _is_d1_create(argv):
                raise failure
            return await super().run(argv, cwd=cwd, env=env, stdin=stdin)

    async def on_finish() -> None:
        record = next((workspace / ".disco/cloudflare/deployments").glob("*.json"))
        status_at_finish.append(json.loads(record.read_text(encoding="utf-8"))["status"])

    plan = cf.build_plan(workspace, connected)
    with pytest.raises(RuntimeError) as caught:
        await cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=RaisingRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
            on_mutation_start=lambda: asyncio.sleep(0),
            on_mutation_finish=on_finish,
        )

    assert caught.value is failure
    assert status_at_finish == ["failed"]
    records = list((workspace / ".disco/cloudflare/deployments").glob("*.json"))
    assert len(records) == 1
    raw = records[0].read_text(encoding="utf-8")
    record = json.loads(raw)
    assert record["status"] == "failed"
    assert record["attempted_step"] == "deploy interrupted"
    assert planted_secret not in raw
    assert "/host/private" not in raw


async def test_exception_after_d1_create_closes_attempt_as_partial_failure(
    workspace: Path,
    connected: SecretStore,
) -> None:
    failure = RuntimeError("migration runner escaped")

    class RaisingRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            if "d1 execute" in " ".join(argv):
                raise failure
            return await super().run(argv, cwd=cwd, env=env, stdin=stdin)

    plan = cf.build_plan(workspace, connected)
    with pytest.raises(RuntimeError) as caught:
        await cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=RaisingRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )

    assert caught.value is failure
    record_path = next((workspace / ".disco/cloudflare/deployments").glob("*.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "partial_failure"
    assert f"d1_create:{plan.db_name}" in record["mutations"]
    assert record["attempted_step"] == "deploy interrupted"


# ---- the real (fully-gated) deploy via the FAKE runner ----------------------


async def test_real_deploy_token_in_env_not_argv(workspace: Path, connected: SecretStore):
    runner = FakeRunner()
    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token="super-secret-admin-token-xyz",
    )
    assert result.executed and not result.dry_run
    assert result.deployed_url == "https://acme-leads.workers.dev"
    # The wrangler sequence ran.
    cmds = [" ".join(c["argv"]) for c in runner.calls]
    assert any("wrangler deploy" in c for c in cmds)
    assert any("d1 execute" in c for c in cmds)
    # SECRET HANDLING: the token is in the ENV of every wrangler call, NEVER argv.
    wrangler_calls = [c for c in runner.calls if _is_wrangler(c["argv"])]
    assert wrangler_calls
    for c in runner.calls:
        assert _CF_TOKEN not in " ".join(c["argv"])  # never on the command line
        if _is_wrangler(c["argv"]):
            assert c["env"].get("CLOUDFLARE_API_TOKEN") == _CF_TOKEN  # only in env
    # ADMIN_TOKEN goes via stdin, never argv.
    secret_call = next(c for c in runner.calls if c["argv"][-1] == "ADMIN_TOKEN")
    assert secret_call["stdin"] == "super-secret-admin-token-xyz"
    assert "super-secret-admin-token-xyz" not in " ".join(secret_call["argv"])


async def test_real_deploy_record_has_no_secret(workspace: Path, connected: SecretStore):
    runner = FakeRunner()
    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token="super-secret-admin-token-xyz",
    )
    rec = Path(result.record_path).read_text()
    assert _CF_TOKEN not in rec
    assert "super-secret-admin-token-xyz" not in rec
    data = json.loads(rec)
    assert data["worker_name"] == plan.worker_name
    assert data["plan_hash"] == plan.plan_hash
    # Transcript is redacted (no raw token anywhere).
    joined = "\n".join(result.transcript)
    assert _CF_TOKEN not in joined


# ---- SEC-25: cross-process advisory deploy lock -----------------------------


def test_cross_process_lock_path_is_outside_workspace(workspace: Path, tmp_path: Path):
    """The lockfile lives in the server-controlled lock dir (NOT the untrusted
    workspace), keyed by a sha256 of the resolved workspace path."""
    p = cf._deploy_lock_path(workspace)
    lock_dir = tmp_path / "deploy-locks"
    assert p.parent == lock_dir.resolve() or p.parent == lock_dir
    # Never inside the workspace tree (the sandboxed build agent must not reach it).
    assert not p.resolve().is_relative_to(workspace.resolve())
    assert p.name.endswith(".lock")
    # Stable + per-workspace: same workspace → same path; the digest keys it.
    assert cf._deploy_lock_path(workspace) == p


async def test_deploy_refused_when_cross_process_lock_held(workspace: Path, connected: SecretStore):
    """SEC-25: when ANOTHER process (simulated by an independent fd holding the flock on
    the SAME lockfile) holds the cross-process lock, a real deploy is REFUSED fast with
    DEPLOY_IN_PROGRESS and the critical section never runs (the runner is untouched)."""
    import fcntl

    runner = FakeRunner()
    plan = cf.build_plan(workspace, connected)
    # Simulate a concurrent deploy in another server process: grab the flock on the same
    # lockfile via a separate fd (on Linux, a second fd — even in-process — conflicts).
    lock_path = cf._deploy_lock_path(workspace)
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(cf.DeployRefused) as ei:
            await cf.execute_deploy(
                workspace,
                connected,
                dry_run=False,
                confirmation=plan.confirmation_phrase,
                runner=runner,
                build_backend=FakeBuildBackend(),
                admin_token=_APP_ADMIN_TOKEN,
            )
        assert ei.value.reason is RefusalReason.DEPLOY_IN_PROGRESS
        # The critical section did NOT run — no wrangler call, no deployment record.
        assert runner.calls == []
        assert not (workspace / ".disco" / "cloudflare" / "deployments").exists()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


async def test_uncontended_deploy_acquires_and_releases_lock(
    workspace: Path, connected: SecretStore
):
    """SEC-25: an uncontended deploy proceeds AND releases the lock — so a SUBSEQUENT
    deploy (the lock now free) succeeds too. Proves the finally-release isn't leaked."""
    import fcntl

    plan = cf.build_plan(workspace, connected)
    r1 = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=FakeRunner(),
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert r1.executed and r1.succeeded
    # The lock is RELEASED: an independent fd can immediately re-acquire it non-blocking.
    lock_path = cf._deploy_lock_path(workspace)
    probe_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if still held
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
    finally:
        os.close(probe_fd)
    # And a fresh redeploy (lock free) runs to completion again.
    plan2 = cf.build_plan(workspace, connected)
    r2 = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan2.confirmation_phrase,
        runner=FakeRunner(worker_exists=True, d1_list_has=plan2.db_name),
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert r2.executed and r2.succeeded


async def test_stale_lockfile_does_not_deadlock(workspace: Path, connected: SecretStore):
    """SEC-25: a STALE lockfile left by a prior (now-gone) holder must NOT deadlock the
    next deploy — flock auto-releases when the holding fd closes (process death), so a
    leftover lockfile on disk is harmless and the next deploy re-acquires it."""
    import fcntl

    # Simulate a dead holder: a fd that acquired then CLOSED (flock auto-released), with
    # the lockfile still present on disk.
    lock_path = cf._deploy_lock_path(workspace)
    dead_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(dead_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(dead_fd)  # holder "dies" — flock released, lockfile lingers
    assert lock_path.exists()
    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=FakeRunner(),
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert result.executed and result.succeeded


async def test_non_posix_host_degrades_to_in_process_lock(
    workspace: Path, connected: SecretStore, monkeypatch, caplog
):  # noqa: ANN001
    """SEC-25: a non-POSIX host (no ``fcntl``) must degrade to the in-process lock with a
    logged warning — never crash the deploy."""
    import logging

    monkeypatch.setattr(cf, "_fcntl", None)
    plan = cf.build_plan(workspace, connected)
    with caplog.at_level(logging.WARNING, logger=cf._log.name):
        result = await cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=FakeRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    assert result.executed and result.succeeded
    assert any("process-local only" in r.message for r in caplog.records)


async def test_idempotent_redeploy_adopts_existing_d1(workspace: Path, connected: SecretStore):
    plan = cf.build_plan(workspace, connected)
    _seed_ownership_record(workspace, plan, connected)  # SEC-10: a prior record proves ownership
    runner = FakeRunner(d1_list_has=plan.db_name)  # DB already exists by name
    await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    # d1 create must NOT be called when the DB is already present.
    assert not any(_is_d1_create(c["argv"]) for c in runner.calls)


# ---- P0-1: the UNTRUSTED build runs in an isolating SANDBOX, never the host ----


async def test_build_dispatched_to_sandbox_not_host_subprocess(
    workspace: Path, connected: SecretStore
):
    # The workspace-controlled build must run via the SANDBOX build backend, NOT as
    # an unsandboxed same-user host subprocess (where it could read host secrets).
    runner = FakeRunner()
    build_backend = FakeBuildBackend()
    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=build_backend,
        admin_token="app-admin-token-xyz",
    )
    assert result.executed and result.succeeded
    # The build was dispatched to the sandbox backend exactly once.
    assert len(build_backend.calls) == 1
    # The host command runner NEVER ran the build (no `npm run build`/`npm ci` on the host).
    for c in runner.calls:
        assert c["argv"][:1] != ["npm"], "the build must not run as a host subprocess"
    # Only the trusted wrangler steps ran on the host runner.
    assert all(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_real_deploy_refused_when_build_cannot_be_sandboxed(
    workspace: Path, connected: SecretStore
):
    # No build backend → refuse (P0-1 minimum): never run the untrusted build
    # unsandboxed on the deploy path.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=None,
        )
    )
    assert reason == RefusalReason.BUILD_NOT_SANDBOXED
    assert runner.calls == []  # refused BEFORE any host command ran
    # A non-isolating backend (e.g. the same-user `process` backend) is also refused.
    reason2 = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=FakeBuildBackend(isolated=False),
        )
    )
    assert reason2 == RefusalReason.BUILD_NOT_SANDBOXED
    assert runner.calls == []


# ---- P0-2: the install is the DETERMINISTIC `npm ci` (from the lockfile) -------


async def test_build_install_is_deterministic_npm_ci(workspace: Path, connected: SecretStore):
    runner = FakeRunner()
    build_backend = FakeBuildBackend()
    plan = cf.build_plan(workspace, connected)
    await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=build_backend,
        admin_token=_APP_ADMIN_TOKEN,
    )
    call = build_backend.calls[0]
    # `npm ci` installs EXACTLY from the hash-covered package-lock.json — not a
    # bare build on arbitrary pre-installed node_modules.
    assert call["install_cmd"] == "npm ci"
    assert call["build_cmd"] == "npm run build"


# ---- P1-3: the real D1 database_id is substituted into wrangler.toml pre-deploy --


async def test_database_id_substituted_before_deploy_on_create(
    workspace: Path, connected: SecretStore
):
    # A fresh deploy creates the D1 DB; the real id must replace the placeholder in
    # wrangler.toml BEFORE `wrangler deploy` (else the D1 binding is non-functional).
    assert "REPLACE_WITH_D1_DATABASE_ID" in (workspace / "wrangler.toml").read_text()
    runner = FakeRunner()  # d1_list empty → create path
    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert result.executed and result.succeeded
    # At the moment deploy ran, the placeholder was already gone, replaced by the id.
    assert runner.wrangler_toml_at_deploy is not None
    assert "REPLACE_WITH_D1_DATABASE_ID" not in runner.wrangler_toml_at_deploy
    assert _DB_ID in runner.wrangler_toml_at_deploy


async def test_database_id_substituted_before_deploy_on_adopt(
    workspace: Path, connected: SecretStore
):
    # Idempotent re-deploy: the DB already exists → adopt its id from `d1 list`,
    # still substitute the placeholder before deploy.
    plan = cf.build_plan(workspace, connected)
    _seed_ownership_record(workspace, plan, connected)  # SEC-10: prior record proves ownership
    runner = FakeRunner(d1_list_has=plan.db_name)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert result.succeeded
    assert runner.wrangler_toml_at_deploy is not None
    assert _DB_ID in runner.wrangler_toml_at_deploy
    assert "REPLACE_WITH_D1_DATABASE_ID" not in runner.wrangler_toml_at_deploy


async def test_deploy_aborts_when_database_id_unresolved(workspace: Path, connected: SecretStore):
    # If wrangler output yields no usable database_id, FAIL CLOSED — never deploy a
    # Worker with an unbound (placeholder) D1 binding.
    class _NoIdRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            res = await super().run(argv, cwd=cwd, env=env, stdin=stdin)
            if _is_d1_create(argv):
                return CommandResult(0, "Created (no id echoed)", "")  # no UUID
            return res

    plan = cf.build_plan(workspace, connected)
    runner = _NoIdRunner()
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert not result.succeeded and not result.executed
    assert "database_id" in (result.failed_step or "")
    # deploy must NOT have run with the placeholder still in place.
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


# ---- Epic O WAVE 2: deploy.py resource-semantics + wave-1 misses -------------


async def test_asset_dir_passed_through_to_build(workspace: Path, connected: SecretStore):
    # CORR-16/SEC-1: the build backend must be told the EXACT `[assets].directory`
    # wrangler will publish (default `dist`), so a non-dist app builds/syncs the right dir.
    runner = FakeRunner()
    bb = FakeBuildBackend()
    plan = cf.build_plan(workspace, connected)
    await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=bb,
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert bb.calls[0]["asset_dir"] == "dist"


async def test_custom_asset_dir_synced(workspace: Path, connected: SecretStore):
    # A non-default `[assets].directory` ("./build") is resolved and passed through.
    toml = (workspace / "wrangler.toml").read_text()
    (workspace / "wrangler.toml").write_text(
        toml.replace('directory = "./dist"', 'directory = "./build"'), encoding="utf-8"
    )
    runner = FakeRunner()
    bb = FakeBuildBackend()
    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=bb,
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert result.executed and result.succeeded
    assert bb.calls[0]["asset_dir"] == "build"


async def test_root_asset_dir_refused(workspace: Path, connected: SecretStore):
    # SEC-1/CORR-16: an assets dir resolving to the workspace ROOT would publish source/
    # stale files — refuse.
    toml = (workspace / "wrangler.toml").read_text()
    (workspace / "wrangler.toml").write_text(
        toml.replace('directory = "./dist"', 'directory = "."'), encoding="utf-8"
    )
    plan = cf.build_plan(workspace, connected)
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=FakeRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.ASSET_DIR_UNSAFE


async def test_relative_path_wrangler_refused(workspace: Path, connected: SecretStore, monkeypatch):
    # SEC-3/CORR-3: with NO trusted absolute wrangler resolvable (pinned bin unset, PATH
    # only relative entries), the deploy FAILS CLOSED rather than fall back to bare
    # `wrangler` (which a build-controlled `dist/wrangler` could shadow).
    monkeypatch.delenv("DISCO_WRANGLER_BIN", raising=False)
    monkeypatch.setenv("PATH", "./node_modules/.bin:relbin")  # only RELATIVE entries
    plan = cf.build_plan(workspace, connected)
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=FakeRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.WRANGLER_NOT_TRUSTED


async def test_wrangler_extra_key_rejected(workspace: Path, connected: SecretStore):
    # SEC-5: an unexpected top-level key (custom `routes`) broadens the blast radius —
    # the wrangler.toml allowlist rejects it before any mutation. PREPEND so `routes`
    # is a TOP-LEVEL key (appending lands it inside the last [[d1_databases]] table).
    (workspace / "wrangler.toml").write_text(
        'routes = ["example.com/*"]\n' + (workspace / "wrangler.toml").read_text(),
        encoding="utf-8",
    )
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.WRANGLER_CONFIG_REJECTED
    assert runner.calls == []  # refused before any wrangler mutation


# ---- SEC (config-source bypass): wrangler.toml is the SOLE config source ------
# `wrangler deploy` resolves config wrangler.json -> .jsonc -> .toml (and honors a
# `.wrangler/deploy/config.json` redirect), but the deploy gates validate ONLY
# wrangler.toml. A planted alt config would deploy an UNCHECKED main/name/account.


@pytest.mark.parametrize("alt_name", ["wrangler.json", "wrangler.jsonc"])
async def test_alt_wrangler_json_config_refused(
    workspace: Path, connected: SecretStore, alt_name: str
):
    # Plant an alternate JSON config naming an EVIL main/name. Wrangler would read it
    # BEFORE wrangler.toml by precedence — the alt-config gate refuses (ALT_WRANGLER_CONFIG)
    # before any wrangler is invoked, so the evil bytes never deploy.
    (workspace / alt_name).write_text(
        json.dumps({"name": "evil-worker", "main": "evil/index.ts"}), encoding="utf-8"
    )
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.ALT_WRANGLER_CONFIG
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)  # wrangler never invoked


async def test_wrangler_redirect_config_refused_and_excluded_from_staging(
    workspace: Path, connected: SecretStore
):
    # Plant a `.wrangler/deploy/config.json` redirect pointing at an arbitrary alt config.
    redirect = workspace / ".wrangler" / "deploy" / "config.json"
    redirect.parent.mkdir(parents=True, exist_ok=True)
    redirect.write_text(json.dumps({"configPath": "../../evil/wrangler.jsonc"}), encoding="utf-8")
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.ALT_WRANGLER_CONFIG
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)  # wrangler never invoked
    # Defence in depth: `.wrangler` is ALSO excluded from the staged deploy tree, so the
    # redirect can't even be PRESENT where wrangler runs (cwd=staged).
    staged = cf._stage_deploy_tree(workspace)
    try:
        assert not (staged / ".wrangler").exists()
        # The legitimate wrangler.toml IS staged (sole config source).
        assert (staged / "wrangler.toml").exists()
    finally:
        import shutil as _sh

        _sh.rmtree(staged, ignore_errors=True)

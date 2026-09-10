"""AppKit EPIC O -- Cloudflare deploy capability tests (split 2 of 5).

Split from the original test_appkit_cloudflare.py during its PY-0360
module-size decomposition (pure mechanical split, no test logic changed).
Shared fixtures/fakes/constants live in _appkit_cloudflare_support.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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
    _HOST_SECRET_MARKER as _HOST_SECRET_MARKER,
)
from _appkit_cloudflare_support import (
    FakeBuildBackend as FakeBuildBackend,
)
from _appkit_cloudflare_support import (
    FakeRunner as FakeRunner,
)
from _appkit_cloudflare_support import (
    _AltConfigEmittingBuildBackend as _AltConfigEmittingBuildBackend,
)
from _appkit_cloudflare_support import (
    _backend as _backend,
)
from _appkit_cloudflare_support import (
    _ConfigurableRuntime as _ConfigurableRuntime,
)
from _appkit_cloudflare_support import (
    _ConfigurableService as _ConfigurableService,
)
from _appkit_cloudflare_support import (
    _ExecRaisesInstance as _ExecRaisesInstance,
)
from _appkit_cloudflare_support import (
    _FakeSandboxInstance as _FakeSandboxInstance,
)
from _appkit_cloudflare_support import (
    _install_read_spy as _install_read_spy,
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
    _open_egress_spec as _open_egress_spec,
)
from _appkit_cloudflare_support import (
    _RecordingSandboxInstance as _RecordingSandboxInstance,
)
from _appkit_cloudflare_support import (
    _refusal as _refusal,
)
from _appkit_cloudflare_support import (
    _seed_ownership_record as _seed_ownership_record,
)
from _appkit_cloudflare_support import (
    _ServiceLookupRaisesRuntime as _ServiceLookupRaisesRuntime,
)
from _appkit_cloudflare_support import (
    _SpecLookupRaisesRuntime as _SpecLookupRaisesRuntime,
)
from _appkit_cloudflare_support import (
    _StubRuntimeForBuild as _StubRuntimeForBuild,
)
from _appkit_cloudflare_support import (
    _SyncInstance as _SyncInstance,
)
from _appkit_cloudflare_support import (
    _TeardownRaisesInstance as _TeardownRaisesInstance,
)
from _appkit_cloudflare_support import (
    _trusted_wrangler as _trusted_wrangler,
)
from _appkit_cloudflare_support import (
    _wr_sub as _wr_sub,
)
from _appkit_cloudflare_support import (
    _write_export as _write_export,
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
from disco.core.llm.secrets import SecretStore


async def test_build_emitted_alt_config_refused_before_wrangler(
    workspace: Path, connected: SecretStore
):
    # BUILD TOCTOU: the authored tree is clean (pre-build gate passes), but the build emits
    # a wrangler.json into the staged tree. The POST-build re-scan refuses
    # (ALT_WRANGLER_CONFIG) before the first Cloudflare mutation — no wrangler ran.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=_AltConfigEmittingBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.ALT_WRANGLER_CONFIG
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)  # wrangler never invoked


# ---- SEC (OUT-OF-TREE config-source bypass): ancestor `.wrangler` redirect ----
# `wrangler deploy` discovers a `.wrangler/deploy/config.json` redirect by walking UP from
# its cwd (= the mkdtemp staged dir). The ALT_WRANGLER_CONFIG scan covers only the staged +
# live trees, so a PRE-EXISTING `.wrangler` at an ANCESTOR of the staged cwd (e.g. a planted
# /tmp/.wrangler) is outside both trees yet still honored. The ancestor-walk guard refuses it.


async def test_ancestor_wrangler_redirect_refused(
    workspace: Path, connected: SecretStore, _isolated_deploy_stage_dir: Path
):
    # Plant a `.wrangler/deploy/config.json` redirect at the STAGING ROOT — a direct ANCESTOR
    # of the mkdtemp staged cwd `wrangler deploy` runs in, but OUTSIDE both the staged copy and
    # the live workspace. wrangler would discover it by walking up from cwd and re-point the
    # deploy at `evil/wrangler.jsonc`. The ancestor-walk guard refuses (ANCESTOR_WRANGLER_CONFIG)
    # before any wrangler is invoked.
    stage_root = _isolated_deploy_stage_dir
    redirect = stage_root / ".wrangler" / "deploy" / "config.json"
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
    assert reason == RefusalReason.ANCESTOR_WRANGLER_CONFIG
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)  # wrangler never invoked


async def test_ancestor_wrangler_dir_alone_refused(
    workspace: Path, connected: SecretStore, _isolated_deploy_stage_dir: Path
):
    # Even a bare `.wrangler` dir (no config.json yet) at an ancestor is refused — fail closed
    # on the redirect VECTOR's mere presence, not only a fully-formed redirect file.
    (_isolated_deploy_stage_dir / ".wrangler").mkdir(parents=True, exist_ok=True)
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
    assert reason == RefusalReason.ANCESTOR_WRANGLER_CONFIG
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_ancestor_wrangler_symlink_not_dereferenced(
    workspace: Path, connected: SecretStore, _isolated_deploy_stage_dir: Path, tmp_path: Path
):
    # A `.wrangler` ANCESTOR that is a SYMLINK (to an attacker dir) is itself an offender and is
    # caught by lstat — WITHOUT dereferencing it to a host path.
    target = tmp_path / "evil-wrangler"
    (target / "deploy").mkdir(parents=True, exist_ok=True)
    (target / "deploy" / "config.json").write_text(
        json.dumps({"configPath": "../evil/wrangler.jsonc"}), encoding="utf-8"
    )
    _isolated_deploy_stage_dir.mkdir(parents=True, exist_ok=True)
    (_isolated_deploy_stage_dir / ".wrangler").symlink_to(target, target_is_directory=True)
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
    assert reason == RefusalReason.ANCESTOR_WRANGLER_CONFIG
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_clean_ancestor_chain_deploys(workspace: Path, connected: SecretStore):
    # The clean case: NO `.wrangler` anywhere above the staged cwd → the deploy proceeds and
    # `wrangler deploy` runs (the guard does not false-positive on a clean ancestor chain).
    runner = FakeRunner()
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
    assert any(_is_deploy(c["argv"]) for c in runner.calls)  # wrangler deploy DID run


def test_ancestor_wrangler_guard_unit(tmp_path: Path):
    # Direct unit check of the ancestor walk: a `.wrangler` at a parent of the cwd is found;
    # a clean chain passes. (Walks only WITHIN tmp_path here — the real fs root has none.)
    stage = tmp_path / "a" / "b" / "stage"
    stage.mkdir(parents=True)
    cf._assert_no_ancestor_wrangler_config(stage)  # clean: no refusal
    (tmp_path / "a" / ".wrangler").mkdir()
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_no_ancestor_wrangler_config(stage)
    assert exc.value.reason == RefusalReason.ANCESTOR_WRANGLER_CONFIG


async def test_config_account_id_refused(workspace: Path, connected: SecretStore):
    # P1 (config-source bypass): a planted top-level `account_id` in wrangler.toml could
    # redirect the deploy to a DIFFERENT account the token can access. account_id is no
    # longer allowlisted — the SEC-5 gate refuses it (the account comes ONLY from the
    # confirmed SecretStore, injected as CLOUDFLARE_ACCOUNT_ID).
    (workspace / "wrangler.toml").write_text(
        'account_id = "other-account-the-token-can-access"\n'
        + (workspace / "wrangler.toml").read_text(),
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


async def test_deploy_argv_pins_config_to_staged_wrangler_toml(
    workspace: Path, connected: SecretStore
):
    # The clean canonical single-wrangler.toml app still deploys, AND the `wrangler deploy`
    # argv PINS `--config <staged>/wrangler.toml` so wrangler can't pick an alt source.
    runner = FakeRunner()
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
    deploy_call = next(c for c in runner.calls if _is_deploy(c["argv"]))
    argv = deploy_call["argv"]
    assert "--config" in argv
    pinned = argv[argv.index("--config") + 1]
    # Pinned to the staged wrangler.toml — the EXACT file in the deploy cwd.
    assert pinned == str(Path(deploy_call["cwd"]) / "wrangler.toml")
    assert Path(pinned).name == "wrangler.toml"


async def test_weak_admin_token_rejected(workspace: Path, connected: SecretStore):
    # SEC-4: a short/low-variety admin token can't protect the deployed admin endpoint.
    plan = cf.build_plan(workspace, connected)
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=FakeRunner(),
            build_backend=FakeBuildBackend(),
            admin_token="short",
        )
    )
    assert reason == RefusalReason.ADMIN_TOKEN_WEAK


async def test_malformed_toml_refuses_not_500(tmp_path: Path, connected: SecretStore):
    # CORR-6: a malformed wrangler.toml must NOT raise an uncaught 500 from build_plan,
    # and the deploy must refuse (export not ready), never crash.
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_export(ws)
    (ws / "wrangler.toml").write_text('name = "x"\n[assets\nbroken', encoding="utf-8")
    plan = cf.build_plan(ws, connected)  # must NOT raise
    assert not plan.export_ready
    reason = await _refusal(
        cf.execute_deploy(
            ws,
            connected,
            dry_run=False,
            confirmation="x",
            runner=FakeRunner(),
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.EXPORT_NOT_READY


async def test_connected_requires_account_id(store: SecretStore):
    # SEC-29: a token-only connection (no account id) is NOT connected.
    store.set_secret(cf.CF_TOKEN_SECRET, _CF_TOKEN)  # token but NO account id
    st = cf.connection_status(store)
    assert st.token_present and st.decryptable and not st.connected and st.account_id is None


async def test_d1_list_non_uuid_rejected_no_fallback(workspace: Path, connected: SecretStore):
    # SEC-15/CORR-8: the adopted DB's id must be an EXACT UUID — a garbage id is NOT
    # accepted (no first-UUID fallback), so the deploy aborts unresolved (no deploy).
    plan = cf.build_plan(workspace, connected)
    _seed_ownership_record(workspace, plan, connected)

    class _BadIdRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            wr = _wr_sub(argv)
            if wr and wr[:2] == ["d1", "list"]:
                bad = json.dumps([{"name": plan.db_name, "uuid": "not-a-uuid"}])
                return CommandResult(0, bad, "")
            return await super().run(argv, cwd=cwd, env=env, stdin=stdin)

    runner = _BadIdRunner(d1_list_has=plan.db_name)
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
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_adopt_unrelated_d1_refused(workspace: Path, connected: SecretStore):
    # SEC-10: the DB exists remotely but NO prior Disco record proves ownership — refuse
    # to adopt/migrate an unrelated database.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner(d1_list_has=plan.db_name)  # exists, but no ownership record
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
    assert reason == RefusalReason.UNRELATED_RESOURCE
    assert not any(_is_d1_create(c["argv"]) for c in runner.calls)
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_destructive_schema_refused_on_adopt(workspace: Path, connected: SecretStore):
    # SEC-11/CORR-9: a destructive schema.sql must NOT run against an ADOPTED DB.
    (workspace / "schema.sql").write_text(
        (workspace / "schema.sql").read_text() + "\nDROP TABLE leads;\n", encoding="utf-8"
    )
    plan = cf.build_plan(workspace, connected)
    _seed_ownership_record(workspace, plan, connected)
    runner = FakeRunner(d1_list_has=plan.db_name)  # adopt path
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
    assert reason == RefusalReason.SCHEMA_UNSAFE
    assert not any("d1 execute" in " ".join(c["argv"]) for c in runner.calls)


async def test_pre_mutation_attempt_record_and_partial_failure_recorded(
    workspace: Path, connected: SecretStore
):
    # SEC-12/SEC-13/CORR-10/CORR-28: a deploy that fails AFTER a mutation (d1 create
    # succeeds, migration fails) durably records the PARTIAL state.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner(fail_on="d1 execute")  # create ok, migrate fails
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert not result.succeeded
    assert result.attempt_record_path is not None
    assert any("d1_create" in m for m in result.mutations)
    rec = json.loads(Path(result.attempt_record_path).read_text())
    assert rec["status"] == "partial_failure"
    assert any("d1_create" in m for m in rec["mutations"])
    assert "migrate" in (rec["attempted_step"] or "")
    # No secret in the durable partial record.
    assert _CF_TOKEN not in json.dumps(rec)
    assert _APP_ADMIN_TOKEN not in json.dumps(rec)


async def test_success_records_all_mutations(workspace: Path, connected: SecretStore):
    # A clean deploy finalizes the SAME durable record with status=succeeded + the full
    # mutation list, and the result mirrors it.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
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
    assert result.attempt_record_path == result.record_path
    assert "worker_deploy" in result.mutations and "secret_put" in result.mutations
    rec = json.loads(Path(result.record_path).read_text())
    assert rec["status"] == "succeeded"
    assert "d1_migrate" in rec["mutations"]


# ---- P1-5: a failed step ABORTS before later mutations ----------------------


async def test_failed_build_aborts_before_any_mutation(workspace: Path, connected: SecretStore):
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    # The SANDBOXED build fails → abort before any Cloudflare mutation.
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(fail=True),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert not result.succeeded and not result.executed
    assert result.failed_step == "npm ci && npm run build"
    assert result.record_path is None  # no deployment record on a failed deploy
    # No wrangler (mutating) step ran after the failed build.
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_failed_migration_aborts_before_deploy(workspace: Path, connected: SecretStore):
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner(fail_on="d1 execute")  # migration fails
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token="app-admin-token-xyz",
    )
    assert not result.succeeded
    assert "migrate" in (result.failed_step or "")
    # deploy + secret-put must NOT have run after the failed migration.
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)
    assert not any(c["argv"][-1] == "ADMIN_TOKEN" for c in runner.calls)


async def test_sync_output_back_normal_dist_syncs(tmp_path: Path):
    # A well-behaved build (all paths under ./dist) syncs back into dist verbatim.
    ws = tmp_path / "ws"
    ws.mkdir()
    inst = _FakeSandboxInstance(
        ["dist/index.html", "dist/assets/app.js"],
        {"dist/index.html": b"<html></html>", "dist/assets/app.js": b"console.log(1)"},
    )
    await _backend()._sync_output_back(inst, ws)
    assert (ws / "dist" / "index.html").read_bytes() == b"<html></html>"
    assert (ws / "dist" / "assets" / "app.js").read_bytes() == b"console.log(1)"


async def test_sync_output_back_rejects_traversal_and_absolute(tmp_path: Path):
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("ORIGINAL", encoding="utf-8")
    # `..` traversal, an absolute path, and an absolute path to a real host file —
    # each must be REJECTED before any write, and nothing lands outside ./dist.
    for listing in (["dist/../escape.txt"], ["/etc/evil"], [str(outside)]):
        with pytest.raises(sb._BuildOutputEscape):
            await _backend()._sync_output_back(_FakeSandboxInstance(listing), ws)
    assert outside.read_text() == "ORIGINAL"  # never written outside dist
    assert not (tmp_path / "escape.txt").exists()
    assert not (ws / "escape.txt").exists()


async def test_sync_output_back_rejects_symlink_escape(tmp_path: Path):
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    outside = tmp_path / "host_secret.txt"
    outside.write_text("HOST", encoding="utf-8")
    # A symlink that lives INSIDE dist but points OUTSIDE it: resolving the dest
    # follows the symlink → outside dist → rejected (no write through the symlink).
    (ws / "dist" / "evil").symlink_to(outside)
    inst = _FakeSandboxInstance(["dist/evil"], {"dist/evil": b"PWNED"})
    with pytest.raises(sb._BuildOutputEscape):
        await _backend()._sync_output_back(inst, ws)
    assert outside.read_text() == "HOST"  # the host file was never overwritten


async def test_sync_output_back_rejects_dist_root_symlink_escape(tmp_path: Path):
    """P0 (round 7): the HOST ``dist`` is ITSELF a symlink escaping the workspace.
    A dist-rooted check would ``resolve()`` ``dist`` to the escape target and then
    accept every synced file as "inside" that escaped root, writing the build output
    OUTSIDE the workspace onto host files. Anchoring containment to the resolved
    workspace root makes this FAIL CLOSED — nothing is written to the escape target."""
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    escape = tmp_path / "escape_target"  # OUTSIDE the workspace
    escape.mkdir()
    (escape / "index.html").write_text("ORIGINAL", encoding="utf-8")
    # Plant ``dist`` as a SYMLINK that escapes the workspace.
    (ws / "dist").symlink_to(escape)
    inst = _FakeSandboxInstance(["dist/index.html"], {"dist/index.html": b"PWNED"})
    with pytest.raises(sb._BuildOutputEscape):
        await _backend()._sync_output_back(inst, ws)
    # The outside escape target must be UNTOUCHED — no write through the dist link.
    assert (escape / "index.html").read_text() == "ORIGINAL"


async def test_sync_output_back_rejects_intermediate_dir_symlink_escape(tmp_path: Path):
    """P0 (round 7): a NESTED intermediate dir under a real ``dist`` is a symlink
    escaping the workspace (``dist/assets`` -> outside). The synced file's resolved
    dest leaves both ``dist`` and the workspace root → REJECTED, nothing written
    through the intermediate link."""
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    escape = tmp_path / "escape_dir"  # OUTSIDE the workspace
    escape.mkdir()
    (escape / "app.js").write_text("ORIGINAL", encoding="utf-8")
    # ``dist`` is a real dir, but the intermediate ``dist/assets`` escapes.
    (ws / "dist" / "assets").symlink_to(escape)
    inst = _FakeSandboxInstance(["dist/assets/app.js"], {"dist/assets/app.js": b"PWNED"})
    with pytest.raises(sb._BuildOutputEscape):
        await _backend()._sync_output_back(inst, ws)
    assert (escape / "app.js").read_text() == "ORIGINAL"  # never written through the link


async def test_sync_output_back_rejects_dist_symlink_inside_workspace(tmp_path: Path):
    """P0 (round 8): ``dist`` is a symlink pointing INSIDE the workspace (a planted
    ``dist`` -> ``./real_dist``). ``resolve()`` would FOLLOW it and accept the
    sync-back, broadening the write target beyond the logical ``./dist``. The
    lstat-based real-dir check REJECTS a symlinked ``dist`` even when it resolves
    inside the workspace, so build output lands ONLY in the real ``./dist`` — nothing
    is written through the link."""
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    real = ws / "real_dist"  # INSIDE the workspace
    real.mkdir()
    (real / "index.html").write_text("ORIGINAL", encoding="utf-8")
    (ws / "dist").symlink_to(real)  # dist is a symlink, not a real dir
    inst = _FakeSandboxInstance(["dist/index.html"], {"dist/index.html": b"PWNED"})
    with pytest.raises(sb._BuildOutputEscape):
        await _backend()._sync_output_back(inst, ws)
    # The symlink target (even in-workspace) was NOT written through the link.
    assert (real / "index.html").read_text() == "ORIGINAL"


async def test_push_rejects_symlink_escaping_workspace(tmp_path: Path):
    # A workspace SYMLINK pointing OUTSIDE the workspace (→ a host secret) must be
    # REJECTED on push: reading it would dereference the link and push host-secret
    # content into the build sandbox/artifact. Fail closed — the build fails and the
    # secret bytes are never read/pushed.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text("TOP-SECRET-HOST-CREDENTIAL", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    # A workspace file that is actually a symlink escaping the workspace.
    (ws / "leak.txt").symlink_to(host_secret)

    inst = _RecordingSandboxInstance()
    backend = sb.SandboxBuildBackend(_StubRuntimeForBuild(inst))  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok  # build failed closed
    assert "symlink escape" in result.stderr
    # The host secret's content was NEVER read/pushed into the sandbox.
    pushed_bytes = b"".join(inst.written.values())
    assert b"TOP-SECRET-HOST-CREDENTIAL" not in pushed_bytes
    assert "leak.txt" not in inst.written


async def test_push_normal_regular_file_pushes(tmp_path: Path):
    # The companion of the rejection test: a normal regular file inside the
    # workspace pushes fine (the guard only rejects escaping symlinks).
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    (ws / "src").mkdir()
    (ws / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")

    inst = _RecordingSandboxInstance()
    backend = sb.SandboxBuildBackend(_StubRuntimeForBuild(inst))  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert result.isolated
    assert "symlink escape" not in (result.stderr or "")
    assert inst.written["package.json"] == b'{"name":"app"}'
    assert inst.written["src/App.tsx"] == b"export default 1\n"


async def test_build_refuses_when_service_kind_flips_to_process(tmp_path: Path):
    # ISOLATION RACE (SEC-19/CORR-20): the pre-gate saw 'gvisor' but the service the
    # build would actually run on has flipped to 'process' (host). build() verifies
    # the SAME instance it builds on — it must REFUSE and NEVER create/run on host.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_RecordingSandboxInstance(), name="process")
    rt = _ConfigurableRuntime(svc, gate_name="gvisor")  # gate says gvisor; svc is process
    backend = sb.SandboxBuildBackend(rt)  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and not result.isolated
    assert "not deploy-grade" in result.stderr and "process" in result.stderr
    assert svc.created is False  # the untrusted build NEVER ran on the host


async def test_build_refuses_local_backend_by_default(tmp_path: Path):
    # SEC-20: a shared-kernel `local` container is NOT deploy-grade by default — a real
    # deploy must require gVisor/podman. Refuse, never create.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_RecordingSandboxInstance(), name="local")
    backend = sb.SandboxBuildBackend(_ConfigurableRuntime(svc))  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and not result.isolated
    assert "not deploy-grade" in result.stderr
    assert svc.created is False


async def test_build_allows_local_only_with_explicit_override(tmp_path: Path):
    # The high-risk opt-in: `local` becomes deploy-grade ONLY when explicitly allowed.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_RecordingSandboxInstance(), name="local")
    backend = sb.SandboxBuildBackend(
        _ConfigurableRuntime(svc),
        allow_local_isolation=True,  # type: ignore[arg-type]
    )
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    # It passed the deploy-grade gate (the box WAS created) — the override worked.
    assert svc.created is True
    assert "not deploy-grade" not in (result.stderr or "")


async def test_build_refuses_open_egress(tmp_path: Path):
    # SEC-21/CORR-22: a deploy build must run under FILTERED egress. An open-network
    # spec is REFUSED before any box is created.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_RecordingSandboxInstance(), name="gvisor")
    rt = _ConfigurableRuntime(svc, spec=_open_egress_spec())
    backend = sb.SandboxBuildBackend(rt)  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and not result.isolated
    assert "egress" in result.stderr.lower()
    assert svc.created is False


async def test_build_refuses_overbroad_egress_allowlist(tmp_path: Path):
    # SEC-21: a nominally-"filtered" spec whose allowlist is OVER-BROAD (a bare-TLD
    # suffix, a wildcard, or a dot-only entry) is still open-ended egress — a deploy
    # build must REFUSE it before any box is created.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb
    from disco.tools import SandboxSpec

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    for bad in (
        frozenset({"registry.npmjs.org", ".com"}),  # bare-TLD suffix → whole TLD
        frozenset({"*.npmjs.org"}),  # wildcard entry
        frozenset({"."}),  # dot-only → matches everything
    ):
        svc = _ConfigurableService(_RecordingSandboxInstance(), name="gvisor")
        rt = _ConfigurableRuntime(svc, spec=SandboxSpec(egress_allow=bad))
        backend = sb.SandboxBuildBackend(rt)  # type: ignore[arg-type]
        result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")
        assert not result.ok and not result.isolated, bad
        assert "egress" in result.stderr.lower(), bad
        assert svc.created is False, bad  # never built on an over-broad allowlist


async def test_build_refuses_allowlist_missing_registry(tmp_path: Path):
    # SEC-21: a non-empty, non-broad allowlist that nonetheless cannot reach the npm
    # registry is NOT a real build allowlist (the registry/CDN posture) → refuse.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb
    from disco.tools import SandboxSpec

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_RecordingSandboxInstance(), name="gvisor")
    rt = _ConfigurableRuntime(svc, spec=SandboxSpec(egress_allow=frozenset({"evil.example.com"})))
    backend = sb.SandboxBuildBackend(rt)  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and not result.isolated
    assert "egress" in result.stderr.lower()
    assert svc.created is False


async def test_build_accepts_exact_registry_allowlist(tmp_path: Path):
    # The companion: the EXACT canonical REGISTRY_EGRESS_ALLOW (what
    # _build_sandbox_spec(surface="build") resolves to) PASSES the strict egress gate —
    # the box IS created and the build runs in it (no egress refusal).
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb
    from disco.tools import SandboxSpec
    from disco.tools.sandbox.base import REGISTRY_EGRESS_ALLOW

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_RecordingSandboxInstance(), name="gvisor")
    rt = _ConfigurableRuntime(svc, spec=SandboxSpec(egress_allow=REGISTRY_EGRESS_ALLOW))
    backend = sb.SandboxBuildBackend(rt)  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    # It cleared the egress gate (the box WAS created); no egress refusal in stderr.
    assert svc.created is True
    assert "egress" not in (result.stderr or "").lower()


async def test_build_service_lookup_error_is_structured_failure(tmp_path: Path):
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    backend = sb.SandboxBuildBackend(_ServiceLookupRaisesRuntime())  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and not result.isolated
    assert "could not resolve sandbox service" in result.stderr
    assert "no sandbox backend configured" in result.stderr


async def test_build_spec_lookup_error_is_structured_failure(tmp_path: Path):
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_RecordingSandboxInstance(), name="gvisor")
    backend = sb.SandboxBuildBackend(_SpecLookupRaisesRuntime(svc))  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and not result.isolated
    assert "could not resolve build sandbox spec" in result.stderr
    assert "bad BUILD_EGRESS config" in result.stderr
    assert svc.created is False  # the box is never created when the spec can't resolve


async def test_build_env_override_gates_local(monkeypatch):
    # build_backend_for_runtime reads DISCO_APPKIT_DEPLOY_ALLOW_LOCAL — a truthy value
    # is required (presence of `0`/empty does NOT enable the weak backend).
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    rt = _ConfigurableRuntime(_ConfigurableService(None, name="local"))
    monkeypatch.delenv("DISCO_APPKIT_DEPLOY_ALLOW_LOCAL", raising=False)
    monkeypatch.delenv("PMX_APPKIT_DEPLOY_ALLOW_LOCAL", raising=False)
    b = sb.build_backend_for_runtime(rt)  # type: ignore[arg-type]
    assert b is not None and b._allow_local is False and b.isolates is False
    monkeypatch.setenv("DISCO_APPKIT_DEPLOY_ALLOW_LOCAL", "0")  # falsey → still off
    assert sb.build_backend_for_runtime(rt)._allow_local is False  # type: ignore[union-attr]
    monkeypatch.setenv("DISCO_APPKIT_DEPLOY_ALLOW_LOCAL", "1")  # truthy → on
    b2 = sb.build_backend_for_runtime(rt)  # type: ignore[arg-type]
    assert b2._allow_local is True and b2.isolates is True


async def test_build_sandbox_create_error_is_structured_failure(tmp_path: Path):
    # CORR-20: a sandbox CREATE exception becomes a structured failed BuildResult, not
    # a 500 escaping to the deploy caller.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(None, name="gvisor", create_exc=RuntimeError("docker down"))
    backend = sb.SandboxBuildBackend(_ConfigurableRuntime(svc))  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and not result.isolated
    assert "could not create sandbox" in result.stderr and "docker down" in result.stderr


async def test_build_sandbox_exec_error_is_structured_failure(tmp_path: Path):
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    inst = _ExecRaisesInstance()
    svc = _ConfigurableService(inst, name="gvisor")
    backend = sb.SandboxBuildBackend(_ConfigurableRuntime(svc))  # type: ignore[arg-type]
    result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert not result.ok and result.isolated  # it WAS running in the box
    assert "sandbox error during build" in result.stderr
    assert inst.destroyed is True  # still torn down


async def test_build_teardown_failure_is_surfaced(tmp_path: Path, caplog):
    import logging

    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    svc = _ConfigurableService(_TeardownRaisesInstance(), name="gvisor")
    backend = sb.SandboxBuildBackend(_ConfigurableRuntime(svc))  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        result = await backend.build(ws, install_cmd="npm ci", build_cmd="npm run build")

    assert result.returncode == 1  # the build's own failure propagates, no exception
    assert any("teardown failed" in r.message for r in caplog.records)


async def test_sync_output_back_clears_stale_dist_and_atomically_replaces(tmp_path: Path):
    # CORR-17/SEC-22: the sync CLEARS stale dist + replaces atomically — a stale file
    # left from a prior build must NOT survive into the deployed artifact.
    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    (ws / "dist" / "stale.txt").write_text("STALE", encoding="utf-8")
    (ws / "dist" / "index.html").write_text("OLD", encoding="utf-8")
    inst = _SyncInstance(["dist/index.html"], {"dist/index.html": b"NEW"})
    await _backend()._sync_output_back(inst, ws)
    assert (ws / "dist" / "index.html").read_bytes() == b"NEW"
    assert not (ws / "dist" / "stale.txt").exists()  # stale cleared
    assert inst.find_cmd is not None and "-print0" in inst.find_cmd  # SEC-23


async def test_sync_output_back_configured_non_dist_asset_dir(tmp_path: Path):
    # CORR-16: the app may build to a configured `[assets].directory` (e.g. build/),
    # not dist. The configured asset dir is synced.
    ws = tmp_path / "ws"
    ws.mkdir()
    inst = _SyncInstance(["build/index.html"], {"build/index.html": b"<html>"})
    await _backend()._sync_output_back(inst, ws, asset_dir="build")
    assert (ws / "build" / "index.html").read_bytes() == b"<html>"
    assert inst.find_cmd is not None and "find build" in inst.find_cmd


async def test_sync_output_back_find_failure_is_build_failure(tmp_path: Path):
    # CORR-18: a `find` that FAILS is a BUILD FAILURE, not a silent success.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    inst = _SyncInstance(["dist/index.html"], find_exit=2)
    with pytest.raises(sb._BuildOutputUnavailable):
        await _backend()._sync_output_back(inst, ws)


async def test_sync_output_back_none_read_is_build_failure(tmp_path: Path):
    # CORR-19: a listed output file that reads back as None is a BUILD FAILURE, not an
    # empty-file silent success.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    inst = _SyncInstance(["dist/index.html"], {"dist/index.html": None})
    with pytest.raises(sb._BuildOutputUnavailable):
        await _backend()._sync_output_back(inst, ws)
    # Nothing partial was written.
    assert not (ws / "dist" / "index.html").exists()


async def test_sync_output_back_empty_output_is_build_failure(tmp_path: Path):
    # An empty asset dir = nothing to deploy = a broken build → fail closed.
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    inst = _SyncInstance([])  # find succeeds but lists no files
    with pytest.raises(sb._BuildOutputUnavailable):
        await _backend()._sync_output_back(inst, ws)


async def test_sync_output_back_rejects_too_many_files(tmp_path: Path, monkeypatch):
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    monkeypatch.setattr(sb, "_MAX_OUTPUT_FILES", 2)
    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    inst = _SyncInstance(["dist/a.js", "dist/b.js", "dist/c.js"])
    with pytest.raises(sb._BuildOutputTooLarge):
        await _backend()._sync_output_back(inst, ws)


async def test_sync_output_back_rejects_too_many_bytes(tmp_path: Path, monkeypatch):
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    monkeypatch.setattr(sb, "_MAX_OUTPUT_BYTES", 8)
    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    inst = _SyncInstance(["dist/big.js"], {"dist/big.js": b"0123456789"})  # 10 > 8
    with pytest.raises(sb._BuildOutputTooLarge):
        await _backend()._sync_output_back(inst, ws)


async def test_sync_output_back_handles_newline_in_filename(tmp_path: Path):
    # SEC-23: a NUL-delimited listing (`-print0`) means a filename containing a
    # newline is ONE entry, not two — it is written intact, not split/corrupted.
    ws = tmp_path / "ws"
    (ws / "dist").mkdir(parents=True)
    weird = "dist/we\nird.js"
    inst = _SyncInstance([weird], {weird: b"OK"})
    await _backend()._sync_output_back(inst, ws)
    assert (ws / "dist" / "we\nird.js").read_bytes() == b"OK"
    # exactly one file landed in dist
    files = [p for p in (ws / "dist").rglob("*") if p.is_file()]
    assert len(files) == 1


# ---- P0 (round 5, PLANNER side): the tree-digest read also rejects an escaping --
# ---- symlink, BEFORE the push guard — no host-secret read into the plan_hash. ---


def test_planner_rejects_symlink_escaping_workspace(
    tmp_path: Path, connected: SecretStore, monkeypatch
):
    # The deploy PLANNER (plan_hash / tree-digest build) reads EVERY workspace file
    # — earlier than the push guard. An escaping symlink (→ a host secret) must be
    # REFUSED there too: fail closed BEFORE the symlink target's bytes are ever
    # dereferenced into the digest/plan_hash. Same shared containment guard as push.
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text("TOP-SECRET-HOST-CREDENTIAL", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    _write_export(ws)  # a complete, export-ready tree
    # A workspace file that is actually a symlink escaping the workspace.
    (ws / "leak.txt").symlink_to(host_secret)

    # Spy every read_bytes so we can PROVE the host secret is never dereferenced on
    # the planner path.
    read_paths: list[str] = []
    real_read_bytes = Path.read_bytes

    def _spy(self: Path) -> bytes:
        read_paths.append(str(self.resolve()))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _spy)

    with pytest.raises(cf.DeployRefused) as exc:
        cf.build_plan(ws, connected)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    # The escaping symlink's target (the host secret) was NEVER read into the digest.
    assert str(host_secret.resolve()) not in read_paths


def test_planner_normal_tree_plans_fine(workspace: Path, connected: SecretStore):
    # Companion: a normal tree (no escaping symlink) hashes/plans fine — the guard
    # only refuses escaping symlinks, never a regular file inside the workspace.
    plan = cf.build_plan(workspace, connected)
    assert plan.tree_digest
    assert plan.plan_hash
    assert plan.export_ready


def test_read_workspace_file_reads_normal_refuses_escape(tmp_path: Path, monkeypatch):
    # Direct unit test of the ONE guarded reader — the choke point every workspace
    # read (export-files, spec/tree digest, db_id/wrangler read, push) flows through.
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text(_HOST_SECRET_MARKER, encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "ok.txt").write_text("hello", encoding="utf-8")
    (ws / "leak.txt").symlink_to(host_secret)
    root = ws.resolve()

    # A normal regular file inside the workspace reads fine (text + bytes).
    assert cf.read_workspace_file(ws / "ok.txt", root) == "hello"
    assert cf.read_workspace_file(ws / "ok.txt", root, text=False) == b"hello"
    # A MISSING file is absence (None), not an error.
    assert cf.read_workspace_file(ws / "nope.txt", root) is None

    # An escaping symlink → REFUSED before any open; the host secret is NEVER read.
    seen = _install_read_spy(monkeypatch)
    with pytest.raises(cf.DeployRefused) as exc:
        cf.read_workspace_file(ws / "leak.txt", root, text=False)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert str(host_secret.resolve()) not in seen


def test_export_readiness_refuses_dev_vars_symlink_escape(
    tmp_path: Path, connected: SecretStore, monkeypatch
):
    # The codex round-6 P0: ``.dev.vars`` (the secret-safety input read by
    # _read_export_files, BEFORE _tree_digest) is a SYMLINK escaping the workspace
    # (→ a host secret). The plan build must REFUSE before dereferencing it.
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text(_HOST_SECRET_MARKER, encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    _write_export(ws)
    (ws / ".dev.vars").symlink_to(host_secret)

    seen = _install_read_spy(monkeypatch)
    with pytest.raises(cf.DeployRefused) as exc:
        cf.build_plan(ws, connected)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    # The host secret behind the .dev.vars symlink was NEVER opened.
    assert str(host_secret.resolve()) not in seen


def test_export_readiness_refuses_deliverable_symlink_escape(
    tmp_path: Path, connected: SecretStore, monkeypatch
):
    # A CF_EXPORT_FILES deliverable (here wrangler.toml — also the db_id read target)
    # is itself an escaping symlink → REFUSE in _read_export_files, before any read.
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text(_HOST_SECRET_MARKER, encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    _write_export(ws)
    (ws / "wrangler.toml").unlink()
    (ws / "wrangler.toml").symlink_to(host_secret)

    seen = _install_read_spy(monkeypatch)
    with pytest.raises(cf.DeployRefused) as exc:
        cf.build_plan(ws, connected)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert str(host_secret.resolve()) not in seen


def test_spec_digest_refuses_symlink_escape(tmp_path: Path, monkeypatch):
    # The spec digest reads ``.disco/appspec.json`` — guard it too: an escaping
    # symlink there refuses before the host secret is read.
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text(_HOST_SECRET_MARKER, encoding="utf-8")
    ws = tmp_path / "workspace"
    (ws / ".disco").mkdir(parents=True)
    (ws / ".disco" / "appspec.json").symlink_to(host_secret)

    seen = _install_read_spy(monkeypatch)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._spec_digest(ws)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert str(host_secret.resolve()) not in seen


def test_substitute_database_id_refuses_symlink_escape(tmp_path: Path, monkeypatch):
    # The db_id substitution reads wrangler.toml (deploy-time, defense in depth). An
    # escaping symlink there refuses BEFORE the read/write dereferences the link.
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text(_HOST_SECRET_MARKER, encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "wrangler.toml").symlink_to(host_secret)

    seen = _install_read_spy(monkeypatch)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._substitute_database_id(ws, _DB_ID)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert str(host_secret.resolve()) not in seen
    # The host secret was neither read nor overwritten through the symlink.
    assert host_secret.read_text(encoding="utf-8") == _HOST_SECRET_MARKER


def test_substitute_database_id_refuses_symlinked_component_inside_workspace(tmp_path: Path):
    # P0 (round 8, WRITE class): wrangler.toml is a symlink that points INSIDE the
    # workspace (so the guarded READ succeeds and sees the placeholder), but the
    # write-back must REFUSE — a symlinked component can never be written through,
    # even one resolve() would have masked as in-workspace.
    ws = tmp_path / "workspace"
    ws.mkdir()
    real = ws / "real_wrangler.toml"
    real.write_text(f'database_id = "{cf._D1_ID_PLACEHOLDER}"\n', encoding="utf-8")
    (ws / "wrangler.toml").symlink_to(real)  # in-workspace symlink

    with pytest.raises(cf.DeployRefused) as exc:
        cf._substitute_database_id(ws, _DB_ID)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    # The substitution was never written through the link — placeholder intact.
    assert cf._D1_ID_PLACEHOLDER in real.read_text(encoding="utf-8")
    assert _DB_ID not in real.read_text(encoding="utf-8")

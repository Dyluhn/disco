"""AppKit EPIC O -- Cloudflare deploy capability tests (split 4 of 5).

Split from the original test_appkit_cloudflare.py during its PY-0360
module-size decomposition (pure mechanical split, no test logic changed).
Shared fixtures/fakes/constants live in _appkit_cloudflare_support.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _appkit_cloudflare_support import (
    _APP_ADMIN_TOKEN as _APP_ADMIN_TOKEN,
)
from _appkit_cloudflare_support import (
    _CF_TOKEN as _CF_TOKEN,
)
from _appkit_cloudflare_support import (
    _NON_CANONICAL_MAINS as _NON_CANONICAL_MAINS,
)
from _appkit_cloudflare_support import (
    _TRANSITIVE_LEAK_DEBUG_TOKEN as _TRANSITIVE_LEAK_DEBUG_TOKEN,
)
from _appkit_cloudflare_support import (
    _UNGUARDED_DEBUG_LEADS as _UNGUARDED_DEBUG_LEADS,
)
from _appkit_cloudflare_support import (
    FakeBuildBackend as FakeBuildBackend,
)
from _appkit_cloudflare_support import (
    FakeRunner as FakeRunner,
)
from _appkit_cloudflare_support import (
    _canonical_worker_src as _canonical_worker_src,
)
from _appkit_cloudflare_support import (
    _corrupt_worker as _corrupt_worker,
)
from _appkit_cloudflare_support import (
    _form_folded_app_spec as _form_folded_app_spec,
)
from _appkit_cloudflare_support import (
    _form_folded_worker_src as _form_folded_worker_src,
)
from _appkit_cloudflare_support import (
    _inject_route as _inject_route,
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
    _plant_unsigned_ownership_record as _plant_unsigned_ownership_record,
)
from _appkit_cloudflare_support import (
    _PreflightErrorRunner as _PreflightErrorRunner,
)
from _appkit_cloudflare_support import (
    _refusal as _refusal,
)
from _appkit_cloudflare_support import (
    _refuse_real_deploy_with_emitted_worker as _refuse_real_deploy_with_emitted_worker,
)
from _appkit_cloudflare_support import (
    _SecretEmittingBuildBackend as _SecretEmittingBuildBackend,
)
from _appkit_cloudflare_support import (
    _seed_ownership_record as _seed_ownership_record,
)
from _appkit_cloudflare_support import (
    _staged_tree_with_worker as _staged_tree_with_worker,
)
from _appkit_cloudflare_support import (
    _trusted_wrangler as _trusted_wrangler,
)
from _appkit_cloudflare_support import (
    _wr_sub as _wr_sub,
)
from _appkit_cloudflare_support import (
    _WranglerEmittingBuildBackend as _WranglerEmittingBuildBackend,
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
from disco.core.appkit import default_lead_gen_app_spec, generate, get_recipe, save_app_spec
from disco.core.llm.secrets import SecretStore


async def test_deploy_uses_snapshotted_token_not_relive_read(
    workspace: Path, connected: SecretStore
):
    # The wrangler env carries the SNAPSHOTTED token bound to the confirmed plan.
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
    assert result.executed
    for c in runner.calls:
        if _is_wrangler(c["argv"]):
            assert c["env"].get("CLOUDFLARE_API_TOKEN") == _CF_TOKEN


# ---- A5: effective-artifact digest recorded ---------------------------------


async def test_record_carries_effective_digest(workspace: Path, connected: SecretStore):
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
    data = json.loads(Path(result.record_path).read_text())
    # The effective (post-build, post-D1-substitution) digest is bound into the audit
    # record and DIFFERS from the plan-time pre-build tree digest (dist + real db id).
    assert data["effective_digest"]
    assert data["effective_digest"] != plan.tree_digest


async def test_deploy_refused_when_worker_does_not_fail_closed(
    workspace: Path, connected: SecretStore
):
    # SEC-4: a Worker whose admin gate does NOT fail closed when ADMIN_TOKEN is unset is
    # refused BEFORE any Cloudflare mutation — never publish an unverified admin gate.
    _corrupt_worker(
        workspace,
        lambda s: s.replace("if (!expected) return false", "if (!expected) return true"),
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
    assert reason == RefusalReason.WORKER_AUTH_UNVERIFIED
    # No wrangler step ran (refused pre-mutation).
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_deploy_refused_when_worker_leaks_admin_token(
    workspace: Path, connected: SecretStore
):
    # SEC-4: a Worker that echoes env.ADMIN_TOKEN into a response body (exfiltration) is
    # refused even though its auth structure is otherwise intact.
    _corrupt_worker(
        workspace,
        lambda s: s + "\nconst _leak = JSON.stringify(env.ADMIN_TOKEN);\n",
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
    assert reason == RefusalReason.WORKER_AUTH_UNVERIFIED
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_deploy_proceeds_with_verified_worker_auth(workspace: Path, connected: SecretStore):
    # The canonical generated Worker enforces the admin gate + never leaks the token, so
    # the SEC-4 gate PASSES and the deploy proceeds to publish.
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
    assert any(_is_deploy(c["argv"]) for c in runner.calls)


def test_assert_worker_auth_verified_unit(tmp_path: Path):
    # Missing worker → refused.
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_auth_verified(empty)
    assert exc.value.reason == RefusalReason.WORKER_AUTH_UNVERIFIED
    # The canonical worker passes (no raise).
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    spec = default_lead_gen_app_spec("Acme Leads", recipe)
    tree = generate(spec, recipe.to_design_spec())
    ws = tmp_path / "ok"
    (ws / "worker").mkdir(parents=True)
    (ws / "worker" / "index.ts").write_text(tree["worker/index.ts"], encoding="utf-8")
    cf._assert_worker_auth_verified(ws)  # must not raise


def test_canonical_worker_passes_canonical_gate(tmp_path: Path):
    # The pristine generated worker (regenerated from the staged spec) MATCHES → no raise.
    ws = _staged_tree_with_worker(tmp_path, _canonical_worker_src())
    cf._assert_worker_is_canonical(ws)  # must not raise


def test_form_folded_worker_passes_canonical_gate(tmp_path: Path):
    # The canonical reconstruction goes through generate(app_spec, ...), so folded
    # primitive worker additions are canonical instead of being compared to pre-fold lead_gen.
    folded = _form_folded_app_spec()
    ws = _staged_tree_with_worker(tmp_path, _form_folded_worker_src(), app_spec=folded)
    cf._assert_worker_is_canonical(ws)  # must not raise


def test_form_folded_canonical_gate_refuses_extra_route(tmp_path: Path):
    folded = _form_folded_app_spec()
    malicious = _inject_route(
        _form_folded_worker_src(),
        '    if (url.pathname === "/api/forms/debug") return new Response("nope");\n',
    )
    ws = _staged_tree_with_worker(tmp_path, malicious, app_spec=folded)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.WORKER_NOT_CANONICAL


def test_canonical_gate_tolerates_cosmetic_reformat(tmp_path: Path):
    # A benign re-save (CRLF line endings + trailing whitespace + extra trailing blank
    # lines) is normalized away — still the canonical worker → no raise.
    src = _canonical_worker_src()
    reformatted = src.replace("\n", "  \r\n") + "\r\n\r\n"
    ws = _staged_tree_with_worker(tmp_path, reformatted)
    cf._assert_worker_is_canonical(ws)  # must not raise


def test_canonical_gate_refuses_transitive_admin_token_leak(tmp_path: Path):
    # SEC4-1 bypass: a /debug-token route that transitively leaks env.ADMIN_TOKEN. Defeats
    # the taint heuristic, but is non-canonical → WORKER_NOT_CANONICAL.
    malicious = _inject_route(_canonical_worker_src(), _TRANSITIVE_LEAK_DEBUG_TOKEN)
    ws = _staged_tree_with_worker(tmp_path, malicious)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.WORKER_NOT_CANONICAL


def test_canonical_gate_refuses_startswith_unguarded_lead_read(tmp_path: Path):
    # SEC4-2 bypass: an unauthenticated lead read via url.pathname.startsWith("/debug-leads").
    # Defeats the route enumerator, but is non-canonical → WORKER_NOT_CANONICAL.
    malicious = _inject_route(_canonical_worker_src(), _UNGUARDED_DEBUG_LEADS)
    ws = _staged_tree_with_worker(tmp_path, malicious)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.WORKER_NOT_CANONICAL


def test_canonical_gate_fails_closed_on_missing_spec(tmp_path: Path):
    # Spec missing/unloadable → cannot regenerate the canonical worker → fail CLOSED.
    ws = _staged_tree_with_worker(tmp_path, _canonical_worker_src(), with_spec=False)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.WORKER_NOT_CANONICAL


def test_canonical_gate_fails_closed_on_unloadable_spec(tmp_path: Path):
    # A corrupt (non-JSON) appspec → loader raises → fail CLOSED (refuse), never deploy.
    ws = _staged_tree_with_worker(tmp_path, _canonical_worker_src())
    (ws / ".disco" / "appspec.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.WORKER_NOT_CANONICAL


def test_canonical_gate_fails_closed_on_missing_worker(tmp_path: Path):
    # No worker/index.ts in the staged tree → refuse (the deployed worker must BE the
    # canonical generated one).
    ws = tmp_path / "staged"
    ws.mkdir()
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    save_app_spec(ws, default_lead_gen_app_spec("Acme Leads", recipe))
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.WORKER_NOT_CANONICAL


@pytest.mark.parametrize("bad_main", _NON_CANONICAL_MAINS)
def test_wrangler_allowlist_refuses_non_canonical_main(tmp_path: Path, bad_main: str):
    # Pre-build SEC-5 allowlist gate: a non-canonical `main` value → MAIN_NOT_CANONICAL.
    (tmp_path / "wrangler.toml").write_text(
        f'name = "acme"\nmain = "{bad_main}"\n', encoding="utf-8"
    )
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_wrangler_allowlisted(tmp_path)
    assert exc.value.reason == RefusalReason.MAIN_NOT_CANONICAL


@pytest.mark.parametrize("good_main", ["worker/index.ts", "./worker/index.ts"])
def test_wrangler_allowlist_accepts_canonical_main(tmp_path: Path, good_main: str):
    # The canonical entry (and its `./`-prefixed equivalent) pass the allowlist gate.
    (tmp_path / "wrangler.toml").write_text(
        f'name = "acme"\nmain = "{good_main}"\n', encoding="utf-8"
    )
    cf._assert_wrangler_allowlisted(tmp_path)  # must not raise


@pytest.mark.parametrize("bad_main", _NON_CANONICAL_MAINS)
def test_canonical_gate_ties_main_to_worker_entry(tmp_path: Path, bad_main: str):
    # The post-build canonical gate proves the staged worker/index.ts is pristine, then
    # REQUIRES `main` to name EXACTLY that file. A pristine worker + a redirected `main`
    # (a look-alike/escape) → MAIN_NOT_CANONICAL (the file wrangler runs must BE the file
    # we canonical-checked).
    ws = _staged_tree_with_worker(tmp_path, _canonical_worker_src(), wrangler_main=bad_main)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.MAIN_NOT_CANONICAL


def test_canonical_gate_refuses_lookalike_worker_via_main(tmp_path: Path):
    # The full bypass: a pristine worker/index.ts (passes the canonical source match) sits
    # alongside a MALICIOUS look-alike `....worker/index.ts`, and `main` points at the
    # look-alike. The gate validates worker/index.ts but refuses because `main` does not
    # name it → the look-alike is NEVER deployed.
    ws = _staged_tree_with_worker(
        tmp_path, _canonical_worker_src(), wrangler_main="....worker/index.ts"
    )
    lookalike = ws / "....worker"
    lookalike.mkdir()
    (lookalike / "index.ts").write_text(
        "export default { async fetch() { return new Response('pwned'); } };\n",
        encoding="utf-8",
    )
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_worker_is_canonical(ws)
    assert exc.value.reason == RefusalReason.MAIN_NOT_CANONICAL


async def test_build_emitting_main_redirect_refused_before_wrangler(
    workspace: Path, connected: SecretStore
):
    # BUILD-1 + SEC-1-class: the authored wrangler.toml `main` is canonical (export-ready
    # passes, pre-build allowlist passes), but the build REWRITES `main` to a look-alike
    # `....worker/index.ts` post-build. The post-build canonical gate refuses
    # (MAIN_NOT_CANONICAL) BEFORE any wrangler mutation — the redirect never deploys.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=_WranglerEmittingBuildBackend("....worker/index.ts"),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.MAIN_NOT_CANONICAL
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)  # NO wrangler ran at all


async def test_authored_non_canonical_main_refused_at_export_gate(
    workspace: Path, connected: SecretStore
):
    # End-to-end: a malicious `main = "....worker/index.ts"` in the AUTHORED wrangler.toml
    # makes export-readiness fail, so execute_deploy refuses at GATE 1 (EXPORT_NOT_READY) —
    # wrangler is never invoked. (The deploy-time MAIN_NOT_CANONICAL gates are the
    # defense-in-depth for a post-export redirect; see the build-emit test above.)
    (workspace / "wrangler.toml").write_text(
        (workspace / "wrangler.toml")
        .read_text()
        .replace('main = "worker/index.ts"', 'main = "....worker/index.ts"'),
        encoding="utf-8",
    )
    plan = cf.build_plan(workspace, connected)
    assert not plan.export_ready
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase or "x",
            runner=runner,
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.EXPORT_NOT_READY
    assert runner.calls == []


async def test_build_emitting_transitive_leak_worker_refused_before_wrangler(
    workspace: Path, connected: SecretStore
):
    # SEC4-1 + BUILD-1: pre-build worker is pristine canonical (heuristic gate passes); the
    # build EMITS a worker that transitively leaks env.ADMIN_TOKEN via /debug-token. The
    # post-build canonical-match gate refuses BEFORE any wrangler mutation.
    malicious = _inject_route(_canonical_worker_src(), _TRANSITIVE_LEAK_DEBUG_TOKEN)
    reason, runner = await _refuse_real_deploy_with_emitted_worker(workspace, connected, malicious)
    assert reason == RefusalReason.WORKER_NOT_CANONICAL
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)  # NO wrangler ran at all


async def test_build_emitting_startswith_unguarded_lead_read_refused(
    workspace: Path, connected: SecretStore
):
    # SEC4-2 + BUILD-1: the build emits an unauthenticated lead read via
    # url.pathname.startsWith("/debug-leads"). Non-canonical → refused, no deploy.
    malicious = _inject_route(_canonical_worker_src(), _UNGUARDED_DEBUG_LEADS)
    reason, runner = await _refuse_real_deploy_with_emitted_worker(workspace, connected, malicious)
    assert reason == RefusalReason.WORKER_NOT_CANONICAL
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_build_emitting_arbitrary_worker_refused(workspace: Path, connected: SecretStore):
    # BUILD-1 (core): the sandbox build replaces worker/index.ts with an arbitrary worker
    # (here: auth stripped entirely). The deployed worker != the regenerated canonical
    # worker → WORKER_NOT_CANONICAL, before `wrangler deploy` ever runs.
    pwned = "export default { async fetch() { return new Response('pwned'); } };\n"
    reason, runner = await _refuse_real_deploy_with_emitted_worker(workspace, connected, pwned)
    assert reason == RefusalReason.WORKER_NOT_CANONICAL
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_canonical_worker_build_proceeds_to_deploy(workspace: Path, connected: SecretStore):
    # The honest path: the build does NOT touch the worker, so the staged worker stays the
    # canonical one → the canonical-match gate passes and the deploy proceeds to publish.
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
    assert any(_is_deploy(c["argv"]) for c in runner.calls)


# ---- WAVE 3 SEC-10: ownership-gate an existing same-name Worker overwrite ----


async def test_existing_unrelated_worker_overwrite_refused(workspace: Path, connected: SecretStore):
    # SEC-10: a Worker of this name already exists but NO prior Disco record proves we own
    # it → refuse rather than overwrite an unrelated script. No deploy runs.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner(worker_exists=True)  # exists remotely; no ownership record seeded
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
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_existing_owned_worker_is_adopted(workspace: Path, connected: SecretStore):
    # SEC-10: an existing same-name Worker WITH a prior Disco ownership record is ours —
    # adopt/overwrite it (the deploy proceeds).
    plan = cf.build_plan(workspace, connected)
    _seed_ownership_record(workspace, plan, connected)
    runner = FakeRunner(worker_exists=True)
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
    assert any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_malformed_worker_deployments_list_aborts(workspace: Path, connected: SecretStore):
    # SEC-10/SEC-15: a SUCCESSFUL `deployments list` with a non-array body must ABORT,
    # never fall open to overwriting a possibly-unrelated Worker.
    plan = cf.build_plan(workspace, connected)

    class _BadDeploymentsRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            wr = _wr_sub(argv)
            if wr and wr[:2] == ["deployments", "list"]:
                return CommandResult(0, "{not-an-array}", "")
            return await super().run(argv, cwd=cwd, env=env, stdin=stdin)

    runner = _BadDeploymentsRunner()
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert not result.executed and not result.succeeded
    assert "deployments list" in (result.failed_step or "")
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_worker_preflight_error_fails_closed(workspace: Path, connected: SecretStore):
    # SEC-10-A: an AMBIGUOUS nonzero preflight (no documented not-found) → DeployRefused
    # (WORKER_PREFLIGHT_FAILED). It must NOT proceed as "absent" and must NOT deploy.
    plan = cf.build_plan(workspace, connected)
    runner = _PreflightErrorRunner(
        preflight_stderr="✘ [ERROR] A request to the Cloudflare API failed (10000 auth error)"
    )
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
    assert reason == RefusalReason.WORKER_PREFLIGHT_FAILED
    # Fail closed BEFORE any mutation — no deploy, no d1 create.
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)
    assert not any(_is_d1_create(c["argv"]) for c in runner.calls)


async def test_worker_preflight_documented_not_found_proceeds(
    workspace: Path, connected: SecretStore
):
    # SEC-10-A: a nonzero preflight carrying the DOCUMENTED Cloudflare not-found signal
    # (code 10007 / script_not_found) is the ONE nonzero outcome we trust → proceed as a
    # FRESH deploy (the Worker really does not exist).
    plan = cf.build_plan(workspace, connected)
    runner = _PreflightErrorRunner(
        preflight_stderr="✘ [ERROR] workers.api.error.script_not_found [code: 10007]"
    )
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
    assert any(_is_deploy(c["argv"]) for c in runner.calls)


def test_preflight_signals_worker_absent_unit():
    # Documented not-found markers → absent (trusted); everything else → fail closed.
    for found in (
        "workers.api.error.script_not_found",
        "✘ [ERROR] ... [code: 10007]",
        "the script was not found",
        "Could not find a worker named acme-leads",
    ):
        assert cf._preflight_signals_worker_absent(CommandResult(1, "", found))
    for ambiguous in (
        "authentication error [code: 10000]",
        "ETIMEDOUT connecting to api.cloudflare.com",
        "Unknown arguments: json",  # an old wrangler that doesn't support --json
        "",
    ):
        assert not cf._preflight_signals_worker_absent(CommandResult(1, "", ambiguous))


# ---- WAVE 4 SEC-10-B: ownership proof must be SERVER-SIGNED, not forgeable ----


async def test_planted_unsigned_ownership_record_does_not_authorize_overwrite(
    workspace: Path, connected: SecretStore
):
    # SEC-10-B: the untrusted build agent plants an UNSIGNED record in the digest-skipped
    # `.disco` dir naming the real Worker. It must NOT authorize overwriting the existing
    # (unrelated) Worker — the deploy is refused exactly as if no record existed.
    plan = cf.build_plan(workspace, connected)
    _plant_unsigned_ownership_record(workspace, plan)  # no signature
    runner = FakeRunner(worker_exists=True)
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
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_planted_forged_signature_ownership_record_does_not_authorize_adopt(
    workspace: Path, connected: SecretStore
):
    # SEC-10-B: a planted record with an ARBITRARY forged signature (the agent can't read
    # the server key) fails the HMAC compare → does NOT authorize adopting the existing D1.
    plan = cf.build_plan(workspace, connected)
    _plant_unsigned_ownership_record(workspace, plan, signature="deadbeef" * 8)
    runner = FakeRunner(d1_list_has=plan.db_name)  # DB exists remotely
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


async def test_signed_record_for_other_account_does_not_authorize(
    workspace: Path, connected: SecretStore
):
    # SEC-10-B: a record SIGNED for a DIFFERENT account must not authorize adoption on THIS
    # account — the signature binds account_id, so the account-mismatch is rejected.
    plan = cf.build_plan(workspace, connected)
    # Sign a record (via the real signer) but for a different account_id than the plan's.
    sig = cf._ownership_signature(
        connected,
        account_id="some-other-account-9999",
        worker_name=plan.worker_name,
        db_name=plan.db_name,
        mutations=[f"d1_create:{plan.db_name}", "worker_deploy"],
        create=True,
    )
    d = workspace / ".disco" / "cloudflare" / "deployments"
    d.mkdir(parents=True, exist_ok=True)
    (d / "20260101T000000_000000Z-foreign.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "worker_name": plan.worker_name,
                "db_name": plan.db_name,
                "account_id": "some-other-account-9999",  # NOT this deploy's account
                "mutations": [f"d1_create:{plan.db_name}", "worker_deploy"],
                "signature": sig,
            }
        ),
        encoding="utf-8",
    )
    runner = FakeRunner(worker_exists=True)
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


async def test_signed_ownership_record_authorizes_adopt(workspace: Path, connected: SecretStore):
    # SEC-10-B (positive): a legitimately server-SIGNED record (account + resource +
    # proof mutation) DOES authorize adopting the existing D1 + overwriting the Worker.
    plan = cf.build_plan(workspace, connected)
    _seed_ownership_record(workspace, plan, connected)
    runner = FakeRunner(d1_list_has=plan.db_name, worker_exists=True)
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
    assert any(_is_deploy(c["argv"]) for c in runner.calls)
    assert not any(_is_d1_create(c["argv"]) for c in runner.calls)  # adopted, not created


def test_has_ownership_record_requires_valid_signature_unit(
    workspace: Path, connected: SecretStore
):
    # Unit: a planted unsigned record → not owned; a real signed record → owned (worker+d1).
    plan = cf.build_plan(workspace, connected)
    _plant_unsigned_ownership_record(workspace, plan)
    assert not cf._has_ownership_record(workspace, plan, connected, kind="worker")
    assert not cf._has_ownership_record(workspace, plan, connected, kind="d1")
    _seed_ownership_record(workspace, plan, connected)
    assert cf._has_ownership_record(workspace, plan, connected, kind="worker")
    assert cf._has_ownership_record(workspace, plan, connected, kind="d1")


# ---- WAVE 3 SEC-15/CORR-8: malformed d1 list aborts (no fall-open) ----------


async def test_malformed_d1_list_aborts(workspace: Path, connected: SecretStore):
    plan = cf.build_plan(workspace, connected)

    class _BadD1ListRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            wr = _wr_sub(argv)
            if wr and wr[:2] == ["d1", "list"]:
                return CommandResult(0, '{"object":"not a list"}', "")
            return await super().run(argv, cwd=cwd, env=env, stdin=stdin)

    runner = _BadD1ListRunner()
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert not result.executed and not result.succeeded
    assert "d1 list" in (result.failed_step or "")
    # Never fell open to creating a fresh DB / deploying.
    assert not any(_is_d1_create(c["argv"]) for c in runner.calls)
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


# ---- WAVE 3 SEC-9: Cloudflare-safe resource-name validation -----------------


def test_invalid_worker_name_refused(workspace: Path, connected: SecretStore):
    toml = workspace / "wrangler.toml"
    # The generated worker name is NAMESPACED for collision avoidance (a stable
    # per-app hash suffix), so read it back from a clean plan rather than hardcode
    # the hash — the test tracks the generator and still proves the bad name is refused.
    current = cf.build_plan(workspace, connected).worker_name
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(f'name = "{current}"', 'name = "Bad Name!"'),
        encoding="utf-8",
    )
    with pytest.raises(cf.DeployRefused) as exc:
        cf.build_plan(workspace, connected)
    assert exc.value.reason == RefusalReason.INVALID_RESOURCE_NAME


def test_validate_resource_names_unit():
    # Empty names are deferred to the export-readiness gate (no raise).
    cf._validate_resource_names("", "")
    cf._validate_resource_names("acme-leads", "acme_leads_db")  # valid → no raise
    for bad in ("-leading-hyphen", "has space", "WAY" + "x" * 70, "bad/slash", "bad.dot"):
        with pytest.raises(cf.DeployRefused) as exc:
            cf._validate_resource_names(bad, "ok")
        assert exc.value.reason == RefusalReason.INVALID_RESOURCE_NAME


# ---- WAVE 3 CORR-15: runner unavailable is a distinct refusal ----------------


async def test_runner_unavailable_refused(workspace: Path, connected: SecretStore):
    plan = cf.build_plan(workspace, connected)
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=None,
            build_backend=FakeBuildBackend(),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.RUNNER_UNAVAILABLE


# ---- WAVE 3 SEC-16: exclusive record create + hardlink rejection -------------


def test_write_workspace_file_exclusive_refuses_preexisting(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "rec.json").write_text("PRE", encoding="utf-8")
    with pytest.raises(cf.DeployRefused) as exc:
        cf.write_workspace_file("rec.json", "new", ws.resolve(), exclusive=True)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert (ws / "rec.json").read_text() == "PRE"  # never overwritten


def test_write_workspace_file_refuses_hardlinked_target(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "host-secret.txt"
    outside.write_text("HOST", encoding="utf-8")
    link = ws / "rec.json"
    os.link(outside, link)  # a hardlink aliasing a host file into the workspace
    with pytest.raises(cf.DeployRefused) as exc:
        cf.write_workspace_file("rec.json", "clobber", ws.resolve())
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert outside.read_text() == "HOST"  # the host file was never clobbered


def test_write_workspace_file_retries_short_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    payload = json.dumps({"status": "complete", "detail": "x" * 8192})
    real_write = os.write
    calls = 0

    def short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        chunk = data[: max(1, len(data) // 3)]
        return real_write(fd, chunk)

    monkeypatch.setattr(cf.os, "write", short_write)
    target = cf.write_workspace_file("rec.json", payload, ws.resolve())

    assert calls > 1
    assert target.read_text(encoding="utf-8") == payload
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "complete"


def test_write_workspace_file_publish_failure_preserves_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "rec.json"
    previous = json.dumps({"status": "in_progress", "attempt": 1})
    target.write_text(previous, encoding="utf-8")

    def fail_publish(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("injected crash before atomic publication")

    monkeypatch.setattr(cf.os, "replace", fail_publish)
    with pytest.raises(OSError, match="injected crash"):
        cf.write_workspace_file(
            "rec.json",
            json.dumps({"status": "succeeded", "attempt": 1}),
            ws.resolve(),
        )

    assert target.read_text(encoding="utf-8") == previous
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "in_progress"
    assert not list(ws.glob(".rec.json.*.tmp"))


def test_write_workspace_file_refuses_workspace_root_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-workspace"
    real.mkdir()
    linked = tmp_path / "workspace-link"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(cf.DeployRefused) as caught:
        cf.write_workspace_file("record.json", "payload", linked)

    assert caught.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert not (real / "record.json").exists()


def test_write_workspace_file_refuses_ancestor_swap_during_descriptor_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = tmp_path / "workspace"
    original_disco = ws / ".disco"
    (original_disco / "cloudflare" / "deployments").mkdir(parents=True)
    moved_disco = ws / ".disco-before-swap"
    outside = tmp_path / "outside"
    (outside / "cloudflare" / "deployments").mkdir(parents=True)
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):  # noqa: ANN001, ANN202
        nonlocal swapped
        if path == ".disco" and dir_fd is not None and not swapped:
            original_disco.rename(moved_disco)
            original_disco.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(cf.os, "open", racing_open)
    with pytest.raises(cf.DeployRefused) as caught:
        cf.write_workspace_file(
            ".disco/cloudflare/deployments/receipt.json",
            "payload",
            ws,
        )

    assert swapped
    assert caught.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert not list(outside.rglob("*.json"))
    assert not list(outside.rglob("*.tmp"))


# ---- WAVE 3 CORR-7: effective_digest persisted at the worker_deploy step ------


async def test_effective_digest_recorded_on_partial_failure_after_deploy(
    workspace: Path, connected: SecretStore
):
    # A failure at `secret put` (AFTER the worker deploy) still records the effective
    # digest + worker_deploy mutation, and the failed result exposes attempt_record_path.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner(fail_on="secret put")
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
    assert result.attempt_record_path is not None  # CORR-7: exposed on the failed result
    rec = json.loads(Path(result.attempt_record_path).read_text())
    assert "worker_deploy" in rec["mutations"]
    assert rec["effective_digest"]  # CORR-7: persisted with the worker_deploy step
    assert rec["status"] == "partial_failure"


# ---- WAVE 3 SEC-17/18: deployable-file denylist + secret-shaped value scan ----


async def test_deploy_refused_on_credential_file_in_tree(workspace: Path, connected: SecretStore):
    (workspace / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nx\n", encoding="utf-8")
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
    assert reason == RefusalReason.PLAINTEXT_SECRET_REFUSED
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_deploy_refused_on_secret_shaped_value_in_source(
    workspace: Path, connected: SecretStore
):
    (workspace / "worker" / "config.ts").write_text(
        'export const KEY = "AKIA1234567890ABCDEF";\n', encoding="utf-8"
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
    assert reason == RefusalReason.PLAINTEXT_SECRET_REFUSED
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


def test_assert_no_secret_shaped_files_allows_clean_tree(workspace: Path):
    cf._assert_no_secret_shaped_files(workspace)  # canonical tree → no raise


async def test_post_build_secret_value_rescan_refuses(workspace: Path, connected: SecretStore):
    # SEC-18: the authored tree is clean (passes the pre-build scan), but the build BAKES a
    # secret-shaped value into ./dist. The POST-build rescan must catch it and refuse
    # BEFORE any Cloudflare mutation.
    cf._assert_no_secret_shaped_files(workspace)  # the authored tree is clean
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=_SecretEmittingBuildBackend(emit_value=True),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.PLAINTEXT_SECRET_REFUSED
    # Refused after the (sandboxed) build but BEFORE any Cloudflare/wrangler contact.
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_post_build_credential_file_rescan_refuses(workspace: Path, connected: SecretStore):
    # SEC-17: the build emits a credential-NAMED file (.env.production) into ./dist. The
    # post-build denylist rescan refuses before any mutation.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=_SecretEmittingBuildBackend(emit_file=True),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.PLAINTEXT_SECRET_REFUSED
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


# ---- SEC-17: ALL .env* / .dev.vars* credential-file VARIANTS denied -----------
# The exact-name denylist (``.env``/``.dev.vars``) missed documented multi-segment
# variants (``.env.production.local``, ``.env.prod-1``, ``.dev.vars.production``, …). A
# build emitting one of these into ./dist would otherwise ship it as a PUBLIC static
# asset → token leak. The broadened STEM match must refuse each, with wrangler never run.


@pytest.mark.parametrize(
    "credfile",
    [
        ".env.production.local",
        ".env.prod-1",
        ".dev.vars.production",
        ".env.local",
        ".dev.vars.staging",
    ],
)
async def test_post_build_credential_file_variant_refuses(
    workspace: Path, connected: SecretStore, credfile: str
):
    # SEC-17: the build emits a credential-file VARIANT into ./dist. The broadened
    # post-build denylist rescan must refuse before any Cloudflare mutation.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=_SecretEmittingBuildBackend(emit_file=True, emit_file_name=credfile),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.PLAINTEXT_SECRET_REFUSED
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        ".env.production",
        ".env.production.local",
        ".env.prod-1",
        ".dev.vars",
        ".dev.vars.production",
        ".dev.vars.staging",
        ".npmrc",
        "id_rsa",
        "server.pem",
        "tls.key",
    ],
)
def test_deny_deploy_file_re_matches_credential_names(name: str):
    assert cf._DENY_DEPLOY_FILE_RE.search(name) is not None


@pytest.mark.parametrize(
    "name",
    ["index.html", "style.css", "app.js", "main.tsx", "data.json", "environment.ts", "envoy.yaml"],
)
def test_deny_deploy_file_re_allows_benign_names(name: str):
    assert cf._DENY_DEPLOY_FILE_RE.search(name) is None

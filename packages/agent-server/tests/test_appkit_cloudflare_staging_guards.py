"""AppKit EPIC O -- Cloudflare deploy capability tests (split 3 of 5).

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
    _APP_SECRET as _APP_SECRET,
)
from _appkit_cloudflare_support import (
    _CF_TOKEN as _CF_TOKEN,
)
from _appkit_cloudflare_support import (
    FakeBuildBackend as FakeBuildBackend,
)
from _appkit_cloudflare_support import (
    FakeRunner as FakeRunner,
)
from _appkit_cloudflare_support import (
    _AdminEchoRunner as _AdminEchoRunner,
)
from _appkit_cloudflare_support import (
    _client as _client,
)
from _appkit_cloudflare_support import (
    _ConcurrencyBuildBackend as _ConcurrencyBuildBackend,
)
from _appkit_cloudflare_support import (
    _CredSwapBuildBackend as _CredSwapBuildBackend,
)
from _appkit_cloudflare_support import (
    _FakeRuntime as _FakeRuntime,
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
    _minimal_plan as _minimal_plan,
)
from _appkit_cloudflare_support import (
    _refusal as _refusal,
)
from _appkit_cloudflare_support import (
    _SymlinkEmittingBuildBackend as _SymlinkEmittingBuildBackend,
)
from _appkit_cloudflare_support import (
    _trusted_wrangler as _trusted_wrangler,
)
from _appkit_cloudflare_support import (
    _ws_with_toml as _ws_with_toml,
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
from disco.agent_server.redaction import redact_text
from disco.core.llm.secrets import SecretStore
from disco.tools.projects import ProjectStore


@pytest.mark.parametrize(
    "component", [".disco", ".disco/cloudflare", ".disco/cloudflare/deployments"]
)
def test_write_deploy_record_refuses_symlinked_disco_component(tmp_path: Path, component: str):
    # .disco is digest-SKIPPED (excluded from containment), so a planted symlink at
    # ANY component of .disco/cloudflare/deployments could redirect the record WRITE
    # outside the workspace. The guarded writer LSTATs every component and REFUSES.
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "exfil"  # OUTSIDE the workspace — must stay untouched
    outside.mkdir()

    # Build the parent chain up to (but not including) the symlinked component, then
    # plant that component as a symlink to the outside dir.
    parts = component.split("/")
    parent = ws
    for p in parts[:-1]:
        parent = parent / p
        parent.mkdir()
    (parent / parts[-1]).symlink_to(outside)

    with pytest.raises(cf.DeployRefused) as exc:
        cf._persist_record(
            ws, cf._new_record_rel(), _minimal_plan(ws), status="succeeded", mutations=[]
        )
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    # Nothing was written outside the workspace through the planted symlink.
    assert list(outside.rglob("*")) == []


def test_write_deploy_record_normal_writes_inside_workspace(tmp_path: Path):
    # Companion: a clean workspace writes the record under .disco/cloudflare/deployments.
    ws = tmp_path / "ws"
    ws.mkdir()
    rec_path = cf._persist_record(
        ws,
        cf._new_record_rel(),
        _minimal_plan(ws),
        status="succeeded",
        mutations=["worker_deploy"],
        deployed_url="https://w.workers.dev",
    )
    p = Path(rec_path)
    assert p.exists()
    assert p.is_relative_to(ws / ".disco" / "cloudflare" / "deployments")
    assert json.loads(p.read_text(encoding="utf-8"))["worker_name"] == "w"


def test_no_raw_workspace_reads_outside_guarded_reader():
    # Regression guard: the ONLY raw ``read_text``/``read_bytes`` calls in the
    # package live INSIDE the single guarded reader (read_workspace_file). No
    # per-site read can regress back in and bypass the containment guard.
    import inspect
    import re

    import disco.agent_server.appkit_cloudflare.deploy as deploy_mod
    import disco.agent_server.appkit_cloudflare.sandbox_build as sandbox_mod

    raw_read = re.compile(r"\.(?:read_text|read_bytes)\(")
    reader_src = inspect.getsource(deploy_mod.read_workspace_file)
    reads_in_reader = len(raw_read.findall(reader_src))
    assert reads_in_reader >= 1, "the guarded reader must actually read"

    # deploy.py: every raw read is inside read_workspace_file, nowhere else.
    assert len(raw_read.findall(inspect.getsource(deploy_mod))) == reads_in_reader
    # sandbox_build.py: zero raw workspace reads — the push routes through the reader.
    assert len(raw_read.findall(inspect.getsource(sandbox_mod))) == 0


def test_no_raw_workspace_writes_outside_guarded_writer():
    # Regression guard (WRITE class): the ONLY raw write_text/write_bytes/mkdir calls
    # in the package live INSIDE the guarded writer pair (write_workspace_file +
    # ensure_workspace_dir) — PLUS _stage_deploy_tree, which writes the immutable
    # staged COPY into a fresh temp tree we own (NOT a workspace path, so it needs no
    # per-component workspace guard; its READS still route through the guarded reader) —
    # and _deploy_lock_dir, which mkdir's the SERVER-CONTROLLED cross-process deploy-lock
    # directory (SEC-25), also explicitly NOT a workspace path (it must live OUTSIDE the
    # untrusted workspace) — PLUS _deploy_stage_root, which mkdir's the SERVER-CONTROLLED
    # staging-root directory (the out-of-tree wrangler-config bypass guard), likewise NOT a
    # workspace path. No per-site WORKSPACE write/mkdir can regress back in and bypass
    # containment.
    import inspect
    import re

    import disco.agent_server.appkit_cloudflare.deploy as deploy_mod
    import disco.agent_server.appkit_cloudflare.sandbox_build as sandbox_mod

    raw_write = re.compile(r"\.(?:write_text|write_bytes|mkdir)\(")
    writer_src = (
        inspect.getsource(deploy_mod.write_workspace_file)
        + inspect.getsource(deploy_mod._open_workspace_write_parent)
        + inspect.getsource(deploy_mod.ensure_workspace_dir)
        + inspect.getsource(deploy_mod._stage_deploy_tree)
        + inspect.getsource(deploy_mod._deploy_lock_dir)
        + inspect.getsource(deploy_mod._deploy_stage_root)
    )
    writes_in_writer = len(raw_write.findall(writer_src))
    assert writes_in_writer >= 2, "the guarded writer must actually write + mkdir"

    # deploy.py: every raw write/mkdir is inside the guarded writer pair or the staging
    # copier (which writes to the temp staging tree), nowhere else.
    assert len(raw_write.findall(inspect.getsource(deploy_mod))) == writes_in_writer
    # sandbox_build.py: zero raw workspace writes — sync-back routes through the writer.
    assert len(raw_write.findall(inspect.getsource(sandbox_mod))) == 0


async def test_admin_token_never_in_transcript_or_record(workspace: Path, connected: SecretStore):
    runner = _AdminEchoRunner()
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
    # The fake wrangler DID echo the token (prove the test is exercising the leak).
    secret_call = next(c for c in runner.calls if c["argv"][-1] == "ADMIN_TOKEN")
    assert secret_call["stdin"] == _APP_ADMIN_TOKEN
    # ...yet the literal admin token appears NOWHERE in the transcript...
    joined = "\n".join(result.transcript)
    assert _APP_ADMIN_TOKEN not in joined
    assert _CF_TOKEN not in joined
    # ...nor in the persisted deployment record.
    rec = Path(result.record_path).read_text()
    assert _APP_ADMIN_TOKEN not in rec
    assert _CF_TOKEN not in rec


async def test_admin_token_literal_scrubbed_even_if_echoed_in_a_recorded_step(
    workspace: Path, connected: SecretStore
):
    # Belt-and-suspenders: even on a RECORDED step (here the deploy step echoes the
    # token), the literal admin/CF token value is scrubbed from the stored output.
    class _DeployEchoRunner(FakeRunner):
        async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
            if _is_deploy(argv):
                self.wrangler_toml_at_deploy = (cwd / "wrangler.toml").read_text()
                # A recorded step that leaks BOTH the admin token and the CF token.
                return CommandResult(
                    0,
                    f"Published to https://acme-leads.workers.dev token={_APP_ADMIN_TOKEN} "
                    f"cf={_CF_TOKEN}",
                    "",
                )
            return await super().run(argv, cwd=cwd, env=env, stdin=stdin)

    runner = _DeployEchoRunner()
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
    joined = "\n".join(result.transcript)
    assert _APP_ADMIN_TOKEN not in joined
    assert _CF_TOKEN not in joined
    assert "[REDACTED]" in joined  # the literal scrub fired


# ---- P1: a real deploy REQUIRES an admin_token (deployed admin endpoint protected) --


async def test_real_deploy_requires_admin_token(workspace: Path, connected: SecretStore):
    # Without an admin_token a real deploy is refused (the deployed admin endpoint
    # would be unauthenticated) — fail closed BEFORE the build or any mutation.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    backend = FakeBuildBackend()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=backend,
            admin_token=None,
        )
    )
    assert reason == RefusalReason.ADMIN_TOKEN_REQUIRED
    assert runner.calls == [] and backend.calls == []  # nothing built or mutated
    # A whitespace-only admin_token is likewise refused.
    reason2 = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=FakeRunner(),
            build_backend=FakeBuildBackend(),
            admin_token="   ",
        )
    )
    assert reason2 == RefusalReason.ADMIN_TOKEN_REQUIRED


# ---- redaction P1: the Cloudflare cfut_ token prefix is scrubbed ------------


def test_cfut_token_redacted():
    out = redact_text(f"Authorization: Bearer {_CF_TOKEN}")
    assert _CF_TOKEN not in out
    assert "REDACTED" in out


def test_route_status_and_connect(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch)
    assert client.get("/api/appkit/cloudflare/status").json()["connected"] is False
    res = client.post(
        "/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    assert _CF_TOKEN not in res.text  # token never echoed
    assert client.get("/api/appkit/cloudflare/status").json()["connected"] is True


def test_route_deploy_plan_dry_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, cid = _client(tmp_path, monkeypatch)
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    plan = client.post("/api/appkit/cloudflare/deploy-plan", json={"conversation_id": cid}).json()
    assert plan["export_ready"] and plan["connected"]
    assert plan["confirmation_phrase"]


def test_route_deploy_default_dry_run_then_real(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, ps, cid = _client(
        tmp_path, monkeypatch, runner=runner, build_backend=FakeBuildBackend()
    )
    initial_versions = ps.list_versions(cid)
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    # default dry-run: executes nothing.
    res = client.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid})
    assert res.status_code == 200
    assert res.json()["dry_run"] is True and res.json()["executed"] is False
    assert runner.calls == []
    assert ps.list_versions(cid) == initial_versions
    # real deploy needs the exact phrase.
    phrase = res.json()["plan"]["confirmation_phrase"]
    res2 = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res2.status_code == 200
    assert res2.json()["executed"] is True
    assert res2.json()["succeeded"] is True
    latest = ps.list_versions(cid)[0]
    assert latest.seq > initial_versions[0].seq
    with ps.open_verified_version(cid, latest.seq) as verified:
        records = [entry.path for entry in verified.files if entry.path.endswith(".json")]
        deployment = next(
            path for path in records if path.startswith(".disco/cloudflare/deployments/")
        )
        assert json.loads(verified.read_bytes(deployment))["status"] == "succeeded"


def test_route_refuses_unsealed_mirror_drift_before_runner(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, ps, cid = _client(
        tmp_path,
        monkeypatch,
        runner=runner,
        build_backend=FakeBuildBackend(),
    )
    (ps.path_for(cid) / "schema.sql").write_text("-- unsealed drift", encoding="utf-8")

    response = client.post(
        "/api/appkit/cloudflare/deploy-plan",
        json={"conversation_id": cid},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "workspace_not_committed"
    assert runner.calls == []


def test_route_revalidates_committed_head_at_first_mutation(tmp_path: Path, monkeypatch):
    """Late drift after planning cannot be blessed by the mutation finalizer."""

    class _LateDriftRuntime(_FakeRuntime):
        def __init__(self, ps: ProjectStore) -> None:
            super().__init__(ps)
            self._require_calls = 0

        async def require_committed_host_mirror_locked(self, conversation_id: str):  # noqa: ANN202
            self._require_calls += 1
            if self._require_calls == 3:
                (self._ps.path_for(conversation_id) / "late-drift.txt").write_text(
                    "not part of the committed deploy head",
                    encoding="utf-8",
                )
            return await super().require_committed_host_mirror_locked(conversation_id)

    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, ps, cid = _client(
        tmp_path,
        monkeypatch,
        runner=runner,
        build_backend=FakeBuildBackend(),
        runtime_factory=_LateDriftRuntime,
    )
    initial_versions = ps.list_versions(cid)
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    phrase = client.post(
        "/api/appkit/cloudflare/deploy-plan", json={"conversation_id": cid}
    ).json()["confirmation_phrase"]

    response = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "workspace_not_committed"
    assert ps.list_versions(cid) == initial_versions
    assert not (ps.path_for(cid) / ".disco" / "cloudflare" / "deployments").exists()


def test_route_deploy_refusal_no_confirmation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, cid = _client(tmp_path, monkeypatch, runner=FakeRunner())
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    res = client.post(
        "/api/appkit/cloudflare/deploy", json={"conversation_id": cid, "dry_run": False}
    )
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == RefusalReason.CONFIRMATION_REQUIRED.value


def test_route_deploy_refused_without_isolating_sandbox(tmp_path: Path, monkeypatch):
    # P0-1: with NO injected build backend, the route builds the production sandbox
    # backend from the runtime. The test runtime has no isolating sandbox → a real
    # deploy is refused (BUILD_NOT_SANDBOXED), never run unsandboxed.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, _ps, cid = _client(tmp_path, monkeypatch, runner=runner)  # build_backend=None
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    dry = client.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid}).json()
    phrase = dry["plan"]["confirmation_phrase"]
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={"conversation_id": cid, "dry_run": False, "confirmation": phrase},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == RefusalReason.BUILD_NOT_SANDBOXED.value
    assert runner.calls == []  # nothing ran on the host


def test_route_deploy_refusal_autonomous_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_AUTONOMOUS", "1")
    client, _ps, cid = _client(tmp_path, monkeypatch, runner=FakeRunner())
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={"conversation_id": cid, "dry_run": False, "confirmation": "whatever"},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == RefusalReason.AUTONOMOUS.value


# ---- P0-3: the HTTP layer is authoritative (owner auth + CORS + no phrase leak) --


def test_route_requires_owner_auth(tmp_path: Path, monkeypatch):
    # NO owner token header → 401 on EVERY route, and nothing is executed.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, _ps, cid = _client(tmp_path, monkeypatch, runner=runner, owner_auth=False)
    for method, path, body in [
        ("get", "/api/appkit/cloudflare/status", None),
        ("post", "/api/appkit/cloudflare/connect", {"token": _CF_TOKEN, "account_id": _ACCOUNT}),
        ("post", "/api/appkit/cloudflare/deploy-plan", {"conversation_id": cid}),
        (
            "post",
            "/api/appkit/cloudflare/deploy",
            {"conversation_id": cid, "dry_run": False, "confirmation": "x"},
        ),
    ]:
        res = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        assert res.status_code == 401, f"{path} should require owner auth"
        assert res.json()["detail"]["reason"] == "owner_auth_required"
    assert runner.calls == []  # the unauthenticated deploy never reached the runner


def test_route_deploy_plan_phrase_not_leaked_unauthenticated(tmp_path: Path, monkeypatch):
    # /deploy-plan must NOT serve the confirmation phrase to an unauthenticated caller.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    anon, _ps, cid = _client(tmp_path, monkeypatch, owner_auth=False)
    res = anon.post("/api/appkit/cloudflare/deploy-plan", json={"conversation_id": cid})
    assert res.status_code == 401
    assert "confirmation_phrase" not in res.text
    assert "DEPLOY " not in res.text  # the phrase shape never appears


def test_route_deploy_cors_not_wildcard(tmp_path: Path, monkeypatch):
    # P0-3: even though the global CORS is wildcard, the deploy surface must NOT
    # return Access-Control-Allow-Origin: * (no unauthorized cross-origin reads).
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch, with_cors=True)
    res = client.get("/api/appkit/cloudflare/status", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") != "*"
    assert "access-control-allow-origin" not in res.headers  # evil origin not allowlisted


def test_route_deploy_cors_reflects_allowlisted_origin(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_OWNER_ORIGIN", "https://owner.example, https://ops.example")
    client, _ps, _cid = _client(tmp_path, monkeypatch, with_cors=True)
    res = client.get("/api/appkit/cloudflare/status", headers={"Origin": "https://owner.example"})
    assert res.headers.get("access-control-allow-origin") == "https://owner.example"


# ---- P1-4: the REAL runner is wired by default (owner path is functional) -----


def test_route_default_runner_is_real_and_functional(tmp_path: Path, monkeypatch):
    # With NO injected runner the router defaults to the REAL SubprocessCommandRunner
    # (not a dead None stub). Patch that default to a fake to prove the owner path
    # actually drives a runner end-to-end without shelling out.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    import disco.agent_server.appkit_cloudflare.routes as cfr

    fake = FakeRunner()
    monkeypatch.setattr(cfr, "SubprocessCommandRunner", lambda: fake)
    # runner=None → real default (patched above). A fake isolating build backend is
    # injected so the build-sandbox gate passes; we're proving the RUNNER default here.
    client, _ps, cid = _client(tmp_path, monkeypatch, build_backend=FakeBuildBackend())
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    dry = client.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid}).json()
    assert fake.calls == []  # dry-run still zero-side-effect
    phrase = dry["plan"]["confirmation_phrase"]
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res.status_code == 200 and res.json()["executed"] is True
    assert any(_is_wrangler(c["argv"]) for c in fake.calls)  # the real path drove the runner


# ---- P1: the route threads admin_token → `wrangler secret put ADMIN_TOKEN` ----


def test_route_real_deploy_threads_admin_token_runs_secret_put(tmp_path: Path, monkeypatch):
    # The owner request's admin_token must reach `wrangler secret put ADMIN_TOKEN`
    # (via stdin, never argv/response) so the deployed admin endpoint is protected.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, _ps, cid = _client(
        tmp_path, monkeypatch, runner=runner, build_backend=FakeBuildBackend()
    )
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    dry = client.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid}).json()
    phrase = dry["plan"]["confirmation_phrase"]
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res.status_code == 200 and res.json()["executed"] is True
    secret_call = next(c for c in runner.calls if c["argv"][-1] == "ADMIN_TOKEN")
    assert secret_call["stdin"] == _APP_ADMIN_TOKEN  # value rides on stdin
    assert _APP_ADMIN_TOKEN not in " ".join(secret_call["argv"])  # never argv
    assert _APP_ADMIN_TOKEN not in res.text  # never echoed in the response


def test_route_real_deploy_refused_without_admin_token(tmp_path: Path, monkeypatch):
    # A route-driven real deploy WITHOUT admin_token fails closed (409) and runs
    # nothing on the host — the deployed admin endpoint must never be unprotected.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, _ps, cid = _client(
        tmp_path, monkeypatch, runner=runner, build_backend=FakeBuildBackend()
    )
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    dry = client.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid}).json()
    phrase = dry["plan"]["confirmation_phrase"]
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={"conversation_id": cid, "dry_run": False, "confirmation": phrase},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == RefusalReason.ADMIN_TOKEN_REQUIRED.value
    assert runner.calls == []  # nothing mutated


async def test_deploy_refused_on_symlinked_file_in_dist(
    workspace: Path, connected: SecretStore, tmp_path: Path
):
    # A symlinked FILE that the build emits into ./dist (regenerated after the plan
    # digest, so the digest never saw it) → REFUSE before wrangler deploy runs.
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text("TOP-SECRET-HOST-CREDENTIAL", encoding="utf-8")
    backend = _SymlinkEmittingBuildBackend(link_rel="dist/leak.txt", link_target=host_secret)

    runner = FakeRunner()
    plan = cf.build_plan(workspace, connected)  # clean tree (no dist yet) → plan ok
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=backend,
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    # wrangler deploy must NEVER have been invoked (the public upload never fired).
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)
    # The host secret was never read through the link by us.
    assert host_secret.read_text() == "TOP-SECRET-HOST-CREDENTIAL"


async def test_deploy_refused_on_symlinked_subdir_in_dist(
    workspace: Path, connected: SecretStore, tmp_path: Path
):
    # A symlinked SUBDIRECTORY under ./dist pointing OUTSIDE the workspace → REFUSE
    # before wrangler deploy. os.walk(followlinks=False) would SILENTLY SKIP this
    # subtree (the exact gap the digest had); the scan DETECTS it via lstat and FAILS.
    escape = tmp_path / "escape_dir"  # OUTSIDE the workspace
    escape.mkdir()
    (escape / "secret.js").write_text("HOST-SECRET", encoding="utf-8")
    (workspace / "dist").mkdir(exist_ok=True)
    (workspace / "dist" / "assets").symlink_to(escape)  # symlinked dir → outside

    runner = FakeRunner()
    plan = cf.build_plan(workspace, connected)
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
    assert reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    # wrangler deploy must NEVER have been invoked.
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)
    assert (escape / "secret.js").read_text() == "HOST-SECRET"  # untouched


async def test_deploy_refused_on_symlinked_dist_root(
    workspace: Path, connected: SecretStore, tmp_path: Path
):
    # The dist ROOT itself is a symlink escaping the workspace → REFUSE before
    # wrangler deploy (the per-component lstat catches a symlinked dist root).
    escape = tmp_path / "escape_root"  # OUTSIDE the workspace
    escape.mkdir()
    (escape / "index.html").write_text("HOST", encoding="utf-8")
    # Plant dist as a symlink BEFORE the build; FakeBuildBackend.build does
    # mkdir(exist_ok=True) which is a no-op on an existing symlink-to-dir, then
    # writes index.html through it — but the deploy-time scan refuses it first.
    (workspace / "dist").symlink_to(escape)

    runner = FakeRunner()
    plan = cf.build_plan(workspace, connected)
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
    assert reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)


async def test_clean_dist_reaches_wrangler_deploy(workspace: Path, connected: SecretStore):
    # The companion: a symlink-FREE dist (the FakeBuildBackend writes a plain
    # dist/index.html) passes the scan and reaches `wrangler deploy` — the guard only
    # refuses symlinks, never a normal regular file/dir in the upload tree.
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
    assert any(_is_deploy(c["argv"]) for c in runner.calls)


def test_assert_deploy_tree_symlink_free_unit(tmp_path: Path):
    # Direct unit test of the scan: a clean dist passes; a symlinked file and a
    # symlinked subdir each raise DeployRefused(WORKSPACE_SYMLINK_ESCAPE).
    ws = tmp_path / "ws"
    (ws / "dist" / "assets").mkdir(parents=True)
    (ws / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (ws / "dist" / "assets" / "app.js").write_text("1", encoding="utf-8")
    # Clean: returns the absolute dist dir.
    out = cf.assert_deploy_tree_symlink_free(ws, "dist")
    assert out == (ws / "dist").resolve()

    outside = tmp_path / "outside.txt"
    outside.write_text("X", encoding="utf-8")
    (ws / "dist" / "assets" / "link.js").symlink_to(outside)
    with pytest.raises(cf.DeployRefused) as exc:
        cf.assert_deploy_tree_symlink_free(ws, "dist")
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE


# ============================================================================
# Epic O — Cluster A (codex deploy-security keystones)
# ============================================================================

# ---- A1: immutable staged copy — rejects symlink/hardlink, deploy uses the copy --


def test_stage_rejects_symlinked_file(tmp_path: Path):
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text("HOST", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    (ws / "leak.txt").symlink_to(host_secret)  # symlinked FILE in the deploy tree
    with pytest.raises(cf.DeployRefused) as exc:
        cf._stage_deploy_tree(ws)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE
    assert host_secret.read_text() == "HOST"  # never copied/read through the link


def test_stage_rejects_symlinked_dir(tmp_path: Path):
    escape = tmp_path / "escape"
    escape.mkdir()
    (escape / "x.js").write_text("HOST", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    (ws / "assets").symlink_to(escape)  # symlinked DIRECTORY
    with pytest.raises(cf.DeployRefused) as exc:
        cf._stage_deploy_tree(ws)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE


def test_stage_rejects_symlinked_root(tmp_path: Path):
    real = tmp_path / "real_ws"
    real.mkdir()
    (real / "package.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "ws"
    link.symlink_to(real)  # the workspace root itself is a symlink
    with pytest.raises(cf.DeployRefused) as exc:
        cf._stage_deploy_tree(link)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE


def test_stage_rejects_hardlink(tmp_path: Path):
    host_secret = tmp_path / "host_secret.txt"
    host_secret.write_text("HOST", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    # A HARDLINK (multi-link regular file) aliasing a host file outside the workspace.
    os.link(host_secret, ws / "hl.txt")
    with pytest.raises(cf.DeployRefused) as exc:
        cf._stage_deploy_tree(ws)
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE


def test_stage_copies_regular_files_symlink_free(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    (ws / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")
    # Skipped subtrees must NOT be copied.
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "junk").write_text("x", encoding="utf-8")
    staged = cf._stage_deploy_tree(ws)
    try:
        assert staged != ws and staged.is_dir()
        assert (staged / "package.json").read_text() == '{"name":"app"}'
        assert (staged / "src" / "App.tsx").read_text() == "export default 1\n"
        assert not (staged / "node_modules").exists()  # regenerated subtree excluded
        # The staged tree is symlink-free by construction.
        for root, dirs, names in os.walk(staged):
            for entry in (*dirs, *names):
                assert not (Path(root) / entry).is_symlink()
    finally:
        import shutil

        shutil.rmtree(staged, ignore_errors=True)


async def test_deploy_runs_from_staged_copy_not_live_tree(workspace: Path, connected: SecretStore):
    # The real deploy must build + run wrangler from the IMMUTABLE staged COPY, never
    # the live mutable workspace (defeats check-then-use TOCTOU).
    runner = FakeRunner()
    backend = FakeBuildBackend()
    plan = cf.build_plan(workspace, connected)
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=backend,
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert result.executed and result.succeeded
    # The build backend received the STAGED path, not the live workspace.
    assert backend.calls[0]["workspace"] != str(workspace)
    assert "disco-deploy-stage-" in backend.calls[0]["workspace"]
    # Every wrangler call ran with cwd in the staged copy, NOT the live workspace.
    for c in runner.calls:
        if _is_wrangler(c["argv"]):
            assert c["cwd"] != str(workspace)
            assert "disco-deploy-stage-" in c["cwd"]
    # The build wrote ./dist into the STAGED copy — the live workspace stays clean.
    assert not (workspace / "dist").exists()
    # The non-secret deploy record DID persist to the LIVE workspace.
    assert Path(result.record_path).is_relative_to(workspace / ".disco")


async def test_concurrent_deploys_serialize_on_lock(workspace: Path, connected: SecretStore):
    backend = _ConcurrencyBuildBackend()
    plan = cf.build_plan(workspace, connected)
    phrase = plan.confirmation_phrase

    async def _one():
        return await cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=phrase,
            runner=FakeRunner(),
            build_backend=backend,
            admin_token=_APP_ADMIN_TOKEN,
        )

    r1, r2 = await asyncio.gather(_one(), _one())
    assert r1.executed and r2.executed
    # The lock kept the two deploys from ever building the same workspace at once.
    assert backend.max_active == 1


# ---- A2: trusted wrangler binary (NEVER workspace node_modules/.bin) ----------


def test_resolve_trusted_wrangler_prefers_pinned_abs_path(tmp_path: Path, monkeypatch):
    pinned = tmp_path / "trusted" / "wrangler"
    pinned.parent.mkdir()
    pinned.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DISCO_WRANGLER_BIN", str(pinned))
    ws = tmp_path / "ws"
    ws.mkdir()
    assert cf._resolve_trusted_wrangler(ws) == str(pinned)


def test_resolve_trusted_wrangler_rejects_pinned_inside_workspace(tmp_path: Path, monkeypatch):
    # WAVE 2 (SEC-3): a DISCO_WRANGLER_BIN INSIDE the untrusted workspace is rejected, and
    # with no other trusted absolute wrangler resolvable the deploy FAILS CLOSED (no bare
    # `wrangler` fallback that the workspace could shadow).
    ws = tmp_path / "ws"
    (ws / "node_modules" / ".bin").mkdir(parents=True)
    evil = ws / "node_modules" / ".bin" / "wrangler"
    evil.write_text("#!/bin/sh\necho pwned\n", encoding="utf-8")
    monkeypatch.setenv("DISCO_WRANGLER_BIN", str(evil))
    monkeypatch.setattr(cf.shutil, "which", lambda _name, path=None: None)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._resolve_trusted_wrangler(ws)
    assert exc.value.reason == RefusalReason.WRANGLER_NOT_TRUSTED


def test_resolve_trusted_wrangler_rejects_which_inside_workspace(tmp_path: Path, monkeypatch):
    # WAVE 2 (SEC-3): a `which` that resolves INSIDE the workspace is rejected and, with
    # nothing trusted left, the resolution FAILS CLOSED (never the workspace binary).
    ws = tmp_path / "ws"
    (ws / "node_modules" / ".bin").mkdir(parents=True)
    evil = ws / "node_modules" / ".bin" / "wrangler"
    evil.write_text("x", encoding="utf-8")
    monkeypatch.delenv("DISCO_WRANGLER_BIN", raising=False)
    monkeypatch.setattr(cf.shutil, "which", lambda _name, path=None: str(evil))
    with pytest.raises(cf.DeployRefused) as exc:
        cf._resolve_trusted_wrangler(ws)
    assert exc.value.reason == RefusalReason.WRANGLER_NOT_TRUSTED


async def test_deploy_uses_pinned_trusted_wrangler_never_npx_or_workspace(
    workspace: Path, connected: SecretStore, tmp_path: Path, monkeypatch
):
    # Plant a malicious wrangler in the workspace node_modules/.bin; it must NEVER be
    # used. With DISCO_WRANGLER_BIN pinned, every wrangler argv[0] is the trusted path.
    (workspace / "node_modules" / ".bin").mkdir(parents=True)
    evil = workspace / "node_modules" / ".bin" / "wrangler"
    evil.write_text("#!/bin/sh\ncat ~/.cloudflare-creds\n", encoding="utf-8")
    pinned = tmp_path / "trusted_bin" / "wrangler"
    pinned.parent.mkdir()
    pinned.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DISCO_WRANGLER_BIN", str(pinned))

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
    wrangler_calls = [c for c in runner.calls if _is_wrangler(c["argv"])]
    assert wrangler_calls
    for c in runner.calls:
        assert "npx" not in c["argv"]  # never the npx-resolved invocation
        if _is_wrangler(c["argv"]):
            assert c["argv"][0] == str(pinned)  # the TRUSTED pinned binary
            assert str(evil) not in c["argv"]  # never the planted workspace binary


def test_asset_dir_rejects_absolute_and_traversal(tmp_path: Path):
    for bad in ("/etc", "../x", "../../x", "./../x"):
        ws = _ws_with_toml(tmp_path / bad.replace("/", "_"), bad)
        with pytest.raises(cf.DeployRefused) as exc:
            cf._deploy_asset_dir_rel(ws)
        assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE


def test_asset_dir_exact_resolution(tmp_path: Path):
    # "./build" normalises to "build". WAVE 2: "." (the workspace root) is REFUSED outright
    # (it would publish source/stale files) — see test_root_asset_dir_refused. SEC-17 (P1):
    # ".git" (a _TREE_SKIP_DIRS root) is now ALSO refused — see
    # test_asset_dir_under_skip_root_refused.
    with pytest.raises(cf.DeployRefused) as exc:
        cf._deploy_asset_dir_rel(_ws_with_toml(tmp_path / "a", "."))
    assert exc.value.reason == RefusalReason.ASSET_DIR_UNSAFE
    assert cf._deploy_asset_dir_rel(_ws_with_toml(tmp_path / "c", "./build")) == "build"
    assert cf._deploy_asset_dir_rel(_ws_with_toml(tmp_path / "d", "public/static")) == (
        "public/static"
    )


@pytest.mark.parametrize(
    "served_dir",
    [
        ".disco/public",
        "node_modules/public",
        ".git/x",
        ".wrangler/x",
        ".disco",  # the skip root itself, not just nested under it
        "node_modules",
        "build/.disco/assets",  # a skip root as an INTERIOR component, any depth
    ],
)
def test_asset_dir_under_skip_root_refused(tmp_path: Path, served_dir: str):
    # SEC-17 (P1): the served [assets].directory must not BE — or be NESTED UNDER — an
    # internal/regenerated root the deploy-tree scan globally skips (_TREE_SKIP_DIRS:
    # .disco/node_modules/.git/.wrangler). A served dir buried there hides the PUBLISHED
    # bytes from _assert_no_secret_shaped_files (which walks via the skip-pruning
    # _iter_tree_files), so wrangler could publish an unscanned secret as a PUBLIC asset.
    ws = _ws_with_toml(tmp_path / served_dir.replace("/", "_"), served_dir)
    with pytest.raises(cf.DeployRefused) as exc:
        cf._deploy_asset_dir_rel(ws)
    assert exc.value.reason == RefusalReason.ASSET_DIR_UNSAFE


def test_scanner_targets_resolved_assets_dir(tmp_path: Path):
    # The pre-deploy symlink scan must run on the EXACT resolved [assets] tree wrangler
    # uploads — here "public", not the default "dist". A symlink under public → refuse.
    ws = tmp_path / "ws"
    (ws / "public").mkdir(parents=True)
    (ws / "public" / "index.html").write_text("<html></html>", encoding="utf-8")
    out = cf.assert_deploy_tree_symlink_free(ws, "public")
    assert out == (ws / "public").resolve()
    outside = tmp_path / "outside.txt"
    outside.write_text("X", encoding="utf-8")
    (ws / "public" / "leak.html").symlink_to(outside)
    with pytest.raises(cf.DeployRefused) as exc:
        cf.assert_deploy_tree_symlink_free(ws, "public")
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE


def test_scanner_rejects_missing_assets_dir(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(cf.DeployRefused) as exc:
        cf.assert_deploy_tree_symlink_free(ws, "nonexistent")
    assert exc.value.reason == RefusalReason.WORKSPACE_SYMLINK_ESCAPE


# ---- A4: plaintext-secret scan (.env + wrangler.toml [vars]) -----------------


def test_assert_no_plaintext_secrets_env_file(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("API_TOKEN=sk-live-deadbeef\nPUBLIC_NAME=Acme\n", encoding="utf-8")
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_no_plaintext_secrets(ws)
    assert exc.value.reason == RefusalReason.PLAINTEXT_SECRET_REFUSED


def test_assert_no_plaintext_secrets_vars_table(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "wrangler.toml").write_text(
        'name = "w"\n[vars]\nADMIN_SECRET = "hunter2"\n', encoding="utf-8"
    )
    with pytest.raises(cf.DeployRefused) as exc:
        cf._assert_no_plaintext_secrets(ws)
    assert exc.value.reason == RefusalReason.PLAINTEXT_SECRET_REFUSED


def test_assert_no_plaintext_secrets_allows_nonsecret(tmp_path: Path):
    # Non-secret-named vars + .env entries do NOT trip the scan.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("PUBLIC_NAME=Acme\nFEATURE_FLAG=1\n", encoding="utf-8")
    (ws / "wrangler.toml").write_text(
        'name = "w"\n[vars]\nSITE_NAME = "Acme Leads"\n', encoding="utf-8"
    )
    cf._assert_no_plaintext_secrets(ws)  # no raise


async def test_deploy_refused_on_env_plaintext_secret(workspace: Path, connected: SecretStore):
    # A .env carrying a plaintext secret in the deploy tree → real deploy refused
    # (the secret would ship). Written BEFORE the plan so the phrase matches the tree.
    (workspace / ".env").write_text("API_TOKEN=sk-live-xyz\n", encoding="utf-8")
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
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)  # never deployed


async def test_deploy_refused_on_credential_swap_mid_deploy(
    workspace: Path, connected: SecretStore
):
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    backend = _CredSwapBuildBackend(
        connected, new_token="cfut_attackerToken999", new_account="attacker-account-000"
    )
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=backend,
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.NO_CONNECTED_ACCOUNT
    # Refused after the build, BEFORE any wrangler mutation reached the new account.
    assert not any(_is_deploy(c["argv"]) for c in runner.calls)
    assert not any(_is_d1_create(c["argv"]) for c in runner.calls)

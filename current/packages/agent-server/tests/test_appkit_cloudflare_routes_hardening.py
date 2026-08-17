"""AppKit EPIC O -- Cloudflare deploy capability tests (split 5 of 5).

Split from the original test_appkit_cloudflare.py during its PY-0360
module-size decomposition (pure mechanical split, no test logic changed).
Shared fixtures/fakes/constants live in _appkit_cloudflare_support.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
from pathlib import Path

import httpx
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
    _OWNER_HEADERS as _OWNER_HEADERS,
)
from _appkit_cloudflare_support import (
    _OWNER_TOKEN as _OWNER_TOKEN,
)
from _appkit_cloudflare_support import (
    AccountScopedVerifier as AccountScopedVerifier,
)
from _appkit_cloudflare_support import (
    FakeBuildBackend as FakeBuildBackend,
)
from _appkit_cloudflare_support import (
    FakeRunner as FakeRunner,
)
from _appkit_cloudflare_support import (
    FakeVerifier as FakeVerifier,
)
from _appkit_cloudflare_support import (
    _build_app as _build_app,
)
from _appkit_cloudflare_support import (
    _client as _client,
)
from _appkit_cloudflare_support import (
    _connect as _connect,
)
from _appkit_cloudflare_support import (
    _FakeVerifyResp as _FakeVerifyResp,
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
    _patch_verify_httpx as _patch_verify_httpx,
)
from _appkit_cloudflare_support import (
    _phrase_for as _phrase_for,
)
from _appkit_cloudflare_support import (
    _refusal as _refusal,
)
from _appkit_cloudflare_support import (
    _SecretEmittingBuildBackend as _SecretEmittingBuildBackend,
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
from disco.agent_server.appkit_cloudflare.models import DeployRefused, RefusalReason
from disco.agent_server.appkit_cloudflare.routes import (
    _AUTH_FAIL_LIMIT,
    _scrub_paths,
    _scrub_text,
    _validate_account_id,
)
from disco.agent_server.appkit_cloudflare.wrangler import HttpTokenVerifier, SubprocessCommandRunner
from disco.core.llm.secrets import SecretStore
from fastapi import HTTPException


@pytest.mark.parametrize(
    "tmpl", [".dev.vars.example", ".env.example", ".env.sample", ".env.dist", ".dev.vars.template"]
)
def test_assert_no_secret_shaped_files_allows_template_placeholders(tmp_path: Path, tmpl: str):
    # Conventional NON-secret placeholder templates (the canonical app ships a ROOT
    # .dev.vars.example) are EXEMPT — they must not trip the broadened stem denylist — but
    # ONLY OUTSIDE the served assets dir. Pin a wrangler.toml (served dir = ./dist) and place
    # the placeholder at the ROOT (outside dist), exactly like the canonical app.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "wrangler.toml").write_text(
        'name = "w"\n[assets]\ndirectory = "./dist"\n', encoding="utf-8"
    )
    (ws / "dist").mkdir()
    (ws / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (ws / tmpl).write_text("ADMIN_TOKEN=replace-me\n", encoding="utf-8")  # placeholder, ROOT
    cf._assert_no_secret_shaped_files(ws)  # no raise — outside served dir + no real value


# ---- SEC-17 (P1): close the dist/secrets.env.example template-suffix public leak --------
# The exemption that lets the canonical ROOT .dev.vars.example deploy was too broad: a
# credential-named *.example INSIDE the served assets dir was exempted by NAME and never
# content-scanned, so dist/secrets.env.example carrying a real sk-…/cfut_… token uploaded as
# a PUBLIC asset. Two independent closures: (1) deny ANY credential-named file in the served
# tree regardless of a template suffix; (2) content-scan exempted templates everywhere.


@pytest.mark.parametrize(
    "served_name",
    ["secrets.env.example", ".env.example", ".dev.vars.sample", ".env.dist", ".dev.vars.template"],
)
def test_credential_named_template_in_served_dir_refused(tmp_path: Path, served_name: str):
    # SEC-17 closure (1): a credential-named TEMPLATE inside the served assets dir is denied
    # OUTRIGHT — even with PLACEHOLDER content — because it would publish as a PUBLIC asset.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "wrangler.toml").write_text(
        'name = "w"\n[assets]\ndirectory = "./dist"\n', encoding="utf-8"
    )
    (ws / "dist").mkdir()
    (ws / "dist" / served_name).write_text("ADMIN_TOKEN=changeme\n", encoding="utf-8")
    with pytest.raises(DeployRefused):
        cf._assert_no_secret_shaped_files(ws)


# ---- SEC-17 (P1): close the .disco/public served-dir secret-leak bypass -----------------
# The secret-shaped/denylist scan walks via _iter_tree_files, which GLOBALLY skips the
# internal/regenerated roots (_TREE_SKIP_DIRS: .disco/node_modules/.git/.wrangler). But the
# wrangler [assets].directory (the SERVED tree) could be set UNDER one of those roots — e.g.
# directory = ".disco/public" — so a build emitting .disco/public/leak.js with a reassembled
# sk-…/cfut_… token would be published by Cloudflare yet never scanned. Two closures:
# (1) _deploy_asset_dir_rel REFUSES a served dir nested under a skip root; (2) the scan walks
# the served tree UNCONDITIONALLY (defense in depth).


@pytest.mark.parametrize("served_dir", [".disco/public", "node_modules/public", ".wrangler/x"])
def test_assert_no_secret_shaped_files_refuses_skip_root_served_dir(
    tmp_path: Path, served_dir: str
):
    # SEC-17 closure (1), via the scan entry point: _assert_no_secret_shaped_files resolves
    # the served dir through _deploy_asset_dir_rel, which now REFUSES a skip-root-nested dir.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "wrangler.toml").write_text(
        f'name = "w"\n[assets]\ndirectory = "{served_dir}"\n', encoding="utf-8"
    )
    with pytest.raises(DeployRefused) as exc:
        cf._assert_no_secret_shaped_files(ws)
    assert exc.value.reason == RefusalReason.ASSET_DIR_UNSAFE


def test_served_tree_scanned_unconditionally_even_under_skip_root(tmp_path: Path, monkeypatch):
    # SEC-17 closure (2), defense in depth: even if some path resolved a served dir UNDER a
    # normally-skipped root (here forced past closure (1) by monkeypatching the resolver),
    # the scan walks the served tree DIRECTLY — so a token in .disco/public/leak.js (a path
    # _iter_tree_files prunes) is STILL caught and the deploy refused.
    ws = tmp_path / "ws"
    (ws / ".disco" / "public").mkdir(parents=True)
    (ws / ".disco" / "public" / "leak.js").write_text(
        'var k="sk-' + "z" * 40 + '";export default k;\n', encoding="utf-8"
    )
    # Force the served dir under the skipped .disco root (bypassing closure (1)) to prove the
    # walk is unconditional, not reliant on the refusal.
    monkeypatch.setattr(cf, "_deploy_asset_dir_rel", lambda _ws: ".disco/public")
    # Sanity: the skip-pruning walker never sees the leak (proving the bypass is real).
    assert not any(p.name == "leak.js" for p in cf._iter_tree_files(ws))
    with pytest.raises(DeployRefused):
        cf._assert_no_secret_shaped_files(ws)


async def test_deploy_refused_on_disco_public_assets_dir(workspace: Path, connected: SecretStore):
    # SEC-17 (the codex bypass, end-to-end): the wrangler [assets].directory pointed at
    # .disco/public → execute_deploy refuses at the asset-dir gate (ASSET_DIR_UNSAFE) BEFORE
    # the build runs and BEFORE wrangler is ever invoked. Surgically swap ONLY the served-dir
    # line (keeping the rest of the canonical, export-ready toml), then build the plan AFTER
    # so the confirmation phrase matches the digest of the served-dir config.
    toml_path = workspace / "wrangler.toml"
    original = toml_path.read_text(encoding="utf-8")
    assert 'directory = "./dist"' in original
    toml_path.write_text(
        original.replace('directory = "./dist"', 'directory = ".disco/public"'),
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
            build_backend=_SecretEmittingBuildBackend(
                emit_file=True,
                emit_file_name="leak.js",
                emit_file_body='var k="cfut_' + "y" * 32 + '";\n',
            ),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.ASSET_DIR_UNSAFE
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


@pytest.mark.parametrize(
    "body",
    [
        "OPENAI_API_KEY=sk-" + "a" * 40 + "\n",
        "CLOUDFLARE_API_TOKEN=cfut_" + "b" * 32 + "\n",
    ],
)
def test_root_template_with_real_token_content_scanned(tmp_path: Path, body: str):
    # SEC-17 closure (2): a template OUTSIDE the served dir is exempt from the NAME denylist
    # but STILL content-scanned — a real sk-…/cfut_… token in a ROOT .dev.vars.example is
    # caught (a genuine placeholder would pass).
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "wrangler.toml").write_text(
        'name = "w"\n[assets]\ndirectory = "./dist"\n', encoding="utf-8"
    )
    (ws / "dist").mkdir()
    (ws / ".dev.vars.example").write_text(body, encoding="utf-8")
    with pytest.raises(DeployRefused):
        cf._assert_no_secret_shaped_files(ws)


@pytest.mark.parametrize(
    "body",
    [
        "OPENAI_API_KEY=sk-" + "c" * 40 + "\n",
        "CLOUDFLARE_API_TOKEN=cfut_" + "d" * 32 + "\n",
        "API_TOKEN=changeme\n",  # placeholder — denied purely on the served-tree NAME rule
    ],
)
async def test_deploy_refused_on_template_credential_file_in_served_dir(
    workspace: Path, connected: SecretStore, body: str
):
    # SEC-17 (the codex bypass, end-to-end): a build that emits dist/secrets.env.example
    # (a credential-named template in the served dir) → DeployRefused, wrangler NEVER run —
    # whether it holds a real sk-…/cfut_… token or a mere placeholder.
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=_SecretEmittingBuildBackend(
                emit_file=True, emit_file_name="secrets.env.example", emit_file_body=body
            ),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    assert reason == RefusalReason.PLAINTEXT_SECRET_REFUSED
    assert not any(_is_wrangler(c["argv"]) for c in runner.calls)


async def test_canonical_root_dev_vars_example_still_deploys(
    workspace: Path, connected: SecretStore
):
    # Canonical app: a ROOT .dev.vars.example placeholder (outside the served dir) + a clean
    # dist still deploys — the scoped exemption must not regress the legitimate case.
    assert (workspace / ".dev.vars.example").exists()  # the canonical generator ships it
    runner = FakeRunner()
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=cf.build_plan(workspace, connected).confirmation_phrase,
        runner=runner,
        build_backend=FakeBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert result.executed and result.succeeded
    assert any(_is_wrangler(c["argv"]) for c in runner.calls)


@pytest.mark.parametrize(
    "credfile",
    [".env.production.local", ".env.prod-1", ".dev.vars.production", ".dev.vars.staging"],
)
def test_assert_no_secret_shaped_files_refuses_real_variants(tmp_path: Path, credfile: str):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / credfile).write_text("API_TOKEN=supersecret\n", encoding="utf-8")
    with pytest.raises(DeployRefused):
        cf._assert_no_secret_shaped_files(ws)


async def test_post_build_benign_files_still_deploy(workspace: Path, connected: SecretStore):
    # A build emitting only benign assets (index.html / style.css) deploys cleanly — the
    # broadened denylist must not over-match legitimate static assets.
    class _BenignBuildBackend(FakeBuildBackend):
        async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
            res = await super().build(
                workspace, install_cmd=install_cmd, build_cmd=build_cmd, asset_dir=asset_dir
            )
            out = workspace / asset_dir
            (out / "index.html").write_text("<!doctype html><title>ok</title>\n", encoding="utf-8")
            (out / "style.css").write_text("body{margin:0}\n", encoding="utf-8")
            return res

    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    result = await cf.execute_deploy(
        workspace,
        connected,
        dry_run=False,
        confirmation=plan.confirmation_phrase,
        runner=runner,
        build_backend=_BenignBuildBackend(),
        admin_token=_APP_ADMIN_TOKEN,
    )
    assert result.executed
    assert any(_is_wrangler(c["argv"]) for c in runner.calls)


# ---- A6: deploy env minimization (SEC-32) -----------------------------------


def test_deploy_env_drops_dangerous_vars(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("npm_config_registry", "https://evil.example")
    monkeypatch.setenv("npm_config_cache", "/tmp/evil-cache")
    monkeypatch.setenv("HOME", "/home/victim")
    ws = tmp_path / "ws"
    (ws / "node_modules" / ".bin").mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{ws / 'node_modules' / '.bin'}:/usr/bin")
    home = tmp_path / "throwaway_home"
    home.mkdir()
    env = cf._deploy_env("tok", "acct", home=str(home), unsafe_roots=(ws,))
    assert "NODE_OPTIONS" not in env
    assert "npm_config_registry" not in env
    assert "npm_config_cache" not in env
    assert env["HOME"] == str(home)  # throwaway, NOT /home/victim
    assert env["CLOUDFLARE_API_TOKEN"] == "tok"
    assert env["CLOUDFLARE_ACCOUNT_ID"] == "acct"
    # PATH had its workspace node_modules/.bin entry stripped.
    assert str(ws / "node_modules" / ".bin") not in env.get("PATH", "")
    assert "/usr/bin" in env["PATH"]


async def test_real_deploy_env_is_minimized(workspace: Path, connected: SecretStore, monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("npm_config_registry", "https://evil.example")
    monkeypatch.setenv("HOME", "/home/victim")
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
    wcall = next(c for c in runner.calls if _is_wrangler(c["argv"]))
    assert "NODE_OPTIONS" not in wcall["env"]
    assert "npm_config_registry" not in wcall["env"]
    assert wcall["env"].get("HOME") != "/home/victim"  # throwaway home
    assert wcall["env"].get("CLOUDFLARE_API_TOKEN") == _CF_TOKEN


# ---- SEC-25: two concurrent deploys for one conversation — second gets 409 ----


async def test_concurrent_deploy_for_same_conversation_second_gets_409(tmp_path, monkeypatch):

    import httpx

    monkeypatch.setenv("DISCO_ADMIN_TOKEN", _OWNER_TOKEN)
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))

    started = asyncio.Event()
    gate = asyncio.Event()

    class _BlockingBackend(FakeBuildBackend):
        async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
            # Hold the in-flight slot: signal we're inside the deploy, then block.
            started.set()
            await gate.wait()
            return await super().build(workspace, install_cmd=install_cmd, build_cmd=build_cmd)

    runner = FakeRunner()
    app, _ps, cid = _build_app(tmp_path, runner=runner, build_backend=_BlockingBackend())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=_OWNER_HEADERS
    ) as ac:
        await ac.post(
            "/api/appkit/cloudflare/connect",
            json={"token": _CF_TOKEN, "account_id": _ACCOUNT},
        )
        dry = (await ac.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid})).json()
        phrase = dry["plan"]["confirmation_phrase"]
        body = {
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": _APP_ADMIN_TOKEN,
        }
        # First real deploy enters and BLOCKS inside the (sandboxed) build, holding
        # the per-conversation slot.
        task1 = asyncio.create_task(ac.post("/api/appkit/cloudflare/deploy", json=body))
        await asyncio.wait_for(started.wait(), timeout=5)
        # Second deploy for the SAME conversation, while the first is in flight → 409.
        res2 = await ac.post("/api/appkit/cloudflare/deploy", json=body)
        assert res2.status_code == 409
        assert res2.json()["detail"]["reason"] == "deploy_in_progress"
        # Release the first; it completes successfully.
        gate.set()
        res1 = await task1
        assert res1.status_code == 200 and res1.json()["executed"] is True
        # The slot was released → a fresh deploy is admitted again (new phrase).
        phrase2 = (
            await ac.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid})
        ).json()["plan"]["confirmation_phrase"]
        res3 = await ac.post(
            "/api/appkit/cloudflare/deploy",
            json={
                "conversation_id": cid,
                "dry_run": False,
                "confirmation": phrase2,
                "admin_token": _APP_ADMIN_TOKEN,
            },
        )
        assert res3.status_code == 200 and res3.json()["executed"] is True


# ---- CORR-25: accurate HTTP status mapping ----------------------------------


def test_route_deploy_aborted_step_is_not_200(tmp_path, monkeypatch):
    # A real deploy whose migration step FAILS must NOT return 200 — it is a 502
    # carrying the failed step + detail (a partial failure, distinct from a refusal).
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner(fail_on="d1 execute")  # migration fails mid-deploy
    client, ps, cid = _client(
        tmp_path, monkeypatch, runner=runner, build_backend=FakeBuildBackend()
    )
    _connect(client)
    phrase = _phrase_for(client, cid)
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert detail["reason"] == "deploy_failed"
    assert "migrate" in (detail["failed_step"] or "")
    # The publish step never ran.
    assert not any(c["argv"][:3] == ["npx", "wrangler", "deploy"] for c in runner.calls)
    latest = ps.list_versions(cid)[0]
    with ps.open_verified_version(cid, latest.seq) as verified:
        deployment = next(
            entry.path
            for entry in verified.files
            if entry.path.startswith(".disco/cloudflare/deployments/")
        )
        assert json.loads(verified.read_bytes(deployment))["status"] == "partial_failure"


def test_route_deploy_missing_workspace_is_404(tmp_path, monkeypatch):
    # A well-formed conversation id whose workspace was never created → 404
    # NO_WORKSPACE (distinct from a bad-input 400 and a refused-gate 409).
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch, runner=FakeRunner())
    other = "22222222-2222-2222-2222-222222222222"  # never created on disk
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": other,
            "dry_run": False,
            "confirmation": "x",
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == RefusalReason.NO_WORKSPACE.value


def test_route_deploy_bad_conversation_id_is_400(tmp_path, monkeypatch):
    # A poisoned conversation id → 400 bad input (not 404, not 500).
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch, runner=FakeRunner())
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={"conversation_id": "../../etc/passwd", "dry_run": False, "confirmation": "x"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "bad_conversation_id"


# ---- SEC-27: admin_token is a bounded secret input (over-long rejected, never echoed) --


def test_route_deploy_overlong_admin_token_is_400_and_not_echoed(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, _ps, cid = _client(
        tmp_path, monkeypatch, runner=runner, build_backend=FakeBuildBackend()
    )
    _connect(client)
    phrase = _phrase_for(client, cid)
    huge = "z" * 9000  # over the bounded length
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": huge,
        },
    )
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "invalid_input"
    # The over-long secret value is NEVER echoed back in the error response.
    assert huge not in res.text
    assert runner.calls == []  # nothing ran on the host


# ---- CORR-26: a preexisting deploy ACAO reflecting an arbitrary origin is stripped --


def test_route_deploy_cors_strips_reflected_arbitrary_origin(tmp_path, monkeypatch):
    # A global CORS layer that REFLECTS an arbitrary Origin (not just wildcard) must
    # still NOT leak through on the deploy surface — the strict middleware strips it.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch, cors="reflect")
    res = client.get("/api/appkit/cloudflare/status", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") != "https://evil.example"
    assert "access-control-allow-origin" not in res.headers  # arbitrary origin stripped
    assert "access-control-allow-credentials" not in res.headers


def test_route_deploy_cors_reflect_allows_only_allowlisted_origin(tmp_path, monkeypatch):
    # With the SAME reflecting global CORS, an allowlisted origin DOES survive.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_OWNER_ORIGIN", "https://owner.example")
    client, _ps, _cid = _client(tmp_path, monkeypatch, cors="reflect")
    res = client.get("/api/appkit/cloudflare/status", headers={"Origin": "https://owner.example"})
    assert res.headers.get("access-control-allow-origin") == "https://owner.example"


# ---- SEC-28: owner-auth failures are rate-limited + the token is never logged ----


def test_owner_auth_failures_rate_limited_and_token_not_logged(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch, owner_auth=False)
    bad = "WRONG-OWNER-TOKEN-supersecret-guess-XYZ"
    saw_429 = False
    statuses: list[int] = []
    with caplog.at_level(logging.WARNING, logger="disco.agent_server.appkit_cloudflare.routes"):
        for _ in range(20):
            res = client.get(
                "/api/appkit/cloudflare/status",
                headers={"X-Disco-Owner-Token": bad},
            )
            statuses.append(res.status_code)
            if res.status_code == 429:
                saw_429 = True
                break
    # Repeated bad owner tokens are eventually throttled (429), after a bounded
    # number of 401s — not unlimited online guessing.
    assert saw_429
    assert statuses[0] == 401
    assert statuses.count(401) <= _AUTH_FAIL_LIMIT
    # The presented token value NEVER appears in any log line (audit without secrets).
    assert bad not in caplog.text
    # ...but the auth FAILURE was logged (audit trail exists).
    assert "owner-auth" in caplog.text.lower()


def test_owner_auth_success_clears_failure_tally(tmp_path, monkeypatch):
    # A few failures followed by a SUCCESS resets the tally — a legitimate operator
    # who eventually authenticates is never throttled.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch, owner_auth=False)
    for _ in range(_AUTH_FAIL_LIMIT - 1):
        assert (
            client.get(
                "/api/appkit/cloudflare/status", headers={"X-Disco-Owner-Token": "nope"}
            ).status_code
            == 401
        )
    # A correct token now succeeds and clears the tally.
    ok = client.get("/api/appkit/cloudflare/status", headers=_OWNER_HEADERS)
    assert ok.status_code == 200
    # Subsequent failures start counting from zero again (not instantly throttled).
    assert (
        client.get(
            "/api/appkit/cloudflare/status", headers={"X-Disco-Owner-Token": "nope"}
        ).status_code
        == 401
    )


# ---- SEC-26: responses carry NO absolute host paths -------------------------


def test_route_deploy_response_has_no_absolute_host_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    runner = FakeRunner()
    client, _ps, cid = _client(
        tmp_path, monkeypatch, runner=runner, build_backend=FakeBuildBackend()
    )
    _connect(client)
    phrase = _phrase_for(client, cid)
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": phrase,
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res.status_code == 200
    j = res.json()
    assert j["executed"] is True
    # No absolute host path (the tmp_path root) leaks anywhere in the response body.
    assert str(tmp_path) not in res.text
    # The deployment record is a workspace-RELATIVE path, not an absolute host path.
    assert j["record_path"] is not None
    assert not Path(j["record_path"]).is_absolute()
    assert j["record_path"].startswith(".disco/")
    # The plan references the conversation by its opaque id, not the host workspace path.
    assert "workspace" not in j["plan"]
    assert j["plan"]["conversation_id"] == cid


def test_route_deploy_plan_has_no_absolute_workspace_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, cid = _client(tmp_path, monkeypatch)
    _connect(client)
    res = client.post("/api/appkit/cloudflare/deploy-plan", json={"conversation_id": cid})
    assert res.status_code == 200
    assert str(tmp_path) not in res.text
    assert "workspace" not in res.json()
    assert res.json()["conversation_id"] == cid


# ---- CORR-27: /connect VERIFIES the token before marking the account connected ----


def test_connect_refuses_unverified_token_and_does_not_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(
        tmp_path, monkeypatch, verifier=FakeVerifier(ok=False, detail="Cloudflare rejected it")
    )
    res = _connect(client)
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "token_verification_failed"
    assert _CF_TOKEN not in res.text  # the rejected token is never echoed
    # The account stays DISCONNECTED — an unverified token was never stored.
    assert client.get("/api/appkit/cloudflare/status").json()["connected"] is False


def test_connect_verifies_then_stores_on_success(tmp_path, monkeypatch):
    # The companion: with a passing verifier, connect stores + marks connected, and
    # the verifier was actually consulted (verify-before-store really ran).
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    verifier = FakeVerifier(ok=True)
    client, _ps, _cid = _client(tmp_path, monkeypatch, verifier=verifier)
    res = _connect(client)
    assert res.status_code == 200 and res.json()["connected"] is True
    assert verifier.calls == [_CF_TOKEN]  # the verifier saw the token before storing
    assert client.get("/api/appkit/cloudflare/status").json()["connected"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "   ",  # whitespace only
        "ab",  # too short
        "x" * 100,  # too long (unbounded)
        "../../etc/passwd",  # path traversal
        "/etc/secret",  # absolute path
        "has space",  # whitespace inside
        "a/b/c",  # path separators
        "acct\ninject",  # control char / header injection
    ],
)
def test_connect_rejects_invalid_account_id_and_does_not_store(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    verifier = FakeVerifier(ok=True)
    client, _ps, _cid = _client(tmp_path, monkeypatch, verifier=verifier)
    res = client.post(
        "/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": bad}
    )
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "invalid_account_id"
    # A malformed account id is rejected BEFORE the token is verified or stored.
    assert verifier.calls == []
    assert _CF_TOKEN not in res.text  # the token is never echoed
    assert client.get("/api/appkit/cloudflare/status").json()["connected"] is False


def test_connect_accepts_realistic_cloudflare_account_id(tmp_path, monkeypatch):
    # A real Cloudflare account id (32 lowercase hex) is accepted + stored.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    real_id = "0123456789abcdef0123456789abcdef"
    client, _ps, _cid = _client(tmp_path, monkeypatch, verifier=FakeVerifier(ok=True))
    res = _connect(client, account=real_id)
    assert res.status_code == 200 and res.json()["connected"] is True
    assert res.json()["account_id"] == real_id


def test_connect_refuses_token_not_scoped_to_account(tmp_path, monkeypatch):
    # SEC-29: the token verifies as active, but is NOT scoped to the requested
    # account → the account-bound check refuses, and nothing is stored/connected.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    verifier = AccountScopedVerifier(account="differentaccount99")  # scoped elsewhere
    client, _ps, _cid = _client(tmp_path, monkeypatch, verifier=verifier)
    res = _connect(client)  # connects _ACCOUNT, which the token is NOT scoped for
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "token_verification_failed"
    # The account-bound check was consulted with the REQUESTED account.
    assert verifier.scoped_calls == [(_CF_TOKEN, _ACCOUNT)]
    assert _CF_TOKEN not in res.text
    assert client.get("/api/appkit/cloudflare/status").json()["connected"] is False


def test_connect_binds_verify_to_account_when_supported(tmp_path, monkeypatch):
    # The companion: a token scoped to the requested account connects, and the route
    # used the account-BOUND check (not just the account-agnostic verify).
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    verifier = AccountScopedVerifier(account=_ACCOUNT)
    client, _ps, _cid = _client(tmp_path, monkeypatch, verifier=verifier)
    res = _connect(client)
    assert res.status_code == 200 and res.json()["connected"] is True
    assert verifier.scoped_calls == [(_CF_TOKEN, _ACCOUNT)]
    assert client.get("/api/appkit/cloudflare/status").json()["account_id"] == _ACCOUNT


# ---- SEC-29 (remainder): surface / require account-scoped verification ----


def test_connect_agnostic_verifier_surfaces_account_scoped_false(tmp_path, monkeypatch):
    # The bundled (account-agnostic) verifier proves the token is ACTIVE but NOT that
    # it is bound to this account. The connect must SURFACE that — account_scoped:false
    # — rather than silently presenting it as account-bound.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(tmp_path, monkeypatch, verifier=FakeVerifier(ok=True))
    res = _connect(client)
    assert res.status_code == 200 and res.json()["connected"] is True
    assert res.json()["account_scoped"] is False  # weaker guarantee is surfaced, not hidden


def test_connect_scoped_verifier_surfaces_account_scoped_true(tmp_path, monkeypatch):
    # The companion: an account-scoped verifier yields account_scoped:true.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, _cid = _client(
        tmp_path, monkeypatch, verifier=AccountScopedVerifier(account=_ACCOUNT)
    )
    res = _connect(client)
    assert res.status_code == 200 and res.json()["connected"] is True
    assert res.json()["account_scoped"] is True


def test_connect_refuses_when_account_scope_required_but_only_agnostic(tmp_path, monkeypatch):
    # SEC-29 (remainder): with the operator policy set, an account-agnostic-only verify
    # is REFUSED (the token cannot be proven bound to this account) and nothing is stored.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_REQUIRE_ACCOUNT_SCOPED_VERIFY", "1")
    client, _ps, _cid = _client(tmp_path, monkeypatch, verifier=FakeVerifier(ok=True))
    res = _connect(client)
    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "account_scope_unverifiable"
    assert _CF_TOKEN not in res.text
    # Fail closed: the agnostically-verified token was NOT stored / connected.
    assert client.get("/api/appkit/cloudflare/status").json()["connected"] is False


def test_connect_scoped_verifier_satisfies_required_policy(tmp_path, monkeypatch):
    # Under the same strict policy an account-SCOPED verifier still connects.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_REQUIRE_ACCOUNT_SCOPED_VERIFY", "1")
    client, _ps, _cid = _client(
        tmp_path, monkeypatch, verifier=AccountScopedVerifier(account=_ACCOUNT)
    )
    res = _connect(client)
    assert res.status_code == 200 and res.json()["connected"] is True
    assert res.json()["account_scoped"] is True


def test_validate_account_id_unit():
    # The cleaned, stripped id is returned for a valid value.
    assert _validate_account_id("  abc123account456  ") == "abc123account456"
    assert _validate_account_id("0123456789abcdef0123456789abcdef").startswith("0123")
    for bad in ("", "  ", "ab", "x" * 100, "../etc/passwd", "a b", "a/b"):
        with pytest.raises(HTTPException) as exc:
            _validate_account_id(bad)
        assert exc.value.status_code == 400
        assert exc.value.detail["reason"] == "invalid_account_id"


# ---- SEC-26 (boundary): refusal/error messages carry NO absolute host path ----


def test_scrub_paths_unit():
    # An absolute host path is replaced with an opaque marker; a relative path
    # (a workspace-relative record path) is left intact; None/empty pass through.
    assert _scrub_paths("read /home/dylan/projects/x/secrets.json failed") == ("read <path> failed")
    assert _scrub_paths(".disco/cloudflare/deployments/rec.json") == (
        ".disco/cloudflare/deployments/rec.json"
    )
    assert _scrub_paths("escape at /var/lib/disco/host/projects/abc/workspace now") == (
        "escape at <path> now"
    )
    assert _scrub_paths(None) is None
    assert _scrub_paths("") == ""


def test_scrub_text_unit():
    # SEC-26 (remainder): the free-text scrub strips BOTH absolute host paths AND the
    # internal workspace-relative ``.disco/...`` structure (which _scrub_paths leaves).
    assert _scrub_text("read /home/dylan/projects/x/secrets.json failed") == "read <path> failed"
    assert _scrub_text("wrote .disco/cloudflare/deployments/rec.json ok") == "wrote <path> ok"
    assert _scrub_text("both /var/lib/disco/x and .disco/cloudflare/state.json") == (
        "both <path> and <path>"
    )
    # A bare token containing "disco" but no internal-path structure is left alone.
    assert _scrub_text("disconnected from cloudflare") == "disconnected from cloudflare"
    assert _scrub_text(None) is None
    assert _scrub_text("") == ""


def test_route_deploy_success_scrubs_internal_paths_in_freetext_fields(tmp_path, monkeypatch):
    # SEC-26 (remainder): on the SUCCESS path the free-text result fields (transcript,
    # failed_step/error_detail, plan.export_detail) must NOT leak host paths or the
    # internal ``.disco/...`` workspace layout — only the dedicated record_path field is
    # a deliberate workspace-relative reference.
    from disco.agent_server.appkit_cloudflare.models import DeployExecutionResult, DeployPlan

    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, ps, cid = _client(tmp_path, monkeypatch)
    _connect(client)
    ws = ps.path_for(cid)
    host_leak = "/home/dylan/secret/projects/abc/workspace"
    internal_leak = ".disco/cloudflare/deployments/internal-state.json"
    plan = DeployPlan(
        workspace=host_leak,
        account_id=_ACCOUNT,
        worker_name="acme-leads",
        db_name="acme-leads-db",
        spec_digest="d",
        tree_digest="t",
        plan_hash="h",
        export_ready=True,
        export_detail=f"export ready; evidence at {internal_leak} and {host_leak}/dist",
        connected=True,
        steps=[],
        confirmation_phrase="phrase",
    )
    result = DeployExecutionResult(
        executed=True,
        dry_run=False,
        plan=plan,
        deployed_url="https://acme-leads.workers.dev",
        # The dedicated record path IS a workspace-relative reference (kept as-is).
        record_path=str(ws / ".disco" / "cloudflare" / "deployments" / "rec.json"),
        transcript=[
            "$ wrangler deploy",
            f"wrote {internal_leak}",
            f"host path {host_leak}/node_modules",
        ],
        succeeded=True,
        failed_step=None,
        error_detail=None,
    )

    async def _fake(*_a, **_k):
        return result

    monkeypatch.setattr(cf, "execute_deploy", _fake)
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": "phrase",
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res.status_code == 200
    j = res.json()
    assert j["executed"] is True
    # No absolute host path leaks anywhere.
    assert host_leak not in res.text
    assert "/home/dylan" not in res.text
    # The internal ``.disco/...`` layout is scrubbed out of the FREE-TEXT fields.
    joined_transcript = "\n".join(j["transcript"])
    assert internal_leak not in joined_transcript
    assert "<path>" in joined_transcript
    assert internal_leak not in j["plan"]["export_detail"]
    assert "<path>" in j["plan"]["export_detail"]
    # ...but the dedicated record_path stays the deliberate workspace-relative handle.
    assert j["record_path"] == ".disco/cloudflare/deployments/rec.json"


def test_route_deploy_refusal_message_scrubs_absolute_host_path(tmp_path, monkeypatch):
    # Defense in depth: even if an inner layer put an absolute host path in a
    # DeployRefused detail, the route boundary scrubs it before the client sees it.
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, cid = _client(
        tmp_path, monkeypatch, runner=FakeRunner(), build_backend=FakeBuildBackend()
    )
    _connect(client)
    leak = "/var/lib/disco/secrets/host/projects/abc/workspace"

    async def _raise(*_a, **_k):
        raise cf.DeployRefused(RefusalReason.NO_WORKSPACE, f"missing workspace at {leak}")

    monkeypatch.setattr(cf, "execute_deploy", _raise)
    res = client.post(
        "/api/appkit/cloudflare/deploy",
        json={
            "conversation_id": cid,
            "dry_run": False,
            "confirmation": "x",
            "admin_token": _APP_ADMIN_TOKEN,
        },
    )
    assert res.status_code == 404
    detail = res.json()["detail"]
    assert detail["reason"] == RefusalReason.NO_WORKSPACE.value
    assert leak not in res.text  # the absolute host path never reaches the client
    assert "/var/lib/disco" not in res.text
    assert "<path>" in detail["message"]


def test_route_deploy_plan_refusal_message_scrubs_absolute_host_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", _APP_SECRET)
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    client, _ps, cid = _client(tmp_path, monkeypatch)
    _connect(client)
    leak = "/etc/disco/host/secret/projects/xyz/workspace"

    def _raise(*_a, **_k):
        raise cf.DeployRefused(RefusalReason.WORKSPACE_SYMLINK_ESCAPE, f"escape via {leak}")

    monkeypatch.setattr(cf, "build_plan", _raise)
    res = client.post("/api/appkit/cloudflare/deploy-plan", json={"conversation_id": cid})
    assert res.status_code == 409  # symlink-escape is a 409 (not a missing-workspace 404)
    detail = res.json()["detail"]
    assert detail["reason"] == RefusalReason.WORKSPACE_SYMLINK_ESCAPE.value
    assert leak not in res.text
    assert "<path>" in detail["message"]


# ---- Epic O Cluster D: host subprocess hardening (SEC-31/CORR-23) -------------
# The PRODUCTION SubprocessCommandRunner bounds every host wrangler/npm call so a
# hung/missing/leaky binary can't wedge or exhaust a deploy request. Exercised
# with REAL short-lived python subprocesses (no real wrangler / no network).


async def test_subprocess_runner_missing_binary_is_structured_error_not_raise(tmp_path: Path):
    # A missing wrangler/npm binary must yield a STRUCTURED non-zero result (the
    # executor aborts on it), NOT a raised exception that becomes a 500.

    runner = SubprocessCommandRunner()
    res = await runner.run(
        ["disco-definitely-not-a-real-binary-xyz123"],
        cwd=tmp_path,
        env=dict(os.environ),
    )
    assert not res.ok and res.returncode != 0
    assert "disco-definitely-not-a-real-binary-xyz123" in res.stderr
    assert "failed to launch" in res.stderr.lower()


async def test_subprocess_runner_normal_output_uncapped_and_stdin_works(tmp_path: Path):
    # Companion: a well-behaved step round-trips stdin and returns its (uncapped)
    # output verbatim — the hardening only bounds the pathological cases.
    import sys

    runner = SubprocessCommandRunner()
    res = await runner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        cwd=tmp_path,
        env=dict(os.environ),
        stdin="hello-from-stdin",
    )
    assert res.ok
    assert res.stdout == "HELLO-FROM-STDIN"
    assert "truncated" not in res.stdout


async def test_subprocess_runner_caps_oversized_output(tmp_path: Path):
    # A step that emits a huge stdout torrent is CAPPED (memory + transcript bound),
    # while the process still completes successfully.
    import sys

    runner = SubprocessCommandRunner(max_output_bytes=1000)
    res = await runner.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('A' * 5_000_000)"],
        cwd=tmp_path,
        env=dict(os.environ),
    )
    assert res.ok  # the process itself succeeded
    # Far below the 5 MB emitted — capped near the 1000-byte bound + marker.
    assert len(res.stdout.encode("utf-8")) < 5000
    assert "truncated" in res.stdout
    assert res.stdout.count("A") <= 1000


async def test_subprocess_runner_kills_hung_step_at_timeout(tmp_path: Path):
    # A wrangler step that hangs (here a 30s sleep) must be KILLED at the per-step
    # timeout and return a structured failure — never wedge the deploy indefinitely.
    import sys
    import time

    runner = SubprocessCommandRunner(timeout_s=0.5)
    start = time.monotonic()
    res = await runner.run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=dict(os.environ),
    )
    elapsed = time.monotonic() - start
    assert not res.ok  # structured non-zero, not a hang/raise
    assert elapsed < 10, "the hung step was not killed at the timeout"
    assert "timeout" in res.stderr.lower()


async def test_token_verifier_uses_no_env_proxy_trust(monkeypatch):
    captured = _patch_verify_httpx(
        monkeypatch, response=_FakeVerifyResp(200, {"result": {"status": "active"}})
    )
    ok, detail = await HttpTokenVerifier().verify("cfut_sometoken")
    assert ok and "active" in detail
    # SEC-33: the client is built WITHOUT env trust → a hostile HTTP_PROXY can't
    # intercept the ad-hoc token verify.
    assert captured.get("trust_env") is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ProxyError("proxy refused"),
        httpx.ConnectError("tls handshake failed"),
        httpx.RemoteProtocolError("malformed response"),
        httpx.ReadTimeout("slow"),
        ssl.SSLError("bad cert"),
    ],
)
async def test_token_verifier_transport_errors_are_structured_failures(monkeypatch, exc):
    # Proxy / TLS / protocol / timeout / raw-ssl errors → a structured verify
    # failure, NEVER a raised 500.
    _patch_verify_httpx(monkeypatch, get_exc=exc)
    ok, detail = await HttpTokenVerifier().verify("cfut_sometoken")
    assert not ok
    assert "Cloudflare" in detail


@pytest.mark.parametrize("body", [[1, 2, 3], None, "a-string", 42])
async def test_token_verifier_non_object_json_body_is_structured_failure(monkeypatch, body):
    # A non-object top-level JSON body (array/null/string/number) must NOT crash
    # the verifier (no AttributeError → 500) — it's a structured failure.
    _patch_verify_httpx(monkeypatch, response=_FakeVerifyResp(200, body))
    ok, detail = await HttpTokenVerifier().verify("cfut_sometoken")
    assert not ok and detail


async def test_token_verifier_non_dict_result_field_is_structured_failure(monkeypatch):
    # ``result`` present but not an object (a malformed/hostile body) must also not
    # crash — handled as inactive/structured failure.
    _patch_verify_httpx(monkeypatch, response=_FakeVerifyResp(200, {"result": "not-an-object"}))
    ok, detail = await HttpTokenVerifier().verify("cfut_sometoken")
    assert not ok and "expected 'active'" in detail


async def test_token_verifier_non_json_body_is_structured_failure(monkeypatch):
    _patch_verify_httpx(monkeypatch, response=_FakeVerifyResp(200, ValueError("not json")))
    ok, detail = await HttpTokenVerifier().verify("cfut_sometoken")
    assert not ok and "non-JSON" in detail

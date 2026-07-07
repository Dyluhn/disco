"""AppKit EPIC O — Cloudflare deploy capability: gates, dry-run, credentials.

NO real network / no real wrangler / no real Cloudflare account. A FAKE command
runner records the calls a real deploy WOULD make; the four hard gates are each
asserted to BLOCK a real deploy; the dry-run path is proven side-effect-free; the
API token is proven to live only as ciphertext and never in argv / transcript /
record. A real live deploy is the owner's documented action (OWNER_GUIDE), never
exercised here.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from disco.agent_server.appkit_cloudflare import deploy as cf
from disco.agent_server.appkit_cloudflare.models import DeployRefused, RefusalReason
from disco.agent_server.appkit_cloudflare.routes import (
    _AUTH_FAIL_LIMIT,
    _scrub_paths,
    _scrub_text,
    _validate_account_id,
    make_cloudflare_router,
)
from disco.agent_server.appkit_cloudflare.wrangler import (
    BuildResult,
    CommandResult,
    HttpTokenVerifier,
    SubprocessCommandRunner,
)
from disco.agent_server.redaction import redact_text
from disco.core.appkit import (
    default_lead_gen_app_spec,
    generate,
    get_recipe,
    save_app_spec,
)
from disco.core.appkit.form_primitive import FormSpec, apply_form_spec
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import ProjectStore
from fastapi import FastAPI, HTTPException

_APP_SECRET = "test-app-secret-with-enough-entropy-0123456789"
_CF_TOKEN = "cfut_realCloudflareToken1234567890abcdefABCDEF"
_ACCOUNT = "abc123account456"
# P0-3: the server-side owner token the deploy routes require (X-Disco-Owner-Token).
_OWNER_TOKEN = "owner-secret-token-abcdef-0123456789"
_OWNER_HEADERS = {"X-Disco-Owner-Token": _OWNER_TOKEN}


class _RouteTestClient:
    """Tiny sync HTTP client for these route tests.

    The repo's current Starlette TestClient shim hangs with httpx 0.28 in this
    environment. The Cloudflare route tests only need simple HTTP request/response
    behavior, so drive the ASGI app directly.
    """

    def __init__(self, app: FastAPI, *, headers: dict[str, str] | None = None) -> None:
        self._app = app
        self._headers = dict(headers or {})

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:  # noqa: ANN003
        headers = {**self._headers, **dict(kwargs.pop("headers", {}) or {})}
        json_body = kwargs.pop("json", None)
        if kwargs:
            raise TypeError(f"unsupported _RouteTestClient kwargs: {sorted(kwargs)}")
        body = b""
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers.setdefault("content-type", "application/json")
        if body:
            headers.setdefault("content-length", str(len(body)))
        parsed = urlsplit(url)

        async def _send() -> httpx.Response:
            response_started: dict[str, object] = {}
            chunks: list[bytes] = []
            sent_body = False

            async def receive() -> dict[str, object]:
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                if message["type"] == "http.response.start":
                    response_started.update(message)
                elif message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))  # type: ignore[arg-type]

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": parsed.path,
                "raw_path": parsed.path.encode("ascii"),
                "query_string": parsed.query.encode("ascii"),
                "root_path": "",
                "headers": [
                    (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
                ],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
            await asyncio.wait_for(self._app(scope, receive, send), timeout=10)
            return httpx.Response(
                int(response_started.get("status", 500)),
                headers={
                    k.decode("latin-1"): v.decode("latin-1")
                    for k, v in response_started.get("headers", [])  # type: ignore[union-attr]
                },
                content=b"".join(chunks),
                request=httpx.Request(method, f"http://testserver{url}"),
            )

        return asyncio.run(_send())

    def get(self, url: str, **kwargs) -> httpx.Response:  # noqa: ANN003
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:  # noqa: ANN003
        return self.request("POST", url, **kwargs)


# P1-3: the real D1 database_id the fake wrangler `d1 create`/`d1 list` reports.
_DB_ID = "11111111-2222-3333-4444-555555555555"
# P1: the secret set as the DEPLOYED app's ADMIN_TOKEN. REQUIRED for a real deploy
# (so the deployed admin endpoint is protected) — rides only via `wrangler secret
# put ADMIN_TOKEN` on stdin, never argv/log/record.
_APP_ADMIN_TOKEN = "app-admin-token-xyz"


# ---- wrangler-argv helpers (trusted-binary shape) ---------------------------
# A real deploy invokes a TRUSTED wrangler binary directly (SEC-3) — argv[0] is the
# resolved wrangler path (bare "wrangler" by default, or DISCO_WRANGLER_BIN), NEVER
# `npx wrangler`. These helpers recognise a wrangler call regardless of argv[0]'s form.


def _is_wrangler(argv) -> bool:  # noqa: ANN001
    return bool(argv) and Path(argv[0]).name == "wrangler"


def _wr_sub(argv):  # noqa: ANN001
    """The wrangler subcommand tokens (argv after the binary), or None if not a
    wrangler call."""
    return list(argv[1:]) if _is_wrangler(argv) else None


def _is_deploy(argv) -> bool:  # noqa: ANN001
    return _is_wrangler(argv) and list(argv[1:2]) == ["deploy"]


def _is_d1_create(argv) -> bool:  # noqa: ANN001
    return _is_wrangler(argv) and list(argv[1:3]) == ["d1", "create"]


# ---- fixtures ---------------------------------------------------------------


def _write_export(workspace: Path) -> None:
    """Generate a COMPLETE, cloudflare_export_ready-passing app tree into *workspace*."""
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    spec = default_lead_gen_app_spec("Acme Leads", recipe)
    tree = generate(spec, recipe.to_design_spec())
    for rel, body in tree.items():
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    # Persist the AppSpec to .disco/appspec.json — exactly what app_create does — so the
    # POST-build canonical-worker gate (SEC-4/BUILD-1) can regenerate the canonical Worker
    # from the SAME spec and require the deployed worker/index.ts to match it.
    save_app_spec(workspace, spec)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    _write_export(ws)
    return ws


@pytest.fixture(autouse=True)
def _trusted_wrangler(tmp_path: Path, monkeypatch) -> Path:  # noqa: ANN001
    """WAVE 2 (SEC-3/CORR-3): the deploy now FAILS CLOSED unless a TRUSTED wrangler
    binary resolves from an ABSOLUTE path OUTSIDE the workspace/staging tree. Pin one via
    DISCO_WRANGLER_BIN — an absolute file named ``wrangler`` under a dir that is neither
    the workspace nor the system staging tree — so the real-deploy tests exercise the
    full sequence (the fake runner never execs it). Named ``wrangler`` so the argv[0]
    name checks still recognise it."""
    binp = tmp_path / "trusted-bin" / "wrangler"
    binp.parent.mkdir(parents=True, exist_ok=True)
    binp.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DISCO_WRANGLER_BIN", str(binp))
    return binp


@pytest.fixture(autouse=True)
def _isolated_deploy_lock_dir(tmp_path: Path, monkeypatch) -> Path:  # noqa: ANN001
    """SEC-25: pin the CROSS-PROCESS deploy-lock directory into the test's tmp dir so the
    file ``flock`` lockfiles never touch the real ``$XDG_STATE_HOME``/temp dir and each
    test starts with a clean lock state."""
    d = tmp_path / "deploy-locks"
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _isolated_deploy_stage_dir(tmp_path: Path, monkeypatch) -> Path:  # noqa: ANN001
    """Out-of-tree wrangler-config bypass: pin the per-deploy STAGING ROOT into the test's
    own tmp dir so the staged cwd's ANCESTOR chain (which ``wrangler deploy`` walks up to
    discover a ``.wrangler/deploy/config.json`` redirect) is a path THIS test controls —
    isolated from the real shared system temp and starting clean (no ancestor ``.wrangler``).
    A test can then plant a ``.wrangler`` at an ancestor (under ``tmp_path``) to exercise the
    ``_assert_no_ancestor_wrangler_config`` guard."""
    d = tmp_path / "deploy-stage"
    monkeypatch.setenv("DISCO_DEPLOY_STAGE_DIR", str(d))
    return d


def _seed_ownership_record(workspace: Path, plan, store) -> str:  # noqa: ANN001
    """SEC-10/SEC-10-B: write a TRUSTWORTHY prior Disco deploy record proving we own the
    target worker/D1, so an idempotent RE-deploy may adopt the existing remote resource.
    Goes through the REAL writer (:func:`cf._persist_record` with the ``store``) so the
    record carries a VALID server-side HMAC signature + the proof mutations
    (``d1_create:<db>`` + ``worker_deploy``) — exactly what a genuine prior deploy writes.
    ``.disco`` is digest-SKIPPED, so seeding it does NOT perturb the plan_hash/confirmation."""
    return cf._persist_record(
        workspace,
        cf._new_record_rel(),
        plan,
        status="succeeded",
        mutations=[f"d1_create:{plan.db_name}", "d1_migrate", "worker_deploy"],
        deployed_url="https://acme-leads.workers.dev",
        effective_digest="seed-effective-digest",
        store=store,
    )


def _plant_unsigned_ownership_record(workspace: Path, plan, *, signature=None) -> Path:  # noqa: ANN001
    """SEC-10-B (attacker simulation): plant a FORGED ownership record in the digest-
    SKIPPED ``.disco`` dir — the untrusted build agent's forgery vector. It names the real
    target worker/D1 + account + proof mutations but carries NO valid server signature
    (``None`` = unsigned, or an arbitrary forged hex). It MUST authorize nothing."""
    d = workspace / ".disco" / "cloudflare" / "deployments"
    d.mkdir(parents=True, exist_ok=True)
    rec = d / "20260101T000000_000000Z-planted.json"
    payload = {
        "status": "succeeded",
        "worker_name": plan.worker_name,
        "db_name": plan.db_name,
        "account_id": plan.account_id,
        "mutations": [f"d1_create:{plan.db_name}", "worker_deploy"],
    }
    if signature is not None:
        payload["signature"] = signature
    rec.write_text(json.dumps(payload), encoding="utf-8")
    return rec


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets.json", box=SecretBox(_APP_SECRET))


@pytest.fixture
def connected(store: SecretStore) -> SecretStore:
    cf.connect_account(store, token=_CF_TOKEN, account_id=_ACCOUNT)
    return store


class FakeRunner:
    """Records every command. Returns canned ok results; the d1-list output omits
    the db name by default so a fresh deploy creates it. The `d1 create`/`d1 list`
    outputs carry the real database_id so the wrangler.toml substitution (P1-3)
    has a UUID to install."""

    def __init__(
        self,
        *,
        d1_list_has: str | None = None,
        deploy_url: str | None = None,
        fail_on: str | None = None,
        worker_exists: bool = False,
    ) -> None:
        self.calls: list[dict] = []
        self._d1_list_has = d1_list_has
        self._deploy_url = deploy_url or "https://acme-leads.workers.dev"
        # A substring of the joined argv that should return a NON-zero exit (P1-5).
        self._fail_on = fail_on
        # SEC-10 (Worker): `deployments list --name <w> --json` returns a NON-empty array
        # (worker already exists on the account) when True, else `[]` (fresh deploy).
        self._worker_exists = worker_exists
        # P1-3: snapshot of wrangler.toml AT THE MOMENT `wrangler deploy` runs, so a
        # test can prove the real database_id was substituted BEFORE deploy.
        self.wrangler_toml_at_deploy: str | None = None

    async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
        self.calls.append({"argv": list(argv), "env": dict(env), "stdin": stdin, "cwd": str(cwd)})
        joined = " ".join(argv)
        if self._fail_on and self._fail_on in joined:
            return CommandResult(1, "", f"boom: {self._fail_on}")
        # The deploy invokes a TRUSTED wrangler binary directly (NOT `npx wrangler`):
        # argv[0] is the resolved wrangler path (bare "wrangler" in tests, or the
        # DISCO_WRANGLER_BIN abs path); the subcommand is argv[1:].
        wr = _wr_sub(argv)
        if wr is not None:
            if wr[:2] == ["d1", "list"]:
                # JSON listing; includes the db (with its real id) only when adopting.
                out = (
                    json.dumps([{"name": self._d1_list_has, "uuid": _DB_ID}])
                    if self._d1_list_has
                    else "[]"
                )
                return CommandResult(0, out, "")
            if wr[:2] == ["d1", "create"]:
                return CommandResult(0, f'database_id = "{_DB_ID}"\n', "")
            if wr[:2] == ["deployments", "list"]:
                # SEC-10 Worker existence preflight (read-only): a non-empty array means
                # the Worker already exists on the account; `[]` means it does not.
                out = json.dumps([{"id": "dep-1"}]) if self._worker_exists else "[]"
                return CommandResult(0, out, "")
            if wr[:1] == ["deploy"]:
                toml = cwd / "wrangler.toml"
                self.wrangler_toml_at_deploy = toml.read_text() if toml.exists() else None
                return CommandResult(0, f"Published to {self._deploy_url}", "")
        return CommandResult(0, "ok", "")


class FakeBuildBackend:
    """Records the sandboxed build dispatch (P0-1/P0-2). ``isolated`` mirrors a real
    isolating sandbox; ``isolated=False`` simulates a non-isolating backend (the
    deploy must then be refused). Writes a fake ./dist so the deploy is coherent."""

    def __init__(self, *, isolated: bool = True, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._isolated = isolated
        self._fail = fail

    @property
    def isolates(self) -> bool:
        return self._isolated

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        self.calls.append(
            {
                "workspace": str(workspace),
                "install_cmd": install_cmd,
                "build_cmd": build_cmd,
                "asset_dir": asset_dir,
            }
        )
        # Simulate the sandbox syncing the built SPA back into the CONFIGURED asset dir
        # (CORR-16): a non-`dist` app must build+sync the dir wrangler will publish.
        (workspace / asset_dir).mkdir(parents=True, exist_ok=True)
        (workspace / asset_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        rc = 1 if self._fail else 0
        return BuildResult(
            returncode=rc,
            stdout="" if self._fail else "built ok",
            stderr="boom" if self._fail else "",
            isolated=True,
        )


class _ShRes:
    """Minimal sandbox ``exec_shell`` result (``exit_code``/``stdout``/``stderr``)."""

    def __init__(self, exit_code: int, stdout: str, stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandboxInstance:
    """A fake sandbox instance for ``_sync_output_back`` (P0). ``exec_shell`` returns
    the canned ``find`` listing; ``read_file`` returns the build's bytes for a path.
    Lets a test drive the sync-back containment check WITHOUT a real sandbox."""

    def __init__(self, listing: list[str], files: dict[str, bytes] | None = None) -> None:
        self._listing = listing
        self._files = files or {}

    async def exec_shell(self, cmd, *, timeout_s):  # noqa: ANN001
        # The real backend lists with `find … -print0` (NUL-delimited, SEC-23) so a
        # newline in a filename can't corrupt the listing — mirror that here.
        return _ShRes(0, "\0".join(self._listing), "")

    async def read_file(self, rel):  # noqa: ANN001
        return self._files.get(rel, b"BUILT")


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


# ---- the four HARD gates (each BLOCKS a real deploy) -------------------------


async def _refusal(coro) -> RefusalReason:
    with pytest.raises(cf.DeployRefused) as exc:
        await coro
    return exc.value.reason


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


class _AltConfigEmittingBuildBackend(FakeBuildBackend):
    """A sandbox build that — beyond compiling ./dist — EMITS an alternate wrangler config
    (wrangler.json) into the synced-back staged tree, AFTER the pre-build alt-config gate
    ran. Only the POST-build re-scan can catch it before any wrangler mutation."""

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        res = await super().build(
            workspace, install_cmd=install_cmd, build_cmd=build_cmd, asset_dir=asset_dir
        )
        (workspace / "wrangler.json").write_text(
            json.dumps({"name": "evil-worker", "main": "evil/index.ts"}), encoding="utf-8"
        )
        return res


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


# ---- P0: _sync_output_back containment — build output can't escape ./dist ----


def _backend():
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    # runtime is unused by _sync_output_back; pass a throwaway object.
    return sb.SandboxBuildBackend(object())  # type: ignore[arg-type]


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


# ---- P0 (PUSH side): a workspace symlink escaping the workspace is rejected ----


class _RecordingSandboxInstance:
    """Records every ``write_file`` push so a test can prove WHICH workspace bytes
    reached the build sandbox (and that an escaping symlink's host-secret content
    never did)."""

    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}

    async def write_file(self, rel, data):  # noqa: ANN001
        self.written[rel] = data

    async def exec_shell(self, cmd, *, timeout_s):  # noqa: ANN001
        return _ShRes(0, "", "")

    async def read_file(self, rel):  # noqa: ANN001
        return b""

    async def destroy(self):
        return None


class _StubSandboxService:
    def __init__(self, instance, *, name: str = "gvisor") -> None:  # noqa: ANN001
        self._instance = instance
        self.name = name  # deploy-grade kind verified on the SAME instance by build()
        self.created = False

    async def create(self, spec, *, owner_id, conversation_id):  # noqa: ANN001
        self.created = True
        return self._instance

    async def destroy_by_conversation(self, conversation_id):  # noqa: ANN001
        return None


def _filtered_build_spec():
    """A real FILTERED-egress SandboxSpec (non-empty allowlist, no NETWORK cap) —
    what ``_build_sandbox_spec(surface='build')`` resolves to. build() ENFORCES that
    a deploy build runs under filtered egress, so the stub must return a real spec."""
    from disco.tools import SandboxSpec

    return SandboxSpec(egress_allow=frozenset({"registry.npmjs.org"}))


class _StubRuntimeForBuild:
    """A runtime stub that yields an isolating backend + the recording instance, so
    ``SandboxBuildBackend.build`` exercises the REAL push loop (with its symlink
    containment guard) without a real sandbox."""

    def __init__(self, instance, *, backend_name: str = "gvisor") -> None:  # noqa: ANN001
        self._svc = _StubSandboxService(instance, name=backend_name)

    def sandbox_backend_name(self) -> str:
        return self._svc.name

    def _sandbox_service_now(self):
        return self._svc

    def _build_sandbox_spec(self, *, surface):  # noqa: ANN001
        return _filtered_build_spec()


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


# ---- Epic O Cluster B: isolation-race, deploy-grade gate, egress, structured errs --


class _ConfigurableService:
    """A sandbox service whose isolation ``name``, create exception, and create-count
    are all controllable, so a test can drive the deploy-grade / race gates."""

    def __init__(self, instance, *, name="gvisor", create_exc=None) -> None:  # noqa: ANN001
        self._instance = instance
        self.name = name
        self.created = False
        self._create_exc = create_exc

    async def create(self, spec, *, owner_id, conversation_id):  # noqa: ANN001
        self.created = True
        if self._create_exc is not None:
            raise self._create_exc
        return self._instance

    async def destroy_by_conversation(self, conversation_id):  # noqa: ANN001
        return None


class _ConfigurableRuntime:
    """Runtime stub: the pre-gate ``sandbox_backend_name`` and the build-time
    ``_sandbox_service_now().name`` can DIFFER (to model the settings race), and the
    egress spec is injectable."""

    def __init__(self, svc, *, gate_name=None, spec=None) -> None:  # noqa: ANN001
        self._svc = svc
        self._gate_name = gate_name if gate_name is not None else svc.name
        self._spec = spec if spec is not None else _filtered_build_spec()

    def sandbox_backend_name(self):
        return self._gate_name

    def _sandbox_service_now(self):
        return self._svc

    def _build_sandbox_spec(self, *, surface):  # noqa: ANN001
        return self._spec


def _open_egress_spec():
    """An OPEN-network spec (full NETWORK capability, empty allowlist) — what an
    operator who set BUILD_EGRESS=open would get. A deploy build must REFUSE it."""
    from disco.tools import Capability, SandboxSpec

    return SandboxSpec(permitted=frozenset({Capability.NETWORK}), egress_allow=frozenset())


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


# ---- Epic O wave-2: SERVICE / SPEC lookup errors are structured (CORR-20) -----


class _ServiceLookupRaisesRuntime:
    """A runtime whose ``_sandbox_service_now`` RAISES (no backend configured / a
    provider import error). build() must convert it to a structured failed BuildResult,
    never let it escape as a 500 to the deploy caller."""

    def sandbox_backend_name(self):
        return "gvisor"

    def _sandbox_service_now(self):
        raise RuntimeError("no sandbox backend configured")

    def _build_sandbox_spec(self, *, surface):  # noqa: ANN001
        return _filtered_build_spec()


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


class _SpecLookupRaisesRuntime:
    """A runtime that resolves a deploy-grade service fine but whose
    ``_build_sandbox_spec`` RAISES (bad ``BUILD_EGRESS`` config) — must become a
    structured failed BuildResult, and the box must NEVER be created."""

    def __init__(self, svc) -> None:  # noqa: ANN001
        self._svc = svc

    def sandbox_backend_name(self):
        return self._svc.name

    def _sandbox_service_now(self):
        return self._svc

    def _build_sandbox_spec(self, *, surface):  # noqa: ANN001
        raise RuntimeError("bad BUILD_EGRESS config")


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


class _ExecRaisesInstance:
    """A sandbox instance whose ``exec_shell`` raises mid-build (a sandbox transport
    error) — build() must convert it to a structured failed BuildResult."""

    def __init__(self) -> None:
        self.destroyed = False

    async def write_file(self, rel, data):  # noqa: ANN001
        return None

    async def exec_shell(self, cmd, *, timeout_s):  # noqa: ANN001
        raise RuntimeError("sandbox socket reset")

    async def read_file(self, rel):  # noqa: ANN001
        return b""

    async def destroy(self):
        self.destroyed = True


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


class _TeardownRaisesInstance:
    """Build finishes (non-zero exit, no sync) but teardown raises — surfaced, not
    swallowed."""

    async def write_file(self, rel, data):  # noqa: ANN001
        return None

    async def exec_shell(self, cmd, *, timeout_s):  # noqa: ANN001
        return _ShRes(1, "", "build failed")  # non-zero → no sync, plain failure

    async def read_file(self, rel):  # noqa: ANN001
        return b""

    async def destroy(self):
        raise RuntimeError("destroy timed out")


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


# ---- Epic O Cluster B: _sync_output_back atomic + capped + fail-closed -------


class _SyncInstance:
    """A sandbox instance for _sync_output_back: controllable find exit code, NUL-
    delimited listing (mirrors `-print0`), per-file bytes (None = unreadable), and it
    records the find command so a test can prove `-print0` is used (SEC-23)."""

    def __init__(self, listing, files=None, *, find_exit=0) -> None:  # noqa: ANN001
        self._listing = listing
        self._files = files or {}
        self._find_exit = find_exit
        self.find_cmd = None

    async def exec_shell(self, cmd, *, timeout_s):  # noqa: ANN001
        self.find_cmd = cmd
        return _ShRes(self._find_exit, "\0".join(self._listing), "")

    async def read_file(self, rel):  # noqa: ANN001
        return self._files.get(rel, b"BUILT")


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


# ---- P0 (round 6): ALL workspace reads route through ONE containment-guarded -----
# ---- reader. Every entry point that reads a workspace file refuses an escaping ----
# ---- symlink BEFORE the host-secret bytes are ever opened. ------------------------


_HOST_SECRET_MARKER = "TOP-SECRET-HOST-CREDENTIAL"


def _install_read_spy(monkeypatch) -> list[str]:  # noqa: ANN001
    """Spy EVERY raw file read (``read_text`` + ``read_bytes``), recording the
    RESOLVED path read. Lets a test prove an escaping symlink's host-secret target
    is never opened (it never appears in the recorded list)."""
    seen: list[str] = []
    real_rt = Path.read_text
    real_rb = Path.read_bytes

    def _spy_rt(self: Path, *a, **k):  # noqa: ANN001, ANN202
        seen.append(str(self.resolve()))
        return real_rt(self, *a, **k)

    def _spy_rb(self: Path, *a, **k):  # noqa: ANN001, ANN202
        seen.append(str(self.resolve()))
        return real_rb(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _spy_rt)
    monkeypatch.setattr(Path, "read_bytes", _spy_rb)
    return seen


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


# ---- P0 (round 8, WRITE class): the .disco deployment-record write is contained ----


def _minimal_plan(workspace: Path) -> object:
    from disco.agent_server.appkit_cloudflare.models import DeployPlan

    return DeployPlan(
        workspace=str(workspace),
        account_id="acct",
        worker_name="w",
        db_name="db",
        spec_digest="sd",
        tree_digest="td",
        plan_hash="ph",
        export_ready=True,
        export_detail="",
        connected=True,
    )


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


# ---- P1: the admin_token is NEVER recorded (secret-put output not stored + scrubbed) --


class _AdminEchoRunner(FakeRunner):
    """A fake wrangler that, like a misbehaving real one, ECHOES the stdin admin
    token in its stdout for the `secret put` step — proving the recording layer
    never lets the literal token reach the transcript/record."""

    async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
        res = await super().run(argv, cwd=cwd, env=env, stdin=stdin)
        if argv[-1] == "ADMIN_TOKEN":
            # Echo the secret on BOTH streams, as a leaky wrangler might.
            return CommandResult(0, f"setting secret to {stdin}", f"value={stdin}")
        return res


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


# ---- route wiring (owner API, outside the LLM loop) -------------------------


class _FakeRuntime:
    def __init__(self, ps: ProjectStore) -> None:
        self._ps = ps

    def project_store(self) -> ProjectStore:
        return self._ps


class FakeVerifier:
    """A fake TokenVerifier so /connect's verify-before-store gate (CORR-27) runs
    WITHOUT a real Cloudflare network call. ``ok=False`` simulates a rejected /
    unverifiable token (the account must then NOT be marked connected)."""

    def __init__(self, *, ok: bool = True, detail: str = "token is active") -> None:
        self._ok = ok
        self._detail = detail
        self.calls: list[str] = []

    async def verify(self, token: str) -> tuple[bool, str]:
        self.calls.append(token)
        return self._ok, self._detail


def _build_app(
    tmp_path: Path,
    *,
    runner=None,
    build_backend=None,
    verifier=None,
    cors: str | None = None,
) -> tuple[FastAPI, ProjectStore, str]:
    """Assemble the deploy app + a ready conversation workspace. ``cors`` is None,
    "strict" (wildcard global CORS + the strict middleware), or "reflect" (a global
    CORS that REFLECTS an arbitrary Origin + the strict middleware)."""
    root = tmp_path / "projects"
    root.mkdir()
    ps = ProjectStore(str(root))
    cid = "11111111-1111-1111-1111-111111111111"
    ws = ps.path_for(cid)
    ws.mkdir(parents=True)
    _write_export(ws)
    app = FastAPI()
    if cors is not None:
        from disco.agent_server.appkit_cloudflare import CloudflareDeployCorsMiddleware
        from fastapi.middleware.cors import CORSMiddleware

        if cors == "reflect":
            # A global CORS that REFLECTS an arbitrary Origin (credentialed) — the
            # strict middleware must still strip it on the deploy paths (CORR-26).
            app.add_middleware(
                CORSMiddleware,
                allow_origin_regex=".*",
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        else:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=False,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        app.add_middleware(CloudflareDeployCorsMiddleware)
    app.include_router(
        make_cloudflare_router(
            SqliteEventStore(":memory:"),
            _FakeRuntime(ps),
            runner=runner,
            build_backend=build_backend,
            verifier=verifier or FakeVerifier(),
        )
    )
    return app, ps, cid


def _client(
    tmp_path: Path,
    monkeypatch,
    runner=None,
    *,
    with_cors: bool = False,
    owner_auth: bool = True,
    build_backend=None,
    verifier=None,
    cors: str | None = None,
) -> tuple[_RouteTestClient, ProjectStore, str]:
    # P0-3: every route is owner-gated; the test client presents the owner token by
    # default. ``owner_auth=False`` builds an UNauthenticated client (→ 401).
    monkeypatch.setenv("DISCO_ADMIN_TOKEN", _OWNER_TOKEN)
    app, ps, cid = _build_app(
        tmp_path,
        runner=runner,
        build_backend=build_backend,
        verifier=verifier,
        cors=("strict" if with_cors else cors),
    )
    headers = dict(_OWNER_HEADERS) if owner_auth else {}
    return _RouteTestClient(app, headers=headers), ps, cid


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
    client, _ps, cid = _client(
        tmp_path, monkeypatch, runner=runner, build_backend=FakeBuildBackend()
    )
    client.post("/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": _ACCOUNT})
    # default dry-run: executes nothing.
    res = client.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid})
    assert res.status_code == 200
    assert res.json()["dry_run"] is True and res.json()["executed"] is False
    assert runner.calls == []
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


# ---- P0 (round 9): a PREEXISTING symlink in the dist tree wrangler UPLOADS is -----
# ---- refused at DEPLOY time, BEFORE `wrangler deploy` runs. wrangler FOLLOWS -------
# ---- symlinks, so the only safe upload tree has ZERO symlinks; the plan-time -------
# ---- digest SKIPS symlinked subtrees, so this can only be closed at deploy time. --


class _SymlinkEmittingBuildBackend(FakeBuildBackend):
    """A build backend that emits a SYMLINK into ./dist (simulating the untrusted
    build producing a symlinked deliverable). dist is regenerated by the build AFTER
    the plan digest, so the plan-time containment check never sees it — only the
    DEPLOY-time scan can catch it. ``link_target`` is the symlink's destination."""

    def __init__(self, *, link_rel: str, link_target: Path, **kw) -> None:  # noqa: ANN003
        super().__init__(**kw)
        self._link_rel = link_rel
        self._link_target = link_target

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        res = await super().build(workspace, install_cmd=install_cmd, build_cmd=build_cmd)
        link = workspace / self._link_rel
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self._link_target)  # planted AFTER the plan digest ran
        return res


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


# ---- A1: per-workspace deploy mutex serializes concurrent deploys -----------


class _ConcurrencyBuildBackend(FakeBuildBackend):
    """Tracks the max number of OVERLAPPING builds. With the per-workspace deploy
    mutex, two deploys of the same workspace must never build concurrently."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        for _ in range(8):
            await asyncio.sleep(0)  # give the other deploy every chance to interleave
        res = await super().build(workspace, install_cmd=install_cmd, build_cmd=build_cmd)
        self.active -= 1
        return res


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


# ---- A3: assets.directory exact-resolve (CORR-13) ---------------------------


def _ws_with_toml(tmp_path: Path, directory: str) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "wrangler.toml").write_text(
        f'name = "w"\n[assets]\ndirectory = "{directory}"\n', encoding="utf-8"
    )
    return ws


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


# ---- A5: credential snapshot — a /connect swap mid-deploy is refused ----------


class _CredSwapBuildBackend(FakeBuildBackend):
    """Simulates a `/connect` account/token swap DURING the deploy (between the
    confirmed plan's credential snapshot and the wrangler steps)."""

    def __init__(self, store, *, new_token: str, new_account: str) -> None:  # noqa: ANN001
        super().__init__()
        self._store = store
        self._new_token = new_token
        self._new_account = new_account

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        res = await super().build(workspace, install_cmd=install_cmd, build_cmd=build_cmd)
        cf.connect_account(self._store, token=self._new_token, account_id=self._new_account)
        return res


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


# ---- WAVE 3 SEC-4: worker AUTH-semantics verification before a real deploy ----


def _corrupt_worker(workspace: Path, transform) -> None:  # noqa: ANN001
    """Rewrite worker/index.ts via *transform* (kept export-ready-passing — the structural
    auth check is what we're exercising, not export readiness)."""
    p = workspace / "worker" / "index.ts"
    p.write_text(transform(p.read_text(encoding="utf-8")), encoding="utf-8")


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


# ---- WAVE 5 SEC-4 / BUILD-1: canonical-worker MATCH (non-bypassable worker auth) ----
# The deploy trust no longer comes from PROVING arbitrary worker TS is auth-safe (the
# heuristic inspector — a losing arms race the 3 codex bypasses each won). The deployed
# worker is REQUIRED to BE the canonical worker the generator emits from the app spec,
# checked POST-build. So every heuristic bypass is refused by construction, and a build
# that EMITS/rewrites worker/index.ts post-build (BUILD-1 TOCTOU) is caught before any
# wrangler mutation. The generated worker is NAMESPACE-INDEPENDENT (the SEC-30 namespace
# only renames the Worker/D1 in wrangler.toml), so regenerating from the spec is exact.


def _canonical_worker_src() -> str:
    """The pristine worker/index.ts the generator emits for the `_write_export` app."""
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    spec = default_lead_gen_app_spec("Acme Leads", recipe)
    return generate(spec, recipe.to_design_spec())["worker/index.ts"]


def _form_folded_app_spec():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    spec = default_lead_gen_app_spec("Acme Leads", recipe)
    form = FormSpec.model_validate(
        {
            "form_id": "quote_request",
            "title": "Request a quote",
            "fields": [{"name": "email", "label": "Email", "kind": "email", "required": True}],
            "success_message": "Thanks, we will respond shortly.",
        }
    )
    return apply_form_spec(spec, form)


def _form_folded_worker_src() -> str:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    spec = _form_folded_app_spec()
    return generate(spec, recipe.to_design_spec())["worker/index.ts"]


def _staged_tree_with_worker(
    base: Path,
    worker_src: str,
    *,
    with_spec: bool = True,
    wrangler_main: str | None = "worker/index.ts",
    app_spec=None,
) -> Path:
    """A minimal STAGED-shape tree for the canonical-match unit gate: the frozen
    `.disco/appspec.json` the gate regenerates from + a `worker/index.ts` + a
    `wrangler.toml` whose `main` the gate ties to the canonical worker. Mirrors what
    `_stage_deploy_tree` stages (it copies the spec into the immutable staged tree).

    `wrangler_main` is the `main = "..."` value written into wrangler.toml (defaults to
    the canonical `worker/index.ts`); pass a look-alike/escape value to exercise the
    main-redirect refusal, or None to omit wrangler.toml entirely."""
    ws = base / "staged"
    (ws / "worker").mkdir(parents=True)
    (ws / "worker" / "index.ts").write_text(worker_src, encoding="utf-8")
    if wrangler_main is not None:
        (ws / "wrangler.toml").write_text(
            f'name = "acme-leads"\nmain = "{wrangler_main}"\n', encoding="utf-8"
        )
    if with_spec:
        recipe = get_recipe("editorial-ledger")
        assert recipe is not None
        save_app_spec(ws, app_spec or default_lead_gen_app_spec("Acme Leads", recipe))
    return ws


def _inject_route(worker_src: str, snippet: str) -> str:
    """Insert a route snippet right after the request URL is parsed in fetch()."""
    marker = "const url = new URL(request.url);"
    assert marker in worker_src
    return worker_src.replace(marker, marker + "\n" + snippet, 1)


# SEC4-1: the transitive ADMIN_TOKEN leak — copies env.ADMIN_TOKEN through an alias, then
# echoes it from an unauthenticated /debug-token route. Crafted to slip past the taint
# heuristic (copy-of-alias is not tracked) — but it is NOT the canonical worker → refused.
_TRANSITIVE_LEAK_DEBUG_TOKEN = (
    '    if (url.pathname === "/debug-token") {\n'
    "      const expected = env.ADMIN_TOKEN;\n"
    "      const leakedAdminToken = expected;\n"
    "      return new Response(leakedAdminToken);\n"
    "    }\n"
)

# SEC4-2: an unauthenticated lead read reachable via url.pathname.startsWith — the route
# enumerator misses startsWith/regex/switch dispatch, so it passes all_lead_reads_guarded.
# Non-canonical → refused.
_UNGUARDED_DEBUG_LEADS = (
    '    if (url.pathname.startsWith("/debug-leads")) {\n'
    '      const rows = await env.DB.prepare("SELECT * FROM leads").all();\n'
    "      return Response.json(rows.results);\n"
    "    }\n"
)


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


# ---- SEC-1-class: the `main` entrypoint redirect (exact-match worker/index.ts) ------
# `wrangler deploy` runs whatever wrangler.toml `main` points at, but the canonical-worker
# match gate validates worker/index.ts. A non-canonical `main` (esp. `....worker/index.ts`,
# which the old `lstrip("./")` collapsed to the canonical name) would deploy a DIFFERENT,
# unchecked worker. The deploy now exact-matches `main` to worker/index.ts (no lstrip), in
# BOTH the pre-build allowlist gate AND tied to the post-build canonical gate.

_NON_CANONICAL_MAINS = [
    "....worker/index.ts",  # look-alike dir; lstrip("./") would falsely pass it
    "./....worker/index.ts",
    "/etc/passwd",  # absolute path
    "../evil.ts",  # parent escape
    "other/index.ts",  # a different in-tree worker
    "worker/evil.ts",  # right dir, wrong file
]


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


class _WranglerEmittingBuildBackend(FakeBuildBackend):
    """BUILD-1 TOCTOU: a sandbox build that — beyond compiling ./dist — REWRITES
    wrangler.toml `main` to a non-canonical target AFTER the pre-build allowlist gate ran.
    Only the POST-build canonical gate (which re-reads the staged wrangler.toml) can catch
    the redirect before any wrangler mutation."""

    def __init__(self, emitted_main: str, **kw) -> None:  # noqa: ANN003
        super().__init__(**kw)
        self._emitted_main = emitted_main

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        res = await super().build(
            workspace, install_cmd=install_cmd, build_cmd=build_cmd, asset_dir=asset_dir
        )
        toml = workspace / "wrangler.toml"
        toml.write_text(
            toml.read_text().replace('main = "worker/index.ts"', f'main = "{self._emitted_main}"'),
            encoding="utf-8",
        )
        return res


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


class _WorkerEmittingBuildBackend(FakeBuildBackend):
    """BUILD-1 TOCTOU simulation: an isolating sandbox build that — beyond compiling the
    frontend into ./dist — EMITS a (malicious) worker/index.ts into the staged tree. The
    sync-back replaces the pristine worker AFTER the pre-build auth gate ran, so only the
    POST-build canonical-match gate can catch it (before any wrangler mutation)."""

    def __init__(self, emitted_worker: str, **kw) -> None:  # noqa: ANN003
        super().__init__(**kw)
        self._emitted_worker = emitted_worker

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        res = await super().build(
            workspace, install_cmd=install_cmd, build_cmd=build_cmd, asset_dir=asset_dir
        )
        # Post-build emit: overwrite the staged worker source with the attacker's worker.
        (workspace / "worker").mkdir(parents=True, exist_ok=True)
        (workspace / "worker" / "index.ts").write_text(self._emitted_worker, encoding="utf-8")
        return res


async def _refuse_real_deploy_with_emitted_worker(
    workspace: Path, connected: SecretStore, emitted_worker: str
) -> tuple[RefusalReason, FakeRunner]:
    """Drive a real deploy whose sandbox build EMITS *emitted_worker* post-build. The
    pre-build worker is the pristine canonical one (so the heuristic auth gate PASSES);
    only the POST-build canonical-match gate refuses. Returns (reason, runner)."""
    plan = cf.build_plan(workspace, connected)
    runner = FakeRunner()
    reason = await _refusal(
        cf.execute_deploy(
            workspace,
            connected,
            dry_run=False,
            confirmation=plan.confirmation_phrase,
            runner=runner,
            build_backend=_WorkerEmittingBuildBackend(emitted_worker),
            admin_token=_APP_ADMIN_TOKEN,
        )
    )
    return reason, runner


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


# ---- WAVE 4 SEC-10-A: Worker-existence preflight FAILS CLOSED ----------------
# A nonzero/errored `deployments list` must NOT be read as "Worker absent" (which would
# let the deploy fall open and OVERWRITE an existing Worker). Only a successful parsed
# "absent", or a nonzero exit carrying a DOCUMENTED not-found signal, may proceed.


class _PreflightErrorRunner(FakeRunner):
    """Returns a NONZERO exit for the Worker-existence preflight (`deployments list`),
    simulating an old wrangler / auth / transport failure. ``stderr`` is configurable so a
    test can drive both the ambiguous-error case and the documented not-found case."""

    def __init__(self, *, preflight_stderr: str = "boom: transport error", **kw) -> None:  # noqa: ANN003
        super().__init__(**kw)
        self._preflight_stderr = preflight_stderr

    async def run(self, argv, *, cwd, env, stdin=None):  # noqa: ANN001
        wr = _wr_sub(argv)
        if wr and wr[:2] == ["deployments", "list"]:
            self.calls.append(
                {"argv": list(argv), "env": dict(env), "stdin": stdin, "cwd": str(cwd)}
            )
            return CommandResult(1, "", self._preflight_stderr)
        return await super().run(argv, cwd=cwd, env=env, stdin=stdin)


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


# ---- WAVE 4 SEC-17/18: POST-BUILD rescan catches a secret EMITTED by the build ----


class _SecretEmittingBuildBackend(FakeBuildBackend):
    """Simulates a build that EMITS a secret into the synced-back output dir — the exact
    gap the pre-build scan misses (it scanned the authored tree, before the build ran).
    ``emit_value`` bakes a secret-shaped token into a JS bundle; ``emit_file`` drops a
    credential-named file (denylist) into the asset dir (name = ``emit_file_name``)."""

    def __init__(
        self,
        *,
        emit_value: bool = False,
        emit_file: bool = False,
        emit_file_name: str = ".env.production",
        emit_file_body: str = "API_TOKEN=supersecret\n",
        **kw,  # noqa: ANN003
    ) -> None:
        super().__init__(**kw)
        self._emit_value = emit_value
        self._emit_file = emit_file
        self._emit_file_name = emit_file_name
        self._emit_file_body = emit_file_body

    async def build(self, workspace, *, install_cmd, build_cmd, asset_dir="dist"):  # noqa: ANN001
        res = await super().build(
            workspace, install_cmd=install_cmd, build_cmd=build_cmd, asset_dir=asset_dir
        )
        out = workspace / asset_dir
        if self._emit_value:
            # A real AWS key id baked into a built bundle (SEC-18 secret-shaped value).
            (out / "bundle.js").write_text(
                'var k="AKIAIOSFODNN7EXAMPLE";export default k;\n', encoding="utf-8"
            )
        if self._emit_file:
            # A credential-named file dropped into the published dir (SEC-17 denylist).
            (out / self._emit_file_name).write_text(self._emit_file_body, encoding="utf-8")
        return res


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


# ---- CLUSTER C (deploy-security): routes hardening ----------------------------
# SEC-25 per-conversation deploy lock / CORR-25 accurate status mapping / CORR-26
# CORS / SEC-27 bounded secret input / SEC-28 owner-auth rate limit + audit /
# SEC-26 no absolute host paths / CORR-27 verify-before-connected.


def _connect(client, *, account=_ACCOUNT):
    return client.post(
        "/api/appkit/cloudflare/connect", json={"token": _CF_TOKEN, "account_id": account}
    )


def _phrase_for(client, cid: str) -> str:
    dry = client.post("/api/appkit/cloudflare/deploy", json={"conversation_id": cid}).json()
    return dry["plan"]["confirmation_phrase"]


# ---- SEC-25: two concurrent deploys for one conversation — second gets 409 ----


async def test_concurrent_deploy_for_same_conversation_second_gets_409(tmp_path, monkeypatch):
    import asyncio

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
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert detail["reason"] == "deploy_failed"
    assert "migrate" in (detail["failed_step"] or "")
    # The publish step never ran.
    assert not any(c["argv"][:3] == ["npx", "wrangler", "deploy"] for c in runner.calls)


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


# ---- SEC-29: /connect validates the account id + binds verify to the account ----


class AccountScopedVerifier:
    """A TokenVerifier that ALSO exposes ``verify_for_account`` — it authorizes the
    token ONLY for the single account it is scoped to (SEC-29 scope binding). The
    route must prefer this account-bound check over the account-agnostic ``verify``."""

    def __init__(self, *, account: str, ok: bool = True) -> None:
        self._account = account
        self._ok = ok
        self.verify_calls: list[str] = []
        self.scoped_calls: list[tuple[str, str]] = []

    async def verify(self, token: str) -> tuple[bool, str]:
        # The account-agnostic fallback always passes — so a test that sees a refusal
        # PROVES the account-bound path (not this one) decided it.
        self.verify_calls.append(token)
        return True, "token is active"

    async def verify_for_account(self, token: str, account_id: str) -> tuple[bool, str]:
        self.scoped_calls.append((token, account_id))
        if self._ok and account_id == self._account:
            return True, "token is active for this account"
        return False, "token is not authorized for this account"


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
    import os

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
    import os
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
    import os
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
    import os
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


# ---- Epic O Cluster D: CF token verifier hardening (SEC-33/CORR-24) -----------
# The PRODUCTION HttpTokenVerifier must (1) NOT trust env proxies (a hostile
# HTTP_PROXY can't intercept the ad-hoc CF token), and (2) turn the full transport
# error surface + a non-object JSON body into a STRUCTURED verify failure (not 500).


class _FakeVerifyResp:
    """A canned httpx-like response. ``json_value`` may be an Exception to simulate
    a non-JSON body (``res.json()`` raises ValueError)."""

    def __init__(self, status_code: int, json_value: object) -> None:
        self.status_code = status_code
        self._json_value = json_value

    def json(self) -> object:
        if isinstance(self._json_value, Exception):
            raise self._json_value
        return self._json_value


def _patch_verify_httpx(monkeypatch, *, response=None, get_exc=None) -> dict:
    """Replace httpx.AsyncClient with a fake that records its construction kwargs
    and returns/raises a canned GET result — no real network."""
    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            captured.update(k)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):  # noqa: ANN002
            return False

        async def get(self, url, headers=None):  # noqa: ANN001
            if get_exc is not None:
                raise get_exc
            return response

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return captured


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

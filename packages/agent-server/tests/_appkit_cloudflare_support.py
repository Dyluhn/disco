"""Shared test support for the split appkit_cloudflare test modules.

Extracted verbatim (no behavior change) from the original single-file
test_appkit_cloudflare.py during its PY-0360 module-size split. Holds every
non-test top-level fixture / fake / helper / constant; the split test_*.py
modules import what they need from here via explicit re-export imports
(``from _appkit_cloudflare_support import name as name``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from disco.agent_server.appkit_cloudflare import deploy as cf
from disco.agent_server.appkit_cloudflare.models import RefusalReason
from disco.agent_server.appkit_cloudflare.routes import make_cloudflare_router
from disco.agent_server.appkit_cloudflare.wrangler import BuildResult, CommandResult
from disco.agent_server.workspace_commit import WorkspaceCommitUnavailable
from disco.core.appkit import default_lead_gen_app_spec, generate, get_recipe, save_app_spec
from disco.core.appkit.form_primitive import FormSpec, apply_form_spec
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import ProjectStore
from fastapi import FastAPI

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


# ---- the four HARD gates (each BLOCKS a real deploy) -------------------------


async def _refusal(coro) -> RefusalReason:
    with pytest.raises(cf.DeployRefused) as exc:
        await coro
    return exc.value.reason


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


# ---- P0: _sync_output_back containment — build output can't escape ./dist ----


def _backend():
    import disco.agent_server.appkit_cloudflare.sandbox_build as sb

    # runtime is unused by _sync_output_back; pass a throwaway object.
    return sb.SandboxBuildBackend(object())  # type: ignore[arg-type]


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


# ---- route wiring (owner API, outside the LLM loop) -------------------------


class _FakeRuntime:
    def __init__(self, ps: ProjectStore) -> None:
        self._ps, self._workspace = ps, self
        self._lock = asyncio.Lock()
        self.mutations: list[tuple[str, tuple[str, ...]]] = []
        self._committed = ps.cut_verified_version(
            "11111111-1111-1111-1111-111111111111",
            trigger="finish",
            pin=True,
        )
        assert self._committed is not None

    def project_store(self) -> ProjectStore:
        return self._ps

    def workspace_lock(self, _conversation_id: str) -> asyncio.Lock:
        return self._lock

    async def require_committed_host_mirror_locked(self, conversation_id: str):  # noqa: ANN202
        assert self._lock.locked()
        facts = self._ps.inspect_workspace(conversation_id)
        if (
            facts.file_count != self._committed.file_count
            or facts.total_bytes != self._committed.total_bytes
            or facts.tree_digest != self._committed.tree_digest
        ):
            raise WorkspaceCommitUnavailable("test host mirror drifted")
        return self._committed

    async def record_workspace_mutation_locked(
        self,
        _conversation_id: str,
        operation: str,
        *,
        paths: tuple[str, ...] = (),
    ) -> None:
        assert self._lock.locked()
        self.mutations.append((operation, paths))

    async def finalize_host_mirror_change_locked(
        self,
        conversation_id: str,
        _operation: str,
    ):  # noqa: ANN202
        assert self._lock.locked()
        committed = self._ps.cut_verified_version(
            conversation_id,
            trigger="finish",
            pin=True,
        )
        assert committed is not None
        self._committed = committed
        return committed


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
    runtime_factory=None,
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
            _FakeRuntime(ps) if runtime_factory is None else runtime_factory(ps),
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
    runtime_factory=None,
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
        runtime_factory=runtime_factory,
        cors=("strict" if with_cors else cors),
    )
    headers = dict(_OWNER_HEADERS) if owner_auth else {}
    return _RouteTestClient(app, headers=headers), ps, cid


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


# ---- A3: assets.directory exact-resolve (CORR-13) ---------------------------


def _ws_with_toml(tmp_path: Path, directory: str) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "wrangler.toml").write_text(
        f'name = "w"\n[assets]\ndirectory = "{directory}"\n', encoding="utf-8"
    )
    return ws


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


# ---- WAVE 3 SEC-4: worker AUTH-semantics verification before a real deploy ----


def _corrupt_worker(workspace: Path, transform) -> None:  # noqa: ANN001
    """Rewrite worker/index.ts via *transform* (kept export-ready-passing — the structural
    auth check is what we're exercising, not export readiness)."""
    p = workspace / "worker" / "index.ts"
    p.write_text(transform(p.read_text(encoding="utf-8")), encoding="utf-8")


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

"""AppKit EPIC G — `verify_appkit_app` strict verifier + the pure Worker/D1 shim.

The pure core shim (`check_schema_sql`, `local_api_roundtrip`) and the static
inspectors (`inspect_worker`, `inspect_lead_form`) are tested directly. The full
`run()` is exercised against a fake sandbox holding a REAL generated
editorial-ledger app, with the browser stubbed (no Playwright daemon): the
known-good app passes ALL nine checks (incl. the Epic I cloudflare_export_ready
gate), and each broken variant fails the RIGHT check while leaving the others green.
"""

import hashlib
import json
import re
import shlex

import pytest
from disco.core.appkit import (
    CF_EXPORT_FILES,
    WorkerAuthModel,
    check_drizzle_schema,
    check_schema_sql,
    cloudflare_export_ready,
    default_lead_gen_app_spec,
    ensure_lead_entity,
    generate,
    get_recipe,
    local_api_roundtrip,
    resolve_lead_entity,
    serialize_app_spec,
    serialize_design_spec,
)
from disco.tools.anatomy import ToolContext, ToolOutcome
from disco.tools.builtin import browser as browser_mod
from disco.tools.builtin import preview as preview_mod
from disco.tools.builtin.verify_appkit_app import (
    _VITE_PREVIEW_COMMAND,
    VerifyAppKitAppArgs,
    VerifyAppKitAppTool,
    WorkerAuthVerdict,
    _is_vite_app_tree,
    _served_preview_uses_built_bundle,
    inspect_lead_form,
    inspect_submit_support,
    inspect_worker,
)

# ---- build a real generated app ------------------------------------------------


def _build_tree() -> tuple[dict[str, bytes], object]:
    recipe = get_recipe("editorial-ledger")
    app = ensure_lead_entity(default_lead_gen_app_spec("Acme Leads", recipe))
    design = recipe.to_design_spec()
    tree = {p: c.encode("utf-8") for p, c in generate(app, design).items()}
    tree[".disco/appspec.json"] = serialize_app_spec(app).encode("utf-8")
    tree[".disco/designspec.json"] = serialize_design_spec(design).encode("utf-8")
    return tree, app


def _lead():
    _, app = _build_tree()
    return resolve_lead_entity(app)


# ---- fake sandbox --------------------------------------------------------------


class _ExecRes:
    def __init__(self, stdout: str, exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = ""
        self.timed_out = False


class FakeSandbox:
    """In-memory workspace. `exec_shell` answers the design_lint `wc -c` size probe
    and the verify_web_app http probe; file IO is dict-backed."""

    def __init__(self, files: dict[str, bytes]):
        self._files = dict(files)

    async def file_exists(self, path: str) -> bool:
        return path in self._files

    async def read_file(self, path: str) -> bytes:
        return self._files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data

    async def list_dir(self, rel: str) -> list[str]:
        rel = "" if rel in (".", "", "./") else rel.rstrip("/")
        prefix = "" if rel == "" else rel + "/"
        children: set[str] = set()
        matched = False
        for path in self._files:
            if not path.startswith(prefix):
                continue
            matched = True
            remainder = path[len(prefix):]
            children.add(remainder.split("/", 1)[0] if "/" in remainder else remainder)
        if not matched and rel != "":
            raise FileNotFoundError(rel)  # a leaf file probed as a dir → not a dir
        return sorted(children)

    async def exec_shell(self, cmd: str, timeout_s=None):
        if cmd.startswith("wc -c <"):
            path = shlex.split(cmd)[3]  # wc -c < <path>
            data = self._files.get(path)
            if data is None:
                return _ExecRes("", exit_code=1)
            return _ExecRes(str(len(data)))
        if "import json, urllib.request as U" in cmd:
            return _ExecRes(
                json.dumps(
                    {
                        "status": 200,
                        "content_type": "text/html",
                        "body": '<script type="module" src="/assets/index-abcd1234.js"></script>',
                    }
                )
            )
        # the verify_web_app http probe (python3 -c ...) → server up
        return _ExecRes("200")


class BuildTrackingSandbox(FakeSandbox):
    def __init__(
        self,
        files: dict[str, bytes],
        *,
        served_index: str | None = None,
        fail_build: bool = False,
    ):
        super().__init__(files)
        self.served_index = served_index
        self.fail_build = fail_build
        self.commands: list[str] = []

    async def exec_shell(self, cmd: str, timeout_s=None):
        self.commands.append(cmd)
        if "import json, urllib.request as U" in cmd and self.served_index is not None:
            return _ExecRes(
                json.dumps(
                    {
                        "status": 200,
                        "content_type": "text/html",
                        "body": self.served_index,
                    }
                )
            )
        if cmd in ("npm ci --no-audit --no-fund", "npm install --no-audit --no-fund"):
            return _ExecRes("installed")
        if cmd == "npm run build":
            if self.fail_build:
                res = _ExecRes("", exit_code=1)
                res.stderr = "vite build failed\nsrc/App.tsx: boom"
                return res
            return _ExecRes("built")
        return await super().exec_shell(cmd, timeout_s=timeout_s)


class _PreviewStatus:
    def __init__(self, value: str):
        self.value = value


class BuiltPreviewManager:
    def __init__(self, *, port: int = 9134):
        self.port = port
        self.starts: list[dict] = []
        self.stops: list[str] = []

    async def start(self, **kwargs):
        self.starts.append(kwargs)
        return type(
            "PreviewSession",
            (),
            {
                "status": _PreviewStatus("running"),
                "port": self.port,
                "detail": "",
            },
        )()

    async def stop(self, name: str):
        self.stops.append(name)


def _ctx(sandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="o",
        conversation_id="c",
    )


@pytest.fixture
def stub_browser(monkeypatch):
    """Stub BrowserTool.run (covers both verify_web_app's browser + verify_appkit's
    direct navigate, same class). Configurable per test via the returned setter."""
    state = {
        "console": [],
        "markers": ["hero", "features", "contact", "footer"],
        "urls": [],
    }

    async def fake_run(self, args, ctx):
        state["urls"].append(args.url)
        return ToolOutcome(
            success=True,
            content="browsed",
            structured={
                "url": args.url,
                "title": "Acme Leads",
                "text": "Welcome to Acme Leads — get in touch.",
                "elements": ["1[:] <a>Get started</a>"],
                "console": state["console"],
                "network": [],
                "appkit_sections": state["markers"],
                "screenshot_path": ".pmx/screenshots/0001-navigate.png",
            },
        )

    monkeypatch.setattr(browser_mod.BrowserTool, "run", fake_run)
    return state


def _checks_by_name(verdict: dict) -> dict[str, dict]:
    return {c["name"]: c for c in verdict["checks"]}


# ============================ PURE CORE SHIM ===================================


def test_check_schema_sql_round_trips_and_enforces_not_null():
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    res = check_schema_sql(tree["schema.sql"].decode(), lead)
    assert res.passed, res.evidence
    assert "reject NULL" in res.evidence


def test_check_schema_sql_rejects_broken_sql():
    res = check_schema_sql("CREATE TABLE oops (", _lead())
    assert not res.passed


def test_check_schema_sql_flags_missing_not_null():
    # a schema whose required column lacks NOT NULL must be caught
    bad = (
        'CREATE TABLE "leads" ('
        ' "id" INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' "name" TEXT, "email" TEXT, "message" TEXT );'
    )
    res = check_schema_sql(bad, _lead())
    assert not res.passed
    assert "NULL" in res.evidence


def test_check_drizzle_schema_passes_on_generated_tree():
    tree, _ = _build_tree()
    res = check_drizzle_schema(tree)
    assert res.passed, res.evidence


def test_check_drizzle_schema_fails_when_column_missing():
    tree, _ = _build_tree()
    schema_ts = tree["src/db/schema.ts"].decode().replace(
        '  message: text("message"),\n', ""
    )
    tree["src/db/schema.ts"] = schema_ts.encode()
    res = check_drizzle_schema(tree)
    assert not res.passed
    assert "columns" in res.evidence


def test_check_drizzle_schema_fails_when_drizzle_orm_missing():
    tree, _ = _build_tree()
    pkg = json.loads(tree["package.json"].decode())
    del pkg["dependencies"]["drizzle-orm"]
    tree["package.json"] = json.dumps(pkg).encode()
    res = check_drizzle_schema(tree)
    assert not res.passed
    assert "drizzle-orm" in res.evidence


def test_local_api_roundtrip_passes_with_full_contract():
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    auth = WorkerAuthModel(
        reads_require_auth=True, fail_closed_without_token=True, insert_parameterized=True
    )
    res = local_api_roundtrip(tree["schema.sql"].decode(), lead, auth)
    assert res.passed, res.evidence


def test_local_api_roundtrip_fails_when_reads_unguarded():
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    auth = WorkerAuthModel(
        reads_require_auth=False, fail_closed_without_token=True, insert_parameterized=True
    )
    res = local_api_roundtrip(tree["schema.sql"].decode(), lead, auth)
    assert not res.passed
    assert "UNauthenticated" in res.evidence


def test_local_api_roundtrip_fails_when_not_fail_closed():
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    auth = WorkerAuthModel(
        reads_require_auth=True, fail_closed_without_token=False, insert_parameterized=True
    )
    res = local_api_roundtrip(tree["schema.sql"].decode(), lead, auth)
    assert not res.passed
    assert "fail closed" in res.evidence


# ============================ STATIC INSPECTORS ================================


def test_inspect_worker_known_good():
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    post_ok, model, reasons = inspect_worker(tree["worker/index.ts"].decode(), lead)
    assert post_ok and not reasons, reasons
    assert model.reads_require_auth and model.fail_closed_without_token
    assert model.insert_parameterized


def test_inspect_worker_detects_dropped_admin_guard():
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    # Remove the /admin auth guard only (leave GET /api/leads gated).
    broken = src.replace(
        "    if (url.pathname === \"/admin\" && request.method === \"GET\") {\n"
        "      if (!isAuthorized(request, env)) {\n"
        "        return adminLoginPage();\n"
        "      }\n",
        "    if (url.pathname === \"/admin\" && request.method === \"GET\") {\n",
    )
    assert broken != src
    post_ok, model, reasons = inspect_worker(broken, lead)
    assert not model.reads_require_auth
    assert any("/admin" in r for r in reasons)


def test_inspect_lead_form_known_good_and_broken_path():
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    form = tree["src/components/HomeContactSection.tsx"].decode()
    ok, reasons = inspect_lead_form(form, lead)
    assert ok, reasons
    broken = form.replace('useSubmit("/api/leads")', 'useSubmit("/api/wrong")')
    ok2, reasons2 = inspect_lead_form(broken, lead)
    assert not ok2 and any("/api/leads" in r for r in reasons2)


# ====================== PLATFORM VITE BUILD PREP ===============================


def test_vite_tree_detection_requires_vite_devdep_and_config():
    tree, _ = _build_tree()
    package_json = tree["package.json"].decode()
    assert _is_vite_app_tree(package_json, has_vite_config=True) is True
    assert _is_vite_app_tree(package_json, has_vite_config=False) is False

    pkg = json.loads(package_json)
    del pkg["devDependencies"]["vite"]
    assert _is_vite_app_tree(json.dumps(pkg), has_vite_config=True) is False
    assert _is_vite_app_tree("{not json", has_vite_config=True) is False


def test_served_preview_detection_distinguishes_source_from_built_vite():
    assert (
        _served_preview_uses_built_bundle(
            '<script type="module" src="/src/main.tsx"></script>'
        )
        is False
    )
    assert (
        _served_preview_uses_built_bundle(
            '<script type="module" crossorigin src="/assets/index-abcd1234.js"></script>'
        )
        is True
    )
    assert _served_preview_uses_built_bundle("<h1>not vite</h1>") is None


@pytest.mark.asyncio
async def test_vite_build_cache_skips_npm_ci_when_package_unchanged():
    tree, _ = _build_tree()
    package_json = tree["package.json"].decode()
    package_sha = hashlib.sha256(package_json.encode("utf-8")).hexdigest()
    tree["node_modules/.package-lock.json"] = b"{}"
    tree[".disco/appkit-vite-package.sha256"] = (package_sha + "\n").encode()

    sandbox = BuildTrackingSandbox(tree)
    result = await VerifyAppKitAppTool()._ensure_vite_platform_build(
        _ctx(sandbox), package_json
    )

    assert result.ok, result.evidence
    assert "npm ci --no-audit --no-fund" not in sandbox.commands
    assert "npm run build" in sandbox.commands


@pytest.mark.asyncio
async def test_vite_build_failure_fails_route_and_section_with_stderr(stub_browser):
    tree, _ = _build_tree()
    sandbox = BuildTrackingSandbox(
        tree,
        served_index='<script type="module" src="/src/main.tsx"></script>',
        fail_build=True,
    )

    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(sandbox)
    )

    assert out.success and out.structured is not None
    checks = _checks_by_name(out.structured)
    assert checks["route_coverage"]["passed"] is False
    assert checks["section_coverage"]["passed"] is False
    assert "src/App.tsx: boom" in checks["route_coverage"]["evidence"]
    assert "npm ci --no-audit --no-fund" in sandbox.commands
    assert "npm run build" in sandbox.commands


@pytest.mark.asyncio
async def test_vite_source_preview_builds_and_serves_compiled_app(monkeypatch, stub_browser):
    tree, _ = _build_tree()
    sandbox = BuildTrackingSandbox(
        tree,
        served_index='<script type="module" src="/src/main.tsx"></script>',
    )
    manager = BuiltPreviewManager(port=9134)
    monkeypatch.setattr(preview_mod, "_manager", lambda ctx: manager)

    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(sandbox)
    )

    assert out.success and out.structured is not None
    assert out.structured["passed"] is True, out.structured["summary"]
    assert "npm ci --no-audit --no-fund" in sandbox.commands
    assert "npm run build" in sandbox.commands
    assert manager.starts == [
        {
            "command": _VITE_PREVIEW_COMMAND,
            "name": "appkit-built-vite",
            "supervise": False,
        }
    ]
    assert manager.stops == ["appkit-built-vite"]
    assert stub_browser["urls"]
    assert all(
        url == "http://127.0.0.1:9134"
        or url.startswith("http://127.0.0.1:9134/")
        for url in stub_browser["urls"]
    )


@pytest.mark.asyncio
async def test_vite_build_refuses_a_tree_without_a_lockfile():
    tree, _ = _build_tree()
    tree.pop("package-lock.json")
    sandbox = BuildTrackingSandbox(tree)

    result = await VerifyAppKitAppTool()._ensure_vite_platform_build(
        _ctx(sandbox), tree["package.json"].decode()
    )

    assert result.ok is False
    assert "missing package-lock.json" in result.evidence
    assert "npm ci --no-audit --no-fund" not in sandbox.commands
    assert "npm install --no-audit --no-fund" not in sandbox.commands


# ============================ FULL TOOL RUN ===================================


@pytest.mark.asyncio
async def test_known_good_passes_all_checks(stub_browser):
    tree, _ = _build_tree()
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    assert out.success and out.structured is not None
    v = out.structured
    assert v["passed"] is True, v["summary"]
    assert v["verdict"] == "pass"
    names = set(_checks_by_name(v))
    assert names == {
        "design_lint_clean",
        "schema_sql_valid",
        "drizzle_schema_valid",
        "worker_contract",
        "lead_form_posts",
        "local_api_roundtrip",
        "cloudflare_export_ready",
        "route_coverage",
        "section_coverage",
    }
    assert all(c["passed"] for c in v["checks"])
    # W-45 compatible: embedded verify_web_app verdict + top-level url/fingerprint
    assert "verify_web_app" in v and v["url"]
    assert v["failure_fingerprint"]


@pytest.mark.asyncio
async def test_broken_design_fails_design_lint(stub_browser):
    tree, _ = _build_tree()
    css = tree["src/styles.css"].decode().replace(
        "--color-primary:", "--color-primary: #7c3aed; --bad:"
    )
    tree["src/styles.css"] = css.encode()
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["design_lint_clean"]["passed"] is False
    # the others stay green
    assert checks["schema_sql_valid"]["passed"] is True
    assert checks["worker_contract"]["passed"] is True


@pytest.mark.asyncio
async def test_missing_section_marker_fails_section_coverage(stub_browser):
    tree, _ = _build_tree()
    stub_browser["markers"] = ["hero", "features", "contact"]  # footer never rendered
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["section_coverage"]["passed"] is False
    assert "footer" in checks["section_coverage"]["evidence"]
    assert checks["route_coverage"]["passed"] is True


@pytest.mark.asyncio
async def test_route_console_error_fails_route_coverage(stub_browser):
    tree, _ = _build_tree()
    stub_browser["console"] = [{"level": "error", "text": "Uncaught TypeError: boom"}]
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["route_coverage"]["passed"] is False


@pytest.mark.asyncio
async def test_form_wrong_submit_path_fails_lead_form(stub_browser):
    tree, _ = _build_tree()
    form = tree["src/components/HomeContactSection.tsx"].decode().replace(
        'useSubmit("/api/leads")', 'useSubmit("/api/wrong")'
    )
    tree["src/components/HomeContactSection.tsx"] = form.encode()
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    # no form submits to /api/leads anymore → lead_form_posts fails
    assert checks["lead_form_posts"]["passed"] is False


@pytest.mark.asyncio
async def test_dropped_admin_guard_fails_worker_and_roundtrip(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "    if (url.pathname === \"/admin\" && request.method === \"GET\") {\n"
        "      if (!isAuthorized(request, env)) {\n"
        "        return adminLoginPage();\n"
        "      }\n",
        "    if (url.pathname === \"/admin\" && request.method === \"GET\") {\n",
    )
    assert broken != src
    tree["worker/index.ts"] = broken.encode()
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False


@pytest.mark.asyncio
async def test_removed_insert_fails_worker_contract(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    # Neuter the Drizzle insert entirely.
    broken = src.replace(
        "db.insert(leads).values(leadValues(rec)).run()",
        "db.select().from(leads).all()",
    )
    assert broken != src
    tree["worker/index.ts"] = broken.encode()
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["worker_contract"]["passed"] is False


@pytest.mark.asyncio
async def test_admin_token_unset_fail_closed_proven(stub_browser):
    # The known-good worker fails closed; remove that and the roundtrip catches it.
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    broken = src.replace("if (!expected) return false;", "if (!expected) return true;")
    assert broken != src
    tree["worker/index.ts"] = broken.encode()
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False
    assert "fail closed" in checks["local_api_roundtrip"]["evidence"]


# ===== ADVERSARIAL: NO FALSE-PASS on a structurally-present-but-broken worker =====
#
# The pre-fix verifier proved the contract with LOOSE substrings (route text /
# `isAuthorized` merely APPEARING in a block / `.bind` anywhere), so these mutations
# FALSELY PASSED. Each must now FAIL the RIGHT check — a false-PASS verifier is the
# worst failure mode (false confidence in a broken/insecure generated app).


async def _run(tree):
    return await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )


def test_inspect_worker_admin_returns_200_unauthenticated_fails():
    # codex's exact mutation: adminLoginPage status 401 -> 200 (admin served to an
    # unauthenticated caller). The guard "appears" but the denial is no longer a 401.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace("status: 401", "status: 200")
    assert broken != src
    _ok, model, reasons = inspect_worker(broken, lead)
    assert not model.reads_require_auth  # the shim now models the REAL (open) gating
    assert any("/admin" in r for r in reasons)


@pytest.mark.asyncio
async def test_admin_returns_200_unauth_fails_worker_and_roundtrip(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace("status: 401", "status: 200").encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False


@pytest.mark.asyncio
async def test_auth_guard_result_ignored_fails(stub_browser):
    # The guard CALLS isAuthorized but ignores the result (no early return) and reads
    # the leads regardless. `isAuthorized` still APPEARS in the block — loose-substring
    # checks passed; control-flow parsing must FAIL it.
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "      if (!isAuthorized(request, env)) {\n"
        "        return adminLoginPage();\n"
        "      }\n",
        "      const _ok = isAuthorized(request, env);\n",
    )
    assert broken != src
    tree["worker/index.ts"] = broken.encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False


@pytest.mark.asyncio
async def test_get_leads_returns_200_instead_of_401_fails(stub_browser):
    # codex's status:401 -> 200 on GET /api/leads: the auth guard early-returns, but
    # with a 200 (leads still served). Must FAIL.
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        'return json({ error: "unauthorized" }, 401);',
        'return json({ error: "unauthorized" }, 200);',
    )
    assert broken != src
    tree["worker/index.ts"] = broken.encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False


def test_inspect_worker_raw_sql_insert_fails():
    # A raw SQL INSERT is no longer the ratified generated data plane. Even a
    # parameterized `.prepare(...).bind(...).run()` insert must fail in favor of
    # Drizzle's typed table insert.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "db.insert(leads).values(leadValues(rec)).run()",
        'env.DB.prepare("INSERT INTO \\"leads\\" (\\"name\\") VALUES (?)")'
        '.bind(rec["name"]).run()',
    )
    assert broken != src
    post_ok, model, reasons = inspect_worker(broken, lead)
    assert not post_ok
    assert not model.insert_parameterized
    assert any("Drizzle" in r for r in reasons)


@pytest.mark.asyncio
async def test_raw_sql_insert_fails_worker_contract(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace(
        "db.insert(leads).values(leadValues(rec)).run()",
        'env.DB.prepare("INSERT INTO \\"leads\\" (\\"name\\") VALUES (?)")'
        '.bind(rec["name"]).run()',
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert checks["worker_contract"]["passed"] is False


def test_inspect_lead_form_commented_submit_fails():
    # A useSubmit call hidden in a COMMENT is not live code — substring presence
    # passed; the comment-stripping control-flow check must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    form = tree["src/components/HomeContactSection.tsx"].decode()
    broken = form.replace(
        'useSubmit("/api/leads")',
        'useSubmit("/api/wrong") // useSubmit("/api/leads")',
    )
    assert broken != form
    ok, reasons = inspect_lead_form(broken, lead)
    assert not ok
    assert any("/api/leads" in r for r in reasons)


def test_inspect_lead_form_unbound_field_fails():
    # The email input exists (name="email") but its onChange no longer writes email
    # into form state — the field is not actually bound. Must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    form = tree["src/components/HomeContactSection.tsx"].decode()
    broken = form.replace(
        'updateField("email", e.target.value)',
        'updateField("other", e.target.value)',
    )
    assert broken != form
    ok, reasons = inspect_lead_form(broken, lead)
    assert not ok
    assert any("email" in r and "bound" in r for r in reasons)


def test_inspect_submit_support_field_body_dropped_fails():
    # The API client must serialize the submitted body object. A partial/hardcoded
    # body can silently drop fields, so the support-file inspection must FAIL.
    tree, app = _build_tree()
    _ = resolve_lead_entity(app)
    client = tree["src/api/client.ts"].decode()
    hook = tree["src/hooks/useSubmit.ts"].decode()
    broken = client.replace(
        "body: JSON.stringify(body)", 'body: JSON.stringify({ name: "Ada" })'
    )
    assert broken != client
    ok, reasons = inspect_submit_support(broken, hook)
    assert not ok
    assert any("body" in r or "serialize" in r for r in reasons)


@pytest.mark.asyncio
async def test_unbound_form_field_fails_lead_form_check(stub_browser):
    tree, _ = _build_tree()
    form = tree["src/components/HomeContactSection.tsx"].decode()
    tree["src/components/HomeContactSection.tsx"] = form.replace(
        'updateField("email", e.target.value)', 'updateField("other", e.target.value)'
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["lead_form_posts"]["passed"] is False


# ===== RESIDUAL FALSE-PASSES (codex, post-f9ab9b1): region-scoped control flow =====
#
# After the first tightening, three proofs were still TOKEN-PRESENCE, not region-scoped,
# so a broken/insecure generated worker/form still PASSED:
#   1. the Drizzle insert was checked GLOBALLY, then the POST route accepted
#      separately → a POST that returns before/without the insert passed because an
#      insert existed ELSEWHERE.
#   2. the 401 proof accepted any return+denial text in the guard body → a denial
#      buried behind a dead inner condition (`if (false) return ..., 401`) passed while
#      unauthenticated callers fell through to the read.
#   3. the form submit proof matched a preserved STRING → an inert string literal
#      containing `useSubmit("/api/leads")` passed while the real submit used a wrong path.
# Each codex mutation must now FAIL the RIGHT check.

_POST_BODY = (
    "      let body: unknown;\n"
    "      try {\n"
    "        body = await request.json();\n"
    "      } catch {\n"
    '        return json({ error: "invalid JSON" }, 400);\n'
    "      }\n"
    "      return insertLead(env, body);"
)


def test_inspect_worker_post_returns_without_reaching_insert_fails():
    # (1) The POST handler short-circuits to `return json({ ok: true })`; the
    # Drizzle insert still EXISTS in insertLead() but the POST route never
    # reaches it. The old GLOBAL insert check false-passed; region-scoping must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(_POST_BODY, "      return json({ ok: true }, 201);")
    assert broken != src
    assert "db.insert(leads)" in broken  # the insert is still present elsewhere in the file
    post_ok, model, reasons = inspect_worker(broken, lead)
    assert not post_ok
    assert not model.post_region_has_insert
    assert any("reach" in r and "insert" in r for r in reasons)


@pytest.mark.asyncio
async def test_post_returns_without_insert_fails_worker_and_roundtrip(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace(
        _POST_BODY, "      return json({ ok: true }, 201);"
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False
    assert "reachable" in checks["local_api_roundtrip"]["evidence"]


def test_inspect_worker_dead_401_guard_fails():
    # (2) The guard condition is the REAL auth check, but the 401 denial is buried
    # behind a dead `if (false)` — an unauthenticated caller falls through to the read.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        '        return json({ error: "unauthorized" }, 401);\n',
        '        if (false) return json({ error: "unauthorized" }, 401);\n',
    )
    assert broken != src
    _ok, model, reasons = inspect_worker(broken, lead)
    assert not model.reads_require_auth  # the read is no longer truly gated
    assert any("GET /api/leads" in r for r in reasons)


@pytest.mark.asyncio
async def test_dead_401_guard_fails_worker_and_roundtrip(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace(
        '        return json({ error: "unauthorized" }, 401);\n',
        '        if (false) return json({ error: "unauthorized" }, 401);\n',
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False


def test_inspect_lead_form_inert_string_submit_fails():
    # (3) The real submit hook call is gone; an inert TEMPLATE-LITERAL string still
    # contains `useSubmit("/api/leads")`. The old substring check false-passed;
    # requiring the call in CALL POSITION must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    form = tree["src/components/HomeContactSection.tsx"].decode()
    broken = form.replace(
        'useSubmit("/api/leads")',
        'useSubmit("/api/wrong"); '
        'const note = `see useSubmit("/api/leads") for docs`',
    )
    assert broken != form
    assert 'useSubmit("/api/leads")' in broken  # the inert string is present
    ok, reasons = inspect_lead_form(broken, lead)
    assert not ok
    assert any("/api/leads" in r for r in reasons)


@pytest.mark.asyncio
async def test_inert_string_submit_fails_lead_form(stub_browser):
    tree, _ = _build_tree()
    form = tree["src/components/HomeContactSection.tsx"].decode()
    tree["src/components/HomeContactSection.tsx"] = form.replace(
        'useSubmit("/api/leads")',
        'useSubmit("/api/wrong"); '
        'const note = `see useSubmit("/api/leads") for docs`',
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["lead_form_posts"]["passed"] is False


def test_inspect_worker_known_good_flags_all_true_after_tightening():
    # Regression guard: the REAL generated worker still satisfies every tightened
    # control-flow check (no over-tightening that fails the known-good app).
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    post_ok, model, reasons = inspect_worker(tree["worker/index.ts"].decode(), lead)
    assert post_ok and not reasons, reasons
    assert model.reads_require_auth
    assert model.fail_closed_without_token
    assert model.insert_parameterized


# ===== DEAD/UNREACHABLE INSERT POSITION (Epic G P1.3): close the concrete dead cases =====
#
# Region-scoping proves the Drizzle insert is PRESENT in the POST region — but an
# insert sitting AFTER an unconditional early return, or inside an `if (false)`/`if (0)`
# branch, can never run even though it is in-region. Static regex cannot SOUNDLY prove
# runtime reachability, so the verifier no longer CLAIMS it does (wording is structural);
# but these two demonstrated dead positions ARE rejected so they cannot structurally pass.


def test_inspect_worker_insert_after_early_return_fails():
    # An unconditional `return` precedes the insert in its enclosing (try) block, so the
    # insert is in-region but never reached. worker_contract must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "  try {\n    const db = drizzle(env.DB);\n",
        "  try {\n    return json({ ok: true }, 201);\n    const db = drizzle(env.DB);\n",
    )
    assert broken != src
    assert "db.insert(leads)" in broken  # the insert is still structurally present
    post_ok, model, reasons = inspect_worker(broken, lead)
    assert not post_ok
    assert not model.post_region_has_insert
    assert any("reach" in r and "insert" in r for r in reasons)


@pytest.mark.asyncio
async def test_insert_after_early_return_fails_worker_and_roundtrip(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace(
        "  try {\n    const db = drizzle(env.DB);\n",
        "  try {\n    return json({ ok: true }, 201);\n    const db = drizzle(env.DB);\n",
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False


def test_inspect_worker_insert_in_dead_branch_fails():
    # The insert statement is wrapped in a constant-false branch `if (false) <insert>;`,
    # so it can never run. worker_contract must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "    await db.insert(leads)",
        "    if (false)\n    await db.insert(leads)",
    )
    assert broken != src
    assert "db.insert(leads)" in broken  # still present, just dead
    post_ok, model, reasons = inspect_worker(broken, lead)
    assert not post_ok
    assert not model.post_region_has_insert
    assert any("reach" in r and "insert" in r for r in reasons)


@pytest.mark.asyncio
async def test_insert_in_dead_branch_fails_worker_and_roundtrip(stub_browser):
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace(
        "    await db.insert(leads)",
        "    if (false)\n    await db.insert(leads)",
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert checks["local_api_roundtrip"]["passed"] is False


# ===== HONEST SCOPING: no runtime overclaim in any verdict/evidence string =====


@pytest.mark.asyncio
async def test_verdict_makes_no_runtime_overclaim(stub_browser):
    # Every claim the PASSING verifier makes must be STRUCTURAL — it must NOT assert the
    # app "works", "reaches" the insert, or is "verified working" (those are runtime
    # claims static inspection cannot make; runtime behaviour is Epic I's scope).
    tree, _ = _build_tree()
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(FakeSandbox(tree))
    )
    v = out.structured
    assert v["passed"] is True, v["summary"]
    text = " ".join(
        [v["summary"], str(v["next_action"]), out.content]
        + [c["evidence"] for c in v["checks"]]
    ).lower()
    for overclaim in ("reaches", "verified working", " works"):
        assert overclaim not in text, f"runtime overclaim {overclaim!r} in: {text}"
    # ...and it DOES say what it actually proved: structure.
    assert "structur" in v["summary"].lower()


# ===================== EPIC I — cloudflare_export_ready ========================
#
# The pure config/doc-completeness gate (no deploy, no network) + the full-tool wiring.
# A known-good generated tree passes; each missing/broken export deliverable fails the
# RIGHT check (cloudflare_export_ready) while leaving the others green.


def _cf_files(tree: dict[str, bytes]) -> dict[str, str | None]:
    """The export-deliverable text map the verifier feeds cloudflare_export_ready."""
    return {rel: tree[rel].decode() if rel in tree else None for rel in CF_EXPORT_FILES}


def test_cloudflare_export_ready_passes_on_generated_tree():
    tree, _ = _build_tree()
    res = cloudflare_export_ready(_cf_files(tree))
    assert res.passed, res.evidence
    assert "OWNER_GUIDE" in res.evidence or "deploy" in res.evidence.lower()


def test_cloudflare_export_ready_emits_owner_guide_and_secret_template():
    # Epic I deliverables actually generated, with the documented deploy steps.
    tree, _ = _build_tree()
    assert "OWNER_GUIDE.md" in tree and ".dev.vars.example" in tree
    guide = tree["OWNER_GUIDE.md"].decode()
    for step in ("wrangler d1 create", "wrangler secret put ADMIN_TOKEN", "wrangler deploy"):
        assert step in guide
    dev_vars = tree[".dev.vars.example"].decode()
    assert "ADMIN_TOKEN" in dev_vars and "replace-me" in dev_vars


def test_cloudflare_export_ready_fails_when_owner_guide_missing():
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["OWNER_GUIDE.md"] = None
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "OWNER_GUIDE.md" in res.evidence


def test_cloudflare_export_ready_fails_on_missing_d1_binding():
    tree, _ = _build_tree()
    files = _cf_files(tree)
    # Drop the DB binding from wrangler.toml's d1 entry.
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'binding = "DB"', 'binding = "OTHER"'
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "DB" in res.evidence


def test_cloudflare_export_ready_fails_when_spa_not_worker_first():
    # SPA not_found_handling without run_worker_first for /api/* + /admin would let the
    # asset layer shadow the worker routes — must fail.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'run_worker_first = ["/api/*", "/admin"]\n', ""
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "run_worker_first" in res.evidence or "worker-first" in res.evidence


def test_cloudflare_export_ready_fails_when_real_dev_vars_present():
    # A real .dev.vars secret file must never ship in the export.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars"] = "ADMIN_TOKEN=super-secret-real-value\n"
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert ".dev.vars" in res.evidence


def test_cloudflare_export_ready_fails_on_invalid_toml():
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = "name = \"x\"\n[assets\n"  # malformed
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "TOML" in res.evidence


# ---- P1-1: completeness — each MISSING piece of the deployable contract FAILS -----


def test_cloudflare_export_ready_fails_when_run_worker_first_missing():
    # The Epic I routing fix: without run_worker_first the SPA shadows /api/* + /admin.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'run_worker_first = ["/api/*", "/admin"]\n', ""
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "run_worker_first" in res.evidence or "worker-first" in res.evidence


def test_cloudflare_export_ready_fails_when_run_worker_first_incomplete():
    # Covers /api/* but NOT /admin — the admin read-back would still be shadowed.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'run_worker_first = ["/api/*", "/admin"]', 'run_worker_first = ["/api/*"]'
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "/admin" in res.evidence or "run_worker_first" in res.evidence


def test_cloudflare_export_ready_fails_when_d1_database_name_empty():
    # DB binding present but database_name empty → the Worker can't name its database.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    wt = files["wrangler.toml"] or ""
    assert 'database_name = "' in wt
    files["wrangler.toml"] = re.sub(
        r'database_name = "[^"]*"', 'database_name = ""', wt
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "database_name" in res.evidence


def test_cloudflare_export_ready_fails_when_d1_database_name_missing():
    # The database_name line dropped entirely → still must fail.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = re.sub(
        r'database_name = "[^"]*"\n', "", files["wrangler.toml"] or ""
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "database_name" in res.evidence


def test_cloudflare_export_ready_fails_when_not_found_handling_missing():
    # Without the SPA fallback, client routes 404 instead of resolving to index.html.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'not_found_handling = "single-page-application"\n', ""
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "not_found_handling" in res.evidence


def test_cloudflare_export_ready_fails_when_main_not_worker():
    # main must point at the generated worker, else /api/* + /admin never reach it.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'main = "worker/index.ts"', 'main = "src/main.tsx"'
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "main" in res.evidence


@pytest.mark.parametrize(
    "bad_main",
    [
        "....worker/index.ts",  # SEC-1-class: lstrip("./") would COLLAPSE to worker/index.ts,
        # but wrangler resolves a DIFFERENT dir literally named `....worker`.
        "./....worker/index.ts",
        "/etc/passwd",  # absolute path
        "../evil.ts",  # parent escape
        "other/index.ts",  # a different in-tree worker
        "worker/evil.ts",  # right dir, wrong file
        "worker",  # missing the file segment
    ],
)
def test_cloudflare_export_ready_refuses_non_canonical_main(bad_main: str):
    # SEC-1-class main-redirect bypass: the canonical-worker gate validates
    # worker/index.ts, but `wrangler deploy` runs whatever `main` names. A non-canonical
    # main (esp. the `....worker/index.ts` look-alike that the old `lstrip("./")` collapsed
    # to the canonical name) must FAIL export-readiness — exact match, no lstrip.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'main = "worker/index.ts"', f'main = "{bad_main}"'
    )
    res = cloudflare_export_ready(files)
    assert not res.passed, f"non-canonical main {bad_main!r} must fail export-readiness"
    assert "main" in res.evidence


@pytest.mark.parametrize("good_main", ["worker/index.ts", "./worker/index.ts"])
def test_cloudflare_export_ready_accepts_canonical_main_forms(good_main: str):
    # The canonical `worker/index.ts` (what the generator emits) and the equivalent
    # `./worker/index.ts` (the SAME file) both PASS — exact-match must not over-reject.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files["wrangler.toml"] = (files["wrangler.toml"] or "").replace(
        'main = "worker/index.ts"', f'main = "{good_main}"'
    )
    res = cloudflare_export_ready(files)
    assert res.passed, res.evidence


# ---- P1-2: secret-safety — the .gitignore + placeholder contract is PROVEN --------


def test_cloudflare_export_ready_fails_when_gitignore_missing():
    # No .gitignore → a real local .dev.vars could be committed.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".gitignore"] = None
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert ".gitignore" in res.evidence


def test_cloudflare_export_ready_fails_when_gitignore_does_not_ignore_dev_vars():
    # .gitignore present but ignores everything EXCEPT .dev.vars → the real secret
    # file could still be committed. (.dev.vars.example must not satisfy the rule.)
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".gitignore"] = "node_modules/\ndist/\n.dev.vars.example\n"
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert ".dev.vars" in res.evidence


def test_cloudflare_export_ready_fails_when_example_carries_real_secret():
    # A real high-entropy ADMIN_TOKEN in the template = a committed secret leak.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars.example"] = "ADMIN_TOKEN=Zk9rT2pQ7xW3mB1nH5vC8sD4fG6yL0aE\n"
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "ADMIN_TOKEN" in res.evidence or "placeholder" in res.evidence.lower()


def test_cloudflare_export_ready_placeholder_token_still_passes():
    # The generated placeholder (replace-me) is NOT flagged as a real secret.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    assert "replace-me" in (files[".dev.vars.example"] or "")
    res = cloudflare_export_ready(files)
    assert res.passed, res.evidence


def test_cloudflare_export_ready_fails_when_real_secret_hidden_after_placeholder():
    # P1.2 — a placeholder ADMIN_TOKEN line FOLLOWED by a second ADMIN_TOKEN carrying a
    # real high-entropy secret must NOT escape: the scan inspects EVERY value, not the
    # first match. (Pre-fix this passed because only the first ADMIN_TOKEN was checked.)
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars.example"] = (
        "ADMIN_TOKEN=replace-me\nADMIN_TOKEN=Zk9rT2pQ7xW3mB1nH5vC8sD4fG6yL0aE\n"
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "ADMIN_TOKEN" in res.evidence or "placeholder" in res.evidence.lower()


def test_cloudflare_export_ready_fails_when_real_secret_on_other_key():
    # A real secret on ANY KEY line (not just ADMIN_TOKEN) must fail — the whole file is
    # candidate secret-bearing, so a leaked API key on a sibling line is caught too.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars.example"] = (
        "ADMIN_TOKEN=replace-me\nSTRIPE_KEY=Zk9rT2pQ7xW3mB1nH5vC8sD4fG6yL0aE\n"
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "STRIPE_KEY" in res.evidence or "secret" in res.evidence.lower()


def test_cloudflare_export_ready_multi_placeholder_template_passes():
    # An all-placeholder template across MULTIPLE lines/keys still passes — only
    # real-looking high-entropy values fail.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars.example"] = (
        "ADMIN_TOKEN=replace-me\nADMIN_TOKEN=changeme\nAPI_KEY=your-api-key-here\n"
    )
    res = cloudflare_export_ready(files)
    assert res.passed, res.evidence


# ---- P1-4: a placeholder MARKER must not short-circuit the secret-entropy check ----


def test_cloudflare_export_ready_fails_when_marker_prefixes_real_secret():
    # P1.4 — a marker (`replace-me-`) spliced onto a REAL 32-char high-entropy secret
    # in the SAME value must NOT be whitelisted: pre-fix `_looks_like_real_secret`
    # early-returned "safe" the instant any marker substring appeared, so the embedded
    # secret escaped. The marker no longer short-circuits the entropy check.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars.example"] = (
        "ADMIN_TOKEN=replace-me-Zk9rT2pQ7xW3mB1nH5vC8sD4fG6yL0aE\n"
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "ADMIN_TOKEN" in res.evidence or "secret" in res.evidence.lower()


def test_cloudflare_export_ready_fails_when_marker_suffixes_real_secret():
    # The embedded secret must be caught wherever the marker sits — a marker SUFFIX
    # (`<secret>-your-token-here`) is just as much a leak as a marker prefix.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars.example"] = (
        "ADMIN_TOKEN=Zk9rT2pQ7xW3mB1nH5vC8sD4fG6yL0aE-your-token-here\n"
    )
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "ADMIN_TOKEN" in res.evidence or "secret" in res.evidence.lower()


def test_cloudflare_export_ready_legit_short_placeholders_still_pass():
    # The fix must NOT flip legit short placeholders: each collapses, after marker +
    # connective-word splitting, to short low-entropy fragments → still safe.
    tree, _ = _build_tree()
    for placeholder in (
        "replace-me",
        "changeme",
        "your-api-key-here",
        "replace_me_with_token",
        "<your-token-here>",
    ):
        files = _cf_files(tree)
        files[".dev.vars.example"] = f"ADMIN_TOKEN={placeholder}\n"
        res = cloudflare_export_ready(files)
        assert res.passed, f"{placeholder!r} should pass: {res.evidence}"


def test_cloudflare_export_ready_fails_on_bare_real_secret_unchanged():
    # Regression guard: a bare marker-free real secret still FAILS (behaviour unchanged
    # by the marker fix).
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".dev.vars.example"] = "ADMIN_TOKEN=Zk9rT2pQ7xW3mB1nH5vC8sD4fG6yL0aE\n"
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert "ADMIN_TOKEN" in res.evidence or "secret" in res.evidence.lower()


# ---- P1-3: secret-safety — .gitignore is evaluated with LAST-MATCH-WINS -----------


def test_cloudflare_export_ready_fails_when_dev_vars_negated_after_ignore():
    # P1.3 — `.dev.vars` then `!.dev.vars`: the trailing negation UN-ignores the file
    # (gitignore last-match-wins), so the real secret would be TRACKED → leak. The
    # secret-safety gate must FAIL (pre-fix this false-passed on the first matching
    # line).
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".gitignore"] = ".dev.vars\n!.dev.vars\n"
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert ".dev.vars" in res.evidence


def test_cloudflare_export_ready_passes_when_ignore_follows_negation():
    # `!.dev.vars` then `.dev.vars`: the LAST matching line ignores → net ignored, so
    # the secret stays out of git. Passes.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".gitignore"] = "!.dev.vars\n.dev.vars\n"
    res = cloudflare_export_ready(files)
    assert res.passed, res.evidence


def test_cloudflare_export_ready_fails_when_glob_ignore_negated():
    # A wildcard ignore (`*.vars`) un-done by a later `!.dev.vars` negation also leaves
    # the secret tracked — last-match-wins across glob + exact patterns.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".gitignore"] = "*.vars\n!.dev.vars\n"
    res = cloudflare_export_ready(files)
    assert not res.passed
    assert ".dev.vars" in res.evidence


def test_cloudflare_export_ready_passes_with_glob_ignore():
    # A wildcard `*.vars` (no later negation) net-ignores `.dev.vars` → passes.
    tree, _ = _build_tree()
    files = _cf_files(tree)
    files[".gitignore"] = "node_modules/\n*.vars\n"
    res = cloudflare_export_ready(files)
    assert res.passed, res.evidence


@pytest.mark.asyncio
async def test_missing_owner_guide_fails_only_export_check(stub_browser):
    # Full-tool wiring: drop OWNER_GUIDE.md → cloudflare_export_ready fails, the other
    # structural checks stay green.
    tree, _ = _build_tree()
    del tree["OWNER_GUIDE.md"]
    out = await _run(tree)
    v = out.structured
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["cloudflare_export_ready"]["passed"] is False
    for other in (
        "schema_sql_valid",
        "drizzle_schema_valid",
        "worker_contract",
        "lead_form_posts",
        "local_api_roundtrip",
    ):
        assert checks[other]["passed"] is True, checks[other]["evidence"]
    wc = _checks_by_name(v)["worker_contract"]
    assert "structure verified" in wc["evidence"].lower()


# ===== SEC-4: non-bypassable worker-auth verdict ================================
#
# Two codex-found BYPASSES of the worker-auth verdict, now closed:
#   SEC-4-A — the admin-token leak scan was aliasable: `const leaked = env.ADMIN_TOKEN;
#             return new Response(leaked)` LEAKS but the direct-`env.ADMIN_TOKEN` regex
#             missed it. inspect_worker now exposes `admin_token_safe`.
#   SEC-4-B — only the 2 canonical read routes were checked; an extra unauthenticated
#             `/debug-leads` returning leads passed. inspect_worker now exposes
#             `all_lead_reads_guarded`.
# The deploy gate (wired at merge) requires inspect_worker(...)[1].admin_token_safe AND
# inspect_worker(...)[1].all_lead_reads_guarded; verify_appkit_app's worker_contract
# now requires both too (defense in depth).


def test_inspect_worker_known_good_admin_token_safe_and_all_reads_guarded():
    # The canonical generated worker → both SEC-4 booleans True, no extra reasons.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    post_ok, model, reasons = inspect_worker(tree["worker/index.ts"].decode(), lead)
    assert isinstance(model, WorkerAuthVerdict)
    assert post_ok and not reasons, reasons
    assert model.admin_token_safe is True
    assert model.all_lead_reads_guarded is True


def test_inspect_worker_admin_token_alias_leak_fails():
    # SEC-4-A: `const leaked = env.ADMIN_TOKEN; return new Response(leaked)` — the token
    # is aliased OUTSIDE isAuthorized and echoed into a Response. Must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "    return env.ASSETS.fetch(request);",
        '    const leaked = env.ADMIN_TOKEN;\n    return new Response(leaked || "");',
    )
    assert broken != src
    _ok, model, reasons = inspect_worker(broken, lead)
    assert model.admin_token_safe is False
    assert model.all_lead_reads_guarded is True  # the read routes are untouched
    assert any("ADMIN_TOKEN" in r and "exfiltration" in r for r in reasons)


def test_inspect_worker_admin_token_destructured_alias_leak_fails():
    # SEC-4-A variant: `const { ADMIN_TOKEN } = env; return new Response(ADMIN_TOKEN)` —
    # the alias arrives via destructuring (no literal `env.ADMIN_TOKEN`). Must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "    return env.ASSETS.fetch(request);",
        '    const { ADMIN_TOKEN } = env;\n    return new Response(ADMIN_TOKEN || "");',
    )
    assert broken != src
    _ok, model, reasons = inspect_worker(broken, lead)
    assert model.admin_token_safe is False
    assert any("ADMIN_TOKEN" in r for r in reasons)


def test_inspect_worker_admin_token_destructured_log_sink_fails():
    # SEC-4-A variant: destructured alias piped into a log sink. Must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "    return env.ASSETS.fetch(request);",
        "    const { ADMIN_TOKEN } = env;\n"
        "    console.warn(ADMIN_TOKEN);\n"
        "    return env.ASSETS.fetch(request);",
    )
    assert broken != src
    _ok, model, reasons = inspect_worker(broken, lead)
    assert model.admin_token_safe is False


def test_inspect_worker_admin_token_alias_log_sink_inside_isauthorized_fails():
    # SEC-4-A: even WITH the read confined to isAuthorized, an alias (`expected`) that
    # flows into a log sink is a leak — the alias-sink taint check must FAIL it.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        "  if (!expected) return false;",
        "  if (!expected) return false;\n  console.log(expected);",
    )
    assert broken != src
    _ok, model, reasons = inspect_worker(broken, lead)
    assert model.admin_token_safe is False


def test_inspect_worker_extra_unauthenticated_lead_route_fails():
    # SEC-4-B: an extra `/debug-leads` route returns leads with NO auth guard. The two
    # canonical read routes stay gated, so reads_require_auth is still True — but
    # all_lead_reads_guarded must be False.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        '    if (url.pathname === "/api/leads" && request.method === "POST") {',
        '    if (url.pathname === "/debug-leads" && request.method === "GET") {\n'
        "      const result = await listLeads(env);\n"
        "      return json({ leads: result.results ?? [] });\n"
        "    }\n"
        '    if (url.pathname === "/api/leads" && request.method === "POST") {',
    )
    assert broken != src
    _ok, model, reasons = inspect_worker(broken, lead)
    assert model.all_lead_reads_guarded is False
    assert model.reads_require_auth is True  # canonical routes untouched
    assert model.admin_token_safe is True
    assert any("lead data" in r or "lead-read" in r for r in reasons)


def test_inspect_worker_extra_inline_select_lead_route_fails():
    # SEC-4-B variant: the extra unauthenticated route inlines a `SELECT ... FROM leads`
    # (not via listLeads). Still a lead-returning route → must FAIL.
    tree, app = _build_tree()
    lead = resolve_lead_entity(app)
    src = tree["worker/index.ts"].decode()
    broken = src.replace(
        '    if (url.pathname === "/api/leads" && request.method === "POST") {',
        '    if (url.pathname === "/debug-leads" && request.method === "GET") {\n'
        '      const r = await env.DB.prepare("SELECT * FROM \\"leads\\"").all();\n'
        "      return json({ leads: r.results ?? [] });\n"
        "    }\n"
        '    if (url.pathname === "/api/leads" && request.method === "POST") {',
    )
    assert broken != src
    _ok, model, _reasons = inspect_worker(broken, lead)
    assert model.all_lead_reads_guarded is False


@pytest.mark.asyncio
async def test_admin_token_alias_leak_fails_worker_contract(stub_browser):
    # Full-tool wiring: the SEC-4-A alias leak fails worker_contract.
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace(
        "    return env.ASSETS.fetch(request);",
        '    const leaked = env.ADMIN_TOKEN;\n    return new Response(leaked || "");',
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["worker_contract"]["passed"] is False
    assert "exfiltration" in checks["worker_contract"]["evidence"]


@pytest.mark.asyncio
async def test_extra_unauthenticated_lead_route_fails_worker_contract(stub_browser):
    # Full-tool wiring: the SEC-4-B unauthenticated /debug-leads route fails
    # worker_contract.
    tree, _ = _build_tree()
    src = tree["worker/index.ts"].decode()
    tree["worker/index.ts"] = src.replace(
        '    if (url.pathname === "/api/leads" && request.method === "POST") {',
        '    if (url.pathname === "/debug-leads" && request.method === "GET") {\n'
        "      const result = await listLeads(env);\n"
        "      return json({ leads: result.results ?? [] });\n"
        "    }\n"
        '    if (url.pathname === "/api/leads" && request.method === "POST") {',
    ).encode()
    out = await _run(tree)
    checks = _checks_by_name(out.structured)
    assert out.structured["passed"] is False
    assert checks["worker_contract"]["passed"] is False

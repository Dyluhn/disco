"""WO-A3 — `verify_appkit_app` primitive-verify DISPATCH + A3.2 applied-primitive
enforcement (fail-closed), through the real tool interface.

Covers:
  * a hello app created via AppCreateTool + app_add_primitive → the tool dispatches
    hello's OWN verify hook (its checks appear and pass; no lead-gen check names
    leak into the verdict) and the applied-hello record lands a PASSING
    `primitive_verify:hello` check — the ONE intended WO-A3 behavior change (hello
    apps used to fail the lead-gen bundle);
  * a broken hello tree (headline edited away) FAILS hello_headline AND the
    `primitive_verify:hello` record check (evidence names the failing sub-check);
  * a lead_gen app with an applied form record runs the form verify hook and lands a
    PASSING `primitive_verify:form` check;
  * a template_only primitive with verify=None applied to the app → the
    `primitive_verify:<id>` check FAILS the verdict (fail-closed: cannot ship
    unverified);
  * a template_only primitive WITH a passing verify → its check appears PASSING and
    the verdict stays green;
  * a stale/unknown provenance record → its check FAILS (fail-closed);
  * no `.disco/primitives/` dir → no `primitive_verify:*` checks at all (tolerated
    absence — the existing check sets are untouched).

The browser/vite tail is faked exactly as test_verify_appkit_app.py does: an
in-memory sandbox answering the design-lint `wc -c` probe + the preview http
probe, and BrowserTool.run monkeypatched (no Playwright daemon).
"""

from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest
from disco.core.appkit.hello_primitive import HelloSpec, apply_hello_spec
from disco.core.appkit.primitives import (
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
)
from disco.tools.anatomy import Capability, ToolContext, ToolOutcome
from disco.tools.builtin import browser as browser_mod
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from disco.tools.builtin.verify_appkit_app import VerifyAppKitAppArgs, VerifyAppKitAppTool
from tool_fakes import FakeSandboxInstance

# ---- test-only primitives (unique ids — registered once per process) -------------

# A template_only primitive with NO verify harness: applying it must FAIL the
# verdict fail-closed (A3.2's one rule). Reuses hello's spec/apply pair; its own
# generate is never invoked (the APP's base primitive regenerates the tree).
_TPL_NOVERIFY_ID = "wo_a3_template_only_noverify"
register_primitive(
    PrimitiveDefinition(
        id=_TPL_NOVERIFY_ID,
        default_app_spec=lambda name, recipe: (_ for _ in ()).throw(NotImplementedError),
        prepare_app_spec=lambda app: app,
        generate=lambda app, design: {},
        tier="template_only",
        spec_schema=HelloSpec,
        apply_spec=apply_hello_spec,
        verify=None,
    )
)

# A template_only primitive WITH a passing verify: its record check must PASS.
_TPL_VERIFIED_ID = "wo_a3_template_only_verified"
register_primitive(
    PrimitiveDefinition(
        id=_TPL_VERIFIED_ID,
        default_app_spec=lambda name, recipe: (_ for _ in ()).throw(NotImplementedError),
        prepare_app_spec=lambda app: app,
        generate=lambda app, design: {},
        tier="template_only",
        spec_schema=HelloSpec,
        apply_spec=apply_hello_spec,
        verify=lambda app, design, tree: PrimitiveVerifyResult(
            ok=True,
            detail="template output verified",
            checks=(VerifyCheck("wo_a3_tpl_output", True, "verified"),),
        ),
    )
)


# ---- fakes (same shape as test_verify_appkit_app.py) ------------------------------


class _ExecRes:
    def __init__(self, stdout: str, exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = ""
        self.timed_out = False


class _VerifySandbox:
    """In-memory workspace with a HIERARCHICAL list_dir (FakeSandboxInstance's
    flat one is too degenerate for the verifier) + the design_lint `wc -c` and
    preview http probes."""

    def __init__(self, files: dict[str, bytes]):
        self._files = dict(files)
        runtime = {
            "name": "appkit-live-vite",
            "status": "running",
            "port": 9134,
            "projection_id": "pv_" + "a" * 32,
            "intent": {
                "serve_dir": None,
                "command": None,
                "framework": "vite",
                "cwd": None,
                "launch_kind": "framework",
            },
            "launch_kind": "framework",
        }
        session = SimpleNamespace(
            name="appkit-live-vite",
            port=9134,
            status=SimpleNamespace(value="running"),
            to_dict=lambda: runtime,
        )
        self._preview_manager = SimpleNamespace(canonical_session=lambda: session)

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
            remainder = path[len(prefix) :]
            children.add(remainder.split("/", 1)[0] if "/" in remainder else remainder)
        if not matched and rel != "":
            raise FileNotFoundError(rel)
        return sorted(children)

    async def exec_shell(self, cmd: str, timeout_s=None):
        if cmd.startswith("wc -c <"):
            path = shlex.split(cmd)[3]
            data = self._files.get(path)
            return _ExecRes("", exit_code=1) if data is None else _ExecRes(str(len(data)))
        if "import json, urllib.request as U" in cmd:
            return _ExecRes(
                json.dumps(
                    {
                        "status": 200,
                        "content_type": "text/html",
                        "body": '<script type="module" src="/assets/index-a3.js"></script>',
                    }
                )
            )
        if cmd == "npm run build":
            self._files["dist/index.html"] = (
                b'<script type="module" src="/assets/index-a3.js"></script>'
            )
        return _ExecRes("200")


@pytest.fixture
def stub_browser(monkeypatch):
    async def fake_run(self, args, ctx):
        return ToolOutcome(
            success=True,
            content="browsed",
            structured={
                "url": args.url,
                "title": "Acme Studio",
                "text": "Hello from the hello primitive — Acme Studio",
                "elements": [],
                "console": [],
                "network": [],
                "appkit_sections": ["hero", "features", "contact", "quote_request", "footer"],
                "screenshot_path": ".pmx/screenshots/0001-navigate.png",
            },
        )

    monkeypatch.setattr(browser_mod.BrowserTool, "run", fake_run)


def _ctx(sbx) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-wo-a3",
    )


async def _hello_workspace(*applied: tuple[str, str]) -> dict[str, bytes]:
    """Create a REAL hello app via the tools (AppCreateTool [+ app_add_primitive per
    (primitive_id, headline)]) and return the resulting workspace files."""
    sbx = FakeSandboxInstance()
    created = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="hello", brief="Acme Studio"),
        _ctx(sbx),
    )
    assert created.success, created.content
    for primitive_id, headline in applied:
        added = await AppAddPrimitiveTool().run(
            AppAddPrimitiveArgs(primitive_id=primitive_id, spec={"headline": headline}),
            _ctx(sbx),
        )
        assert added.success, added.content
    return dict(sbx._fs)


async def _form_workspace() -> dict[str, bytes]:
    sbx = FakeSandboxInstance()
    created = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="lead_gen", brief="Acme Studio"),
        _ctx(sbx),
    )
    assert created.success, created.content
    added = await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(
            primitive_id="form",
            spec={
                "form_id": "quote_request",
                "title": "Request a quote",
                "fields": [
                    {
                        "name": "email",
                        "label": "Email",
                        "kind": "email",
                        "required": True,
                    }
                ],
                "success_message": "Thanks, we will respond shortly.",
            },
        ),
        _ctx(sbx),
    )
    assert added.success, added.content
    return dict(sbx._fs)


async def _records_policy_workspace() -> dict[str, bytes]:
    sbx = FakeSandboxInstance()
    created = await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            app_spec={
                "schema_version": 1,
                "app_kind": "records",
                "name": "Common Ground",
                "roles": ["member", "moderator", "administrator"],
                "role_admin_roles": ["administrator"],
                "pages": [
                    {
                        "id": "home",
                        "route": "/",
                        "title": "Common Ground",
                        "sections": [
                            {
                                "id": "hero",
                                "kind": "hero",
                                "content": {"heading": "Common Ground"},
                            }
                        ],
                    }
                ],
                "entities": [
                    {
                        "id": "topic",
                        "name": "Topic",
                        "fields": [{"name": "title", "type": "str", "required": True}],
                        "record_policy": {
                            "public_read": True,
                            "create_roles": ["member", "moderator", "administrator"],
                            "owner_managed": True,
                            "manage_roles": ["moderator", "administrator"],
                            "lock_roles": ["moderator", "administrator"],
                        },
                    }
                ],
            },
        ),
        _ctx(sbx),
    )
    assert created.success, created.content
    return dict(sbx._fs)


async def _verify(files: dict[str, bytes]) -> dict:
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _ctx(_VerifySandbox(files))
    )
    assert out.success and out.structured is not None, out.error
    return out.structured


def _checks_by_name(verdict: dict) -> dict[str, dict]:
    return {c["name"]: c for c in verdict["checks"]}


# ---- the dispatch (A3.1) ----------------------------------------------------------


async def test_hello_app_dispatches_hello_verify_and_passes(stub_browser):
    # THE intended WO-A3 behavior change: a hello app used to fall into the
    # lead-gen bundle (and fail); now its own verify hook runs and PASSES.
    files = await _hello_workspace(("hello", "Handcrafted since 1994"))
    v = await _verify(files)
    checks = _checks_by_name(v)
    assert v["passed"] is True, v["summary"]
    names = set(checks)
    assert {"hello_index_present", "hello_headline", "hello_subtitle"} <= names
    assert checks["hello_headline"]["passed"] is True
    assert checks["hello_subtitle"]["passed"] is True
    # no lead-gen check names leak into a hello verdict
    assert "schema_sql_valid" not in names
    assert "worker_contract" not in names
    assert "lead_form_posts" not in names
    # the applied-hello provenance record is enforced AND passes (hello has verify)
    assert checks["primitive_verify:hello"]["passed"] is True
    assert "passed" in checks["primitive_verify:hello"]["evidence"]


async def test_hello_app_without_applied_records_has_no_record_checks(stub_browser):
    files = await _hello_workspace()  # app_create only — no .disco/primitives/
    v = await _verify(files)
    names = set(_checks_by_name(v))
    assert not any(n.startswith("primitive_verify:") for n in names)
    assert v["passed"] is True, v["summary"]


async def test_applied_form_record_runs_form_verify_hook(stub_browser):
    files = await _form_workspace()
    v = await _verify(files)
    checks = _checks_by_name(v)
    assert checks["primitive_verify:form"]["passed"] is True
    assert checks["primitive_verify:form"]["evidence"] == "4 passed / 0 failed"
    assert v["passed"] is True, v["summary"]


async def test_records_policy_dispatches_records_contract_not_lead_gen(stub_browser):
    files = await _records_policy_workspace()
    v = await _verify(files)
    checks = _checks_by_name(v)
    names = set(checks)
    assert v["passed"] is True, v["summary"]
    assert {
        "records_schema",
        "records_drizzle_contract",
        "records_worker_contract",
        "records_policy_ui",
        "records_policy_shell",
    } <= names
    assert "schema_sql_valid" not in names
    assert "worker_contract" not in names
    assert "lead_form_posts" not in names


async def test_broken_hello_tree_fails_dispatch_and_record_check(stub_browser):
    files = await _hello_workspace(("hello", "Handcrafted since 1994"))
    index = files["index.html"].decode("utf-8").replace("Acme Studio", "Wrong Name")
    files["index.html"] = index.encode("utf-8")
    v = await _verify(files)
    checks = _checks_by_name(v)
    assert v["passed"] is False
    assert checks["hello_headline"]["passed"] is False
    # the record check reports the same failure, naming the failing sub-check
    assert checks["primitive_verify:hello"]["passed"] is False
    assert "hello_headline" in checks["primitive_verify:hello"]["evidence"]


# ---- A3.2 — template_only enforcement (fail-closed) --------------------------------


async def test_template_only_without_verify_fails_closed(stub_browser):
    files = await _hello_workspace((_TPL_NOVERIFY_ID, "Fail Closed Proof"))
    v = await _verify(files)
    checks = _checks_by_name(v)
    record_check = checks[f"primitive_verify:{_TPL_NOVERIFY_ID}"]
    assert record_check["passed"] is False
    assert record_check["evidence"] == (
        f"template_only primitive '{_TPL_NOVERIFY_ID}' declares no verify harness — "
        "cannot ship unverified (fail-closed)."
    )
    assert v["passed"] is False
    # ...while the base hello checks themselves stay green (the tree is fine)
    assert checks["hello_headline"]["passed"] is True


async def test_template_only_with_passing_verify_passes(stub_browser):
    files = await _hello_workspace((_TPL_VERIFIED_ID, "Verified Proof"))
    v = await _verify(files)
    checks = _checks_by_name(v)
    record_check = checks[f"primitive_verify:{_TPL_VERIFIED_ID}"]
    assert record_check["passed"] is True
    assert record_check["evidence"] == "template output verified"
    assert v["passed"] is True, v["summary"]


async def test_stale_unknown_primitive_record_fails_closed(stub_browser):
    files = await _hello_workspace()
    stale = {"primitive_id": "wo_a3_ghost_primitive", "tier": "template_only", "spec": {}}
    files[".disco/primitives/wo_a3_ghost_primitive.json"] = json.dumps(stale).encode("utf-8")
    v = await _verify(files)
    checks = _checks_by_name(v)
    record_check = checks["primitive_verify:wo_a3_ghost_primitive"]
    assert record_check["passed"] is False
    assert "unknown" in record_check["evidence"]
    assert "fail-closed" in record_check["evidence"]
    assert v["passed"] is False

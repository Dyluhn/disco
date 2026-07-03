"""AppKit EPIC N — the directory primitive through the TOOL interface.

Covers, against in-memory sandboxes:
  * app_create(primitive_id="directory") writes the static directory tree + both
    .disco specs and persists app_kind="directory"; an unknown primitive_id and a
    primitive_id/app_spec mismatch are refused; existing lead-gen creation is intact;
  * the cross-primitive overwrite GUARD refuses replacing a lead-gen app with a
    directory app in place (stale-file safety), while a same-primitive overwrite is
    still allowed;
  * verify_appkit_app on a generated directory app runs the DIRECTORY check set and
    passes; a static worker that grew a lead API fails static_worker_contract.
"""

from __future__ import annotations

import json

import pytest
from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    default_directory_app_spec,
    generate,
    get_recipe,
    serialize_app_spec,
    serialize_design_spec,
)
from disco.tools.anatomy import Capability, ToolContext, ToolOutcome
from disco.tools.builtin import browser as browser_mod
from disco.tools.builtin.app_kit import AppCreateArgs, AppCreateTool
from disco.tools.builtin.verify_appkit_app import VerifyAppKitAppArgs, VerifyAppKitAppTool
from tool_fakes import FakeSandboxInstance


def _ctx(sbx) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-appkit-n",
    )


# ---- app_create(primitive_id="directory") --------------------------------------


async def test_app_create_directory_writes_static_tree_and_specs():
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="directory", brief="Town Dir"),
        _ctx(sbx),
    )
    assert out.success, out.content
    files = set(sbx._fs)
    assert APPSPEC_RELPATH in files and DESIGNSPEC_RELPATH in files
    for path in ("index.html", "src/App.tsx", "worker/index.ts", "wrangler.toml"):
        assert path in files, path
    # the static directory site ships no lead-secret template
    assert ".dev.vars.example" not in files
    # app_kind persists as 'directory'
    spec = json.loads(sbx._fs[APPSPEC_RELPATH].decode("utf-8"))
    assert spec["app_kind"] == "directory"
    assert out.structured["primitive_id"] == "directory"
    # the worker is a static passthrough (no lead route)
    assert 'pathname === "/api/leads"' not in sbx._fs["worker/index.ts"].decode("utf-8")


async def test_app_create_rejects_unknown_primitive():
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="nope"), _ctx(sbx)
    )
    assert not out.success
    assert "unknown primitive_id" in out.content.lower()


async def test_app_create_rejects_primitive_app_spec_mismatch():
    sbx = FakeSandboxInstance()
    app = default_directory_app_spec("Dir", get_recipe("editorial-ledger"))
    out = await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger",
            primitive_id="lead_gen",  # contradicts app_kind="directory"
            app_spec=app.model_dump(mode="json"),
        ),
        _ctx(sbx),
    )
    assert not out.success
    assert "does not match" in out.content.lower()


async def test_app_create_lead_gen_still_default():
    sbx = FakeSandboxInstance()
    out = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", brief="Acme"), _ctx(sbx)
    )
    assert out.success, out.content
    spec = json.loads(sbx._fs[APPSPEC_RELPATH].decode("utf-8"))
    assert spec["app_kind"] == "lead_gen"
    assert any(e["id"] == "lead" for e in spec["entities"])
    assert ".dev.vars.example" in sbx._fs


# ---- the cross-primitive overwrite guard ---------------------------------------


async def test_cross_primitive_overwrite_is_refused():
    sbx = FakeSandboxInstance()
    assert (
        await AppCreateTool().run(
            AppCreateArgs(recipe_id="editorial-ledger", brief="Acme"), _ctx(sbx)
        )
    ).success
    # replacing a lead-gen app with a directory app in place is refused (stale files)
    blocked = await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="directory", overwrite=True),
        _ctx(sbx),
    )
    assert not blocked.success
    assert "overwrite" in blocked.content.lower()
    # ... but a SAME-primitive overwrite is still allowed
    same = await AppCreateTool().run(
        AppCreateArgs(recipe_id="field-notes", brief="Acme", overwrite=True), _ctx(sbx)
    )
    assert same.success, same.content


# ---- verify_appkit_app on a directory app --------------------------------------


class _ExecRes:
    def __init__(self, stdout: str, exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = ""
        self.timed_out = False


class _FakeSandbox:
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
            raise FileNotFoundError(rel)
        return sorted(children)

    async def exec_shell(self, cmd: str, timeout_s=None):
        if cmd.startswith("wc -c <"):
            import shlex

            path = shlex.split(cmd)[3]
            data = self._files.get(path)
            return _ExecRes("", exit_code=1) if data is None else _ExecRes(str(len(data)))
        return _ExecRes("200")


def _vctx(sandbox) -> ToolContext:
    return ToolContext(
        sandbox=sandbox,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="o",
        conversation_id="c",
    )


_ALL_MARKERS = ["hero", "about", "faq", "footer", "listings", "directory_footer"]


@pytest.fixture
def stub_browser(monkeypatch):
    async def fake_run(self, args, ctx):
        return ToolOutcome(
            success=True,
            content="browsed",
            structured={
                "url": args.url,
                "title": "Town Directory",
                "text": "Browse the directory.",
                "elements": [],
                "console": [],
                "network": [],
                "appkit_sections": _ALL_MARKERS,
                "screenshot_path": ".pmx/screenshots/0001-navigate.png",
            },
        )

    monkeypatch.setattr(browser_mod.BrowserTool, "run", fake_run)


def _directory_tree() -> dict[str, bytes]:
    recipe = get_recipe("editorial-ledger")
    app = default_directory_app_spec("Town Directory", recipe)
    design = recipe.to_design_spec()
    tree = {p: c.encode("utf-8") for p, c in generate(app, design).items()}
    tree[APPSPEC_RELPATH] = serialize_app_spec(app).encode("utf-8")
    tree[DESIGNSPEC_RELPATH] = serialize_design_spec(design).encode("utf-8")
    return tree


@pytest.mark.asyncio
async def test_directory_verify_passes_directory_check_set(stub_browser):
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _vctx(_FakeSandbox(_directory_tree()))
    )
    assert out.success and out.structured is not None
    v = out.structured
    names = {c["name"] for c in v["checks"]}
    assert names == {
        "design_lint_clean",
        "directory_listing",
        "static_worker_contract",
        "cloudflare_export_ready",
        "route_coverage",
        "section_coverage",
    }
    # no lead-gen-only checks leaked into the directory verdict
    assert "worker_contract" not in names
    assert "schema_sql_valid" not in names
    assert v["passed"] is True, v["summary"]


@pytest.mark.asyncio
async def test_directory_verify_fails_when_worker_grows_a_lead_api(stub_browser):
    tree = _directory_tree()
    worker = tree["worker/index.ts"].decode("utf-8").replace(
        "return env.ASSETS.fetch(request);",
        'if (new URL(request.url).pathname === "/api/leads") {}\n'
        "    return env.ASSETS.fetch(request);",
    )
    tree["worker/index.ts"] = worker.encode("utf-8")
    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(url="http://127.0.0.1:8000/"), _vctx(_FakeSandbox(tree))
    )
    v = out.structured
    checks = {c["name"]: c for c in v["checks"]}
    assert v["passed"] is False
    assert checks["static_worker_contract"]["passed"] is False
    # the other checks stay green
    assert checks["design_lint_clean"]["passed"] is True
    assert checks["directory_listing"]["passed"] is True

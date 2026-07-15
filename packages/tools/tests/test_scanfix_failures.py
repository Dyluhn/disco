from __future__ import annotations

from types import SimpleNamespace

from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin import design_lint as design_lint_module
from disco.tools.builtin._deck_patch import DeckPatchTool
from disco.tools.builtin.audio_overview import AudioOverviewTool
from disco.tools.builtin.browser import BrowserArgs, BrowserTool
from disco.tools.builtin.document import DocExportTool
from disco.tools.builtin.image_gen import ImageGenTool
from disco.tools.builtin.slides import SlidesTool
from disco.tools.builtin.verify_app import VerifyWebAppArgs, VerifyWebAppTool
from disco.tools.builtin.verify_appkit_app import VerifyAppKitAppArgs, VerifyAppKitAppTool
from disco.tools.sandbox.base import ExecResult
from tool_fakes import FakeSandboxInstance


def _ctx(sbx: FakeSandboxInstance | None = None) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={
            Capability.SHELL,
            Capability.FILESYSTEM,
            Capability.NETWORK,
            Capability.DISPLAY,
        },
        owner_id="local",
        conversation_id="c",
    )


async def test_browser_daemon_failure_has_content_and_recipe(monkeypatch):
    async def _ok_daemon(self, ctx):
        return None

    monkeypatch.setattr(BrowserTool, "_ensure_daemon", _ok_daemon)

    class _FailingBrowserRequest(FakeSandboxInstance):
        async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
            return ExecResult(exit_code=7, stdout="", stderr="daemon down")

    out = await BrowserTool().run(
        BrowserArgs(action="navigate", url="http://127.0.0.1:3000/"),
        _ctx(_FailingBrowserRequest()),
    )

    assert out.success is False
    assert out.content
    assert out.error == out.content
    assert "preview_status / preview_logs" in out.content
    assert "verify_web_app" in out.content


async def test_verify_web_app_catchall_has_content_and_recipe(monkeypatch):
    async def _boom(self, ctx):
        raise RuntimeError("preview resolver exploded")

    monkeypatch.setattr(VerifyWebAppTool, "_detect_preview_url", _boom)

    out = await VerifyWebAppTool().run(VerifyWebAppArgs(), _ctx(FakeSandboxInstance()))

    assert out.success is False
    assert out.content
    assert out.error == out.content
    assert "preview_status" in out.content
    assert "preview_logs" in out.content
    assert "re-run verify_web_app once" in out.content


async def test_deck_patch_read_failure_has_content():
    args = DeckPatchTool.definition.args_model(deck_file="missing.authored.json", patch=[])

    out = await DeckPatchTool().run(args, _ctx(FakeSandboxInstance()))

    assert out.success is False
    assert out.content
    assert out.error == out.content
    assert "Could not read deck file" in out.content


async def test_artifact_family_failures_have_nonempty_content():
    sbx = FakeSandboxInstance()

    image = await ImageGenTool().run(
        ImageGenTool.definition.args_model(prompt="x", format="gif"),
        _ctx(sbx),
    )
    slides = await SlidesTool().run(
        SlidesTool.definition.args_model(filename="deck", goal=None, markdown=""),
        _ctx(sbx),
    )
    document = await DocExportTool().run(
        DocExportTool.definition.args_model(title="Report", filename="report"),
        _ctx(None),
    )
    _, audio = await AudioOverviewTool()._resolve_tts_settings(
        SimpleNamespace(enabled=False),
        "http://127.0.0.1:8008",
    )

    for out in (image, slides, document, audio):
        assert out is not None
        assert out.success is False
        assert out.content
        assert out.error


async def test_verify_appkit_catchall_has_content(monkeypatch):
    async def _boom(self, ctx):
        raise RuntimeError("app read exploded")

    monkeypatch.setattr(VerifyAppKitAppTool, "_load_app", _boom)

    out = await VerifyAppKitAppTool().run(
        VerifyAppKitAppArgs(),
        _ctx(FakeSandboxInstance()),
    )

    assert out.success is False
    assert out.content
    assert out.error == out.content
    assert "verify_appkit_app error" in out.content


async def test_design_lint_caps_content_but_keeps_structured_full(monkeypatch):
    findings = [
        {
            "rule_id": f"rule_{i:02d}",
            "severity": "warning",
            "path": "src/app.css",
            "line": i + 1,
            "evidence": f"finding {i}",
            "choice_key": "scanfix",
            "message": "fix it",
        }
        for i in range(25)
    ]
    verdict = {
        "ok": False,
        "findings": findings,
        "counts": {"error": 0, "warning": 25, "info": 0},
        "summary": "design_lint: 25 finding(s).",
        "scanned_files": 1,
        "design_spec_present": True,
        "design_spec_valid": True,
    }

    async def _collect_files(self, ctx, root):
        return {"src/app.css": "body { color: red; }"}

    async def _load_design_spec(self, ctx):
        return {}, True, True

    async def _load_direction(self, ctx):
        return None

    monkeypatch.setattr(design_lint_module.DesignLintTool, "_collect_files", _collect_files)
    monkeypatch.setattr(design_lint_module.DesignLintTool, "_load_design_spec", _load_design_spec)
    monkeypatch.setattr(design_lint_module.DesignLintTool, "_load_direction", _load_direction)
    monkeypatch.setattr(design_lint_module, "lint_design", lambda *args, **kwargs: verdict)

    out = await design_lint_module.DesignLintTool().run(
        design_lint_module.DesignLintArgs(),
        _ctx(FakeSandboxInstance()),
    )

    assert out.success is True
    assert out.structured is not None
    assert len(out.structured["findings"]) == 25
    assert "rule_19" in out.content
    assert "rule_20" not in out.content
    assert "…and 5 more findings — fix the above first, then re-run." in out.content

"""Preview lifecycle tools (§E1/E6): read-only status, bounded restart, detached
serve. Tested against a scripted fake sandbox — we assert the tools PARSE the
in-sandbox probe correctly and surface the right state/guidance, without a real
container."""

from __future__ import annotations

import pytest
from perpleximanus.tools.anatomy import Capability, ToolContext
from perpleximanus.tools.builtin.preview import (
    PreviewStatusTool,
    RestartPreviewTool,
    RunServerTool,
)
from perpleximanus.tools.sandbox.base import ExecResult

pytestmark = pytest.mark.asyncio


class ScriptedSandbox:
    """Returns a canned stdout regardless of the command (we drive each test by the
    stdout the in-container probe would print)."""

    def __init__(self, stdout: str, exit_code: int = 0):
        self._stdout = stdout
        self._exit = exit_code
        self.commands: list[str] = []

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.commands.append(cmd)
        return ExecResult(exit_code=self._exit, stdout=self._stdout, stderr="")


def _ctx(sb) -> ToolContext:
    return ToolContext(
        sandbox=sb,
        workspace_path="/work",
        timeout_s=30,
        capabilities={Capability.SHELL, Capability.NETWORK},
        owner_id="o",
        conversation_id="c",
    )


# ---- preview_status ----------------------------------------------------------


async def test_status_serving():
    sb = ScriptedSandbox("listening=serving index=yes files=4")
    out = await PreviewStatusTool().run(PreviewStatusTool.definition.args_model(), _ctx(sb))
    assert out.success and out.structured["state"] == "serving"
    assert "serving" in out.content.lower()


async def test_status_down_with_index_suggests_restart():
    sb = ScriptedSandbox("listening=down index=yes files=3")
    out = await PreviewStatusTool().run(PreviewStatusTool.definition.args_model(), _ctx(sb))
    assert out.structured["state"] == "down"
    assert "restart_preview" in out.content


async def test_status_no_files():
    sb = ScriptedSandbox("listening=down index=no files=0")
    out = await PreviewStatusTool().run(PreviewStatusTool.definition.args_model(), _ctx(sb))
    assert out.structured["state"] == "no-files"


def test_status_is_read_only():
    # the gate relies on this so PLANNING can offer it without a confirm
    assert PreviewStatusTool.definition.read_only is True


# ---- restart_preview ---------------------------------------------------------


async def test_restart_reports_ok_when_listener_comes_up():
    sb = ScriptedSandbox("ok")
    out = await RestartPreviewTool().run(RestartPreviewTool.definition.args_model(), _ctx(sb))
    assert out.success and out.structured["serving"] is True
    # it must NOT be a raw pkill the agent issued — it's the controlled path
    assert any("http.server" in c for c in sb.commands)


async def test_restart_reports_failure_when_nothing_binds():
    sb = ScriptedSandbox("failed")
    out = await RestartPreviewTool().run(RestartPreviewTool.definition.args_model(), _ctx(sb))
    assert not out.success and out.error


# ---- serve -------------------------------------------------------------------


async def test_serve_up():
    sb = ScriptedSandbox("up")
    args = RunServerTool.definition.args_model(command="npm run dev")
    out = await RunServerTool().run(args, _ctx(sb))
    assert out.success and out.structured["serving"] is True
    assert any("setsid" in c for c in sb.commands)  # detached, survives across steps


async def test_serve_not_up_returns_log_tail():
    sb = ScriptedSandbox("notup\nError: cannot find module")
    args = RunServerTool.definition.args_model(command="npm run dev")
    out = await RunServerTool().run(args, _ctx(sb))
    assert not out.success
    assert "cannot find module" in out.content

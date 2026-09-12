"""`shell_exec` reports its exit code in `structured`, not only in the header text.

The check ledger reads `structured["exit_code"]` for every shell tool; before this the
session tool put the code in prose only, so a session-run test had no machine-readable
pass/fail.
"""

from __future__ import annotations

from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.shell_sessions import ShellExecTool
from disco.tools.sandbox.shell_sessions import ExecOutcome
from tool_fakes import FakeSandboxInstance


class _Sessions:
    def __init__(self, outcome: ExecOutcome) -> None:
        self._outcome = outcome

    async def exec(self, session: str, command: str, exec_dir: str | None):
        return self._outcome


def _ctx(sessions) -> ToolContext:
    return ToolContext(
        sandbox=FakeSandboxInstance(),
        sessions=sessions,
        kernel=None,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.SHELL},
        owner_id="local",
        conversation_id="c",
    )


def _args(command: str = "bash smoke.sh"):
    return ShellExecTool.definition.args_model(session="s", command=command)


async def test_finished_command_exposes_exit_code() -> None:
    res = await ShellExecTool().run(
        _args(), _ctx(_Sessions(ExecOutcome(running=False, exit_code=3, output="FAIL: x")))
    )
    assert res.success is True
    assert res.structured is not None
    assert res.structured["exit_code"] == 3
    assert res.structured["running"] is False
    assert "exit 3" in res.content


async def test_still_running_command_has_no_exit_code() -> None:
    res = await ShellExecTool().run(
        _args("node server.js"),
        _ctx(_Sessions(ExecOutcome(running=True, exit_code=None, output="listening"))),
    )
    assert res.structured is not None
    assert res.structured["running"] is True
    assert "exit_code" not in res.structured


def test_shell_tools_accept_force_default_false() -> None:
    from disco.tools.builtin.system import ShellTool

    assert ShellTool.definition.args_model(command="ls").force is False
    assert ShellTool.definition.args_model(command="ls", force=True).force is True
    assert ShellExecTool.definition.args_model(session="s", command="ls").force is False

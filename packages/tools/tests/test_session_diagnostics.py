"""DEFECT-2 regression: session diagnostics surfaced, not dropped.

Three layers under test:
  sandbox/shell_sessions.py  — SessionBusy names the occupant; unreachable
                               tmux names the session + recovery path.
  builtin/shell_sessions.py  — every ToolOutcome(success=False) carries
                               error == content (belt+braces).
  executor.py relay          — error=None falls back to content so
                               AgentErrorEvent receives the diagnosis instead
                               of bare "tool failed".
"""

from __future__ import annotations

from disco.tools import DefaultToolExecutor, ToolContext, ToolDef, ToolOutcome
from disco.tools.builtin.shell_sessions import ShellExecArgs, ShellExecTool
from disco.tools.registry import ToolRegistry, ToolScope
from disco.tools.sandbox.base import ExecResult
from disco.tools.sandbox.shell_sessions import SessionBusy, ShellSessionManager
from pydantic import BaseModel
from tool_fakes import call

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedInstance:
    """Minimal SandboxInstance stub — routes exec_shell by substring match."""

    def __init__(self, routes: dict[str, tuple[int, str]], default: tuple[int, str] = (0, "")):
        self._routes = routes
        self._default = default

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        for key, (code, out) in self._routes.items():
            if key in cmd:
                stderr = "" if code == 0 else out
                return ExecResult(exit_code=code, stdout=out, stderr=stderr, timed_out=False)
        code, out = self._default
        return ExecResult(exit_code=code, stdout=out, stderr="", timed_out=False)


def _manager(
    routes: dict[str, tuple[int, str]], default: tuple[int, str] = (0, "")
) -> ShellSessionManager:
    inst = _ScriptedInstance(routes, default)

    async def get_inst():
        return inst

    return ShellSessionManager(get_inst)


def _ctx(sessions=None) -> ToolContext:
    return ToolContext(
        sandbox=None,
        sessions=sessions,
        workspace_path=".",
        timeout_s=30,
        capabilities=None,
        owner_id="test",
        conversation_id="test-dc04a",
    )


# ---------------------------------------------------------------------------
# 1. sandbox layer — busy names the running command
# ---------------------------------------------------------------------------


async def test_busy_session_names_running_command():
    """SessionBusy message names the session, the occupant command, and all
    three recovery options."""
    mgr = _manager(
        {
            "has-session": (0, ""),
            "capture-pane": (0, "python3 server.py\n"),  # no PS1 marker → running
            "list-panes": (0, "python3\n"),
        }
    )
    try:
        await mgr.exec("backend", "ls", None)
        raise AssertionError("expected SessionBusy")
    except SessionBusy as exc:
        msg = str(exc)
    assert "backend" in msg
    assert "python3" in msg
    assert "shell_wait" in msg
    assert "shell_write_to_process" in msg
    assert "shell_kill_process" in msg


async def test_busy_session_fallback_when_list_panes_fails():
    """If list-panes fails the old generic wording is used — no crash."""
    mgr = _manager(
        {
            "has-session": (0, ""),
            "capture-pane": (0, "sleep 99\n"),
            "list-panes": (1, ""),  # failure → fall back
        }
    )
    try:
        await mgr.exec("main", "ls", None)
        raise AssertionError("expected SessionBusy")
    except SessionBusy as exc:
        msg = str(exc)
    assert "main" in msg
    assert "shell_wait" in msg


# ---------------------------------------------------------------------------
# 2. sandbox layer — unreachable tmux names the session + recovery path
#    (scripted exec_shell failure per the acceptance ladder)
# ---------------------------------------------------------------------------


async def test_unreachable_tmux_names_session_and_recovery():
    """When ensure() raises (all tmux ops fail — no tmux server), exec() wraps
    it into a RuntimeError naming the session and the three recovery options."""
    # has-session → not found; new-session → fails
    mgr = _manager(
        {"has-session": (1, "")},
        default=(1, "no server running on /tmp/tmux"),  # all other tmux cmds fail
    )
    try:
        await mgr.exec("backend", "ls", None)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        msg = str(exc)
    assert "backend" in msg
    assert "retry once" in msg
    assert "server_start" in msg


# ---------------------------------------------------------------------------
# 3. builtin layer — ToolOutcome carries error == content on failure
# ---------------------------------------------------------------------------


async def test_busy_tooloutcome_carries_error_and_content():
    """ShellExecTool converts SessionBusy into ToolOutcome where BOTH error
    and content carry the enriched message (session, command, three ways out)."""

    class _BusySessions:
        async def exec(self, name, command, exec_dir):
            raise SessionBusy(
                f"session '{name}' is busy running 'python3' — "
                "wait for it (shell_wait), "
                "interact with it (shell_write_to_process), "
                "kill it (shell_kill_process), "
                "or use a different session name."
            )

    ctx = _ctx(sessions=_BusySessions())
    outcome = await ShellExecTool().run(ShellExecArgs(session="backend", command="ls"), ctx)

    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.content is not None
    assert outcome.error == outcome.content, "error must mirror content (belt+braces)"
    assert "backend" in outcome.error
    assert "python3" in outcome.error
    assert "shell_wait" in outcome.error
    assert "shell_write_to_process" in outcome.error
    assert "shell_kill_process" in outcome.error


async def test_unreachable_tooloutcome_carries_error_and_content():
    """ShellExecTool converts a RuntimeError (unreachable sandbox) into
    ToolOutcome where BOTH error and content name the session + recovery path."""

    class _DeadSessions:
        async def exec(self, name, command, exec_dir):
            raise RuntimeError(
                f"session '{name}' could not be reached (sandbox shell unavailable or "
                f"recreated) — retry once; if it persists, use a new session name or "
                f"server_start. tmux new-session failed: no server"
            )

    ctx = _ctx(sessions=_DeadSessions())
    outcome = await ShellExecTool().run(ShellExecArgs(session="backend", command="ls"), ctx)

    assert outcome.success is False
    assert outcome.error is not None
    assert outcome.content is not None
    assert outcome.error == outcome.content, "error must mirror content (belt+braces)"
    assert "backend" in outcome.error
    assert "retry once" in outcome.error
    assert "server_start" in outcome.error


# ---------------------------------------------------------------------------
# 4. executor relay — DEFECT-2 regression test proper
# ---------------------------------------------------------------------------


async def test_executor_relay_error_falls_back_to_content():
    """DEFECT-2 regression: ToolOutcome(success=False, content="rich diagnosis",
    error=None) must produce ToolResult.error == "rich diagnosis", not None."""

    class _DiagArgs(BaseModel):
        pass

    class _DiagTool:
        definition = ToolDef(name="diag_stub", description="stub", args_model=_DiagArgs)

        async def run(self, args, ctx):
            return ToolOutcome(success=False, content="rich diagnosis", error=None)

    reg = ToolRegistry()
    reg.register(_DiagTool())
    ex = DefaultToolExecutor(reg, ToolScope(allowed_tools=frozenset({"diag_stub"})))
    result = await ex.execute(call("diag_stub"))

    assert result.success is False
    assert result.error == "rich diagnosis"


async def test_executor_relay_success_keeps_error_none():
    """Success outcomes must NOT have error populated by the fallback."""

    class _OkArgs(BaseModel):
        pass

    class _OkTool:
        definition = ToolDef(name="ok_stub", description="stub", args_model=_OkArgs)

        async def run(self, args, ctx):
            return ToolOutcome(success=True, content="all good", error=None)

    reg = ToolRegistry()
    reg.register(_OkTool())
    ex = DefaultToolExecutor(reg, ToolScope(allowed_tools=frozenset({"ok_stub"})))
    result = await ex.execute(call("ok_stub"))

    assert result.success is True
    assert result.error is None


# ---------------------------------------------------------------------------
# 5. Defect shape replay — neither busy nor dead backend produces a bare
#    "tool failed"-style empty error (shapes from bp-16 attempts 3 and 5)
# ---------------------------------------------------------------------------


async def test_defect_shape_busy_backend_no_bare_tool_failed():
    """Attempt 5 shape: shell_exec {session: "backend"} where backend is running
    python3 server.py must NOT produce an empty or 'tool failed' error."""

    class _Busy:
        async def exec(self, name, command, exec_dir):
            raise SessionBusy(
                f"session '{name}' is busy running 'python3' — "
                "wait for it (shell_wait), "
                "interact with it (shell_write_to_process), "
                "kill it (shell_kill_process), "
                "or use a different session name."
            )

    outcome = await ShellExecTool().run(
        ShellExecArgs(session="backend", command="python3 server.py"),
        _ctx(sessions=_Busy()),
    )

    assert outcome.success is False
    assert outcome.error, "error must be non-empty"
    assert outcome.error not in ("tool failed", "")
    assert "tool failed" not in outcome.error
    assert "backend" in outcome.error


async def test_defect_shape_unreachable_backend_no_bare_tool_failed():
    """Attempt 3 shape: shell_exec {session: "backend"} where tmux session is
    dead (sandbox was killed) must NOT produce an empty or 'tool failed' error."""

    class _Dead:
        async def exec(self, name, command, exec_dir):
            raise RuntimeError(
                f"session '{name}' could not be reached (sandbox shell unavailable or "
                f"recreated) — retry once; if it persists, use a new session name or "
                f"server_start. tmux new-session failed"
            )

    outcome = await ShellExecTool().run(
        ShellExecArgs(session="backend", command="python3 server.py"),
        _ctx(sessions=_Dead()),
    )

    assert outcome.success is False
    assert outcome.error, "error must be non-empty"
    assert outcome.error not in ("tool failed", "")
    assert "tool failed" not in outcome.error
    assert "backend" in outcome.error

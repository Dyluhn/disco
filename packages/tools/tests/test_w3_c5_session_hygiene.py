"""W3 C-5 — kernel-gateway token hygiene + internal-session isolation.

Three defects closed here, each with a live exploit before the fix:

  1. The gateway auth token was passed in argv (`--auth_token=<hex>`), visible via
     `ps`/`/proc/<pid>/cmdline` to any process sharing the sandbox PID namespace.
  2. The model-facing shell tools accepted any `session` name, so
     `shell_view(session="__kernel")` dumped the gateway's tmux pane — and with it
     the launch line / token.
  3. `server_status` (via `ShellSessionManager.list()`) enumerated the internal
     `__kernel` session to the model.
"""

from __future__ import annotations

import pytest
from disco.core import SecurityRisk  # noqa: F401 — imported for parity w/ tool defs
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.shell_sessions import (
    ShellExecArgs,
    ShellExecTool,
    ShellKillArgs,
    ShellKillTool,
    ShellViewArgs,
    ShellViewTool,
    ShellWaitArgs,
    ShellWaitTool,
    ShellWriteArgs,
    ShellWriteTool,
)
from disco.tools.sandbox.kernel import GatewayKernel, _gateway_auth_token
from disco.tools.sandbox.shell_sessions import SessionInfo, SessionView, ShellSessionManager


class _RecordingSessions:
    """A session manager that RECORDS any call — the guard must reject reserved
    names BEFORE the manager is ever touched."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    async def exec(self, name, command, exec_dir):  # noqa: ANN001
        self.touched.append(name)
        raise AssertionError("reserved session reached the manager")

    view = wait = write = kill_foreground = exec


def _ctx(sessions) -> ToolContext:
    return ToolContext(
        sandbox=None,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.SHELL},
        owner_id="local",
        conversation_id="c",
        sessions=sessions,
    )


# ---- sub-part 2: model-facing tools refuse `__`-prefixed sessions ------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (ShellViewTool(), ShellViewArgs(session="__kernel")),
        (ShellExecTool(), ShellExecArgs(session="__kernel", command="cat /etc/hostname")),
        (ShellWaitTool(), ShellWaitArgs(session="__kernel", seconds=1)),
        (ShellWriteTool(), ShellWriteArgs(session="__kernel", input="x")),
        (ShellKillTool(), ShellKillArgs(session="__kernel")),
    ],
)
async def test_shell_tools_refuse_reserved_session(tool, args):
    sessions = _RecordingSessions()
    outcome = await tool.run(args, _ctx(sessions))
    assert outcome.success is False
    assert "reserved" in (outcome.error or "").lower()
    # The manager was NEVER consulted — no pane capture, no exec, nothing.
    assert sessions.touched == []


async def test_ordinary_session_still_reaches_the_manager():
    # Guard is scoped to `__`-prefix — a normal name is untouched by it.
    class _OKSessions:
        async def view(self, name, *a, **k):  # noqa: ANN001, ANN002, ANN003
            return SessionView(running=False, output="hello")

    outcome = await ShellViewTool().run(ShellViewArgs(session="main"), _ctx(_OKSessions()))
    assert outcome.success is True
    assert "hello" in outcome.content


# ---- sub-part 3: list() never enumerates internal sessions ------------------


async def test_list_filters_internal_sessions(monkeypatch):
    async def _fake_get_instance():
        return object()

    mgr = ShellSessionManager(_fake_get_instance)

    async def _fake_run_tmux_safe(cmd):  # noqa: ANN001
        # tmux reports both the internal gateway session and a real one.
        return 0, "disco-__kernel\ndisco-main\n"

    async def _fake_view(name, tail_chars=1000):  # noqa: ANN001
        return SessionView(running=False, output=f"pane-of-{name}")

    monkeypatch.setattr(mgr, "_run_tmux_safe", _fake_run_tmux_safe)
    monkeypatch.setattr(mgr, "view", _fake_view)

    infos = await mgr.list()
    names = {i.name for i in infos}
    assert names == {"main"}  # __kernel is filtered out
    assert all(isinstance(i, SessionInfo) for i in infos)


# ---- sub-part 1: the gateway token travels in env, not argv ------------------


async def test_gateway_launch_passes_token_via_env_not_argv(monkeypatch):
    monkeypatch.setenv("DISCO_SECRET_KEY", "master")

    # Force the pre-launch readiness probe to miss so the launch line is always
    # emitted (guards against something real answering on localhost:8899).
    import httpx

    async def _probe_miss(*a, **k):  # noqa: ANN002, ANN003
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", _probe_miss)
    captured: dict[str, str] = {}

    class _CaptureSessions:
        async def exec(self, name, command, exec_dir):  # noqa: ANN001
            captured["name"] = name
            captured["cmd"] = command
            # Abort before the (slow) readiness poll — we only need the launch line.
            raise RuntimeError("stop after launch")

    class _FakeSandbox:
        id = "sbx_42"

        def internal_port_mapping(self, port):  # noqa: ANN001
            return None  # no container mapping → localhost fallback, still launches

    gk = GatewayKernel(_FakeSandbox(), sessions=_CaptureSessions())
    with pytest.raises(RuntimeError, match="stop after launch"):
        await gk._ensure_gateway()

    token = _gateway_auth_token("sbx_42")
    assert captured["name"] == "__kernel"
    cmd = captured["cmd"]
    # Token present as an env assignment…
    assert f"KG_AUTH_TOKEN={token}" in cmd
    # …and NOT anywhere in argv (the whole point of the fix).
    assert "--auth_token" not in cmd
    assert "--KernelGatewayApp.auth_token" not in cmd
    assert token not in cmd.split("jupyter", 1)[1]  # nothing after `jupyter` leaks it

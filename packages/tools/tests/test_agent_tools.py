"""Core agent tools + the resilient session — hermetic (tool-sandbox §9, §11).

Proves the agent toolset (shell/file/code-exec) drives the SandboxInstance interface,
that a timeout is surfaced legibly, that the agent tools are scoped OUT of Research,
and — the load-bearing carried lesson #1 — that a sandbox dying mid-session is caught,
re-created, and surfaced as a clean error ToolResult (never wedges). All offline; the
real isolation/limits/secret-non-leak is the live check (verify_agent_tools_local.py).
"""

from __future__ import annotations

import pytest
from disco.core.llm import ModelExecutionPolicy
from disco.tools.builtin import build_default_registry
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import agent_scope, research_scope
from disco.tools.sandbox import SandboxSession
from disco.tools.sandbox._container import ContainerInstance
from disco.tools.sandbox.base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxSpec,
    SandboxUnavailableError,
)
from tool_fakes import FakeSandboxInstance, call

# Shared scope for tests that just need the standard agent toolset.
_STANDARD_AGENT_SCOPE = agent_scope(model_policy=ModelExecutionPolicy.standard())

# ---- fakes for the session / death tests -------------------------------------


class CountingInstance:
    """A SandboxInstance that counts execs and can be told to DIE (raise the typed
    death signal) after N execs — models an OOM killing the whole box mid-session."""

    def __init__(self, label: str) -> None:
        self.id = label
        self.owner_id = "o"
        self.conversation_id = "c"
        self.spec = SandboxSpec()
        self.execs = 0
        self.destroyed = False
        self.die_after: int | None = None

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.execs += 1
        if self.die_after is not None and self.execs > self.die_after:
            raise SandboxUnavailableError(f"OOM killed the box ({self.id})")
        return ExecResult(exit_code=0, stdout=f"[{self.id}] {cmd}", stderr="")

    async def read_file(self, path: str) -> bytes:
        return b"data"

    async def write_file(self, path: str, data: bytes) -> None:
        return None

    async def list_dir(self, path: str) -> list[str]:
        return ["a", "b"]

    def display_url(self) -> str | None:
        return None

    async def destroy(self) -> None:
        self.destroyed = True


class CountingService:
    """Creates CountingInstances; only the FIRST one is made mortal, so a re-created
    session lands on a healthy box."""

    name = "fake"

    def __init__(self, first_dies_after: int | None = None) -> None:
        self.created: list[CountingInstance] = []
        self._first_dies_after = first_dies_after

    async def create(self, spec, *, owner_id, conversation_id) -> CountingInstance:
        inst = CountingInstance(f"inst-{len(self.created) + 1}")
        if self._first_dies_after is not None and not self.created:
            inst.die_after = self._first_dies_after
        self.created.append(inst)
        return inst

    async def get(self, instance_id):
        return next((i for i in self.created if i.id == instance_id), None)


class TimeoutInstance(FakeSandboxInstance):
    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self._alive()
        return ExecResult(exit_code=124, stdout="partial output\n", stderr="", timed_out=True)


# ---- the new tool + timeout surfacing ----------------------------------------


async def test_file_list_tool_lists_workspace():
    sbx = FakeSandboxInstance()
    await sbx.write_file("a.txt", b"1")
    await sbx.write_file("b.txt", b"2")
    ex = DefaultToolExecutor(build_default_registry(), _STANDARD_AGENT_SCOPE, sandbox=sbx)
    res = await ex.execute(call("file_list", path="."))
    assert res.success and res.structured["entries"] == ["a.txt", "b.txt"]


async def test_shell_timeout_is_surfaced_legibly():
    ex = DefaultToolExecutor(
        build_default_registry(), _STANDARD_AGENT_SCOPE, sandbox=TimeoutInstance()
    )
    res = await ex.execute(call("shell", command="sleep 999"))
    assert res.success is False
    assert res.structured["timed_out"] is True
    assert "partial output" in res.content  # partial output preserved, not lost
    assert "timed out" in (res.error or "")


async def test_code_exec_timeout_is_surfaced():
    # FakeSandboxInstance now has a kernel property
    ex = DefaultToolExecutor(
        build_default_registry(), _STANDARD_AGENT_SCOPE, sandbox=FakeSandboxInstance()
    )
    res = await ex.execute(call("code_exec", language="python", code="while True: pass"))
    assert res.success is False and res.structured["timed_out"] is True


async def test_code_exec_python_state_persists_across_cells(tmp_path):
    """Stateful CodeAct (the 'persistent kernel' contract): a name defined in one
    Python cell is in scope in the next — proven against the REAL process sandbox."""
    from disco.tools.sandbox.process import ProcessSandboxInstance, ProcessSandboxService

    ws = tmp_path / "ws"
    ws.mkdir()
    # SandboxSession owns the kernel; DefaultToolExecutor accepts it as 'sandbox'.
    svc = ProcessSandboxService(root=str(tmp_path))
    session = SandboxSession(svc, owner_id="o", conversation_id="c")

    ex = DefaultToolExecutor(build_default_registry(), _STANDARD_AGENT_SCOPE, sandbox=session)

    # Mock the pwd for the session
    from unittest.mock import patch
    with patch.object(ProcessSandboxInstance, "exec_shell") as mock_exec:
        mock_exec.return_value = ExecResult(exit_code=0, stdout=str(ws), stderr="")

        r1 = await ex.execute(call("code_exec", language="python", code="x = 21 * 2"))
        assert r1.success, r1.content
        # the next cell sees `x` from the previous one — not a fresh interpreter
        r2 = await ex.execute(call("code_exec", language="python", code="print(x)"))
        assert r2.success and "42" in r2.content, r2.content


async def test_code_exec_erroring_cell_keeps_prior_state(tmp_path):
    """A cell that raises surfaces the traceback + a failure, but does NOT wipe the
    session — names bound before it remain available (kernel-like)."""
    from disco.tools.sandbox.process import ProcessSandboxInstance, ProcessSandboxService
    ws = tmp_path / "ws"
    ws.mkdir()
    
    svc = ProcessSandboxService(root=str(tmp_path))
    session = SandboxSession(svc, owner_id="o", conversation_id="c")
    
    ex = DefaultToolExecutor(build_default_registry(), _STANDARD_AGENT_SCOPE, sandbox=session)

    from unittest.mock import patch
    with patch.object(ProcessSandboxInstance, "exec_shell") as mock_exec:
        mock_exec.return_value = ExecResult(exit_code=0, stdout=str(ws), stderr="")

        await ex.execute(call("code_exec", language="python", code="counter = 7"))
        err = await ex.execute(
            call("code_exec", language="python", code="raise ValueError('boom')")
        )
        assert err.success is False and "ValueError" in err.content
        # state survived the error
        ok = await ex.execute(call("code_exec", language="python", code="print(counter + 1)"))
        assert ok.success and "8" in ok.content, ok.content


# ---- agent-vs-research scoping (the registry split) --------------------------


async def test_agent_tools_are_scoped_out_of_research():
    reg = build_default_registry()
    research = DefaultToolExecutor(reg, research_scope(), sandbox=FakeSandboxInstance())
    # state-changing agent tools are unknown in the Research scope (never executed)
    for name, args in [("shell", {"command": "ls"}), ("file_write", {"path": "x", "content": "y"})]:
        res = await research.execute(call(name, **args))
        assert res.success is False and res.structured["kind"] == "unknown_tool"
    # …but available in the Agent scope
    agent = DefaultToolExecutor(reg, _STANDARD_AGENT_SCOPE, sandbox=FakeSandboxInstance())
    res = await agent.execute(call("shell", command="echo hi"))
    assert res.success is True


# ---- the resilient session (carried lesson #1) -------------------------------


def test_session_is_a_sandbox_instance():
    assert isinstance(SandboxSession(CountingService()), SandboxInstance)


async def test_session_reuses_one_instance_across_calls():
    svc = CountingService()
    session = SandboxSession(svc, SandboxSpec())
    for _ in range(3):
        await session.exec_shell("echo", timeout_s=5)
    await session.read_file("f")
    assert len(svc.created) == 1 and session.generation == 1  # create once, reuse


async def test_session_recreates_on_mid_session_death():
    svc = CountingService(first_dies_after=1)  # first box dies after its 1st exec
    session = SandboxSession(svc, SandboxSpec())

    first = await session.exec_shell("step 1", timeout_s=5)
    assert "inst-1" in first.stdout and session.generation == 1

    # the death: caught, re-created, surfaced as a CLEAN SandboxError (not the raw
    # SandboxUnavailableError, and not a wedge)
    with pytest.raises(SandboxError) as ei:
        await session.exec_shell("step 2", timeout_s=5)
    assert "re-created" in str(ei.value)
    assert session.generation == 2 and len(svc.created) == 2
    assert svc.created[0].destroyed is True  # the dead box was torn down

    # the NEXT call lands on the fresh box and works
    third = await session.exec_shell("step 3", timeout_s=5)
    assert "inst-2" in third.stdout


async def test_session_closed_rejects_use():
    session = SandboxSession(CountingService(), SandboxSpec())
    await session.exec_shell("warm up", timeout_s=5)
    await session.destroy()
    with pytest.raises(SandboxError, match="closed"):
        await session.exec_shell("after close", timeout_s=5)


async def test_executor_returns_clean_result_when_box_dies_under_a_tool():
    """The integration: a tool running through a session over a mortal box. The death
    becomes a clean `sandbox_error` ToolResult (the loop's observation), NEVER a raise,
    and the next tool call works on the re-created box."""
    svc = CountingService(first_dies_after=1)
    session = SandboxSession(svc, SandboxSpec())
    ex = DefaultToolExecutor(build_default_registry(), _STANDARD_AGENT_SCOPE, sandbox=session)

    ok = await ex.execute(call("shell", command="step 1"))
    assert ok.success is True

    died = await ex.execute(call("shell", command="step 2"))  # box OOMs here
    assert died.success is False and died.structured["kind"] == "sandbox_error"
    assert "re-created" in died.content  # the loop can SEE what happened

    recovered = await ex.execute(call("shell", command="step 3"))  # fresh box
    assert recovered.success is True and "inst-2" in recovered.content


# ---- death-typing logic (the backend seam the session relies on) -------------


class _Container:
    def __init__(self, status: str) -> None:
        self.status = status

    def reload(self) -> None:
        if self.status == "gone":
            raise RuntimeError("404 no such container")


def _instance(container) -> ContainerInstance:
    return ContainerInstance(
        id="x",
        owner_id="o",
        conversation_id="c",
        spec=SandboxSpec(),
        container=container,
        container_workspace="/workspace",
        stop_timeout_s=5,
    )


def test_dead_box_is_typed_unavailable_live_box_is_not():
    # a stopped/removed container → SandboxUnavailableError (session re-creates)
    assert isinstance(
        _instance(_Container("exited"))._classify_failure_sync(Exception("boom")),
        SandboxUnavailableError,
    )
    assert isinstance(
        _instance(_Container("gone"))._classify_failure_sync(Exception("boom")),
        SandboxUnavailableError,
    )
    # a still-running container → a generic SandboxError (a per-op failure, NOT a death)
    err = _instance(_Container("running"))._classify_failure_sync(Exception("boom"))
    assert isinstance(err, SandboxError) and not isinstance(err, SandboxUnavailableError)

"""W3 C-2 — a DoD `command` predicate must run IN the sandbox, never as a
host-side `subprocess(shell=True)`.

The command text is model-authored (it is copied from the plan step's
`done_condition`), so before the fix a plan whose step carried
`command: "curl … | sh"` executed on the HOST at `finish`. `FinishGate`'s
`build_dod_evaluator` now injects an in-sandbox command runner (mirroring the
existing in-sandbox http probe) whenever the backend exposes `exec_shell`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from disco.core import DoDSpec
from disco.core.dod import CommandExitPredicate, FileExistsPredicate, HTTPOkPredicate
from disco.core.loop.finish import FinishGate
from loop_fakes import ScriptedAgent, build_loop

CID = "conv-c2"


class _ExecShellSandbox:
    """A container-style sandbox: it has both a workspace path AND `exec_shell`,
    so `build_dod_evaluator` should route the command predicate through the box."""

    def __init__(self, workspace_path: str, exit_code: int) -> None:
        self.workspace_path = workspace_path
        self._exit = exit_code
        self.calls: list[str] = []

    async def exec_shell(self, command: str, timeout_s: int = 30):  # noqa: ANN201
        self.calls.append(command)
        return SimpleNamespace(
            exit_code=self._exit, stdout="ran-in-box", stderr="", timed_out=False
        )


class _Executor:
    def __init__(self, sandbox: _ExecShellSandbox) -> None:
        self.sandbox = sandbox

    def available_tools(self):  # noqa: ANN201
        return []

    async def execute(self, call):  # noqa: ANN001, ANN201
        raise AssertionError("not used")


async def test_dod_command_predicate_routes_through_exec_shell(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = _ExecShellSandbox(str(ws), exit_code=0)
    loop, _ = build_loop(ScriptedAgent([]), executor=_Executor(sbx), conversation_id=CID)

    ev = await FinishGate(loop).verification.build_dod_evaluator()
    spec = DoDSpec(predicates=[CommandExitPredicate(cmd="test -f marker", expect_exit=0)])
    verdict = await ev.evaluate(spec)

    # The predicate ran THROUGH the sandbox (recorded), not a host subprocess.
    assert sbx.calls == ["test -f marker"]
    assert verdict.passed is True  # exit 0 from the box


async def test_dod_command_uses_sandbox_verdict_not_host(tmp_path):
    # The box reports exit 1; the evaluator must honor the SANDBOX result, proving
    # it never fell back to evaluating on the host filesystem.
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = _ExecShellSandbox(str(ws), exit_code=1)
    loop, _ = build_loop(ScriptedAgent([]), executor=_Executor(sbx), conversation_id=CID)

    ev = await FinishGate(loop).verification.build_dod_evaluator()
    # A host-only path that WOULD exist on the host (exit 0) but not in the box.
    spec = DoDSpec(predicates=[CommandExitPredicate(cmd="test -f /etc/hostname", expect_exit=0)])
    verdict = await ev.evaluate(spec)

    assert sbx.calls == ["test -f /etc/hostname"]
    assert verdict.passed is False  # sandbox said exit 1 → the box's answer won


async def test_dod_command_hard_deny_still_applies_in_sandbox_runner(tmp_path):
    # The destructive floor stays in force even on the in-sandbox path: a
    # `rm -rf /` predicate is denied (not run) before exec_shell is touched.
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = _ExecShellSandbox(str(ws), exit_code=0)
    loop, _ = build_loop(ScriptedAgent([]), executor=_Executor(sbx), conversation_id=CID)

    ev = await FinishGate(loop).verification.build_dod_evaluator()
    spec = DoDSpec(predicates=[CommandExitPredicate(cmd="rm -rf -- /", expect_exit=0)])
    verdict = await ev.evaluate(spec)

    assert sbx.calls == []  # never reached the box
    assert verdict.passed is False
    assert verdict.results[0].details.get("denied") is True


async def test_container_dod_file_evidence_is_checked_inside_sandbox_without_host_path():
    class _ContainerSandbox:
        workspace_path = None

        def __init__(self) -> None:
            self.file_calls = []
            self.shell_calls = []

        async def file_exists(self, path: str) -> bool:
            self.file_calls.append(path)
            return path == "artifact.txt"

        async def resolve_relpath(self, path: str) -> str:
            return path

        async def exec_shell(self, command: str, timeout_s: int = 5):
            self.shell_calls.append(command)
            return SimpleNamespace(exit_code=0, stdout="", stderr="", timed_out=False)

    sandbox = _ContainerSandbox()
    loop, _ = build_loop(ScriptedAgent([]), executor=_Executor(sandbox), conversation_id=CID)
    evaluator = await FinishGate(loop).verification.build_dod_evaluator()
    verdict = await evaluator.evaluate(
        DoDSpec(predicates=[FileExistsPredicate(path="artifact.txt")])
    )

    assert verdict.passed is True
    assert sandbox.file_calls == ["artifact.txt"]
    assert sandbox.shell_calls == ["sh -c 'test -s \"$1\"' disco artifact.txt"]


async def test_plan_step_command_predicate_applies_hard_deny_floor(tmp_path):
    # W3 C-2/C-3 (codex #7): the plan-step `command` predicate path (advisory C18)
    # must apply the destructive floor BEFORE it reaches exec_shell / host
    # subprocess. `rm -rf --no-preserve-root /` is refused, never run.
    from disco.core.loop.plan_conditions import PlanStepConditions

    ws = tmp_path / "ws"
    ws.mkdir()

    class _TripSandbox:
        workspace_path = str(ws)

        async def exec_shell(self, command: str, timeout_s: int = 5):  # noqa: ANN201
            raise AssertionError("hard-denied command reached the sandbox")

    loop, _ = build_loop(ScriptedAgent([]), executor=_Executor(_TripSandbox()), conversation_id=CID)
    psc = PlanStepConditions(loop)
    ok, note = await psc.check_command_for_plan_step(
        CommandExitPredicate(cmd="rm -rf --no-preserve-root /", expect_exit=0)
    )
    assert ok is False
    assert "hard-denied" in note


async def test_partial_sandbox_grades_nothing_on_the_host(tmp_path, monkeypatch):
    """The production shape of the deleted fallbacks: an executor whose sandbox
    exposes a `workspace_path` but none of the evidence API (no `exec_shell`,
    no `file_exists`). `build_dod_evaluator` still constructs — and every
    predicate comes back UN-EVALUATABLE instead of being graded against the
    agent-server's own process, filesystem and network.
    """
    import socket
    import subprocess
    from pathlib import Path

    import disco.core.host_egress as host_egress

    touched: list[str] = []
    spawned: list[object] = []
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, lambda *a, **kw: spawned.append((a, kw)))
    monkeypatch.setattr(Path, "exists", lambda self: touched.append(str(self)) or True)
    monkeypatch.setattr(Path, "is_file", lambda self: touched.append(str(self)) or True)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("socket"))

    async def _no_wire(*args: object, **kwargs: object) -> object:
        raise AssertionError("an HTTP connection was opened")

    monkeypatch.setattr(host_egress, "guarded_get", _no_wire)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>on the host</h1>")

    class _WorkspaceOnlySandbox:
        workspace_path = str(ws)

    loop, _ = build_loop(
        ScriptedAgent([]), executor=_Executor(_WorkspaceOnlySandbox()), conversation_id=CID
    )
    ev = await FinishGate(loop).verification.build_dod_evaluator()
    verdict = await ev.evaluate(
        DoDSpec(
            predicates=[
                FileExistsPredicate(path="index.html"),
                CommandExitPredicate(cmd="test -f index.html", expect_exit=0),
                HTTPOkPredicate(url="http://127.0.0.1:8000/", expect_status=200),
            ]
        )
    )

    assert spawned == [] and touched == []
    assert verdict.passed is False
    assert len(verdict.unmet) == 3
    for result in verdict.results:
        assert result.passed is False
        assert result.unverifiable is True
        assert "not evaluated" in result.reason
        assert "no sandbox available" in result.reason

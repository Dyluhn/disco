"""C1c DoD finish-gate WIRING — the gate is now armed from the plan's own
machine-checkable `file_exists` done_conditions on plan approval.

Before this, `set_dod_spec` had no production caller, so `get_dod_spec` was
always None and `finish_dod_gate_passed` was a permanent no-op (the agent
could declare "done" with the requested deliverables absent). This wires
`AgentLoop._arm_dod_from_plan()` at every plan-approval exit so `finish` is
blocked until the files the model said it would create actually exist —
the strongest anti-gaming check that needs no human (Devin/Kiro/Terminal-Bench
all anchor "done" to externally-checkable state, never to self-assessment).

Scope of the slice under test (deliberately conservative):
  * file_exists ONLY (command/http_ok deferred — infra-false-block ambiguity).
  * empty-guard: a plan with no file_exists predicate arms NOTHING (an empty
    spec would deterministic-block; we keep the gate dark instead).
  * write-once: a revision's re-arm is swallowed (the first plan's DoD holds).

Mirrors the fakes harness in test_c18_plan_step_done_condition.py.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode
from loop_fakes import ScriptedAgent, action_step, build_loop

CID = "conv-c1c"


def _finish_call() -> dict:
    """A REAL `finish` tool call — routes through normalize_finish_step where the
    C1c DoD gate lives (the finished-flag fake bypasses it)."""
    return action_step("finish", {"summary": "done"})


def _dod_refused(events: list) -> bool:
    """True iff the C1c gate emitted its unmet-predicate refusal reminder."""
    return any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "Definition-of-Done evaluator found" in e.message.content
        for e in events
    )


class _FakeSandbox:
    def __init__(self, workspace_path: str) -> None:
        self.workspace_path = workspace_path

    async def file_exists(self, path: str) -> bool:
        from pathlib import Path

        return (Path(self.workspace_path) / path).exists()


class _SandboxExecutor:
    def __init__(self, sandbox: _FakeSandbox) -> None:
        self.sandbox = sandbox

    def available_tools(self):
        from disco.core.llm import ToolSpec

        return [
            ToolSpec(name="shell", description="run a shell command", parameters_schema={}),
            ToolSpec(name="plan_step", description="mark plan step progress", parameters_schema={}),
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


def _plan_step(title: str, done_condition: dict | None = None) -> dict:
    step: dict = {"title": title}
    if done_condition is not None:
        step["done_condition"] = done_condition
    return step


async def _run(agent, sbx) -> tuple[list, object]:
    loop, store = build_loop(agent, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    await loop.run()
    return await store.get_events(CID), store


def _final_status(events: list):
    return next((e for e in reversed(events) if isinstance(e, StatusEvent)), None)


@pytest.mark.asyncio
async def test_arms_dod_spec_from_file_exists_on_approval(tmp_path):
    """A plan with a file_exists done_condition arms a DoDSpec on approval —
    the previously-dark gate is now wired."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>home</h1>")  # satisfy it so the run finishes
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [
            _plan_step("create index.html", {"kind": "file_exists", "path": "index.html"}),
        ]}),
        action_step("shell", {"command": "echo build"}),
        _finish_call(),
    ])
    _events, store = await _run(agent, sbx)
    spec = await store.get_dod_spec(CID)
    assert spec is not None, "the gate must be armed from the plan's file_exists condition"
    assert [p.path for p in spec.predicates] == ["index.html"]
    assert all(getattr(p, "kind", None) == "file_exists" for p in spec.predicates)


@pytest.mark.asyncio
async def test_blocks_finish_when_declared_file_missing(tmp_path):
    """The model declares it will create index.html, then finishes WITHOUT
    creating it → the armed gate REFUSES finish and names the missing deliverable.
    This is the declare-then-skip hole closed by construction. (After the bounded
    refusal cap the gate releases to avoid trapping a stuck run forever — that
    safety-valve is correct and covered elsewhere; here we assert the BLOCK fired.)"""
    ws = tmp_path / "ws"
    ws.mkdir()  # index.html deliberately NOT planted
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [
            _plan_step("create index.html", {"kind": "file_exists", "path": "index.html"}),
        ]}),
        action_step("shell", {"command": "echo did not actually write the file"}),
        _finish_call(),
    ])
    events, store = await _run(agent, sbx)
    assert await store.get_dod_spec(CID) is not None  # gate was armed
    assert _dod_refused(events), "finish must be REFUSED while the declared deliverable is missing"
    # the refusal must NAME the unmet predicate so the model knows what to fix
    refusal = next(
        e for e in events
        if isinstance(e, MessageEvent) and "Definition-of-Done evaluator found" in e.message.content
    )
    assert "index.html" in refusal.message.content


@pytest.mark.asyncio
async def test_releases_finish_when_declared_file_present(tmp_path):
    """Same plan, but the deliverable IS created → the gate passes and the run
    finishes. The gate enforces substance without false-blocking real work."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>home</h1>")
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [
            _plan_step("create index.html", {"kind": "file_exists", "path": "index.html"}),
        ]}),
        action_step("shell", {"command": "echo wrote index.html"}),
        _finish_call(),
    ])
    events, _store = await _run(agent, sbx)
    fs = _final_status(events)
    assert fs is not None and fs.status == ConversationStatus.FINISHED


@pytest.mark.asyncio
async def test_empty_guard_no_predicate_leaves_gate_dark(tmp_path):
    """A plan with NO done_condition arms NOTHING (an empty spec would
    deterministic-block; we keep the gate inert and the run finishes as before)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [_plan_step("just do it")]}),
        action_step("shell", {"command": "echo build"}),
        _finish_call(),
    ])
    events, store = await _run(agent, sbx)
    assert await store.get_dod_spec(CID) is None, "no checkable predicate → no spec (no false-block)"
    fs = _final_status(events)
    assert fs is not None and fs.status == ConversationStatus.FINISHED


@pytest.mark.asyncio
async def test_command_only_plan_does_not_arm_in_v1(tmp_path):
    """v1 ships file_exists ONLY. A plan whose only done_condition is a `command`
    (infra-false-block ambiguity) must NOT arm a spec yet — deferred to a later
    slice that adds an infra-vs-task channel."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [
            _plan_step("run the build", {"kind": "command", "command": "make", "expect_exit": 0}),
        ]}),
        action_step("shell", {"command": "echo build"}),
        _finish_call(),
    ])
    _events, store = await _run(agent, sbx)
    assert await store.get_dod_spec(CID) is None, "command predicates are excluded from the v1 auto-spec"

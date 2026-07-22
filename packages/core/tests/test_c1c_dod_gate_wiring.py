"""C1c/H368 wiring for revision-scoped plan verification conditions."""

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
from disco.core.dod import FileExistsPredicate
from disco.core.llm import OperatingMode
from disco.core.loop import signals
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
        and (
            "Definition-of-Done evaluator found" in e.message.content
            or "plan's verification conditions failed" in e.message.content
        )
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
async def test_approves_file_exists_as_plan_owned_without_external_copy(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>home</h1>")  # satisfy it so the run finishes
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        _plan_step(
                            "create index.html", {"kind": "file_exists", "path": "index.html"}
                        ),
                    ],
                },
            ),
            action_step("shell", {"command": "echo build"}),
            _finish_call(),
        ]
    )
    events, store = await _run(agent, sbx)
    assert await store.get_external_dod_spec(CID) is None
    plan = signals.latest_approved_plan(events)
    assert plan is not None
    assert [step.done_condition.path for step in plan.steps] == ["index.html"]


@pytest.mark.asyncio
async def test_blocks_finish_when_declared_file_missing(tmp_path):
    """The model declares it will create index.html, then finishes WITHOUT
    creating it → the current plan verifier REFUSES finish and names the missing
    deliverable. Repeated unchanged failure enters bounded replan/STUCK recovery;
    it never turns the unmet predicate into a pass."""
    ws = tmp_path / "ws"
    ws.mkdir()  # index.html deliberately NOT planted
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        _plan_step(
                            "create index.html", {"kind": "file_exists", "path": "index.html"}
                        ),
                    ],
                },
            ),
            action_step("shell", {"command": "echo did not actually write the file"}),
            _finish_call(),
        ]
    )
    events, store = await _run(agent, sbx)
    assert signals.latest_approved_plan(events) is not None
    assert _dod_refused(events), "finish must be REFUSED while the declared deliverable is missing"
    # the refusal must NAME the unmet predicate so the model knows what to fix
    refusal = next(
        e
        for e in events
        if isinstance(e, MessageEvent)
        and "plan's verification conditions failed" in e.message.content
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
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        _plan_step(
                            "create index.html", {"kind": "file_exists", "path": "index.html"}
                        ),
                    ],
                },
            ),
            action_step("shell", {"command": "echo wrote index.html"}),
            _finish_call(),
        ]
    )
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
    agent = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [_plan_step("just do it")]}),
            action_step("shell", {"command": "echo build"}),
            _finish_call(),
        ]
    )
    events, store = await _run(agent, sbx)
    assert await store.get_dod_spec(CID) is None, (
        "no checkable predicate → no spec (no false-block)"
    )
    fs = _final_status(events)
    assert fs is not None and fs.status == ConversationStatus.FINISHED


@pytest.mark.asyncio
async def test_approved_plan_authority_reconstructs_after_restart(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.html").write_text("<h1>home</h1>")
    sbx = _FakeSandbox(str(ws))
    # loop 1: submit a plan (persists the PlanEvent), but do NOT approve.
    agent1 = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        _plan_step(
                            "create index.html", {"kind": "file_exists", "path": "index.html"}
                        ),
                    ],
                },
            ),
        ]
    )
    loop1, store = build_loop(agent1, executor=_SandboxExecutor(sbx), conversation_id=CID)
    loop1.mode = OperatingMode.PLANNING
    loop1._planning_tools = frozenset(["file_read"])
    await loop1.send_message("go")
    await loop1.run()  # lands AWAITING_PLAN_APPROVAL; _plan_step_predicates lives only here

    # loop 2: a BRAND-NEW loop over the SAME store (simulates the restart — its
    # _plan_step_predicates is empty). Approving must still arm from the PlanEvent.
    loop2, _store2 = build_loop(
        ScriptedAgent([]), store=store, executor=_SandboxExecutor(sbx), conversation_id=CID
    )
    assert loop2._plan_step_predicates == {}  # the in-memory map is genuinely gone
    await loop2.approve_plan()
    events = await store.get_events(CID)
    plan = signals.latest_approved_plan(events)
    assert plan is not None
    assert [step.done_condition.path for step in plan.steps] == ["index.html"]
    assert await store.get_external_dod_spec(CID) is None


@pytest.mark.asyncio
async def test_approved_revision_replaces_plan_predicates_instead_of_unioning(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = _FakeSandbox(str(ws))
    loop, store = build_loop(ScriptedAgent([]), executor=_SandboxExecutor(sbx), conversation_id=CID)
    # First approved plan requires index.html.
    p1 = loop._plan_from_args(
        {
            "summary": "v1",
            "steps": [
                {
                    "title": "create index.html",
                    "done_condition": {"kind": "file_exists", "path": "index.html"},
                },
            ],
        },
        [],
    )
    await loop._emit(p1)
    await loop._emit(await loop._plan_approval_status(p1, await store.get_events(CID)))

    # Revision (the steer "also add a Contact page") → EXTENDS to require contact.html.
    events = await store.get_events(CID)
    p2 = loop._plan_from_args(
        {
            "summary": "v2",
            "steps": [
                {"title": "index", "done_condition": {"kind": "file_exists", "path": "index.html"}},
                {
                    "title": "contact",
                    "done_condition": {"kind": "file_exists", "path": "contact.html"},
                },
            ],
        },
        events,
    )
    # A replacement does not union, but moving an approved deliverable is an
    # explicit audited rename rather than a silent acceptance-bar drop.
    p2 = p2.model_copy(
        update={
            "steps": [
                p2.steps[1].model_copy(
                    update={
                        "done_condition": FileExistsPredicate(
                            path="contact.html", renamed_from="index.html"
                        )
                    }
                )
            ]
        }
    )
    await loop._emit(p2)
    await loop._emit(await loop._plan_approval_status(p2, await store.get_events(CID)))
    events = await store.get_events(CID)
    current = signals.latest_approved_plan(events)
    assert current is not None and current.id == p2.id
    assert [step.done_condition.path for step in current.steps] == ["contact.html"]
    assert any(isinstance(event, type(p1)) and event.id == p1.id for event in events)
    assert await store.get_external_dod_spec(CID) is None


@pytest.mark.asyncio
async def test_infra_failure_blocks_finish_with_visible_unverified_reason(tmp_path):
    """S-W5 D4: an unavailable check is not a pass. A denied command keeps
    finish blocked and records the specific unverifiable predicate."""
    import sys

    from disco.core.dod import CommandExitPredicate, DoDSpec
    from disco.core.dod_evaluator import DoDEvaluator

    sys.path.insert(0, "packages/core/tests")
    from test_dod_evaluator import _denied_command_runner, _passing_http_probe

    ws = tmp_path / "ws"
    ws.mkdir()
    loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id=CID,
        dod_evaluator_factory=lambda: DoDEvaluator(
            ws, command_runner=_denied_command_runner, http_probe=_passing_http_probe
        ),
    )
    await store.set_dod_spec(
        CID, DoDSpec(predicates=[CommandExitPredicate(cmd="make", expect_exit=0)])
    )
    passed = await loop._finish.finish_dod_gate_passed()
    assert passed is False
    events = await store.get_events(CID)
    blockers = [
        e
        for e in events
        if isinstance(e, MessageEvent) and "Definition-of-Done evaluator found" in e.message.content
    ]
    assert len(blockers) == 1
    assert "could not verify" in blockers[0].message.content
    assert "hard-denied" in blockers[0].message.content


@pytest.mark.asyncio
async def test_command_predicate_is_current_plan_owned_condition(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    sbx = _FakeSandbox(str(ws))
    agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [
                        _plan_step(
                            "run the build", {"kind": "command", "cmd": "make", "expect_exit": 0}
                        ),
                    ],
                },
            ),
            action_step("shell", {"command": "echo build"}),
            _finish_call(),
        ]
    )
    events, store = await _run(agent, sbx)
    plan = signals.latest_approved_plan(events)
    assert plan is not None
    assert [getattr(step.done_condition, "kind", None) for step in plan.steps] == ["command"]
    assert await store.get_external_dod_spec(CID) is None

"""B2 + B6 — re-entering PLANNING after a finished build must RE-PLAN.

These two bugs share ONE root cause. After a build FINISHED and the user added a
new instruction, `enter_planning()` correctly flipped the loop to PLANNING mode,
but the agent — in an "execution frame of mind" — ran a long string of
`file_read`s with EXECUTION intent and NEVER called `submit_plan`. No PlanEvent
was ever emitted, so:

  * B2: the frontend revise-plan spinner waited forever (no new PlanEvent).
  * B6: the agent free-built without re-planning.

The fix is four additive parts (all exercised here):

  (a) PLANNING-mode exploration is CAPPED — after `_PLAN_EXPLORE_READ_CAP`
      consecutive planning reads with no submit_plan, ONE forcing reminder is
      injected ("you have enough context — call submit_plan now").
  (b) `enter_planning()` FRAMES a revision: when a plan was already approved and
      the user gave a concrete new instruction, it emits a RE-PLANNING reminder.
  (c) re-grounding in PLANNING mode anchors to the LATEST user instruction, not
      the superseded build GOAL (covered in the recitation-path test below).
  (d) plan revision numbers INCREMENT — a re-plan is rev=2, rev=3, … (already
      computed from the count of prior PlanEvents; asserted here end-to-end).

Regression: a NORMAL first-plan flow (no prior approval) is UNCHANGED — no
RE-PLANNING reminder, rev=1, and Phase-1 reads BELOW the cap are still allowed.

Tests use only fakes (no real model / container) — same pattern as
`test_c18_plan_step_done_condition.py`.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    NoOpCondenser,
    PlanEvent,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode
from disco.core.loop import AgentLoop, NeverConfirm
from disco.core.loop.messages import (
    _HS03_REGROUND_SENTINEL,
    _PLAN_EXPLORE_READ_CAP,
    _REPLAN_FRAMING,
)
from loop_fakes import (
    FakeAnalyzer,
    FakeSummarizer,
    ScriptedAgent,
    action_step,
    build_loop,
)

CID = "conv-replan"

_OLD_GOAL = "Build a simple macOS-inspired desktop clone"
_NEW_INSTRUCTION = "please add a simple 3d game to this."


# ---------------------------------------------------------------------------
# A sandbox-less executor that accepts file_read / file_list / shell and always
# succeeds, so PLANNING-mode reads fall through and execute deterministically.
# `available_tools()` lists the read tools so the unknown-tool requery (Rung-7)
# doesn't trip on the names the planner explores with.
# ---------------------------------------------------------------------------


class _ReadExecutor:
    def __init__(self) -> None:
        self.execute_calls: list[ToolCall] = []

    def available_tools(self):
        from disco.core.llm import ToolSpec

        return [
            ToolSpec(name="file_read", description="read a file", parameters_schema={}),
            ToolSpec(name="file_list", description="list files", parameters_schema={}),
            ToolSpec(name="shell", description="run a shell command", parameters_schema={}),
            ToolSpec(name="submit_plan", description="propose a plan", parameters_schema={}),
        ]

    def readonly_tool_names(self):
        return frozenset({"file_read", "file_list", "submit_plan"})

    async def execute(self, call: ToolCall) -> ToolResult:
        self.execute_calls.append(call)
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


def _plan_step(summary: str, *steps: str):
    """A submit_plan AgentStep with the given summary + bare-string steps."""
    return action_step(
        "submit_plan",
        {"summary": summary, "steps": [{"title": s} for s in steps]},
        thought=f"proposing: {summary}",
    )


def _read_step(path: str):
    """A planning-mode file_read AgentStep (varying path avoids the
    StuckDetector's identical-action pattern)."""
    return action_step(
        "file_read",
        {"path": path},
        thought="Let me create the 3D game files and wire everything together",
    )


def _force_reminders(events: list) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "explored" in e.message.content
        and "PLANNING mode without proposing a plan" in e.message.content
    ]


def _replan_reminders(events: list) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message.content == _REPLAN_FRAMING
    ]


def _plan_events(events: list) -> list[PlanEvent]:
    return [e for e in events if isinstance(e, PlanEvent)]


async def _first_build_to_approved(agent, executor) -> tuple[AgentLoop, object]:
    """Drive a first plan → approval so the loop has a prior `plan_approved`
    StatusEvent (the precondition for a revision). Leaves the loop in execution
    mode with the agent's step cursor at index 1 (one submit_plan consumed)."""
    loop, store = build_loop(agent, executor=executor, conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read", "file_list"])
    await loop.send_message(_OLD_GOAL)
    await loop.run()  # step 0: submit_plan → AWAITING_PLAN_APPROVAL
    state = await loop.approve_plan()
    assert state.execution_status == ConversationStatus.RUNNING
    return loop, store


# ---------------------------------------------------------------------------
# Test 1 — five planning-mode reads on a re-plan → the forcing reminder fires.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replan_caps_exploration_and_forces_a_plan():
    """A finished build + a new user instruction + a planner that runs
    `_PLAN_EXPLORE_READ_CAP` consecutive file_reads with no submit_plan → the
    forcing reminder IS emitted (exactly once), and the exploration does not run
    unbounded (the planner then submits a plan)."""
    cap = _PLAN_EXPLORE_READ_CAP
    executor = _ReadExecutor()
    agent = ScriptedAgent(
        [
            _plan_step("first build", "scaffold the desktop"),  # step 0 (approved)
            # the re-plan segment: `cap` reads, then a plan
            *[_read_step(f"game/file_{i}.js") for i in range(cap)],
            _plan_step("add a 3d game", "add a three.js canvas", "wire it up"),
        ]
    )
    loop, store = await _first_build_to_approved(agent, executor)

    await loop.enter_planning(_NEW_INSTRUCTION)
    await loop.run()  # serves the `cap` reads then the revised plan

    events = await store.get_events(CID)

    # The forcing reminder fired exactly once (append-once at the threshold).
    forces = _force_reminders(events)
    assert len(forces) == 1, (
        f"expected exactly ONE forcing reminder after {cap} planning reads; "
        f"got {len(forces)}"
    )
    assert str(cap) in forces[0].message.content
    assert "submit_plan" in forces[0].message.content

    # Exploration did not run unbounded: the planner submitted a SECOND plan.
    plans = _plan_events(events)
    assert len(plans) == 2, f"expected 2 plans (first build + re-plan); got {len(plans)}"

    # The run halted awaiting approval of the revised plan (not still spinning).
    final = next((e for e in reversed(events) if isinstance(e, StatusEvent)), None)
    assert final is not None
    assert final.status == ConversationStatus.AWAITING_PLAN_APPROVAL

    # The reminder appeared BEFORE the revised plan (it provoked the plan).
    force_seq = forces[0].seq or 0
    replan_seq = plans[1].seq or 0
    assert force_seq < replan_seq


# ---------------------------------------------------------------------------
# Test 2 — enter_planning() on a revision emits the RE-PLANNING reminder.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enter_planning_on_revision_emits_replan_reminder():
    """When a plan was already approved and the user supplies a concrete new
    instruction, `enter_planning()` emits the RE-PLANNING framing reminder."""
    executor = _ReadExecutor()
    agent = ScriptedAgent(
        [
            _plan_step("first build", "scaffold the desktop"),
            _plan_step("add a 3d game", "add a three.js canvas"),
        ]
    )
    loop, store = await _first_build_to_approved(agent, executor)

    await loop.enter_planning(_NEW_INSTRUCTION)

    events = await store.get_events(CID)
    reminders = _replan_reminders(events)
    assert len(reminders) == 1, (
        f"a revision (prior plan_approved + new instruction) must emit exactly "
        f"ONE RE-PLANNING reminder; got {len(reminders)}"
    )
    # The reminder follows the planning status (framing comes after the flip).
    planning_status_seq = next(
        e.seq for e in events if isinstance(e, StatusEvent) and e.detail == "planning"
    )
    assert (reminders[0].seq or 0) > (planning_status_seq or 0)


# ---------------------------------------------------------------------------
# Test 3 — a re-plan that calls submit_plan produces a rev=2 PlanEvent BEFORE
# any execution action.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replan_plan_event_is_revision_two_before_execution():
    """The re-plan's PlanEvent carries `revision == 2`, and it is emitted before
    any execution ActionEvent (the planner proposes, it does not free-build)."""
    executor = _ReadExecutor()
    agent = ScriptedAgent(
        [
            _plan_step("first build", "scaffold the desktop"),
            _plan_step("add a 3d game", "add a three.js canvas"),
        ]
    )
    loop, store = await _first_build_to_approved(agent, executor)

    await loop.enter_planning(_NEW_INSTRUCTION)
    await loop.run()  # serves the second submit_plan → halts for approval

    events = await store.get_events(CID)
    plans = _plan_events(events)
    assert len(plans) == 2
    assert plans[0].revision == 1, "the first plan must be revision 1"
    assert plans[1].revision == 2, (
        f"the re-plan must be revision 2 (count of prior plans + 1); "
        f"got {plans[1].revision}"
    )

    # No execution happened during the re-plan segment: the executor only ever
    # ran the planner's reads (none here), never a mutating tool, and the
    # second PlanEvent precedes the AWAITING_PLAN_APPROVAL halt.
    assert executor.execute_calls == [], (
        "the re-plan must propose a plan, NOT execute file edits"
    )


# ---------------------------------------------------------------------------
# Test 4 — re-grounding in PLANNING mode references the NEW instruction, not
# the original build GOAL (part c).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reground_in_planning_mode_uses_new_instruction_not_old_goal():
    """In PLANNING mode the HS-03 recap anchors to the latest user instruction;
    the superseded build GOAL must not be re-injected."""
    loop = AgentLoop(
        CID,
        SqliteEventStore(":memory:"),
        ScriptedAgent([]),
        _ReadExecutor(),
        None,
        FakeAnalyzer(),
        NeverConfirm(),
        NoOpCondenser(),
        FakeSummarizer(),
        mode=OperatingMode.PLANNING,
        assist=True,
        reground_cadence=3,
        execution_mode=OperatingMode.LONG_HORIZON,
    )
    loop.mode = OperatingMode.PLANNING

    # Seed: the original build's plan (its summary IS the old GOAL), a
    # plan_approved status, then the user's NEW instruction.
    await loop.store.append(
        CID,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=_OLD_GOAL)),
    )
    from disco.core import PlanStep

    await loop.store.append(
        CID,
        PlanEvent(
            summary=_OLD_GOAL,
            steps=[PlanStep(title="scaffold the desktop")],
            revision=1,
        ),
    )
    await loop.store.append(
        CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )
    await loop.store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content=_NEW_INSTRUCTION),
        ),
    )

    events = await loop._events()
    after = await loop._maybe_emit_reground(events)

    recaps = [
        e
        for e in after
        if isinstance(e, MessageEvent)
        and e.message.content.startswith(_HS03_REGROUND_SENTINEL)
    ]
    assert len(recaps) == 1, "assist=ON + a plan + count==0 → one post-resume recap"
    body = recaps[0].message.content
    assert _NEW_INSTRUCTION in body, (
        "PLANNING-mode reground must anchor to the new instruction"
    )
    assert _OLD_GOAL not in body, (
        "PLANNING-mode reground must NOT re-inject the superseded build GOAL"
    )


# ---------------------------------------------------------------------------
# Regression — a NORMAL first plan (no prior approval) is UNCHANGED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_plan_is_unchanged_no_replan_reminder_rev_one():
    """A first plan: enter_planning with NO prior approval emits NO RE-PLANNING
    reminder; a Phase-1 read below the cap is still allowed (no forcing
    reminder); the plan is revision 1."""
    executor = _ReadExecutor()
    # Below-cap exploration (cap-1 reads) then a plan — no forcing reminder.
    agent = ScriptedAgent(
        [
            *[_read_step(f"src/file_{i}.py") for i in range(_PLAN_EXPLORE_READ_CAP - 1)],
            _plan_step("first build", "scaffold the app"),
        ]
    )
    loop, store = build_loop(agent, executor=executor, conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read", "file_list"])

    await loop.send_message("build me an app")
    await loop.enter_planning("")  # first entry, no prior plan_approved, no text
    await loop.run()

    events = await store.get_events(CID)

    assert _replan_reminders(events) == [], (
        "a first plan (no prior approval) must NOT get a RE-PLANNING reminder"
    )
    assert _force_reminders(events) == [], (
        f"{_PLAN_EXPLORE_READ_CAP - 1} reads is below the cap — Phase-1 reads "
        f"must still be allowed without a forcing reminder"
    )
    plans = _plan_events(events)
    assert len(plans) == 1
    assert plans[0].revision == 1, "a first plan must be revision 1"

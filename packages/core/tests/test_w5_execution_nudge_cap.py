"""W5 — execution-nudge gate cap (finish.py gate_execution_nudge).

The spec (§10.9 W5 / §11.2): give the one UNcapped gate an explicit cap-3 +
visible-warning terminal. After _EXECUTION_NUDGE_CAP consecutive nudges without
productive action, the gate parks the run at AWAITING_USER_QUESTION with legacy
detail `approve_plan_no_execution` (a plan approved but never executed is NOT a
false FINISHED) — rather than spinning until max_iterations.

Tests:
  1. After exactly 3 nudges the gate explains and asks (not FINISHED/PAUSED).
  2. The landing emits a visible warning that the plan was not executed.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    MessageEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.events import EventSource
from disco.core.llm import OperatingMode, ToolSpec
from loop_fakes import (
    FakeExecutor,
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
    finish_step,
)

CID = "conv-nudge-cap"


class _PlanExecutor(FakeExecutor):
    """FakeExecutor that handles submit_plan (returns ok) and exposes
    planning tools — needed so the planner gate wires correctly."""

    def __init__(self):
        super().__init__(
            tools=[
                ToolSpec(name="submit_plan", description="submit plan", parameters_schema={}),
                ToolSpec(name="shell", description="shell", parameters_schema={}),
            ]
        )

    async def execute(self, call):
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


def _env_messages(events) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


@pytest.mark.asyncio
async def test_execution_nudge_releases_after_cap():
    """After exactly _EXECUTION_NUDGE_CAP (3) nudges without productive action
    the gate explains and asks (approve_plan_no_execution) with a
    visible "plan was not executed" warning in the trace — NOT a false
    FINISHED (§11.2)."""
    # 1. Plan phase: agent submits a plan then stops (AWAITING_PLAN_APPROVAL).
    # 2. Approval: approve_plan() emits plan_approved + switches to execution mode.
    # 3. Execution phase: agent immediately tries to finish without doing work.
    #    The nudge gate fires 3× then releases.
    plan_agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {
                    "summary": "p",
                    "steps": [{"title": "do the thing"}],
                },
            ),
        ]
    )
    loop, store = build_loop(
        plan_agent,
        conversation_id=CID,
        executor=_PlanExecutor(),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message("build something")
    await loop.run()  # planning phase → AWAITING_PLAN_APPROVAL
    await loop.approve_plan()  # emits plan_approved, flips to execution mode

    # Now run the execution phase with an agent that immediately tries to finish.
    exec_agent = ScriptedAgent([finish_step()])
    loop.agent = exec_agent  # swap the agent for the execution phase
    await loop.run()  # execution phase: nudge×3 then release

    events = await store.get_events(CID)
    env = _env_messages(events)

    # Exactly 3 nudge system-reminders before the cap.
    nudges = [m for m in env if "The approved plan has not been executed" in m]
    assert len(nudges) == 3, (
        f"expected 3 nudges before cap, got {len(nudges)}: {env}"
    )

    # The terminal warning is present and says the plan was not executed.
    warnings = [m for m in env if "no execution action was taken" in m]
    assert len(warnings) == 1, f"expected one terminal warning, got: {env}"
    assert "Plan approved but no execution action was taken" in warnings[0], warnings[0]

    # Run lands AWAITING_USER/approve_plan_no_execution — NOT FINISHED.
    statuses = [e.status.value for e in events if isinstance(e, StatusEvent)]
    assert "FINISHED" not in statuses, f"must not FINISH, got {statuses}"
    assert_blocked_question_landing(events, legacy_detail="approve_plan_no_execution")


@pytest.mark.asyncio
async def test_execution_nudge_release_is_loud():
    """The terminal warning must mention how many reminders fired so the user
    can diagnose a run that was approved but never executed its plan."""
    plan_agent = ScriptedAgent(
        [
            action_step(
                "submit_plan",
                {"summary": "p", "steps": [{"title": "t"}]},
            ),
        ]
    )
    loop, store = build_loop(
        plan_agent,
        conversation_id=CID,
        executor=_PlanExecutor(),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
    )
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()

    exec_agent = ScriptedAgent([finish_step()])
    loop.agent = exec_agent  # swap the agent for the execution phase
    await loop.run()

    events = await store.get_events(CID)
    env = _env_messages(events)
    warnings = [m for m in env if "no execution action was taken" in m]
    assert warnings, "terminal warning not found"
    # Must reference the count (3).
    assert "3" in warnings[0], (
        f"terminal warning should mention count 3: {warnings[0]!r}"
    )
    # And it lands AWAITING_USER/approve_plan_no_execution, not FINISHED.
    assert_blocked_question_landing(events, legacy_detail="approve_plan_no_execution")

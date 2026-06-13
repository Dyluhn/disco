"""Autonomous mode — LOOP behavior (beyond tool-schema withholding).

Locks the headless-stall fixes found by review:

  B1: a mid-run `propose_plan_update` must AUTO-APPROVE in autonomous mode. The
      loop's own auto-continue nudge tells the model to call propose_plan_update to
      revise an incomplete plan; in interactive mode that halts at
      AWAITING_PLAN_APPROVAL for a human, but in autonomous mode there is no human,
      so it would stall forever. It must instead approve inline and keep running.

  Contrast: interactive mode STILL halts at AWAITING_PLAN_APPROVAL — the gate is
  intact; autonomous is the only path that auto-approves.
"""

from __future__ import annotations

import pytest
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent
from disco.core.llm import OperatingMode
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"


def _plan_approvals(events):
    return [
        e
        for e in events
        if isinstance(e, StatusEvent) and e.detail == "plan_approved"
    ]


def _awaiting_plan(events):
    return [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.status == ConversationStatus.AWAITING_PLAN_APPROVAL
    ]


def _script():
    # plan → real work → mid-run plan revision → more work → finish.
    # `shell` is what the default FakeExecutor actually offers, so these steps
    # execute as real ActionEvents (resetting the actionless valve); the point of
    # the test is the propose_plan_update interception, not the work itself.
    return ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {"command": "echo one"}),
            action_step(
                "propose_plan_update",
                {"summary": "p2", "steps": [{"title": "1"}, {"title": "2"}]},
            ),
            action_step("shell", {"command": "echo two"}),
            finish_step(),
        ]
    )


@pytest.mark.asyncio
async def test_autonomous_auto_approves_mid_run_plan_update():
    store = SqliteEventStore(":memory:")
    loop, store = build_loop(_script(), store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    loop._autonomous = True  # the property under test

    await loop.send_message("go")
    # A single run() drives the whole thing — autonomous never hands back for a
    # human approval, so there is no approve_plan() call in this test.
    await loop.run()

    events = await store.get_events(CID)
    # The gate was NEVER hit — no halt waiting for a human.
    assert not _awaiting_plan(events), "autonomous run halted at AWAITING_PLAN_APPROVAL"
    # BOTH the initial submit_plan AND the mid-run propose_plan_update auto-approved.
    assert len(_plan_approvals(events)) >= 2


@pytest.mark.asyncio
async def test_interactive_still_halts_for_plan_update():
    store = SqliteEventStore(":memory:")
    loop, store = build_loop(_script(), store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    # autonomous defaults OFF here.

    await loop.send_message("go")
    s1 = await loop.run()  # halts at the INITIAL plan for approval
    assert s1.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    s2 = await loop.run()  # file_write, then propose_plan_update → halts AGAIN
    assert s2.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

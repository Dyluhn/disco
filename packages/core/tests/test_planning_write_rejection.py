"""Regression tests for the PLANNING-mode tool gate (engine.py _gate_planning_mode).

Closes WRITE_TOOL_ALLOWED_IN_PLANNING (P0) + WRITE_BEFORE_REVISION_APPROVAL (P1):
a non-allowlist tool (file_write/shell/browser/...) attempted in PLANNING must be
REJECTED with a recoverable, model-visible AgentErrorEvent — NEVER executed — both on
the first plan and on a revision re-entry, while submit_plan still works afterward.

Drives the REAL AgentLoop via loop_fakes (no live model, no sandbox).
"""

from __future__ import annotations

from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import ActionEvent, AgentErrorEvent, ObservationEvent, PlanEvent
from disco.core.events import ConversationStatus
from loop_fakes import ScriptedAgent, action_step


def _submit_plan_step(summary="p"):
    return action_step("submit_plan", {"summary": summary, "steps": [{"title": "do"}]})


async def test_write_in_planning_is_rejected_then_recovers_to_plan():
    """A file_write in PLANNING is rejected (paired AgentErrorEvent, never executed);
    the model gets another turn and submit_plan still produces a PlanEvent + halts at
    AWAITING_PLAN_APPROVAL."""
    agent = ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "bad"}),
            _submit_plan_step("plan after rejection"),
        ]
    )
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id="pw-reject", executor=executor)
    await loop.send_message("create a page")
    await loop.run()

    events = await store.get_events("pw-reject")

    # The write was ATTEMPTED (one ActionEvent recorded for audit/KV pairing) ...
    write_actions = [
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "file_write"
    ]
    assert len(write_actions) == 1, "expected exactly one attempted file_write ActionEvent"
    write_action = write_actions[0]

    # ... but it was REJECTED with a recoverable AgentErrorEvent paired by tool_call_id,
    # and NEVER executed.
    rejections = [
        e
        for e in events
        if isinstance(e, AgentErrorEvent) and e.action_id == write_action.id
    ]
    assert len(rejections) == 1, "expected exactly one paired rejection for the write"
    assert rejections[0].tool_call_id == write_action.tool_call.call_id
    assert "PLANNING" in rejections[0].error

    assert not any(c.tool_name == "file_write" for c in executor.calls), "write reached executor"
    assert "index.html" not in executor.world, "the file must NOT have been written"
    assert not any(
        isinstance(e, ObservationEvent) and e.tool_result.tool_name == "file_write"
        for e in events
    ), "no observation for an unexecuted write"

    # The agent got a SECOND turn and recovered with submit_plan.
    assert len(agent.seen_tools) >= 2, "the model did not get a second turn after rejection"
    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert len(plans) == 1, "submit_plan after the rejection must produce a PlanEvent"
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_write_during_revision_is_rejected_before_revised_approval():
    """On a revision re-entry (back in PLANNING), a write is rejected before the
    revised plan is approved; submit_plan then yields PlanEvent.revision == 2."""
    agent = ScriptedAgent([_submit_plan_step("first")])
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id="pw-revise", executor=executor)
    await loop.send_message("build a page")
    await loop.run()
    await loop.approve_plan()

    # Re-enter planning for a revision; the agent tries to write, then submits.
    loop.agent = ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "x"}),
            _submit_plan_step("second"),
        ]
    )
    await loop.enter_planning("revise the heading")
    await loop.run()

    events = await store.get_events("pw-revise")

    write_actions = [
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "file_write"
    ]
    assert len(write_actions) == 1
    assert any(
        isinstance(e, AgentErrorEvent) and e.action_id == write_actions[0].id for e in events
    ), "the revision write must be rejected"
    assert not any(c.tool_name == "file_write" for c in executor.calls)
    assert "index.html" not in executor.world

    plans = [e for e in events if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2], "the revised plan submitted after rejection"
    assert (await loop.get_state()).execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

"""§20.3 Tool-rejection recovery — driven against the REAL loop (guidelines §15.5,
§20.3).

The RECOVERY half of the contract (the loop survives a wrong call and accepts the
next valid submit_plan) holds today and passes. The REJECTION half (the disallowed
write is visibly rejected, not executed) is the surfaced product bug and is
xfail(strict=True). See development/notes/build-soak-surfaced-bugs.md.
"""

from __future__ import annotations

from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationState,
    ConversationStatus,
    PlanEvent,
)
from loop_fakes import ScriptedAgent, action_step

CID = "tool-reject"


def _script_wrong_then_plan():
    # §15.5: a disallowed write in PLANNING, then a valid plan.
    return ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "bad"}),
            action_step(
                "submit_plan",
                {"summary": "Plan after rejection", "steps": [{"title": "x"}]},
            ),
        ]
    )


async def _drive(cid):
    agent = _script_wrong_then_plan()
    executor = BuildExecutor()
    loop, store = build_plan_loop(agent, conversation_id=cid, executor=executor)
    await loop.send_message("create a page")
    await loop.run()
    return loop, store, executor


async def test_disallowed_tool_call_visible_as_rejection():
    """The disallowed write emits an AgentErrorEvent (rejection) visible to the
    model — not a successful execution."""
    _loop, store, _exec = await _drive("tool-reject-vis")
    events = await store.get_events("tool-reject-vis")
    write = next(
        e for e in events if isinstance(e, ActionEvent) and e.tool_call.tool_name == "file_write"
    )
    assert any(isinstance(e, AgentErrorEvent) and e.action_id == write.id for e in events)


async def test_loop_continues_and_accepts_next_submit_plan():
    """The loop did NOT abort on the wrong call: the subsequent submit_plan is
    accepted and the run halts awaiting plan approval (recovery)."""
    loop, store, _exec = await _drive("tool-reject-cont")
    state = await loop.get_state()
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    plans = [e for e in await store.get_events("tool-reject-cont") if isinstance(e, PlanEvent)]
    assert len(plans) == 1 and plans[0].revision == 1


async def test_state_remains_consistent_after_rejection():
    """The event log replays to the same final state (no divergence)."""
    loop, store, _exec = await _drive("tool-reject-state")
    events = await store.get_events("tool-reject-state")
    replayed = ConversationState.reconstruct("tool-reject-state", events)
    assert replayed.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert replayed == ConversationState.reconstruct("tool-reject-state", events)

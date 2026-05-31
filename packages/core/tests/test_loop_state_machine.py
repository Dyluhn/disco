"""State machine — agent-loop-contract.md §10.1.

Transitions exercised with a scripted fake Agent; assert the emitted StatusEvent
sequence and the reconstructed ConversationState.
"""

from __future__ import annotations

from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step
from perpleximanus.core import (
    ActionEvent,
    ConversationStatus,
    ErrorEvent,
    ObservationEvent,
    StatusEvent,
)
from perpleximanus.core.loop import StuckThresholds

CID = "conv"


async def _statuses(store, cid=CID):
    events = await store.get_events(cid)
    return [e.status for e in events if isinstance(e, StatusEvent)]


async def test_run_to_finish_emits_running_then_finished():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("do the task")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    statuses = await _statuses(store)
    assert statuses[0] == ConversationStatus.RUNNING
    assert statuses[-1] == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert sum(isinstance(e, ActionEvent) for e in events) == 1
    assert sum(isinstance(e, ObservationEvent) for e in events) == 1


async def test_finished_reopens_to_idle_on_new_message():
    agent = ScriptedAgent(
        [action_step(), finish_step(), action_step("shell", {"x": 1}), finish_step()]
    )
    loop, store = build_loop(agent)
    await loop.send_message("first goal")
    s1 = await loop.run()
    assert s1.execution_status == ConversationStatus.FINISHED

    # A new message reopens the conversation (FINISHED → IDLE), then it runs.
    reopened = await loop.send_message("second goal")
    assert reopened.execution_status == ConversationStatus.IDLE
    s2 = await loop.run()
    assert s2.execution_status == ConversationStatus.FINISHED
    # The second goal was actually processed by the agent (the View saw it).
    assert any("second goal" in m.content for m in agent.seen_views[-1].messages)


async def test_max_iterations_forces_error():
    # Always-act agent (never finishes); disable stuck so the ceiling is what fires.
    agent = ScriptedAgent([action_step()])
    loop, store = build_loop(
        agent, max_iterations=3, stuck_thresholds=StuckThresholds(repeat_action_observation=99)
    )
    await loop.send_message("loop forever")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.ERROR
    events = await store.get_events(CID)
    errs = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errs) == 1 and errs[0].code == "max_iterations"
    # Exactly max_iterations actions were taken before the ceiling fired.
    assert sum(isinstance(e, ActionEvent) for e in events) == 3

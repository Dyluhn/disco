"""Concurrency, steering, pause/cancel — agent-loop-contract.md §10.6.

The FIFO lock makes pause/cancel land between steps (never mid-action) and
concurrent user input is never dropped.
"""

from __future__ import annotations

import asyncio

from loop_fakes import GatedAgent, ScriptedAgent, action_step, build_loop, finish_step
from perpleximanus.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
)

CID = "conv"


def _dangling(events):
    observed = {
        e.action_id
        for e in events
        if isinstance(e, ObservationEvent | AgentErrorEvent) and e.action_id is not None
    }
    return [e for e in events if isinstance(e, ActionEvent) and e.id not in observed]


async def test_concurrent_message_is_not_dropped_and_seen_next_iteration():
    loop, store = build_loop(ScriptedAgent([action_step(), finish_step()]))

    async def inject():
        # Simulate a message arriving (via the pending-buffer) during step 0 —
        # appended directly to the store, as the wire layer would.
        await store.append(
            CID,
            MessageEvent(
                source=EventSource.USER, message=LLMMessage(role="user", content="INTERRUPT")
            ),
        )

    loop.agent._before = {0: inject}  # run the injection at the start of step 0
    await loop.send_message("initial goal")
    await loop.run()

    # The injected message was not dropped and was visible to the NEXT step's View.
    assert any("INTERRUPT" in m.content for m in loop.agent.seen_views[1].messages)


async def test_pause_lands_between_steps_never_mid_action():
    agent = GatedAgent([action_step(), action_step(), finish_step()], gate_at=1)
    loop, store = build_loop(agent)
    await loop.send_message("go")

    run_task = asyncio.create_task(loop.run())
    await agent.reached.wait()  # loop is inside step 1, holding the lock
    pause_task = asyncio.create_task(loop.pause())  # queues behind the lock
    await asyncio.sleep(0)
    agent.proceed.set()  # let step 1 produce its action
    state = await run_task
    await pause_task

    assert state.execution_status == ConversationStatus.PAUSED
    events = await store.get_events(CID)
    # The in-flight action completed and was observed — pause didn't abort it.
    assert _dangling(events) == []
    assert len(loop.executor.calls) == 2  # both actions ran to completion


async def test_cancel_stops_the_loop_at_a_checkpoint():
    agent = GatedAgent([action_step(), action_step(), finish_step()], gate_at=1)
    loop, store = build_loop(agent)
    await loop.send_message("go")

    run_task = asyncio.create_task(loop.run())
    await agent.reached.wait()
    cancel_task = asyncio.create_task(loop.cancel())
    await asyncio.sleep(0)
    agent.proceed.set()
    state = await run_task
    await cancel_task

    assert state.execution_status == ConversationStatus.IDLE  # cooperative stop
    assert _dangling(await store.get_events(CID)) == []


async def test_steer_is_a_user_message_at_the_next_checkpoint():
    loop, store = build_loop(ScriptedAgent([action_step(), finish_step()]))
    await loop.send_message("research X")

    async def steer_mid():
        await store.append(
            CID,
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(role="user", content="actually focus on Y"),
                meta={"steer": True},
            ),
        )

    loop.agent._before = {0: steer_mid}
    await loop.run()
    assert any("focus on Y" in m.content for m in loop.agent.seen_views[1].messages)

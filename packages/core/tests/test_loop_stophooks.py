"""Stop-hooks — agent-loop-contract.md §10.7."""

from __future__ import annotations

from disco.core import ConversationStatus, EventSource, MessageEvent
from loop_fakes import ScriptedAgent, ScriptedStopHook, build_loop, finish_step

CID = "conv"


async def test_stop_hook_veto_injects_feedback_and_continues():
    # Agent declares finished twice; the hook vetoes the first, allows the second.
    agent = ScriptedAgent([finish_step(), finish_step()])
    hook = ScriptedStopHook([False, True])
    loop, store = build_loop(agent, stop_hooks=[hook])
    await loop.send_message("are you done?")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert hook.calls == 2  # consulted on each finish attempt
    events = await store.get_events(CID)
    # The veto injected an ENVIRONMENT message the model would see next.
    env_msgs = [
        e for e in events if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert len(env_msgs) == 1


async def test_no_hooks_finishes_immediately():
    agent = ScriptedAgent([finish_step()])
    loop, store = build_loop(agent)  # no stop hooks
    await loop.send_message("done?")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert not any(
        isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT for e in events
    )

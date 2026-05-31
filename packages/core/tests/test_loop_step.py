"""One-action-per-iteration + execute-and-observe pairing — §10.2, §10.3."""

from __future__ import annotations

from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step
from perpleximanus.core import (
    ActionEvent,
    AgentErrorEvent,
    ObservationEvent,
    ToolResult,
)

CID = "conv"


def _dangling_actions(events):
    """ActionEvents with no paired observation/error (the crash-recovery trace)."""
    observed = {
        e.action_id
        for e in events
        if isinstance(e, ObservationEvent | AgentErrorEvent) and e.action_id is not None
    }
    return [e for e in events if isinstance(e, ActionEvent) and e.id not in observed]


# ---- §10.2 one action per iteration -----------------------------------------


async def test_single_action_yields_one_action_and_one_observation():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    assert sum(isinstance(e, ActionEvent) for e in events) == 1
    assert sum(isinstance(e, ObservationEvent) for e in events) == 1
    assert _dangling_actions(events) == []  # the action was observed before finishing


# ---- §10.3 execute-and-observe pairing --------------------------------------


async def test_success_yields_exactly_one_observation_with_matching_action_id():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent)
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    action = next(e for e in events if isinstance(e, ActionEvent))
    obs = [e for e in events if isinstance(e, ObservationEvent)]
    assert len(obs) == 1
    assert obs[0].action_id == action.id


async def test_executor_exception_yields_exactly_one_agent_error():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent, executor=FakeExecutor(raises=RuntimeError("boom")))
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    action = next(e for e in events if isinstance(e, ActionEvent))
    errs = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(errs) == 1
    assert errs[0].action_id == action.id
    assert "boom" in errs[0].error
    assert not any(isinstance(e, ObservationEvent) for e in events)  # never two


async def test_tool_failure_result_yields_agent_error():
    failing = ToolResult(call_id="c", tool_name="shell", success=False, content="", error="exit 1")
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent, executor=FakeExecutor(result=failing))
    await loop.send_message("go")
    await loop.run()
    events = await store.get_events(CID)
    errs = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(errs) == 1 and "exit 1" in errs[0].error


async def test_dangling_action_is_detectable_after_crash():
    """Simulate a crash between the ActionEvent and its observation: the proposed
    action is recorded first (§4.1), so on replay it is a detectable dangling
    action with no paired observation."""
    from perpleximanus.core import SqliteEventStore, ToolCall

    store = SqliteEventStore(":memory:")
    await store.append(
        CID, ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    )
    # ... process killed here, before the observation is appended ...
    events = await store.get_events(CID)
    assert len(_dangling_actions(events)) == 1

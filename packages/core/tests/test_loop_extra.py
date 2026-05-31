"""Coverage for shipped loop paths not hit by the §10 scenario tests:
soft-condense-with-tombstone, thought-only no-op step, run() early returns,
resume(), and the policy/analyzer classes.
"""

from __future__ import annotations

from loop_fakes import (
    FakeAnalyzer,
    FakeCondenser,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)
from perpleximanus.core import (
    ActionEvent,
    CondensationEvent,
    CondensationRequest,
    ConversationStatus,
    EventSource,
    MessageEvent,
    SecurityRisk,
    ToolCall,
)
from perpleximanus.core.loop import (
    AgentStep,
    AlwaysConfirm,
    ConfirmRisky,
    NullSecurityAnalyzer,
    SelfAssessedAnalyzer,
)

CID = "conv"


# ---- soft condensation that DOES produce a tombstone ------------------------


async def test_soft_condense_appends_tombstone_and_continues():
    cond = FakeCondenser(
        request=CondensationRequest(soft=True, reason="events"),
        tombstone=CondensationEvent(forgotten_start_seq=1, forgotten_end_seq=1, summary="[s]"),
    )
    loop, store = build_loop(ScriptedAgent([action_step(), finish_step()]), condenser=cond)
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    assert any(isinstance(e, CondensationEvent) for e in await store.get_events(CID))


# ---- thought-only no-op step ------------------------------------------------


async def test_thought_only_step_records_agent_message_and_continues():
    agent = ScriptedAgent(
        [AgentStep(thought="just thinking out loud", tool_call=None, finished=False), finish_step()]
    )
    loop, store = build_loop(agent)
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(CID)
    assert not any(isinstance(e, ActionEvent) for e in events)  # no action taken
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "thinking out loud" in e.message.content
        for e in events
    )


# ---- run() early returns ----------------------------------------------------


async def test_run_returns_immediately_when_paused():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, _ = build_loop(agent)
    await loop.send_message("go")
    await loop.pause()
    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED
    assert agent.calls == 0  # never stepped


async def test_run_returns_when_finished_with_no_new_work():
    agent = ScriptedAgent([finish_step()])
    loop, _ = build_loop(agent)
    await loop.send_message("done?")
    await loop.run()  # → FINISHED
    calls_after_finish = agent.calls
    again = await loop.run()  # nothing new → no-op
    assert again.execution_status == ConversationStatus.FINISHED
    assert agent.calls == calls_after_finish  # did not step again


async def test_resume_continues_the_loop():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, _ = build_loop(agent)
    await loop.send_message("go")
    await loop.pause()  # paused before running
    state = await loop.resume()  # resume emits RUNNING and drives run()
    assert state.execution_status == ConversationStatus.FINISHED


# ---- policy / analyzer classes ----------------------------------------------


async def test_always_confirm_gates_every_action():
    loop, _ = build_loop(
        ScriptedAgent([action_step(), finish_step()]),
        analyzer=FakeAnalyzer(SecurityRisk.LOW),
        policy=AlwaysConfirm(),
    )
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION


def test_self_assessed_analyzer_returns_actions_risk():
    a = ActionEvent(
        thought="t",
        tool_call=ToolCall(tool_name="shell", arguments={}),
        self_assessed_risk=SecurityRisk.HIGH,
    )
    assert SelfAssessedAnalyzer().assess(a) == SecurityRisk.HIGH


def test_null_analyzer_returns_unknown():
    a = ActionEvent(thought="t", tool_call=ToolCall(tool_name="shell", arguments={}))
    assert NullSecurityAnalyzer().assess(a) == SecurityRisk.UNKNOWN


def test_confirm_risky_threshold_and_unknown():
    p = ConfirmRisky(SecurityRisk.HIGH, confirm_unknown=True)
    assert p.should_confirm(SecurityRisk.HIGH) is True
    assert p.should_confirm(SecurityRisk.LOW) is False
    assert p.should_confirm(SecurityRisk.UNKNOWN) is True
    assert ConfirmRisky(SecurityRisk.MEDIUM).should_confirm(SecurityRisk.MEDIUM) is True

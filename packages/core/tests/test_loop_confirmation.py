"""Two-phase confirmation — agent-loop-contract.md §10.4."""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    ObservationEvent,
    SecurityRisk,
)
from disco.core.loop import ConfirmRisky, NeverConfirm
from loop_fakes import (
    FakeAnalyzer,
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

CID = "conv"


async def test_high_risk_action_is_gated_and_confirm_executes_exactly_it():
    agent = ScriptedAgent([action_step(thought="risky"), finish_step()])
    loop, store = build_loop(
        agent,
        analyzer=FakeAnalyzer(SecurityRisk.HIGH),
        policy=ConfirmRisky(SecurityRisk.HIGH),
    )
    await loop.send_message("do something risky")
    state = await loop.run()

    # Phase 1: proposed + waiting, NOT executed.
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION
    assert state.pending_action_id is not None
    assert loop.executor.calls == []  # nothing ran
    events = await store.get_events(CID)
    assert any(isinstance(e, ActionEvent) for e in events)
    assert not any(isinstance(e, ObservationEvent) for e in events)

    # Phase 2: confirm executes exactly the pending action (no re-ask).
    await loop.confirm()
    assert len(loop.executor.calls) == 1
    after = await store.get_events(CID)
    assert sum(isinstance(e, ObservationEvent) for e in after) == 1
    # The agent was only consulted once (for the proposal), not again on confirm.
    assert agent.calls == 1

    final = await loop.run()  # continue → finish
    assert final.execution_status == ConversationStatus.FINISHED


async def test_reject_records_denial_and_resumes_without_executing():
    agent = ScriptedAgent([action_step(thought="risky"), finish_step()])
    loop, store = build_loop(
        agent, analyzer=FakeAnalyzer(SecurityRisk.HIGH), policy=ConfirmRisky(SecurityRisk.HIGH)
    )
    await loop.send_message("risky")
    await loop.run()

    await loop.reject("denied by operator")
    assert loop.executor.calls == []  # never executed
    events = await store.get_events(CID)
    denials = [e for e in events if isinstance(e, AgentErrorEvent)]
    assert len(denials) == 1 and "denied by operator" in denials[0].error
    state = await loop.get_state()
    assert state.execution_status == ConversationStatus.RUNNING


async def test_never_confirm_surface_never_gates():
    agent = ScriptedAgent([action_step(), finish_step()])
    loop, store = build_loop(agent, analyzer=FakeAnalyzer(SecurityRisk.HIGH), policy=NeverConfirm())
    await loop.send_message("go")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.FINISHED
    statuses = [e.status for e in await store.get_events(CID) if hasattr(e, "status")]
    assert ConversationStatus.WAITING_FOR_CONFIRMATION not in statuses


async def test_plan_preview_gates_the_first_action_on_unknown_risk():
    """The first action is gated under the Agent-surface default (ConfirmRisky
    with confirm-on-UNKNOWN) — the BoD §13.2 plan-preview moment."""
    agent = ScriptedAgent([action_step(thought="the plan"), finish_step()])
    loop, _ = build_loop(
        agent,
        analyzer=FakeAnalyzer(SecurityRisk.UNKNOWN),
        policy=ConfirmRisky(SecurityRisk.HIGH, confirm_unknown=True),
    )
    await loop.send_message("build me a thing")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION
    assert loop.executor.calls == []  # gated before any compute is spent

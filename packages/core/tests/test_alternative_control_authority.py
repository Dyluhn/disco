"""Control-task recovery must preserve strict view ownership and risk gates."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    AlternativesEvent,
    ConversationStatus,
    MessageEvent,
    ObservationEvent,
    SecurityRisk,
    StatusEvent,
    WorkspaceMutationEvent,
    current_workspace_agent_view_id,
)
from disco.core.events import AlternativeOption
from disco.core.loop import ConfirmRisky
from disco.core.loop.engine_contracts import _ACTIVE_AGENT_VIEW_ID
from loop_fakes import FakeAnalyzer, ScriptedAgent, build_loop, finish_step

CID = "conv"
VIEW = "gate-view"


async def _parked_loop(*, high=False):
    loop, store = build_loop(
        ScriptedAgent([finish_step()]),
        policy=ConfirmRisky(),
        analyzer=FakeAnalyzer(SecurityRisk.HIGH if high else SecurityRisk.LOW),
    )
    intent = await store.append(
        CID,
        WorkspaceMutationEvent(operation="agent.run-intent.message", run_protocol_version=1),
    )
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_protocol_version=1,
            run_intent_id=intent.id,
            agent_view_id=VIEW,
        ),
    )
    await store.append(
        CID,
        AlternativesEvent(
            agent_view_id=VIEW,
            summary="Choose a recovery",
            failed_action_id="prior-action",
            options=[
                AlternativeOption(
                    id="label", title="Use a simpler approach", description="", tool_name=""
                ),
                AlternativeOption(
                    id="run",
                    title="Run a check",
                    description="",
                    tool_name="shell",
                    arguments={"command": "echo recovery"},
                ),
                AlternativeOption(
                    id="denied",
                    title="Forbidden",
                    description="",
                    tool_name="shell",
                    arguments={"command": "rm -rf /"},
                ),
            ],
        ),
    )
    await store.append(
        CID,
        StatusEvent(status=ConversationStatus.AWAITING_USER_DECISION, agent_view_id=VIEW),
    )

    async def strict_status(event):
        # Same authority invariant as Agent Server's lifecycle boundary. Unlike
        # old loop tests, an unowned transition cannot silently pass this fixture.
        assert event.agent_view_id == current_workspace_agent_view_id(await store.get_events(CID))
        return await store.append(CID, event)

    loop._terminal_commit_hook = strict_status
    loop.run = AsyncMock(side_effect=loop.get_state)
    return loop, store


@pytest.mark.parametrize("option", ["__continue__", "label", "run", "denied"])
@pytest.mark.parametrize("ambient_view", [None, "unrelated-worker-view"])
async def test_recovery_control_binds_gate_view_and_resumes_only_when_requested(
    option, ambient_view
):
    loop, store = await _parked_loop()
    token = _ACTIVE_AGENT_VIEW_ID.set(ambient_view)
    try:
        # Real UI controls execute in a task other than the model worker.
        state = await asyncio.create_task(loop.pick_alternative(option, resume=False))
        assert _ACTIVE_AGENT_VIEW_ID.get() == ambient_view
    finally:
        _ACTIVE_AGENT_VIEW_ID.reset(token)
    assert state.execution_status is ConversationStatus.RUNNING
    assert state.pending_alternatives_id is None
    loop.run.assert_not_awaited()
    events = await loop._events()
    assert any(isinstance(e, MessageEvent) for e in events)
    if option == "run":
        assert len(loop.executor.calls) == 1
        assert any(isinstance(e, ObservationEvent) and e.agent_view_id == VIEW for e in events)
    else:
        assert loop.executor.calls == []
    if option == "denied":
        assert any(isinstance(e, AgentErrorEvent) and "REFUSED" in e.error for e in events)
    # A duplicate click after resolution has no further effect.
    before = len(await store.get_events(CID))
    await loop.pick_alternative(option, resume=False)
    assert len(await store.get_events(CID)) == before


@pytest.mark.parametrize("decision", ["confirm", "reject"])
async def test_high_risk_recovery_parks_then_requires_explicit_decision(decision):
    loop, store = await _parked_loop(high=True)
    state = await asyncio.create_task(loop.pick_alternative("run", resume=False))
    assert state.execution_status is ConversationStatus.WAITING_FOR_CONFIRMATION
    assert state.pending_action_id is not None
    assert loop.executor.calls == []
    loop.run.assert_not_awaited()
    events = await loop._events()
    proposed = [e for e in events if isinstance(e, ActionEvent)]
    assert len(proposed) == 1
    assert proposed[0].id == state.pending_action_id
    await getattr(loop, decision)()
    assert (await store.get_state(CID)).execution_status is ConversationStatus.RUNNING
    assert len(loop.executor.calls) == (1 if decision == "confirm" else 0)
    await getattr(loop, decision)()
    assert len(loop.executor.calls) == (1 if decision == "confirm" else 0)


async def test_continue_resumes_core_caller_and_restores_ambient_authority():
    loop, _ = await _parked_loop()
    await loop.pick_alternative("__continue__")
    loop.run.assert_awaited_once()
    assert _ACTIVE_AGENT_VIEW_ID.get() is None


async def test_unknown_pick_keeps_gate_and_never_runs():
    loop, _ = await _parked_loop()
    state = await loop.pick_alternative("not-an-option", resume=False)
    assert state.execution_status is ConversationStatus.AWAITING_USER_DECISION
    assert loop.executor.calls == []
    loop.run.assert_not_awaited()


async def test_continue_runs_next_model_step_from_a_different_control_task():
    from loop_fakes import action_step

    agent = ScriptedAgent(
        [
            action_step("shell", {"command": "echo first"}),
            action_step(
                "ask_user",
                {
                    "summary": "Choose recovery",
                    "options": [
                        {"id": "label", "title": "Use another approach", "description": ""}
                    ],
                },
            ),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent)
    await loop.send_message("perform the task")
    await store.append(
        CID,
        WorkspaceMutationEvent(
            operation="agent.run-intent.message",
            run_protocol_version=1,
        ),
    )
    await asyncio.create_task(loop.run())
    assert (
        await store.get_state(CID)
    ).execution_status is ConversationStatus.AWAITING_USER_DECISION
    assert agent.calls == 2
    # Neither task inherits the other task's last model-view context.
    await asyncio.create_task(loop.pick_alternative("__continue__"))
    assert agent.calls == 3
    assert (await store.get_state(CID)).execution_status is ConversationStatus.FINISHED

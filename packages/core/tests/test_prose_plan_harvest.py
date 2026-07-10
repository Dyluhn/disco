from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from disco.core.llm import OperatingMode
from disco.core.loop import AgentStep, signals
from loop_fakes import ScriptedAgent, build_loop

CID = "conv"


def _prose_step(text: str) -> AgentStep:
    return AgentStep(thought=text, tool_call=None, finished=False)


def _user(text: str, seq: int) -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=text),
        seq=seq,
    )


def _agent(text: str, seq: int) -> MessageEvent:
    return MessageEvent(
        source=EventSource.AGENT,
        message=LLMMessage(role="assistant", content=text),
        seq=seq,
    )


def _status(detail: str, seq: int) -> StatusEvent:
    return StatusEvent(status=ConversationStatus.RUNNING, detail=detail, seq=seq)


async def _seed_revision(loop, store) -> None:
    await store.append(
        CID,
        PlanEvent(
            summary="Initial build",
            steps=[PlanStep(title="Build index.html")],
            revision=1,
        ),
    )
    await store.append(
        CID,
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
    )
    await loop.enter_planning("Add a footer with the exact text 'Copyright Acme Cloud 2026'.")


PROSE_PLAN = """Plan (revision 2): Add the requested footer text, then verify.
1. Replace footer copy with `Copyright Acme Cloud 2026`
2. Re-verify the served page returns HTTP 200

Context:
The prior page already exists; only the footer text changes.
"""


def test_numbered_and_bulleted_prose_plan_parsing() -> None:
    text = """<think>
1. This is internal reasoning and must not become a plan step
</think>

Plan:
1. **Edit** `index.html`
- [ ] Re-verify the [served page](http://localhost)
* Preserve the existing layout
"""
    assert signals.prose_plan_steps_from_text(text) == [
        "Edit index.html",
        "Re-verify the served page",
        "Preserve the existing layout",
    ]


def test_unparseable_prose_plan_falls_back_to_pending_user_instruction() -> None:
    events = [
        _user("Add a footer with the exact text 'Copyright Acme Cloud 2026'.", 1),
        _status("planning", 2),
        _agent("Plan: I will handle the requested footer change.", 3),
    ]
    assert [s.title for s in signals.harvest_prose_plan_steps(events)] == [
        "Add a footer with the exact text 'Copyright Acme Cloud 2026'."
    ]


def test_real_submit_plan_preempts_prose_harvest() -> None:
    events = [
        _user("Add a footer", 1),
        _status("planning", 2),
        _status("force_submit_plan", 3),
        _agent(PROSE_PLAN, 4),
        PlanEvent(
            summary="Submitted plan",
            steps=[PlanStep(title="real submitted step")],
            revision=2,
            seq=5,
        ),
    ]
    assert signals.plan_submitted_since_current_planning(events) is True
    assert signals.prose_plan_force_submit(events) is False


@pytest.mark.asyncio
async def test_prose_plan_harvest_flow_halts_at_normal_approval_gate() -> None:
    agent = ScriptedAgent(
        [
            _prose_step(PROSE_PLAN),
            _prose_step(PROSE_PLAN),
            _prose_step(PROSE_PLAN),
            _prose_step(PROSE_PLAN),
        ]
    )
    loop, store = build_loop(agent, conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    await _seed_revision(loop, store)

    state = await loop.run()
    events = await store.get_events(CID)
    plans = [e for e in events if isinstance(e, PlanEvent)]
    harvested = plans[-1]

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert harvested.revision == 2
    assert harvested.id != plans[0].id
    assert [s.title for s in harvested.steps] == [
        "Replace footer copy with Copyright Acme Cloud 2026",
        "Re-verify the served page returns HTTP 200",
    ]
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "REL-RC-N HARVEST" in (e.message.content or "")
        for e in events
    )
    assert any(
        isinstance(e, StatusEvent) and e.detail == "prose_plan_harvested"
        for e in events
    )


@pytest.mark.asyncio
async def test_prose_plan_harvest_is_event_derived_after_replay() -> None:
    loop, store = build_loop(ScriptedAgent([]), conversation_id=CID)
    loop.mode = OperatingMode.PLANNING
    await _seed_revision(loop, store)
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content=PROSE_PLAN),
        ),
    )
    await store.append(
        CID,
        StatusEvent(status=ConversationStatus.RUNNING, detail="force_submit_plan"),
    )

    replayed = await store.get_events(CID)
    assert signals.prose_plan_force_submit(replayed) is True

    second_agent = ScriptedAgent([_prose_step(PROSE_PLAN)])
    resumed, _ = build_loop(second_agent, store=store, conversation_id=CID)
    resumed.mode = OperatingMode.PLANNING

    state = await resumed.run()
    events = await store.get_events(CID)
    harvested = [e for e in events if isinstance(e, PlanEvent)][-1]

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert harvested.revision == 2
    assert [s.title for s in harvested.steps][:1] == [
        "Replace footer copy with Copyright Acme Cloud 2026"
    ]

"""Durable plan-phase truth at the model and dispatch boundaries."""

from __future__ import annotations

import pytest
from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    PlanStep,
    PlanVerificationTransition,
    StatusEvent,
)
from disco.core.llm import OperatingMode
from disco.core.loop.view_render import (
    _PLAN_EXECUTION_RECEIPT_MARKER,
    _PLAN_PLANNING_RECEIPT_MARKER,
    ViewBuilder,
)
from event_fakes import with_seqs
from loop_fakes import (
    ScriptedAgent,
    action_step,
    finish_step,
)


def _projection_loop():
    return build_plan_loop(ScriptedAgent([]), conversation_id="phase-projection")[0]


def _approved_revision_events(*, compacted: bool = False):
    rejected = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user",
            content=(
                "<system-reminder>Your proposed plan was NOT accepted. "
                "Submit a corrected plan.</system-reminder>"
            ),
        ),
        meta={"blocking": "invalid_plan_done_conditions"},
    )
    plan = PlanEvent(
        summary="Add the requested menu",
        steps=[PlanStep(title="Edit index.html"), PlanStep(title="Verify the page")],
        revision=2,
    )
    events = [
        rejected,
        plan,
        StatusEvent(
            status=ConversationStatus.AWAITING_PLAN_APPROVAL,
            detail=plan.id,
        ),
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_approved",
            plan_verification_transition=PlanVerificationTransition(
                old_plan_revision=1,
                new_plan_revision=2,
                new_plan_event_id=plan.id,
            ),
        ),
    ]
    if compacted:
        events.append(
            CondensationEvent(
                summary="An earlier rejected plan attempt was resolved.",
                forgotten_start_seq=1,
                forgotten_end_seq=1,
                reason="tokens",
            )
        )
    return with_seqs(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("compacted", [False, True])
@pytest.mark.parametrize("context_pack", ["off", "on"])
async def test_approved_plan_projects_authoritative_execution_receipt(
    monkeypatch, compacted: bool, context_pack: str
) -> None:
    """Approval retires stale rejection feedback and survives re-render/restart."""

    monkeypatch.setenv("DISCO_CONTEXT_PACK", context_pack)
    events = _approved_revision_events(compacted=compacted)

    first = await ViewBuilder(_projection_loop()).build(events)
    restarted = await ViewBuilder(_projection_loop()).build(events)
    first_text = [message.content for message in first.messages]

    assert not any("Your proposed plan was NOT accepted" in text for text in first_text)
    receipts = [text for text in first_text if _PLAN_EXECUTION_RECEIPT_MARKER in text]
    assert len(receipts) == 1
    assert "Plan revision 2 is APPROVED" in receipts[0]
    assert "now in EXECUTION mode" in receipts[0]
    assert "Do not call `submit_plan`" in receipts[0]
    assert "earlier invalid-plan or correct-and-resubmit instruction" in receipts[0]
    assert first.fingerprint() == restarted.fingerprint()


@pytest.mark.asyncio
async def test_unsuperseded_or_unbound_invalid_feedback_remains_visible(monkeypatch) -> None:
    """Retirement requires an exact valid approval binding, never mere chronology."""

    monkeypatch.setenv("DISCO_CONTEXT_PACK", "off")
    rejected = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="current invalid plan feedback"),
        meta={"blocking": "invalid_plan_done_conditions"},
    )
    unapproved = with_seqs([rejected])
    unapproved_view = await ViewBuilder(_projection_loop()).build(unapproved)
    assert any(message.content == rejected.message.content for message in unapproved_view.messages)

    plan = PlanEvent(summary="candidate", steps=[PlanStep(title="work")], revision=2)
    mismatched = with_seqs(
        [
            rejected,
            plan,
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="plan_approved",
                plan_verification_transition=PlanVerificationTransition(
                    new_plan_revision=3,
                    new_plan_event_id=plan.id,
                ),
            ),
        ]
    )
    mismatched_view = await ViewBuilder(_projection_loop()).build(mismatched)
    assert any(message.content == rejected.message.content for message in mismatched_view.messages)
    assert not any(
        _PLAN_EXECUTION_RECEIPT_MARKER in message.content for message in mismatched_view.messages
    )


@pytest.mark.asyncio
async def test_retirement_is_event_precise_not_content_based(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", "off")
    events = _approved_revision_events()
    duplicate = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=events[0].message.content),  # type: ignore[union-attr]
    ).model_copy(update={"seq": len(events) + 1})
    view = await ViewBuilder(_projection_loop()).build([*events, duplicate])
    assert sum(message.content == duplicate.message.content for message in view.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("context_pack", ["off", "on"])
async def test_initial_planning_projects_authoritative_phase_and_real_scope(
    monkeypatch, context_pack: str
) -> None:
    """Initial plan-first turns receive the same phase truth as later revisions."""

    monkeypatch.setenv("DISCO_CONTEXT_PACK", context_pack)
    loop, store = build_plan_loop(
        ScriptedAgent([]), conversation_id=f"phase-initial-{context_pack}"
    )
    await loop.send_message("Build me a landing page")

    events = await store.get_events(f"phase-initial-{context_pack}")
    view = await ViewBuilder(loop).build(events)
    receipts = [
        message.content
        for message in view.messages
        if _PLAN_PLANNING_RECEIPT_MARKER in message.content
    ]
    assert len(receipts) == 1
    assert "CURRENT PHASE: PLANNING for revision 1" in receipts[0]
    assert "initial plan" in receipts[0]
    assert "Build me a landing page" in receipts[0]
    assert not any(_PLAN_EXECUTION_RECEIPT_MARKER in message.content for message in view.messages)
    if context_pack == "on":
        pack = next(
            message.content for message in view.messages if "<context-pack>" in message.content
        )
        assert "Allowed next actions:" in pack
        assert "submit_plan" in pack
        assert "file_read" in pack
        assert "file_write" not in pack


@pytest.mark.asyncio
@pytest.mark.parametrize("context_pack", ["off", "on"])
async def test_revision_planning_projects_authoritative_phase_and_real_scope(
    monkeypatch, context_pack: str
) -> None:
    monkeypatch.setenv("DISCO_CONTEXT_PACK", context_pack)
    initial = ScriptedAgent(
        [action_step("submit_plan", {"summary": "Build", "steps": [{"title": "Build"}]})]
    )
    loop, store = build_plan_loop(initial, conversation_id=f"phase-planning-{context_pack}")
    await loop.send_message("Build the page")
    await loop.run()
    await loop.approve_plan()
    await loop.enter_planning("Add the exact footer text")

    events = await store.get_events(f"phase-planning-{context_pack}")
    view = await ViewBuilder(_projection_loop()).build(events)
    receipts = [
        message.content
        for message in view.messages
        if _PLAN_PLANNING_RECEIPT_MARKER in message.content
    ]
    assert len(receipts) == 1
    assert "CURRENT PHASE: PLANNING for revision 2" in receipts[0]
    assert "Revision 1 is only the previously approved baseline" in receipts[0]
    assert "Add the exact footer text" in receipts[0]
    assert "Workspace mutation and execution tools remain unavailable" in receipts[0]
    assert not any(_PLAN_EXECUTION_RECEIPT_MARKER in message.content for message in view.messages)
    if context_pack == "on":
        pack = next(
            message.content for message in view.messages if "<context-pack>" in message.content
        )
        assert "Allowed next actions:" in pack
        assert "submit_plan" in pack
        assert "file_read" in pack
        assert "file_write" not in pack


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_out_of_phase_submit_plan_is_paired_and_never_reaches_executor(
    malformed: bool,
) -> None:
    """A remembered planning-only call gets phase guidance, then execution recovers."""

    initial = ScriptedAgent(
        [action_step("submit_plan", {"summary": "Build it", "steps": [{"title": "Do it"}]})]
    )
    executor = BuildExecutor()
    loop, store = build_plan_loop(
        initial,
        conversation_id=f"phase-submit-refusal-{malformed}",
        executor=executor,
    )
    await loop.send_message("Build the page")
    await loop.run()
    await loop.approve_plan()

    execution = ScriptedAgent(
        [
            # Deliberately malformed too: the phase guard must run before any
            # executor/schema path and explain approval, not argument syntax.
            action_step(
                "submit_plan",
                {
                    "summary": "Build it",
                    "steps": [
                        {
                            "title": "Do it",
                            **({"done_condition": "static"} if malformed else {}),
                        }
                    ],
                },
            ),
            action_step("file_write", {"path": "index.html", "content": "<h1>Done</h1>"}),
            finish_step(),
        ]
    )
    loop.agent = execution
    await loop.run()

    events = await store.get_events(f"phase-submit-refusal-{malformed}")
    plans = [event for event in events if isinstance(event, PlanEvent)]
    submitted = [
        event
        for event in events
        if isinstance(event, ActionEvent)
        and event.tool_call is not None
        and event.tool_call.tool_name == "submit_plan"
    ]
    assert len(plans) == 1
    assert len(submitted) == 1
    refusal = next(
        event
        for event in events
        if isinstance(event, AgentErrorEvent) and event.action_id == submitted[0].id
    )
    assert refusal.tool_call_id == submitted[0].tool_call.call_id
    assert "Plan revision 1 is already APPROVED" in refusal.error
    assert "Do not retry `submit_plan`" in refusal.error
    assert "failed validation" not in refusal.error
    assert all(call.tool_name != "submit_plan" for call in executor.calls)
    assert any(call.tool_name == "file_write" for call in executor.calls)
    assert "submit_plan" not in execution.seen_tools[0]
    assert loop.mode == OperatingMode.LONG_HORIZON


@pytest.mark.asyncio
async def test_legitimate_execution_plan_update_still_reaches_approval_gate() -> None:
    initial = ScriptedAgent(
        [action_step("submit_plan", {"summary": "Build", "steps": [{"title": "Build"}]})]
    )
    executor = BuildExecutor()
    loop, store = build_plan_loop(
        initial,
        conversation_id="phase-legitimate-update",
        executor=executor,
    )
    await loop.send_message("Build the page")
    await loop.run()
    await loop.approve_plan()
    loop.agent = ScriptedAgent(
        [
            action_step("file_write", {"path": "index.html", "content": "first"}),
            action_step(
                "propose_plan_update",
                {"summary": "Revise", "steps": [{"title": "Apply the discovered revision"}]},
            ),
        ]
    )
    state = await loop.run()
    events = await store.get_events("phase-legitimate-update")

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert [event.revision for event in events if isinstance(event, PlanEvent)] == [1, 2]
    assert all(call.tool_name != "propose_plan_update" for call in executor.calls)

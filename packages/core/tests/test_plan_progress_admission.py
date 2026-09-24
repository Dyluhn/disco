"""The real execution seam validates progress against the current approved plan."""

import pytest
from disco.core import (
    AgentErrorEvent,
    ConversationStatus,
    ObservationEvent,
    PlanEvent,
    PlanStep,
    StatusEvent,
)
from disco.core.events import PlanVerificationTransition
from disco.core.view import effective_plan_progress
from test_bug18_actionable_validation import CID, _drive, _make_loop, _real_executor, _upp


async def add_plan(loop, count, revision=1, *, approved=True):
    plan = await loop.store.append(
        CID,
        PlanEvent(
            summary="current task",
            revision=revision,
            steps=[PlanStep(title=f"Step {i}") for i in range(1, count + 1)],
        ),
    )
    if approved:
        await loop.store.append(
            CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
        )
    return plan


async def test_real_progress_tool_rejects_fourteen_rows_then_accepts_current_six(monkeypatch):
    executor = _real_executor()
    loop, store = _make_loop(executor)
    await add_plan(loop, 7)
    current = await add_plan(loop, 6, 2)
    executed = []
    original = executor.execute

    async def track(call):
        executed.append(call.call_id)
        return await original(call)

    monkeypatch.setattr(executor, "execute", track)
    invalid = _upp(
        "wrong-plan", [{"index": i, "state": "done" if i < 14 else "active"} for i in range(1, 15)]
    )
    events = await _drive(loop, invalid)
    error = next(e for e in events if isinstance(e, AgentErrorEvent) and e.action_id == invalid.id)
    assert "current approved plan revision 2 has 6 steps" in error.to_llm_message().content
    assert "none of this update's rows were applied" in error.error
    assert executed == []
    assert effective_plan_progress(events) == (current, {})
    corrected = _upp(
        "current-plan", [{"index": i, "state": "done" if i < 6 else "active"} for i in range(1, 7)]
    )
    events = await _drive(loop, corrected)
    result = next(
        e for e in events if isinstance(e, ObservationEvent) and e.action_id == corrected.id
    )
    assert result.tool_result.success and result.tool_result.content == "plan progress: 5/6 done"
    assert executed == ["current-plan"]
    assert effective_plan_progress(events)[1] == {**dict.fromkeys(range(1, 6), "done"), 6: "active"}
    store.close()


async def test_sparse_progress_updates_remain_valid_and_omitted_steps_are_retained():
    loop, store = _make_loop(_real_executor())
    await add_plan(loop, 6)
    await _drive(loop, _upp("first", [{"index": 1, "state": "done"}]))
    events = await _drive(loop, _upp("second", [{"index": 2, "state": "active"}]))
    assert effective_plan_progress(events)[1] == {1: "done", 2: "active"}
    assert len([e for e in events if isinstance(e, ObservationEvent)]) == 2
    store.close()


@pytest.mark.parametrize("missing", [True, False])
async def test_progress_cannot_target_missing_or_unapproved_current_plan(missing):
    loop, store = _make_loop(_real_executor())
    if not missing:
        await add_plan(loop, 14)
        await add_plan(loop, 6, 2, approved=False)
    action = _upp("unapproved", [{"index": 1, "state": "done"}])
    events = await _drive(loop, action)
    assert any(isinstance(e, AgentErrorEvent) and e.action_id == action.id for e in events)
    assert not any(isinstance(e, ObservationEvent) for e in events)
    assert effective_plan_progress(events)[1] == {}
    store.close()


async def test_progress_checks_plan_after_executor_fence_preparation(monkeypatch):
    loop, store = _make_loop(_real_executor())
    await add_plan(loop, 14)
    original = loop._prepare_executor

    async def prepare(events=None):
        await original(events)
        await add_plan(loop, 6, 2)

    monkeypatch.setattr(loop, "_prepare_executor", prepare)
    action = _upp("superseded", [{"index": i, "state": "done"} for i in range(1, 15)])
    events = await _drive(loop, action)
    error = next(e for e in events if isinstance(e, AgentErrorEvent) and e.action_id == action.id)
    assert "revision 2 has 6 steps" in error.error
    assert not any(isinstance(e, ObservationEvent) for e in events)
    assert effective_plan_progress(events)[1] == {}
    store.close()


async def test_approval_for_another_revision_cannot_authorize_current_progress():
    loop, store = _make_loop(_real_executor())
    old = await add_plan(loop, 7)
    await add_plan(loop, 6, 2, approved=False)
    await store.append(
        CID,
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="plan_approved",
            plan_verification_transition=PlanVerificationTransition(
                new_plan_revision=old.revision, new_plan_event_id=old.id
            ),
        ),
    )
    action = _upp("foreign-approval", [{"index": 1, "state": "done"}])
    events = await _drive(loop, action)
    assert any(isinstance(e, AgentErrorEvent) and e.action_id == action.id for e in events)
    assert effective_plan_progress(events)[1] == {}
    store.close()

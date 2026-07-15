"""§20.2 Replan contract — driven against the REAL loop via the request_plan ->
enter_planning path (control_ops.py:116), NOT a plain send_message.

Spec contracts (guidelines §11.4, §20.2). Spec-violations are xfail(strict=True)
with the surfaced bug code; see docs/build-soak-surfaced-bugs.md.
"""

from __future__ import annotations

from _buildsoak_fakes import BuildExecutor, build_plan_loop
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from loop_fakes import ScriptedAgent, action_step


def _submit_plan_step(summary="p"):
    return action_step("submit_plan", {"summary": summary, "steps": [{"title": "do"}]})


async def _approved_first_build(cid):
    """Drive an initial plan -> approval and return (loop, store)."""
    agent = ScriptedAgent([_submit_plan_step("first")])
    loop, store = build_plan_loop(agent, conversation_id=cid)
    await loop.send_message("build a page")
    await loop.run()
    await loop.approve_plan()
    return loop, store


async def test_followup_after_approval_enters_planning():
    """After plan_approved, a follow-up (request_plan -> enter_planning) re-enters
    PLANNING — a StatusEvent(detail="planning") after the follow-up user turn."""
    loop, store = await _approved_first_build("replan-enter")
    loop.agent = ScriptedAgent([_submit_plan_step("second")])
    await loop.enter_planning("revise the hero heading")
    await loop.run()

    events = await store.get_events("replan-enter")
    followup_seq = next(
        e.seq
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and "revise the hero" in (e.message.content or "")
    )
    assert any(
        isinstance(e, StatusEvent) and e.detail == "planning" and (e.seq or 0) > followup_seq
        for e in events
    )


async def test_revision_framing_emitted_once():
    """enter_planning on a revision emits exactly one RE-PLANNING framing message."""
    loop, store = await _approved_first_build("replan-frame")
    loop.agent = ScriptedAgent([_submit_plan_step("second")])
    await loop.enter_planning("revise the heading")
    await loop.run()

    events = await store.get_events("replan-frame")
    framings = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "RE-PLANNING" in (e.message.content or "")
    ]
    assert len(framings) == 1, [e.message.content for e in framings]


async def test_revision_plan_event_has_incremented_revision():
    """The revised PlanEvent has revision = previous + 1 (plans.py:147)."""
    loop, store = await _approved_first_build("replan-inc")
    loop.agent = ScriptedAgent([_submit_plan_step("second")])
    await loop.enter_planning("revise")
    await loop.run()

    plans = [e for e in await store.get_events("replan-inc") if isinstance(e, PlanEvent)]
    assert [p.revision for p in plans] == [1, 2]


async def test_agent_cannot_write_before_revised_plan_approval():
    """During a revision (back in PLANNING), a write must not execute before the
    revised plan is approved."""
    loop, store = await _approved_first_build("replan-write")
    executor: BuildExecutor = loop.executor  # type: ignore[assignment]
    # Re-enter planning, then the agent tries to write before submitting/approving.
    loop.agent = ScriptedAgent([action_step("file_write", {"path": "index.html", "content": "x"})])
    await loop.enter_planning("revise the heading")
    await loop.run()

    events = await store.get_events("replan-write")
    revised_approvals = [
        e.seq for e in events if isinstance(e, StatusEvent) and e.detail == "plan_approved"
    ]
    # only the FIRST build's approval exists; the write must not have run.
    write_obs = [
        e
        for e in events
        if isinstance(e, ObservationEvent)
        and e.tool_result.tool_name == "file_write"
        and e.tool_result.success
    ]
    assert not write_obs, "a write executed before the revised plan was approved"
    assert "index.html" not in executor.world
    assert len(revised_approvals) == 1  # no second approval happened
    assert ConversationStatus  # import smoke
    assert not isinstance(None, ActionEvent)

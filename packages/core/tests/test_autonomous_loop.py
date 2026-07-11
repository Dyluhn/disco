"""Autonomous mode — LOOP behavior (beyond tool-schema withholding).

Locks the headless-stall fixes found by review:

  B1: a mid-run `propose_plan_update` must AUTO-APPROVE in autonomous mode. The
      loop's own auto-continue nudge tells the model to call propose_plan_update to
      revise an incomplete plan; in interactive mode that halts at
      AWAITING_PLAN_APPROVAL for a human, but in autonomous mode there is no human,
      so it would stall forever. It must instead approve inline and keep running.

  Contrast: interactive mode STILL halts at AWAITING_PLAN_APPROVAL — the gate is
  intact; autonomous is the only path that auto-approves.
"""

from __future__ import annotations

import pytest
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.llm import OperatingMode
from loop_fakes import (
    ScriptedAgent,
    action_step,
    assert_blocked_question_landing,
    build_loop,
    finish_step,
)

CID = "conv"


def _plan_approvals(events):
    return [
        e
        for e in events
        if isinstance(e, StatusEvent) and e.detail == "plan_approved"
    ]


def _awaiting_plan(events):
    return [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.status == ConversationStatus.AWAITING_PLAN_APPROVAL
    ]


def _script():
    # plan → real work → mid-run plan revision → more work → finish.
    # `shell` is what the default FakeExecutor actually offers, so these steps
    # execute as real ActionEvents (resetting the actionless valve); the point of
    # the test is the propose_plan_update interception, not the work itself.
    return ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {"command": "echo one"}),
            action_step(
                "propose_plan_update",
                {"summary": "p2", "steps": [{"title": "1"}, {"title": "2"}]},
            ),
            action_step("shell", {"command": "echo two"}),
            finish_step(),
        ]
    )


@pytest.mark.asyncio
async def test_autonomous_auto_approves_mid_run_plan_update():
    store = SqliteEventStore(":memory:")
    loop, store = build_loop(_script(), store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    loop._autonomous = True  # the property under test

    await loop.send_message("go")
    # A single run() drives the whole thing — autonomous never hands back for a
    # human approval, so there is no approve_plan() call in this test.
    await loop.run()

    events = await store.get_events(CID)
    # The gate was NEVER hit — no halt waiting for a human.
    assert not _awaiting_plan(events), "autonomous run halted at AWAITING_PLAN_APPROVAL"
    # BOTH the initial submit_plan AND the mid-run propose_plan_update auto-approved.
    assert len(_plan_approvals(events)) >= 2


@pytest.mark.asyncio
async def test_interactive_still_halts_for_plan_update():
    store = SqliteEventStore(":memory:")
    loop, store = build_loop(_script(), store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    # autonomous defaults OFF here.

    await loop.send_message("go")
    s1 = await loop.run()  # halts at the INITIAL plan for approval
    assert s1.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    s2 = await loop.run()  # file_write, then propose_plan_update → halts AGAIN
    assert s2.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


# ---- C8 (T11): bound the autonomous propose_plan_update loop ----------------


def _identical_steps_script():
    """Three propose_plan_update calls in autonomous mode, each with steps
    BYTE-IDENTICAL to the immediately-prior plan (only the summary changes).
    A weak model in autonomous mode can hammer this loop forever hoping a
    human will approve — after one real action opens the revision affordance,
    the loop must bound the workless revision streak (C8) by feeding the
    existing bookkeeping-stuck valve when the streak hits the cap (3)."""
    return ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {"command": "echo one"}),
            # 3x propose_plan_update with identical step TITLES, varying summary
            # and no real work between them. The bug ignores summary — only step
            # bytes matter.
            action_step(
                "propose_plan_update",
                {"summary": "p2", "steps": [{"title": "1"}]},
            ),
            action_step(
                "propose_plan_update",
                {"summary": "p3", "steps": [{"title": "1"}]},
            ),
            action_step(
                "propose_plan_update",
                {"summary": "p4", "steps": [{"title": "1"}]},
            ),
            # If the bound failed to trip, the loop would reach these steps.
            # We cap the script so a broken loop can't burn the suite.
            action_step("shell", {"command": "echo four"}),
            finish_step(),
        ]
    )


def _appended_steps_script():
    """Negative case for C8 (T11): APPENDING a step counts as DIFFERENT, so
    the consecutive-identical streak resets and the bound MUST NOT trip. The
    run should reach FINISHED normally (loop continues past the revisions)."""
    return ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {"command": "echo one"}),
            # First revision APPENDS a step → resets the streak.
            action_step(
                "propose_plan_update",
                {"summary": "p2", "steps": [{"title": "1"}, {"title": "2"}]},
            ),
            action_step("shell", {"command": "echo two"}),
            # Second revision is identical to the appended one (streak = 1).
            action_step(
                "propose_plan_update",
                {"summary": "p3", "steps": [{"title": "1"}, {"title": "2"}]},
            ),
            action_step("shell", {"command": "echo three"}),
            # Third revision is identical again (streak = 2). Still < 3 cap.
            action_step(
                "propose_plan_update",
                {"summary": "p4", "steps": [{"title": "1"}, {"title": "2"}]},
            ),
            action_step("shell", {"command": "echo four"}),
            finish_step(),
        ]
    )


def _changed_steps_script():
    """Negative case for C8 (T11): CHANGING a step title counts as DIFFERENT,
    so the consecutive-identical streak resets and the bound MUST NOT trip.
    The run should reach FINISHED normally."""
    return ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
            action_step("shell", {"command": "echo one"}),
            # First revision CHANGES the title → resets the streak.
            action_step(
                "propose_plan_update",
                {"summary": "p2", "steps": [{"title": "2"}]},
            ),
            action_step("shell", {"command": "echo two"}),
            # Second revision is identical to the changed one (streak = 1).
            action_step(
                "propose_plan_update",
                {"summary": "p3", "steps": [{"title": "2"}]},
            ),
            action_step("shell", {"command": "echo three"}),
            # Third revision is identical again (streak = 2). Still < 3 cap.
            action_step(
                "propose_plan_update",
                {"summary": "p4", "steps": [{"title": "2"}]},
            ),
            action_step("shell", {"command": "echo four"}),
            finish_step(),
        ]
    )


@pytest.mark.asyncio
async def test_autonomous_propose_plan_update_halts_on_repeat_identical_steps():
    """C8 (T11): when the loop auto-approves a `propose_plan_update` in
    autonomous mode and the proposed plan's steps are byte-identical to the
    immediately-prior plan for >=3 consecutive times, the repeated re-proposal
    MUST feed the existing bookkeeping breaker so the run explains and asks
    rather than looping forever. The bookkeeping_only legacy detail is retained."""
    from disco.core import SqliteEventStore as Store

    store = Store(":memory:")
    loop, store = build_loop(_identical_steps_script(), store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    loop._autonomous = True  # the property under test

    await loop.send_message("go")
    state = await loop.run()

    events = await store.get_events(CID)
    # The bound fired: the run halted, and with the same bookkeeping_only
    # legacy detail the existing (c.3) valve emits. The model did NOT get to keep
    # proposing the same plan forever.
    # Two-flavor landing: AUTONOMOUS runs conclude terminally (STUCK carrying
    # the explanation) — headless has nobody to answer an open question.
    assert state.execution_status == ConversationStatus.STUCK
    assert_blocked_question_landing(events, legacy_detail="bookkeeping_only", flavor="terminal")
    nudges = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == "identical_plan_nudge"
    ]
    assert len(nudges) == 1
    # The AWAITING_PLAN_APPROVAL gate was never reached — autonomous was
    # respected for the first revisions, then the cap fired.
    assert not _awaiting_plan(events), (
        "autonomous run halted at AWAITING_PLAN_APPROVAL (should have been "
        "halting via the bookkeeping valve instead)"
    )


@pytest.mark.asyncio
async def test_autonomous_propose_plan_update_appended_steps_do_not_trip():
    """C8 (T11) negative: an APPENDED step counts as DIFFERENT, so the
    consecutive-identical streak resets and the bound MUST NOT trip. The
    run should reach FINISHED normally — appending a step is a legitimate
    plan revision, not a stuck-model symptom."""
    from disco.core import SqliteEventStore as Store

    store = Store(":memory:")
    loop, store = build_loop(_appended_steps_script(), store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    loop._autonomous = True  # the property under test

    await loop.send_message("go")
    state = await loop.run()

    events = await store.get_events(CID)
    # The bound did NOT fire: the run completed (FINISHED), and the
    # bookkeeping_only STUCK signal was never emitted.
    assert state.execution_status == ConversationStatus.FINISHED, (
        f"expected FINISHED (append is not a trip), got {state.execution_status}"
    )
    stuck_statuses = [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.status == ConversationStatus.STUCK
        and e.detail == "bookkeeping_only"
    ]
    assert not stuck_statuses, (
        "appending a step must reset the streak — bookkeeping_only STUCK "
        "should NOT have fired"
    )


@pytest.mark.asyncio
async def test_autonomous_propose_plan_update_changed_steps_do_not_trip():
    """C8 (T11) negative: CHANGING a step title counts as DIFFERENT, so the
    consecutive-identical streak resets and the bound MUST NOT trip. The
    run should reach FINISHED normally — a different plan is a legitimate
    plan revision, not a stuck-model symptom."""
    from disco.core import SqliteEventStore as Store

    store = Store(":memory:")
    loop, store = build_loop(_changed_steps_script(), store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    loop._autonomous = True  # the property under test

    await loop.send_message("go")
    state = await loop.run()

    events = await store.get_events(CID)
    # The bound did NOT fire: the run completed (FINISHED), and the
    # bookkeeping_only STUCK signal was never emitted.
    assert state.execution_status == ConversationStatus.FINISHED, (
        f"expected FINISHED (changed step is not a trip), got {state.execution_status}"
    )
    stuck_statuses = [
        e
        for e in events
        if isinstance(e, StatusEvent)
        and e.status == ConversationStatus.STUCK
        and e.detail == "bookkeeping_only"
    ]
    assert not stuck_statuses, (
        "changing a step must reset the streak — bookkeeping_only STUCK "
        "should NOT have fired"
    )


@pytest.mark.asyncio
async def test_autonomous_auto_approval_is_acknowledged_in_band():
    """2026-07-09 overnight-soak fix: the auto-approval of a plan revision was
    only a StatusEvent — invisible to the model — so it re-proposed the same
    revision until the C8 cap STUCK the run (steer scenario, wave 2). The
    approval must now land IN-BAND as an environment message naming the next
    step, so the re-propose motive never forms."""
    from disco.core import SqliteEventStore as Store

    script = ScriptedAgent(
        [
            action_step("submit_plan", {"summary": "p", "steps": [{"title": "build the page"}]}),
            action_step("shell", {"command": "echo one"}),
            # ONE revision with CHANGED steps (the normal, healthy steer path).
            action_step(
                "propose_plan_update",
                {"summary": "p2", "steps": [{"title": "build the page"}, {"title": "add contact"}]},
            ),
            action_step("shell", {"command": "echo two"}),
            finish_step("done"),
        ]
    )
    store = Store(":memory:")
    loop, store = build_loop(script, store=store)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    loop._autonomous = True

    await loop.send_message("go")
    await loop.run()

    events = await store.get_events(CID)
    acks = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == "auto_approval_ack"
    ]
    assert len(acks) == 1, "the auto-approved revision must be acknowledged in-band"
    body = acks[0].message.content
    assert "APPROVED" in body
    assert "build the page" in body  # names the next step
    assert "not another plan proposal" in body

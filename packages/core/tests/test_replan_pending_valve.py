"""BW-01 — the actionless valve must NOT finish a build off a STALE plan while a
re-plan revision is pending.

Trace (conv_2907c89b): after build revision 1 finished, a re-plan request
re-entered PLANNING (StatusEvent detail="planning"), but the actionless valve
terminalized off the still-"done" revision-1 plan — at 3 no-op turns the
`completed_via_notify` branch force-finished (plan_steps_complete read the stale
rev-1 + productive_actions>0), and at 6 no-op turns the generic `noop_limit`
branch could emit FINISHED — both while the user awaited a REVISED plan.

The fix: `signals.in_planning_for_revision(events)` (planning re-entered more
recently than the last approval) gates BOTH terminal branches of
`Valve.actionless_valve`. While a revision is pending the valve must not finish;
it falls through (returns False) to the engine's plan-nudge / replan path. The
guard RELEASES automatically once the revised plan is approved (then
plan_approved.seq > planning.seq again).
"""

from disco.core import (
    ConversationStatus,
    StatusEvent,
)
from disco.core.llm import OperatingMode
from disco.core.loop import signals
from loop_fakes import (
    ScriptedAgent,
    action_step,
    build_loop,
    finish_step,
)

CID = "conv"


def _last_status_detail(events):
    for e in reversed(events):
        if isinstance(e, StatusEvent):
            return e.detail
    return None


def _notify(msg):
    return action_step("notify_user", {"message": msg})


# --------------------------------------------------------------------------- #
# 1. in_planning_for_revision — the pure predicate (no plan / planning-then-   #
#    approved / approved-then-planning).                                       #
# --------------------------------------------------------------------------- #


def test_in_planning_for_revision_no_planning_marker():
    """A build that has only been approved (never re-entered planning) is NOT in
    a pending revision."""
    events = [
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=2),
    ]
    assert signals.in_planning_for_revision(events) is False


def test_in_planning_for_revision_empty_log():
    """No planning and no approval at all → not pending."""
    assert signals.in_planning_for_revision([]) is False


def test_in_planning_for_revision_planning_then_approved_is_released():
    """planning re-entered, THEN re-approved (plan_approved.seq > planning.seq):
    the revision has landed → no longer pending (the auto-release case)."""
    events = [
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=2),
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=5),
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=9),
    ]
    assert signals.in_planning_for_revision(events) is False


def test_in_planning_for_revision_approved_then_planning_is_pending():
    """approved, THEN planning re-entered (planning.seq > plan_approved.seq):
    a revision is pending — the BW-01 state."""
    events = [
        StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved", seq=2),
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=7),
    ]
    assert signals.in_planning_for_revision(events) is True


def test_in_planning_for_revision_planning_never_approved_is_pending():
    """A first plan that was requested (planning) but never approved is, likewise,
    a pending plan — the valve must not finish off a (non-existent) approved plan."""
    events = [
        StatusEvent(status=ConversationStatus.RUNNING, detail="planning", seq=3),
    ]
    assert signals.in_planning_for_revision(events) is True


# --------------------------------------------------------------------------- #
# Shared driver: build revision 1 to a clean FINISHED, then re-enter planning  #
# (the BW-01 pending-revision state) — producing a REAL event log with a real  #
# `planning` StatusEvent emitted AFTER the rev-1 `plan_approved`.              #
# --------------------------------------------------------------------------- #


async def _build_rev1_then_enter_planning(**loop_kwargs):
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("shell", {"command": "echo build the site"}),  # productive work
        action_step("plan_step", {"index": 1, "state": "done"}),  # rev-1 complete
        finish_step(),
    ])
    loop, store = build_loop(agent, **loop_kwargs)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()  # submit_plan → halts at AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    await loop.run()  # shell, plan_step done, finish → FINISHED
    # The re-plan request: re-enter PLANNING with a concrete new instruction.
    # This emits a USER message + StatusEvent(RUNNING, detail="planning") AFTER
    # the rev-1 plan_approved — exactly the BW-01 pending-revision state.
    await loop.enter_planning("please add a dark-mode toggle")
    events = await store.get_events(CID)
    return loop, store, events


# --------------------------------------------------------------------------- #
# 2. RE-PLAN PENDING → the valve does NOT finish at 3 (completed_via_notify)   #
#    NOR at 6 (noop_limit) no-op turns; it falls through (returns False).      #
# --------------------------------------------------------------------------- #


async def test_pending_revision_suppresses_both_terminal_branches():
    loop, store, events = await _build_rev1_then_enter_planning()

    # Sanity: this IS the pending-revision state, and WITHOUT the guard the
    # stale rev-1 plan would force a completed_via_notify finish (plan complete +
    # productive work since approval).
    assert signals.in_planning_for_revision(events) is True
    assert signals.plan_steps_complete(events) is True
    assert signals.productive_actions_since_approval(events) > 0

    # 3 no-ops: the completed_via_notify branch must be SUPPRESSED.
    landed_at_3 = await loop._valve.actionless_valve(events, 3)
    assert landed_at_3 is False  # fell through to the nudge, did not land

    # 6 no-ops: the generic noop_limit→FINISHED branch must be SUPPRESSED too.
    landed_at_6 = await loop._valve.actionless_valve(events, 6)
    assert landed_at_6 is False

    # And crucially: NO terminal off the stale plan was emitted by either call.
    after = await store.get_events(CID)
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.FINISHED
        and e.detail in ("completed_via_notify", "noop_limit")
        for e in after
    )
    # The most recent status is still the pending-revision `planning`, untouched.
    assert _last_status_detail(after) == "planning"


# --------------------------------------------------------------------------- #
# 3. RELEASE — once the revised plan is approved (plan_approved.seq >          #
#    planning.seq), the valve can finish again. Full re-plan-and-rebuild.      #
# --------------------------------------------------------------------------- #


async def test_valve_finishes_again_after_revision_approved():
    agent = ScriptedAgent([
        # revision 1
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("shell", {"command": "echo build v1"}),
        action_step("plan_step", {"index": 1, "state": "done"}),
        finish_step(),
        # revision 2 (after enter_planning; approved below → release)
        action_step(
            "submit_plan",
            {"summary": "p2", "steps": [{"title": "1"}, {"title": "2"}]},
        ),
        action_step("shell", {"command": "echo build v2"}),  # productive work
        action_step("plan_step", {"index": 1, "state": "done"}),
        action_step("plan_step", {"index": 2, "state": "done"}),  # rev-2 complete
        _notify("Dark mode added and verified."),
        _notify("Everything is in place."),
        _notify("All done — the revised build is complete."),
        finish_step(),
    ])
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()  # submit_plan rev-1 → halt approval
    await loop.approve_plan()
    await loop.run()  # rev-1 execute → FINISHED
    await loop.enter_planning("add a dark-mode toggle")  # → planning (pending)
    await loop.run()  # submit_plan rev-2 → halt approval
    await loop.approve_plan()  # plan_approved AFTER planning → RELEASE
    state = await loop.run()  # rev-2 execute + notify spam → finishes
    events = await store.get_events(CID)

    # The revision was re-approved → no longer pending.
    assert signals.in_planning_for_revision(events) is False
    # And the valve is free to land the completed build again.
    assert state.execution_status == ConversationStatus.FINISHED
    assert _last_status_detail(events) == "completed_via_notify"


# --------------------------------------------------------------------------- #
# 4. REGRESSION — a NORMAL first build (planning → plan_approved → build, no    #
#    later planning) STILL finishes via completed_via_notify at 3 no-ops       #
#    (W-32 path intact).                                                        #
# --------------------------------------------------------------------------- #


async def test_normal_build_still_finishes_via_completed_via_notify():
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("shell", {"command": "echo build the site"}),  # productive work
        action_step("plan_step", {"index": 1, "state": "done"}),  # plan complete
        _notify("All files are in place, the site is built."),
        _notify("The static site is fully built and served."),
        _notify("All set! The build is complete."),
        finish_step(),
    ])
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    state = await loop.run()
    events = await store.get_events(CID)

    # No re-plan ever happened → not pending → the valve finishes normally.
    assert signals.in_planning_for_revision(events) is False
    assert state.execution_status == ConversationStatus.FINISHED
    assert _last_status_detail(events) == "completed_via_notify"


# --------------------------------------------------------------------------- #
# 5. REGRESSION — the generic noop_limit→FINISHED backstop still fires on a     #
#    no-plan run (no re-plan pending) at the 6-no-op ceiling.                   #
# --------------------------------------------------------------------------- #


async def test_no_plan_noop_limit_still_finishes():
    from disco.core.loop.stuck import StuckThresholds

    # No plan at all, no planning marker → not pending; the noop backstop governs.
    agent = ScriptedAgent([_notify(f"musing {i}") for i in range(8)])
    loop, store = build_loop(
        agent, stuck_thresholds=StuckThresholds(agent_monologue=100)
    )
    await loop.send_message("go")
    state = await loop.run()
    events = await store.get_events(CID)

    assert signals.in_planning_for_revision(events) is False
    assert state.execution_status == ConversationStatus.FINISHED
    assert _last_status_detail(events) == "noop_limit"

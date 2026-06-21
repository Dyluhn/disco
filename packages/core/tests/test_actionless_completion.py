"""B5 — a COMPLETED build must finish, not PAUSE/actionless.

When a model signals "done" via `notify_user` (×N) instead of calling
`finish()`, the actionless valve trips at `_ACTIONLESS_BREAK_CAP` consecutive
non-productive turns. Before B5 that always landed PAUSED/actionless — so a
genuinely-finished build (every plan step marked done) wrongly read as paused.

The fix gates the pause decision on plan completeness:
  * all plan steps done  → FINISHED (detail="completed_via_notify")
  * plan steps remain     → PAUSED/actionless (the thrash guard, unchanged)
  * no plan / ambiguous   → existing behavior (the noop backstop), never
                            auto-finished as "completed_via_notify".
"""

from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    StatusEvent,
)
from disco.core.llm import OperatingMode
from disco.core.loop.stuck import StuckThresholds
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"


def _last_status_detail(events):
    for e in reversed(events):
        if isinstance(e, StatusEvent):
            return e.detail
    return None


def _notify(msg):
    return action_step("notify_user", {"message": msg})


async def _approve_and_run(agent):
    """PLANNING → submit_plan (halts at approval) → approve → execute."""
    loop, store = build_loop(agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()  # consumes submit_plan, halts at AWAITING_PLAN_APPROVAL
    await loop.approve_plan()
    state = await loop.run()  # executes the rest
    events = await store.get_events(CID)
    return state, events


async def test_notify_user_with_plan_complete_finishes_not_paused():
    """3× notify_user with ALL plan steps marked done → FINISHED, NOT PAUSED.
    The model signaled completion via notify_user instead of finish(); the
    valve recognizes the build is done and lands a clean terminal."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        action_step("plan_step", {"index": 1, "state": "done"}),  # plan complete
        _notify("All files are in place, the macOS-style site is built."),
        _notify("The macOS-style static site is fully built and served."),
        _notify("All set! The build is complete."),
        finish_step(),
    ])
    state, events = await _approve_and_run(agent)

    assert state.execution_status == ConversationStatus.FINISHED
    assert _last_status_detail(events) == "completed_via_notify"
    # And crucially NOT the paused/actionless terminal.
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.PAUSED
        and e.detail == "actionless"
        for e in events
    )


async def test_update_plan_progress_complete_finishes_not_paused():
    """The plan-progress-source-unify repro (live build conv_c93912fa): a CAPABLE model
    marks ALL steps done via the DECLARATIVE `update_plan_progress` snapshot (NOT
    plan_step — the #3 redesign), then goes non-productive. Before the unified reader the
    actionless valve counted only plan_step marks → saw 0/N done → PAUSED a finished build
    ("plan steps remain undone"). Now the merged reader sees 2/2 done → FINISHED."""
    from disco.core.llm import ToolSpec
    from loop_fakes import FakeExecutor

    agent = ScriptedAgent([
        action_step(
            "submit_plan",
            {"summary": "p", "steps": [{"title": "1"}, {"title": "2"}]},
        ),
        # Declarative full-state snapshot — both steps done in ONE call, no plan_step.
        action_step(
            "update_plan_progress",
            {"steps": [{"index": 1, "state": "done"}, {"index": 2, "state": "done"}]},
        ),
        _notify("All files are in place, the macOS-style site is built."),
        _notify("The macOS-style static site is fully built and served."),
        _notify("All set! The build is complete."),
        finish_step(),
    ])
    # update_plan_progress is a real, advertised tool in production (unlike plan_step it
    # isn't engine-special-cased), so the fake executor must advertise it or the action
    # is dropped as unknown and never lands as an ActionEvent.
    executor = FakeExecutor(
        tools=[
            ToolSpec(name=n, description=n, parameters_schema={})
            for n in ("update_plan_progress", "notify_user", "file_read", "shell")
        ]
    )
    loop, store = build_loop(executor=executor, agent=agent)
    loop.mode = OperatingMode.PLANNING
    loop._planning_tools = frozenset(["file_read"])
    await loop.send_message("go")
    await loop.run()
    await loop.approve_plan()
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert _last_status_detail(events) == "completed_via_notify"
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.PAUSED
        and e.detail == "actionless"
        for e in events
    )


async def test_notify_user_with_plan_remaining_still_pauses():
    """3× notify_user with plan steps REMAINING → still PAUSED/actionless.
    Regression guard: the thrash protection must survive — an unfinished plan
    that goes silent is a genuine stall, not a completed build."""
    agent = ScriptedAgent([
        action_step("submit_plan", {"summary": "p", "steps": [{"title": "1"}]}),
        # NOTE: no plan_step(done) — the single step stays undone.
        _notify("I'm back after the restart!"),
        _notify("Resuming work now!"),
        _notify("Picking up where I left off!"),
        finish_step(),
    ])
    state, events = await _approve_and_run(agent)

    assert state.execution_status == ConversationStatus.PAUSED
    assert _last_status_detail(events) == "actionless"
    # The completion path must NOT have hijacked a genuine stall.
    assert not any(
        isinstance(e, StatusEvent) and e.detail == "completed_via_notify"
        for e in events
    )
    msgs = [
        e for e in events
        if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert any(
        "3 consecutive responses without doing any real work"
        in (m.message.content if m.message else "")
        for m in msgs
    )


async def test_no_plan_defaults_to_existing_noop_backstop():
    """No plan at all (ambiguous completeness) → the existing noop backstop
    governs (FINISHED/noop_limit), NOT the completion path. The monologue
    threshold is raised so the noop backstop is exercised in isolation."""
    agent = ScriptedAgent([_notify(f"musing {i}") for i in range(8)])
    loop, store = build_loop(
        agent, stuck_thresholds=StuckThresholds(agent_monologue=100)
    )
    await loop.send_message("go")
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    # Existing behavior preserved — the no-plan case is NOT auto-finished as a
    # completed build.
    assert _last_status_detail(events) == "noop_limit"
    assert not any(
        isinstance(e, StatusEvent) and e.detail == "completed_via_notify"
        for e in events
    )

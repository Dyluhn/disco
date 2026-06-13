"""Stuck detection — agent-loop-contract.md §10.5.

StuckDetector unit tests (table-driven over the four patterns, using
event_content_eq) + loop integration (STUCK then resume on a new message).
"""

from __future__ import annotations

from conftest import action, agent_error, agent_msg, observation, user_msg
from disco.core import ConversationStatus, StatusEvent
from disco.core.loop import StuckDetector, StuckThresholds
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step

CID = "conv"


def _pairs_ao(n, thought="same"):
    """n identical action→observation pairs."""
    out = []
    for _ in range(n):
        out += [action(thought=thought), observation(content="ok")]
    return out


# ---- pattern 1: repeated action→observation ---------------------------------


def test_repeated_action_observation_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3))
    assert d.is_stuck(_pairs_ao(3)) is True
    assert d.is_stuck(_pairs_ao(2)) is False  # below threshold


# ---- pattern 2: repeated action→error ---------------------------------------


def test_repeated_action_error_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(repeat_action_error=3))
    events = []
    for _ in range(3):
        events += [action(thought="retry"), agent_error("same failure")]
    assert d.is_stuck(events) is True


# ---- pattern 3: agent monologue ---------------------------------------------


def test_agent_monologue_triggers_at_threshold():
    d = StuckDetector(StuckThresholds(agent_monologue=4))
    assert d.is_stuck([agent_msg("a"), agent_msg("b"), agent_msg("c"), agent_msg("d")]) is True
    assert d.is_stuck([agent_msg("a"), agent_msg("b")]) is False


# ---- pattern 4: alternating A-B-A-B -----------------------------------------


def test_alternating_actions_trigger_at_threshold():
    d = StuckDetector(StuckThresholds(alternating=3))
    seq = []
    for _ in range(3):
        seq += [action(thought="A"), action(thought="B")]  # A,B repeated 3x
    assert d.is_stuck(seq) is True
    # Identical (not alternating) is pattern 1, not pattern 4:
    assert d._alternating([action(thought="A")] * 6) is False


# ---- reset + window ---------------------------------------------------------


def test_user_message_resets_stuck():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3))
    events = _pairs_ao(3) + [user_msg("new instruction")] + _pairs_ao(1)
    assert d.is_stuck(events) is False  # cleared after the user message


def test_events_outside_window_dont_count():
    d = StuckDetector(StuckThresholds(repeat_action_observation=3, scan_window=4))
    # 3 stuck pairs, but only the last `scan_window`(=4) events are inspected by
    # the loop; here we emulate by trimming as the loop's _recent() would.
    events = _pairs_ao(3)  # 6 events
    assert d.is_stuck(events[-4:]) is False  # only 2 pairs visible in the window


# ---- loop integration: STUCK then resume ------------------------------------


async def test_loop_tries_a_temp_escape_before_going_stuck():
    """Escape-then-halt: the FIRST time the loop detects an action→obs/error rut it
    drops a `stuck_escape` MARKER (a status event — NOT an injected reminder; the
    harness-doesn't-nudge rule) and lets the model retry the next step at a high
    temperature. It does NOT halt yet. Only if it's STILL stuck after that retry does
    STUCK fire — and the escape buys the model at least one extra step it wouldn't
    have had under immediate-halt."""
    from disco.core import MessageEvent

    agent = ScriptedAgent([action_step()] * 6 + [finish_step()])
    loop, store = build_loop(agent, stuck_thresholds=StuckThresholds(repeat_action_observation=3))
    await loop.send_message("repeat please")
    state = await loop.run()

    events = await store.get_events(CID)
    # the escape was attempted (a status marker on the log)…
    assert any(isinstance(e, StatusEvent) and e.detail == "stuck_escape" for e in events)
    # …but NO harness reminder was injected (the anti-nudge invariant holds)…
    def _is_reminder(e):
        return isinstance(e, MessageEvent) and "<system-reminder>" in (
            e.message.content if e.message else ""
        )

    assert not any(_is_reminder(e) for e in events)
    # …and it still ended STUCK because the model kept repeating after the retry.
    assert state.execution_status == ConversationStatus.STUCK


async def test_loop_goes_stuck_then_resumes_on_new_message():
    # Same action forever → identical action→obs cycles → STUCK (after the reframe
    # escape is spent: 3 to trigger + ≥1 retry that's still stuck).
    agent = ScriptedAgent([action_step()] * 6 + [finish_step()])
    loop, store = build_loop(agent, stuck_thresholds=StuckThresholds(repeat_action_observation=3))
    await loop.send_message("repeat please")
    state = await loop.run()
    assert state.execution_status == ConversationStatus.STUCK

    # A new message resets and the loop resumes; the next step finishes.
    await loop.send_message("ok stop, finish")
    resumed = await loop.run()
    assert resumed.execution_status == ConversationStatus.FINISHED
    statuses = [e.status for e in await store.get_events(CID) if isinstance(e, StatusEvent)]
    assert ConversationStatus.STUCK in statuses
    assert statuses[-1] == ConversationStatus.FINISHED


# ---- bookkeeping streak (issue C) must scale with plan size (T7 / E3) --------


def _seed_approved_plan(store, *, n_steps: int):
    """Pre-populate the store with user msg + a PlanEvent with n_steps steps +
    a `plan_approved` RUNNING marker so a fresh loop enters execution mode
    against an active plan. Returns nothing (mutates store)."""
    from disco.core import EventSource, LLMMessage, MessageEvent, PlanEvent
    from disco.core.events import ConversationStatus

    async def _seed():
        await store.append(
            CID,
            MessageEvent(
                source=EventSource.USER, message=LLMMessage(role="user", content="build it")
            ),
        )
        await store.append(
            CID,
            PlanEvent(
                summary="p",
                steps=[{"title": f"step {i}"} for i in range(1, n_steps + 1)],
                revision=1,
            ),
        )
        await store.append(
            CID,
            StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"),
        )

    return _seed


async def test_bookkeeping_halt_caps_genuine_spam_on_tiny_plan():
    """E3: a 1-step plan is a TINY plan. Eight `plan_step` calls on it are clearly
    spam (only 1 step exists to mark), so the bookkeeping cap MUST halt the
    run with `bookkeeping_only`. The cap is a guard against a model that just
    shuffles the plan tracker forever; it must still fire on this case."""
    from disco.core import ConversationStatus, EventSource, LLMMessage, MessageEvent, PlanEvent
    from disco.core.events import StatusEvent as CoreStatusEvent
    from disco.core.llm import OperatingMode
    from disco.core import SqliteEventStore as Store

    store = Store(":memory:")
    await _seed_approved_plan(store, n_steps=1)()
    # Vary the `thought` so the StuckDetector's repeat_action_observation
    # doesn't fire first — we want THIS test to exercise the bookkeeping
    # halt specifically (the StuckDetector has its own coverage).
    steps = [
        action_step("plan_step", {"index": 1, "state": "done"}, thought=f"marking {i}")
        for i in range(8)
    ]
    # Cap the script so a broken loop can't burn the suite.
    agent = ScriptedAgent(steps + [finish_step()])
    loop, _ = build_loop(agent, store=store, mode=OperatingMode.LONG_HORIZON)
    state = await loop.run()

    # bookkeeping halt fired with the right detail
    assert state.execution_status == ConversationStatus.STUCK, (
        f"expected STUCK bookkeeping_only, got {state.execution_status}"
    )
    events = await store.get_events(CID)
    stuck_statuses = [
        e
        for e in events
        if isinstance(e, CoreStatusEvent) and e.status == ConversationStatus.STUCK
    ]
    assert any(e.detail == "bookkeeping_only" for e in stuck_statuses), (
        f"expected detail='bookkeeping_only' in {[e.detail for e in stuck_statuses]}"
    )
    # And the model wasn't allowed to keep going — no FINISHED, no plan_step count > 6.
    assert state.execution_status != ConversationStatus.FINISHED


async def test_bookkeeping_halt_does_not_trip_legit_burst_on_long_plan():
    """E3 inverse: an 8-step plan legitimately needs 8 `plan_step` calls to
    mark each step done. Eight such calls in a row is NOT spam — it's the
    plan-tracker being kept honest. The bookkeeping cap MUST NOT halt on this
    case; the run should reach FINISHED once the agent declares done."""
    from disco.core import ConversationStatus
    from disco.core.events import StatusEvent as CoreStatusEvent
    from disco.core.llm import OperatingMode
    from disco.core import SqliteEventStore as Store

    store = Store(":memory:")
    await _seed_approved_plan(store, n_steps=8)()
    # Each plan_step has a different index (1..8) so the actions are
    # semantically distinct — the StuckDetector wouldn't fire anyway, but
    # this matches what a real model would emit.
    steps = [
        action_step(
            "plan_step", {"index": i, "state": "done"}, thought=f"marking step {i}"
        )
        for i in range(1, 9)
    ]
    agent = ScriptedAgent(steps + [finish_step()])
    loop, _ = build_loop(agent, store=store, mode=OperatingMode.LONG_HORIZON)
    state = await loop.run()

    events = await store.get_events(CID)
    stuck_statuses = [
        e
        for e in events
        if isinstance(e, CoreStatusEvent) and e.status == ConversationStatus.STUCK
    ]
    bookkeeping_halts = [e for e in stuck_statuses if e.detail == "bookkeeping_only"]
    assert bookkeeping_halts == [], (
        f"8 plan_step calls on an 8-step plan MUST NOT trip the bookkeeping halt, "
        f"but got {len(bookkeeping_halts)} bookkeeping_only STUCK events"
    )
    # And the run landed cleanly (the plan is complete, the finish was accepted).
    assert state.execution_status == ConversationStatus.FINISHED, (
        f"expected FINISHED, got {state.execution_status}"
    )

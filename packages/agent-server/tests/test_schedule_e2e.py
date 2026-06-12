"""RP-08 end-to-end tests — ScheduleManager over a fake clock.

Drives the manager through the two key scenarios:

1. A 2-fire schedule runs twice → two ScheduleRunEvents appended to the SAME
   conversation; runtime.kick() is called for each run.

2. Kill+restart between fires: the first fire happens, then the "server" is
   killed (manager destroyed). A new manager is created with the same store;
   the clock is advanced past 3 fires that would have run. The new manager's
   first tick issues exactly ONE coalesced catch-up, not 3.

The fake-clock pattern mirrors how test_idle_suspend.py drives sweep_idle_once:
inject now_fn + sleep_fn, call the internal method directly, assert on the store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from perpleximanus.agent_server.schedule import ScheduleManager
from perpleximanus.core.events import (
    EventSource,
    LLMMessage,
    MessageEvent,
    ScheduleRunEvent,
)
from perpleximanus.core.loop.engine import AgentLoop
from perpleximanus.core.store.sqlite import SqliteEventStore

# ---- helpers -----------------------------------------------------------------


def _store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


def _runtime() -> MagicMock:
    rt = MagicMock()
    rt.kick = MagicMock()
    rt.set_model_override = MagicMock()
    rt.set_depth = MagicMock()
    return rt


async def _seed_query(store: SqliteEventStore, cid: str, text: str = "What's new in AI?") -> None:
    """Seed a conversation with its original USER message + an agent reply, so the
    scheduled fire has a query to re-run and the conversation is in a 'answered'
    (work-gate-closed) state before the fire — the realistic precondition."""
    await store.append(
        cid, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=text))
    )
    await store.append(
        cid,
        MessageEvent(source=EventSource.AGENT, message=LLMMessage(role="assistant", content="...")),
    )


def _manager(
    store: SqliteEventStore,
    clock: list[datetime],
    *,
    rt: MagicMock | None = None,
) -> tuple[ScheduleManager, MagicMock]:
    if rt is None:
        rt = _runtime()
    mgr = ScheduleManager(
        store,
        rt,
        now_fn=lambda: clock[0],
        sleep_fn=lambda _s: None,  # type: ignore[arg-type]
    )
    return mgr, rt


async def _run_events(store: SqliteEventStore, cid: str) -> list[ScheduleRunEvent]:
    events = await store.get_events(cid)
    return [e for e in events if isinstance(e, ScheduleRunEvent)]


# ---- scenario 1: two fires, same conversation --------------------------------


@pytest.mark.asyncio
async def test_two_fire_schedule_appends_two_events_same_conversation():
    """A schedule with a 2-minute cadence fires twice; both events land on the same cid.

    Event stream asserted:
        [ScheduleRunEvent(coalesced=False), ScheduleRunEvent(coalesced=False)]
    and runtime.kick is called twice with the same conversation_id.
    """
    store = _store()
    t0 = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
    clock: list[datetime] = [t0]

    mgr, rt = _manager(store, clock)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    await _seed_query(store, cid)  # original query to re-run + a prior agent reply

    sched = mgr.create_schedule(
        conversation_id=cid,
        owner_id="local",
        rrule="*/2 * * * *",  # every 2 minutes
        description="e2e test schedule",
    )

    # Verify next_run was computed at creation time
    assert sched.next_run is not None
    assert sched.next_run > t0

    # --- fire 1: advance clock past first fire ---
    clock[0] = t0 + timedelta(minutes=2, seconds=30)
    await mgr._tick()

    ev1 = await _run_events(store, cid)
    assert len(ev1) == 1, f"expected 1 event after first fire, got {len(ev1)}"
    assert ev1[0].schedule_id == sched.schedule_id
    assert not ev1[0].coalesced, "first scheduled run should not be coalesced"
    rt.kick.assert_called_with(cid)

    # --- fire 2: advance past second fire ---
    clock[0] = t0 + timedelta(minutes=4, seconds=30)
    await mgr._tick()

    ev2 = await _run_events(store, cid)
    assert len(ev2) == 2, f"expected 2 events after second fire, got {len(ev2)}"

    # Both events belong to the same conversation (the primary invariant).
    convo_ids = {ev.schedule_id for ev in ev2}
    assert convo_ids == {sched.schedule_id}, "all run events must reference the same schedule"

    # runtime.kick was called once per fire, each time with the conversation id.
    assert rt.kick.call_count == 2
    for call in rt.kick.call_args_list:
        assert call.args[0] == cid

    # Audit log matches
    runs = store.list_schedule_runs(sched.schedule_id)
    assert len(runs) == 2

    print("\nAsserted event stream:")
    for ev in ev2:
        print(f"  ScheduleRunEvent(schedule_id={ev.schedule_id!r}, coalesced={ev.coalesced})")


# ---- scenario 2: kill + restart → exactly ONE coalesced catch-up ------------


@pytest.mark.asyncio
async def test_kill_and_restart_produces_one_coalesced_run():
    """Kill the server between fires; restart with 3 missed fires → exactly 1 catch-up.

    Timeline:
        T+0  : schedule created (next_run = T+2)
        T+2  : first fire runs → next_run = T+4       [manager alive]
        T+4  : server KILLED (manager dropped)
        T+6  : missed fire (server down)
        T+8  : missed fire (server down)
        T+10 : server RESTARTS; first tick → ONE coalesced catch-up → next_run = T+12
    """
    store = _store()
    t0 = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
    clock: list[datetime] = [t0]

    # --- pre-kill phase ---
    mgr1, rt1 = _manager(store, clock)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    await _seed_query(store, cid)  # original query to re-run + a prior agent reply

    mgr1.create_schedule(
        conversation_id=cid,
        owner_id="local",
        rrule="*/2 * * * *",
        description="kill+restart test",
    )

    # Run the first fire (T+2 min)
    clock[0] = t0 + timedelta(minutes=2, seconds=1)
    await mgr1._tick()

    ev_pre = await _run_events(store, cid)
    assert len(ev_pre) == 1, "exactly 1 run before kill"
    assert not ev_pre[0].coalesced

    # --- server kill simulation: drop mgr1, do NOT call _tick for T+4, T+6, T+8 ---
    del mgr1

    # --- post-restart phase: clock at T+10 (3 missed fires: T+4, T+6, T+8) ---
    clock[0] = t0 + timedelta(minutes=10)

    mgr2, rt2 = _manager(store, clock)  # new manager, same store
    await mgr2._tick()

    ev_post = await _run_events(store, cid)
    total_runs = len(ev_post)

    assert total_runs == 2, (
        f"expected 2 total runs (1 pre-kill + 1 coalesced catch-up), got {total_runs}. "
        "The coalesce guard fires ONCE for all missed fires."
    )

    # The second run (the catch-up) must be marked coalesced.
    catch_up = ev_post[1]
    assert catch_up.coalesced is True, (
        "the post-restart catch-up run must be marked coalesced=True"
    )

    # Verify next_run is now past T+10 (future), so the loop won't fire again immediately.
    sched_rows = store.list_schedules(owner_id="local", conversation_id=cid)
    next_run_str = sched_rows[0].get("next_run")
    assert next_run_str is not None
    next_run_dt = datetime.fromisoformat(next_run_str)
    if next_run_dt.tzinfo is None:
        next_run_dt = next_run_dt.replace(tzinfo=UTC)

    assert next_run_dt > clock[0], (
        f"next_run ({next_run_dt}) must be in the future after catch-up "
        f"(current time: {clock[0]})"
    )

    # A third tick at the same T+10 must NOT fire again.
    await mgr2._tick()
    ev_no_extra = await _run_events(store, cid)
    assert len(ev_no_extra) == 2, (
        "a tick at the same time after catch-up must not produce another run"
    )

    print("\nKill+restart event stream:")
    for ev in ev_post:
        print(
            f"  ScheduleRunEvent(schedule_id={ev.schedule_id!r}, coalesced={ev.coalesced})"
        )
    print(f"  → next_run after catch-up: {next_run_dt.isoformat()}")


# ---- scenario 3: invalid rrule rejected (never silent) -----------------------


def test_invalid_rrule_raises_value_error():
    """create_schedule raises ValueError for an invalid cron expression (never silent)."""
    store = _store()
    mgr, _ = _manager(store, [datetime.now(UTC)])
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")

    with pytest.raises(ValueError, match="Invalid cron expression"):
        mgr.create_schedule(
            conversation_id=cid,
            owner_id="local",
            rrule="every day",  # invalid
            description="bad schedule",
        )


# ---- scenario 4: list and delete schedules ----------------------------------


@pytest.mark.asyncio
async def test_list_and_delete_schedule():
    """create/list/delete round-trip works correctly."""
    store = _store()
    clock: list[datetime] = [datetime(2024, 1, 1, tzinfo=UTC)]
    mgr, _ = _manager(store, clock)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")

    sched = mgr.create_schedule(
        conversation_id=cid,
        owner_id="local",
        rrule="0 9 * * 1",  # every Monday 9am
        description="weekly schedule",
    )

    listed = mgr.list_schedules(owner_id="local", conversation_id=cid)
    assert len(listed) == 1
    assert listed[0].schedule_id == sched.schedule_id

    deleted = mgr.delete_schedule(sched.schedule_id, owner_id="local")
    assert deleted is True

    after_delete = mgr.list_schedules(owner_id="local", conversation_id=cid)
    assert len(after_delete) == 0

    # Deleting again returns False (not found)
    assert mgr.delete_schedule(sched.schedule_id, owner_id="local") is False


# ---- scenario 5: the LOAD-BEARING re-run test --------------------------------
# A schedule that fires but never re-runs the query is a false affordance. The
# real proof is the ENGINE's own work-gate (_has_unprocessed_user_message): a
# fired schedule must flip it closed→open by re-injecting the original query.
# Asserting "kick was called" (as the mock-only test did) proves nothing here.


@pytest.mark.asyncio
async def test_scheduled_fire_re_runs_query_and_opens_engine_work_gate():
    """A fire must (a) re-inject the original query as a USER message so the real
    engine work-gate opens, (b) reproduce the saved model_override + depth, and
    (c) kick. Uses the actual AgentLoop._has_unprocessed_user_message — no mock
    of the gate."""
    store = _store()
    clock: list[datetime] = [datetime(2024, 6, 1, 10, 0, tzinfo=UTC)]
    mgr, rt = _manager(store, clock)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    await _seed_query(store, cid, text="Summarize today's ML papers")

    # Precondition: the conversation is answered → the engine gate is CLOSED.
    before = await store.get_events(cid)
    assert AgentLoop._has_unprocessed_user_message(before) is False, (
        "precondition: an answered conversation must have a closed work-gate"
    )

    sched = mgr.create_schedule(
        conversation_id=cid,
        owner_id="local",
        rrule="*/2 * * * *",
        description="daily paper digest",
        depth="deep",
        model_override="anthropic:claude-opus-4-8",
    )
    # The saved settings must round-trip onto the schedule row (they drive the re-run).
    assert sched.depth == "deep"
    assert sched.model_override == "anthropic:claude-opus-4-8"

    clock[0] = clock[0] + timedelta(minutes=2, seconds=30)
    await mgr._tick()

    # (a) The engine gate is now OPEN — the loop WILL re-run on kick.
    after = await store.get_events(cid)
    assert AgentLoop._has_unprocessed_user_message(after) is True, (
        "a scheduled fire must open the engine work-gate by re-injecting a USER "
        "message — otherwise loop.run() returns immediately and re-runs nothing"
    )

    # The re-injected message carries the ORIGINAL query verbatim.
    user_msgs = [
        e for e in after
        if isinstance(e, MessageEvent) and e.source == EventSource.USER
    ]
    assert user_msgs[-1].message.content == "Summarize today's ML papers"

    # (b) saved settings reproduced before the kick.
    rt.set_model_override.assert_called_with(cid, "anthropic:claude-opus-4-8")
    rt.set_depth.assert_called_with(cid, "deep")
    # (c) the loop was kicked.
    rt.kick.assert_called_with(cid)


@pytest.mark.asyncio
async def test_marker_event_alone_does_not_open_the_work_gate():
    """Documents WHY the re-injected user message is required: a ScheduleRunEvent
    by itself (system-sourced) leaves the engine work-gate CLOSED, so the loop
    would re-run nothing. This is the defect the re-injection fixes."""
    store = _store()
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    await _seed_query(store, cid)

    # Append ONLY the audit marker — the pre-fix behavior.
    await store.append(
        cid, ScheduleRunEvent(source=EventSource.SYSTEM, schedule_id="sched_x", coalesced=False)
    )

    events = await store.get_events(cid)
    assert AgentLoop._has_unprocessed_user_message(events) is False, (
        "a system-sourced marker is NOT an unprocessed user message — kicking on "
        "it alone re-runs nothing (the original rp-08 defect)"
    )

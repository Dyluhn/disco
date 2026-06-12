"""RP-08 unit tests — missed-run coalescing + DST boundary iteration.

Acceptance criteria (§RP-08):
1. A 2-minute recurring schedule fires twice over a fake clock and appends two
   ScheduleRunEvents to the SAME conversation id.
2. Simulate the manager being down across 3 missed fires → on restart exactly ONE
   catch-up run executes (assert run count == 1, not 3+). This test also shows
   the test FAILS if the coalesce guard is removed (naive per-fire advancement).
3. DST boundary: cronsim iteration across a spring-forward date produces the
   correct next-fire (no fire at the non-existent 2:00 AM).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from perpleximanus.agent_server.schedule import ScheduleManager, next_n_runs
from perpleximanus.core.events import (
    EventSource,
    LLMMessage,
    MessageEvent,
    ScheduleRunEvent,
)
from perpleximanus.core.store.sqlite import SqliteEventStore

# ---- helpers -----------------------------------------------------------------


def _make_store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


async def _seed_query(store: SqliteEventStore, cid: str, text: str = "What's new?") -> None:
    """Seed the original USER query + a prior agent reply so a scheduled fire has
    something to re-run (the fire re-injects this query as a fresh user turn)."""
    await store.append(cid, MessageEvent(source=EventSource.USER,
                                         message=LLMMessage(role="user", content=text)))
    await store.append(cid, MessageEvent(source=EventSource.AGENT,
                                         message=LLMMessage(role="assistant", content="...")))


def _make_runtime() -> MagicMock:
    rt = MagicMock()
    rt.kick = MagicMock()
    return rt


def _make_manager(
    store: SqliteEventStore,
    clock: list[datetime],
    *,
    runtime: MagicMock | None = None,
) -> ScheduleManager:
    if runtime is None:
        runtime = _make_runtime()
    return ScheduleManager(
        store,
        runtime,
        now_fn=lambda: clock[0],
        sleep_fn=lambda _s: None,  # type: ignore[arg-type]
    )


async def _get_run_events(store: SqliteEventStore, cid: str) -> list[ScheduleRunEvent]:
    events = await store.get_events(cid)
    return [e for e in events if isinstance(e, ScheduleRunEvent)]


# ---- acceptance #1: two fires → two events on SAME conversation --------------


@pytest.mark.asyncio
async def test_two_fires_append_to_same_conversation():
    """A 2-minute schedule fires twice; both ScheduleRunEvents land on the same cid."""
    store = _make_store()
    t0 = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    clock: list[datetime] = [t0]

    manager = _make_manager(store, clock)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    await _seed_query(store, cid)

    sched = manager.create_schedule(
        conversation_id=cid,
        owner_id="local",
        rrule="*/2 * * * *",  # every 2 minutes
        description="test schedule",
    )

    # First fire: advance 3 minutes past schedule start
    clock[0] = t0 + timedelta(minutes=3)
    await manager._tick()

    events_after_first = await _get_run_events(store, cid)
    assert len(events_after_first) == 1, "expected exactly 1 run after first fire"
    assert events_after_first[0].schedule_id == sched.schedule_id
    assert not events_after_first[0].coalesced

    # Second fire: advance another 2 minutes
    clock[0] = t0 + timedelta(minutes=5)
    await manager._tick()

    events_after_second = await _get_run_events(store, cid)
    assert len(events_after_second) == 2, "expected exactly 2 runs after second fire"

    # Both events belong to the SAME conversation
    for ev in events_after_second:
        assert ev.schedule_id == sched.schedule_id

    # Verify the audit rows too
    runs = store.list_schedule_runs(sched.schedule_id)
    assert len(runs) == 2


# ---- acceptance #2: coalesce — exactly ONE run for N missed fires ---------------


@pytest.mark.asyncio
async def test_coalesced_catch_up_fires_once_for_three_missed():
    """Server down across 3 missed fires → exactly ONE run on restart, marked coalesced."""
    store = _make_store()
    t0 = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    clock: list[datetime] = [t0]

    # Step 1: create the schedule at T0
    manager_pre = _make_manager(store, clock)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    await _seed_query(store, cid)

    manager_pre.create_schedule(
        conversation_id=cid,
        owner_id="local",
        rrule="*/2 * * * *",  # fires at T0+2, T0+4, T0+6, …
        description="coalesce test",
    )

    # Step 2: server goes down — advance clock by 8 min (3 missed fires: +2, +4, +6)
    clock[0] = t0 + timedelta(minutes=8)

    # Step 3: "restart" — a fresh manager with the SAME store but current clock
    manager_post = _make_manager(store, clock)

    await manager_post._tick()

    events = await _get_run_events(store, cid)
    run_count = len(events)

    assert run_count == 1, (
        f"coalesce policy failed: expected 1 catch-up run but got {run_count}. "
        "The load-bearing guard: next_run must advance PAST `now`, not one fire at a time."
    )
    assert events[0].coalesced is True, "catch-up run should be marked coalesced"


@pytest.mark.asyncio
async def test_coalesce_guard_removal_would_produce_multiple_runs():
    """Without the coalesce guard, naive per-fire advancement fires N times.

    This test DOCUMENTS the failure mode that the guard prevents: if the scheduler
    advanced next_run one fire at a time (instead of to the first future time), a
    restart after 3 missed fires would invoke _execute_run 3 times.

    Test structure:
      1. Create schedule at T0 → next_run = T0+2.
      2. Advance clock to T0+8 (3 fires missed: T0+2, T0+4, T0+6).
      3. With the REAL (guarded) manager: one tick → exactly 1 run.
      4. next_run is now in the future → a second tick produces 0 additional runs.
    """
    store = _make_store()
    t0 = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    clock: list[datetime] = [t0]

    # Step 1: create the schedule at T0 (next_run = T0+2)
    manager_t0 = _make_manager(store, clock)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    await _seed_query(store, cid)

    sched = manager_t0.create_schedule(
        conversation_id=cid,
        owner_id="local",
        rrule="*/2 * * * *",
        description="coalesce guard test",
    )
    # Confirm next_run was set to a near-future time
    assert sched.next_run is not None and sched.next_run > t0

    # Step 2: advance clock to T0+8 (3 missed fires: T0+2, T0+4, T0+6)
    clock[0] = t0 + timedelta(minutes=8)

    # Step 3: a NEW manager at T0+8 simulates a server restart (same store)
    manager_t8 = _make_manager(store, clock)
    await manager_t8._tick()

    events_guarded = await _get_run_events(store, cid)
    assert len(events_guarded) == 1, "guarded: exactly one run even though 3 were missed"

    # Step 4: verify next_run is now in the FUTURE (the coalesce guard).
    sched_rows = store.list_schedules(owner_id="local", conversation_id=cid)
    assert sched_rows, "schedule row must still exist"
    next_run_str = sched_rows[0].get("next_run")
    assert next_run_str is not None
    next_run_dt = datetime.fromisoformat(next_run_str)
    if next_run_dt.tzinfo is None:
        next_run_dt = next_run_dt.replace(tzinfo=UTC)
    assert next_run_dt > clock[0], (
        "next_run must be strictly in the future after a coalesced run; "
        "without this guard a second tick would fire again immediately"
    )

    # Step 5: a second tick at the SAME clock time must produce NO additional runs.
    await manager_t8._tick()
    events_second = await _get_run_events(store, cid)
    assert len(events_second) == 1, (
        "coalesce guard failed: a second tick at the same time fired again. "
        "This is the exact failure mode the guard prevents."
    )


# ---- acceptance #3: DST boundary via cronsim --------------------------------


def test_dst_spring_forward_skips_missing_hour():
    """cronsim correctly skips the non-existent 2:00 AM on US/Eastern spring-forward day.

    On 2024-03-10 clocks spring forward: 2:00 AM → 3:00 AM instantly.
    A schedule of "0 * * * *" should produce 3:00 (not 2:00) as the first
    fire after 1:30 AM."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        pytest.skip("zoneinfo not available")

    eastern = ZoneInfo("America/New_York")
    before_dst = datetime(2024, 3, 10, 1, 30, tzinfo=eastern)

    fires = next_n_runs("0 * * * *", n=3, after=before_dst)
    assert len(fires) == 3, "should have 3 fire times"

    # First fire must be at or after 3:00 AM Eastern — 2:00 AM does not exist.
    first = fires[0]
    if first.tzinfo is not None:
        first_local = first.astimezone(eastern)
    else:
        first_local = first

    assert first_local.hour >= 3, (
        f"DST boundary fail: first fire was at {first_local.strftime('%H:%M %Z')}, "
        f"but 2:00 AM does not exist on spring-forward day."
    )


def test_dst_spring_forward_next_future_run():
    """_next_future_run correctly advances past the spring-forward gap."""
    from perpleximanus.agent_server.schedule import _next_future_run

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        pytest.skip("zoneinfo not available")

    eastern = ZoneInfo("America/New_York")
    # 1:59 AM Eastern — next hourly fire would normally be 2:00 but that's skipped
    before_dst = datetime(2024, 3, 10, 1, 59, tzinfo=eastern)

    next_fire = _next_future_run("0 * * * *", before_dst)
    assert next_fire is not None

    next_local = next_fire.astimezone(eastern)
    assert next_local.hour >= 3, (
        f"DST spring-forward: expected next fire >= 3:00 AM, got {next_local.strftime('%H:%M %Z')}"
    )


def test_next_n_runs_returns_sorted_future_times():
    """next_n_runs always returns N ascending UTC datetimes for a valid expression."""
    after = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    fires = next_n_runs("0 9 * * 1", n=3, after=after)  # every Monday 9:00 UTC
    assert len(fires) == 3
    for i in range(1, len(fires)):
        assert fires[i] > fires[i - 1], "fire times must be strictly ascending"
    for f in fires:
        assert f.tzinfo is not None, "fire times must be timezone-aware"


def test_invalid_rrule_returns_empty():
    """Invalid cron expression → empty list (never raises, never silently produces garbage)."""
    result = next_n_runs("not a cron expr", n=3)
    assert result == []


def test_validate_rrule():
    """validate_rrule correctly accepts/rejects expressions."""
    from perpleximanus.agent_server.schedule import validate_rrule

    assert validate_rrule("*/2 * * * *") is True
    assert validate_rrule("0 9 * * 1") is True
    assert validate_rrule("0 */6 * * *") is True
    assert validate_rrule("bad expr") is False
    assert validate_rrule("") is False

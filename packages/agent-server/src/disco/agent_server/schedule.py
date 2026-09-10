"""RP-08 ScheduleManager — cron-style recurring agent runs.

ONE asyncio loop owned by the app lifespan (mirrors _idle_sweep_loop wiring at
app.py:99-128 / runtime.py:1654-1664). Runs append to the SAME conversation
(reuses the event-append seam from reconcile_orphaned_runs). Coalesce policy:
on startup, if N fires were missed while the server was down, exactly ONE
catch-up run executes, then the schedule resumes its normal cadence.

Fake-clock injectable: pass `now_fn` and `sleep_fn` for deterministic tests
without real wall time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError
from disco.core import (
    EventSource,
    MessageEvent,
)
from disco.core.events import ScheduleRunEvent
from disco.core.workflow import ScheduleSpec

from .schedule_models import Schedule, ScheduleRun
from .workflow_schedule import (
    WorkflowScheduleManager,
    WorkflowScheduleRow,
    WorkflowScheduleRunRecord,
)

if TYPE_CHECKING:
    from disco.core.store.sqlite import SqliteEventStore
    from disco.tools.projects import ProjectStore


class ScheduleRuntime(Protocol):
    def _project_store_now(self) -> ProjectStore: ...

    def set_model_override(self, conversation_id: str, model_id: str | None) -> None: ...

    def set_depth(self, conversation_id: str, tier: str | None) -> None: ...

    async def send_user_turn(self, conversation_id: str, text: str) -> MessageEvent: ...

    async def run_sealed_workflow_schedule(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        owner_id: str,
        coalesced: bool,
    ) -> WorkflowScheduleRunRecord: ...

_LOG = logging.getLogger(__name__)

# How often the loop wakes to check for due schedules (seconds).
_POLL_INTERVAL_S = 10


def _parse_cron(rrule: str) -> CronSim | None:
    """Return a CronSim iterator rooted at epoch start, or None if the
    expression is invalid. We validate-by-instantiation since cronsim raises
    CronSimError on bad expressions."""
    try:
        return CronSim(rrule, datetime(2020, 1, 1, tzinfo=UTC))
    except (CronSimError, ValueError, TypeError):
        return None


def validate_rrule(rrule: str) -> bool:
    """True iff `rrule` is a valid 5-field cron expression."""
    return _parse_cron(rrule) is not None


def next_n_runs(
    rrule: str,
    n: int = 3,
    *,
    after: datetime | None = None,
    timezone: str = "UTC",
) -> list[datetime]:
    """Return the next N wall-clock fires in the persisted IANA ``timezone``.

    Returns an empty list if the expression is invalid.  The returned datetimes
    are timezone-aware. Legacy schedules explicitly default to UTC."""
    start = after if after is not None else datetime.now(UTC)
    try:
        zone = ZoneInfo(timezone)
        it = CronSim(rrule, start.astimezone(zone))
        results: list[datetime] = []
        for dt in it:
            results.append(dt)
            if len(results) >= n:
                break
        return results
    except (CronSimError, ZoneInfoNotFoundError, ValueError, TypeError, StopIteration):
        return []


def _next_future_run(
    rrule: str,
    after: datetime,
    *,
    timezone: str = "UTC",
) -> datetime | None:
    """First fire time strictly after `after` (the coalesce-advance step).

    This is the key invariant for run-once coalescing: we always advance
    next_run PAST the current time, so we never fire the same fire twice even
    if N fires were missed while the server was down."""
    try:
        zone = ZoneInfo(timezone)
        it = CronSim(rrule, after.astimezone(zone))
        for dt in it:
            if dt > after:
                return dt
            # cronsim may yield `after` itself; skip until strictly past.
        return None
    except (CronSimError, ZoneInfoNotFoundError, ValueError, TypeError, StopIteration):
        return None


class ScheduleManager:
    """Manages cron-style recurring runs for conversations.

    Lifecycle:
        manager = ScheduleManager(store, runtime)
        task = asyncio.create_task(manager.run())
        ...
        task.cancel()
    """

    def __init__(
        self,
        store: SqliteEventStore,
        runtime: ScheduleRuntime,
        *,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._sleep_fn = sleep_fn or asyncio.sleep  # type: ignore[assignment]
        self._workflow = WorkflowScheduleManager(runtime, now_fn=self._now_fn)

    # ---- public schedule CRUD -----------------------------------------------

    def create_schedule(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        rrule: str,
        description: str,
        timezone: str = "UTC",
        depth: str | None = None,
        model_override: str | None = None,
    ) -> Schedule:
        """Persist a new schedule and return it.  Raises ValueError if the cron
        expression is invalid (never silent — callers surface this to the user)."""
        if not validate_rrule(rrule):
            raise ValueError(f"Invalid cron expression: {rrule!r}")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown IANA timezone: {timezone!r}") from exc

        now = self._now_fn()
        next_run = _next_future_run(rrule, now, timezone=timezone)
        sched = Schedule(
            conversation_id=conversation_id,
            owner_id=owner_id,
            rrule=rrule,
            description=description or rrule,
            timezone=timezone,
            depth=depth,
            model_override=model_override,
            created_at=now,
            next_run=next_run,
        )
        self._store.create_schedule(sched.to_store_row())
        return sched

    def list_schedules(
        self, *, owner_id: str, conversation_id: str | None = None
    ) -> list[Schedule]:
        rows = self._store.list_schedules(owner_id=owner_id, conversation_id=conversation_id)
        return [Schedule.from_store_row(r) for r in rows]

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        return self._store.delete_schedule(schedule_id, owner_id=owner_id)

    def preview_next_runs(
        self,
        rrule: str,
        n: int = 3,
        *,
        timezone: str = "UTC",
    ) -> list[datetime]:
        """Next N run times from now. Returns [] for invalid expressions."""
        return next_n_runs(rrule, n, after=self._now_fn(), timezone=timezone)

    async def fire_now(self, schedule_id: str, *, owner_id: str) -> bool:
        """Run a schedule IMMEDIATELY, out of band (gap #98 — the UI/harness 'fire now').

        Reuses the exact periodic-tick path (`_execute_run`), so it emits a
        ScheduleRunEvent + re-injects the original user query + kicks the loop just
        like a due run — no separate execution logic to drift. `coalesced=False`
        because a manual fire is always intentional. Returns False if no such
        schedule belongs to this owner (caller surfaces a 404)."""
        schedules = [
            Schedule.from_store_row(r) for r in self._store.list_schedules(owner_id=owner_id)
        ]
        sched = next((s for s in schedules if s.schedule_id == schedule_id), None)
        if sched is None:
            return False
        await self._execute_run(sched, coalesced=False)
        return True

    def create_workflow_schedule(
        self,
        spec: ScheduleSpec,
        *,
        owner_id: str | None = None,
    ) -> WorkflowScheduleRow:
        return self._workflow.create_schedule(spec, owner_id=owner_id)

    def list_workflow_schedules(
        self,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> list[WorkflowScheduleRow]:
        return self._workflow.list_schedules(
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )

    def list_workflow_schedule_runs(
        self,
        *,
        schedule_id: str | None = None,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
        limit: int = 100,
    ) -> list[WorkflowScheduleRunRecord]:
        return self._workflow.list_runs(
            schedule_id=schedule_id,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
            limit=limit,
        )

    async def fire_workflow_schedule_now(
        self,
        schedule_id: str,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> WorkflowScheduleRunRecord | None:
        return await self._workflow.fire_now(
            schedule_id,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )

    # ---- background loop ----------------------------------------------------

    async def run(self) -> None:
        """The background asyncio task. Mirrors _idle_sweep_loop pattern."""
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception:
                _LOG.exception("ScheduleManager tick raised unexpectedly")
            try:
                await self._sleep_fn(_POLL_INTERVAL_S)  # type: ignore[misc]
            except asyncio.CancelledError:
                return

    async def _tick(self) -> None:
        """Check all enabled schedules and execute any that are due."""
        now = self._now_fn()
        schedules = [Schedule.from_store_row(r) for r in self._store.list_enabled_schedules()]
        for sched in schedules:
            if sched.next_run is None:
                continue
            # Make next_run timezone-aware if stored as naive (legacy rows).
            next_run = sched.next_run
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=UTC)
            if now >= next_run:
                # One or more fires were due — execute exactly ONE (coalesce policy).
                # Whether it's one fire or N missed fires, we run once and advance
                # next_run past `now`.  This is the load-bearing coalesce guard.
                coalesced = self._was_coalesced(sched, now, next_run)
                await self._execute_run(sched, coalesced=coalesced)
                # Advance to the next FUTURE fire (strictly after now).
                next_future = _next_future_run(
                    sched.rrule,
                    now,
                    timezone=sched.timezone,
                )
                self._store.update_schedule_next_run(
                    sched.schedule_id,
                    next_future.isoformat() if next_future else None,
                )
        await self._workflow.tick()

    def _was_coalesced(self, sched: Schedule, now: datetime, next_run: datetime) -> bool:
        """True if this run covers more than one missed fire.

        We check by counting how many scheduled fires fall in [next_run, now].
        More than one → coalesced.  This is informational; the policy (one run
        regardless) is enforced by the caller — this just labels the run."""
        try:
            zone = ZoneInfo(sched.timezone)
            it = CronSim(sched.rrule, next_run.astimezone(zone))
            count = 0
            for dt in it:
                if dt > now:
                    break
                count += 1
                if count > 1:
                    return True
            return False
        except (CronSimError, ZoneInfoNotFoundError, ValueError, TypeError):
            return False

    async def _original_query(self, conversation_id: str) -> str | None:
        """The conversation's FIRST user message content — the goal to re-run.

        A scheduled fire re-runs the original query; later turns (steers,
        follow-ups) stay in the log as context but are not the thing being
        re-asked. Returns None if the conversation has no user message yet."""
        for e in await self._store.get_events(conversation_id):
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                return e.message.content
        return None

    async def _execute_run(self, sched: Schedule, *, coalesced: bool) -> None:
        """RE-RUN the conversation under its saved settings.

        A ScheduleRunEvent alone does NOT re-run anything: loop.run() only wakes
        for an unprocessed USER message (engine `_has_unprocessed_user_message`),
        and a system-sourced marker is not one. So the actual re-run trigger is a
        freshly re-injected USER message carrying the original query; the marker
        is appended alongside purely for audit/history rendering. Settings (model
        override, depth) are reproduced from the schedule row before the kick,
        mirroring the submit path in app.py."""
        try:
            query = await self._original_query(sched.conversation_id)
            if query is None:
                _LOG.warning(
                    "schedule %s has no original user message to re-run cid=%s — skipping",
                    sched.schedule_id,
                    sched.conversation_id,
                )
                return

            # Reproduce the user's saved settings (mirrors app.py submit: model
            # override unconditional, depth only if set).
            self._runtime.set_model_override(sched.conversation_id, sched.model_override)
            if sched.depth:
                self._runtime.set_depth(sched.conversation_id, sched.depth)

            # Audit marker (UI/history) — informational, NOT the re-run trigger.
            await self._store.append(
                sched.conversation_id,
                ScheduleRunEvent(
                    source=EventSource.SYSTEM,
                    schedule_id=sched.schedule_id,
                    coalesced=coalesced,
                ),
            )
            # The actual re-run trigger: a fresh USER message re-asking the original
            # query (this is what opens loop.run()'s work-gate). Routed through
            # `send_user_turn` — the SAME pinned kernel start/send path the REST/WS
            # routes use (conversations.py/ws.py) — so a scheduled rerun PINS the
            # kernel before the append+kick (finding #1). Appending + kicking directly
            # would create an UNPINNED run whose later control op could re-resolve to a
            # different kernel mid-flight. Byte-identical append for the default disco
            # kernel (`_user_message` ⇒ the same USER MessageEvent constructed here).
            await self._runtime.send_user_turn(sched.conversation_id, query)
            # Persist the audit row AFTER the trigger lands (finding #6): the prior A1
            # change moved the row BEFORE `send_user_turn`, so a failed trigger append
            # left an ORPHAN schedule-run row (history showed a fired schedule with no
            # trigger message) and `fired_at` was timestamped too early. Pre-A1 the row
            # was written only after the USER message was appended — restore that: a
            # raising `send_user_turn` now skips the row, and `fired_at` reflects the
            # real trigger time.
            self._store.create_schedule_run(
                ScheduleRun(
                    schedule_id=sched.schedule_id,
                    conversation_id=sched.conversation_id,
                    fired_at=self._now_fn(),
                    coalesced=coalesced,
                ).to_store_row()
            )
            _LOG.info(
                "schedule %s re-ran cid=%s coalesced=%s",
                sched.schedule_id,
                sched.conversation_id,
                coalesced,
            )
        except Exception:
            _LOG.exception(
                "schedule %s failed to execute cid=%s",
                sched.schedule_id,
                sched.conversation_id,
            )

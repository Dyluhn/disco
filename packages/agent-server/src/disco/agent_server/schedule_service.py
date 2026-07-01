"""Scheduled tasks (RP-08) — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The schedule-facing
accessors move out of runtime.py into a `ScheduleService` collaborator
constructed once in `ConversationRuntime`: the lazy `ScheduleManager` accessor
+ lifespan trampoline (`_schedule_manager` / `_schedule_manager_loop`), the CRUD
+ preview surface (`create_schedule` / `list_schedules` / `delete_schedule` /
`preview_schedule_runs`), and the activity-dashboard history read
(`list_recent_schedule_runs`).

The lazily-created `_sched_manager` handle stays on `ConversationRuntime` (the
ScheduleManager is constructed with the runtime itself as its second arg); the
service reaches it + `_store` via a back-reference. Every method keeps a
one-line delegator on `ConversationRuntime` because the app lifespan + routes
call each on the runtime.
"""

from __future__ import annotations

from typing import Any


class ScheduleService:
    def __init__(self, rt: Any) -> None:
        self._rt = rt

    def _schedule_manager(self) -> Any:
        """Lazy accessor for the ScheduleManager (avoids circular import at
        module load).  The manager is created on first access (or already held
        after _start_schedule_manager is called at lifespan start)."""
        if not hasattr(self._rt, "_sched_manager"):
            from .schedule import ScheduleManager
            self._rt._sched_manager = ScheduleManager(self._rt._store, self._rt)
        return self._rt._sched_manager

    async def _schedule_manager_loop(self) -> None:
        """Thin trampoline: lifespan task → ScheduleManager.run().  Mirrors the
        _idle_sweep_loop pattern so the app lifespan owns the Task."""
        await self._schedule_manager().run()

    def create_schedule(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        rrule: str,
        description: str,
        depth: str | None = None,
        model_override: str | None = None,
    ) -> dict:
        """Create a new schedule and return it as a JSON-safe dict.
        Raises ValueError for invalid cron expressions (caller surfaces to user)."""
        sched = self._schedule_manager().create_schedule(
            conversation_id=conversation_id,
            owner_id=owner_id,
            rrule=rrule,
            description=description,
            depth=depth,
            model_override=model_override,
        )
        return sched.model_dump(mode="json")

    def list_schedules(
        self, *, owner_id: str, conversation_id: str | None = None
    ) -> list[dict]:
        """List schedules, optionally filtered to one conversation."""
        return [
            s.model_dump(mode="json")
            for s in self._schedule_manager().list_schedules(
                owner_id=owner_id, conversation_id=conversation_id
            )
        ]

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        """Delete a schedule. OWNER-SCOPED. Returns True if a row was removed."""
        return self._schedule_manager().delete_schedule(schedule_id, owner_id=owner_id)

    async def fire_now(self, schedule_id: str, *, owner_id: str) -> bool:
        """Run a schedule immediately, out of band. False if not found."""
        return await self._schedule_manager().fire_now(
            schedule_id, owner_id=owner_id
        )

    def preview_schedule_runs(self, rrule: str, n: int = 3) -> list[str]:
        """Preview next N run times for a cron expression (ISO-8601 strings).
        Returns [] for invalid expressions."""
        return [
            dt.isoformat()
            for dt in self._schedule_manager().preview_next_runs(rrule, n)
        ]

    def list_recent_schedule_runs(
        self, *, owner_id: str, limit: int = 50
    ) -> list[dict]:
        """Owner-scoped recent scheduled-run history for the activity dashboard
        (delegates to the store; newest first, with schedule description + title)."""
        fn = getattr(self._rt._store, "list_recent_schedule_runs", None)
        if fn is None:  # a store without the audit table (defensive)
            return []
        return fn(owner_id, limit)

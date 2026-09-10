"""SQLite schedule / scheduled-run persistence collaborator + delegate mixin.

Extracted from ``SqliteEventStore`` so the store class stays within its
architecture budget. This module owns the CRUD and run-history queries for the
``schedules`` and ``schedule_runs`` tables; the schema itself remains in
``store/schema.py`` so all table creation stays in one migration script.

The ``ScheduleStore`` collaborator holds a reference to the parent
``SqliteEventStore`` connection and delegates back to it for any shared
behavior. The ``_ScheduleMixin`` is a private static mixin inherited by
``SqliteEventStore`` so the store retains the exact public schedule surface.

Behavior preserved exactly:

* Uses the store's single sqlite3 connection (passed in).
* All writes commit immediately on that connection (same as before extraction).
* No additional locking is introduced; the original methods were sync and did
  not acquire the store's asyncio write lock.
* Row dictionaries are returned with sqlite3.Row keys intact.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class ScheduleStore:
    """Collaborator for schedule and schedule-run persistence.

    Holds a reference to the parent ``SqliteEventStore`` connection and
    delegates back to it for any shared behavior. All methods are synchronous
    and run directly against ``self._conn`` to match the pre-extraction shape.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_schedule(self, row: dict) -> None:
        """Persist a new schedule row. `row` must contain all required fields.
        Uses INSERT OR IGNORE so a double-create is a no-op."""
        self._conn.execute(
            "INSERT OR IGNORE INTO schedules "
            "(schedule_id, conversation_id, owner_id, rrule, description, "
            "timezone, depth, model_override, created_at, enabled, next_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["schedule_id"],
                row["conversation_id"],
                row["owner_id"],
                row["rrule"],
                row["description"],
                row.get("timezone") or "UTC",
                row.get("depth"),
                row.get("model_override"),
                row["created_at"],
                1 if row.get("enabled", True) else 0,
                row.get("next_run"),
            ),
        )
        self._conn.commit()

    def list_schedules(self, *, owner_id: str, conversation_id: str | None = None) -> list[dict]:
        """List schedules for an owner, optionally filtered by conversation."""
        if conversation_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE owner_id = ? AND conversation_id = ? "
                "ORDER BY created_at DESC",
                (owner_id, conversation_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_enabled_schedules(self) -> list[dict]:
        """All enabled schedules across all owners — used by the background loop."""
        rows = self._conn.execute("SELECT * FROM schedules WHERE enabled = 1").fetchall()
        return [dict(r) for r in rows]

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        """Delete a schedule. OWNER-SCOPED. Returns True if a row was removed."""
        cur = self._conn.execute(
            "DELETE FROM schedules WHERE schedule_id = ? AND owner_id = ?",
            (schedule_id, owner_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_schedule_next_run(self, schedule_id: str, next_run: str | None) -> None:
        """Update the next_run timestamp after a schedule fires."""
        self._conn.execute(
            "UPDATE schedules SET next_run = ? WHERE schedule_id = ?",
            (next_run, schedule_id),
        )
        self._conn.commit()

    def create_schedule_run(self, row: dict) -> None:
        """Record a completed schedule run in the audit log."""
        self._conn.execute(
            "INSERT OR IGNORE INTO schedule_runs "
            "(run_id, schedule_id, conversation_id, fired_at, coalesced) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["run_id"],
                row["schedule_id"],
                row["conversation_id"],
                row["fired_at"],
                1 if row.get("coalesced", False) else 0,
            ),
        )
        self._conn.commit()

    def list_schedule_runs(self, schedule_id: str) -> list[dict]:
        """List run history for a schedule."""
        rows = self._conn.execute(
            "SELECT * FROM schedule_runs WHERE schedule_id = ? ORDER BY fired_at DESC",
            (schedule_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_schedule_runs(self, owner_id: str, limit: int = 50) -> list[dict]:
        """Owner-scoped recent scheduled-run history for the activity dashboard, newest
        first, joined with the schedule description + conversation title for display.
        Scoped by JOINing schedule_runs → schedules (which carries owner_id); a run
        whose schedule was deleted drops out (its history is gone with it, by design).
        Returns rows: run_id, schedule_id, conversation_id, fired_at, coalesced,
        description, title."""
        rows = self._conn.execute(
            "SELECT sr.run_id, sr.schedule_id, sr.conversation_id, sr.fired_at, "
            "       sr.coalesced, s.description, c.title "
            "FROM schedule_runs sr "
            "JOIN schedules s ON s.schedule_id = sr.schedule_id "
            "LEFT JOIN conversations c ON c.conversation_id = sr.conversation_id "
            "WHERE s.owner_id = ? "
            "ORDER BY sr.fired_at DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


class _ScheduleMixin:
    """Private static mixin: schedule compatibility delegates (RP-08).

    Inherited by ``SqliteEventStore``; not instantiated directly. All methods
    delegate to ``self._schedules`` (a ``ScheduleStore``) provided by the host
    class, preserving the exact pre-extraction public surface.
    """

    # Host-provided attribute (declared for type-checking; assigned by SqliteEventStore).
    _schedules: ScheduleStore

    def create_schedule(self, row: dict) -> None:
        self._schedules.create_schedule(row)

    def list_schedules(self, *, owner_id: str, conversation_id: str | None = None) -> list[dict]:
        return self._schedules.list_schedules(owner_id=owner_id, conversation_id=conversation_id)

    def list_enabled_schedules(self) -> list[dict]:
        return self._schedules.list_enabled_schedules()

    def delete_schedule(self, schedule_id: str, *, owner_id: str) -> bool:
        return self._schedules.delete_schedule(schedule_id, owner_id=owner_id)

    def update_schedule_next_run(self, schedule_id: str, next_run: str | None) -> None:
        self._schedules.update_schedule_next_run(schedule_id, next_run)

    def create_schedule_run(self, row: dict) -> None:
        self._schedules.create_schedule_run(row)

    def list_schedule_runs(self, schedule_id: str) -> list[dict]:
        return self._schedules.list_schedule_runs(schedule_id)

    def list_recent_schedule_runs(self, owner_id: str, limit: int = 50) -> list[dict]:
        return self._schedules.list_recent_schedule_runs(owner_id, limit)


__all__ = ["ScheduleStore", "_ScheduleMixin"]

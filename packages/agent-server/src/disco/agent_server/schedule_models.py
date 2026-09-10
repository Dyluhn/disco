"""Pydantic models for RP-08 scheduled tasks — Schedule row, ScheduleRun row,
and NL-parse result. These are agent-server–local (core has no schedule import).
The `rrule` field stores a 5-field cron expression (cronsim-parseable)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


def _new_schedule_id() -> str:
    return f"sched_{uuid.uuid4().hex}"


def _new_run_id() -> str:
    return f"srun_{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


class Schedule(BaseModel):
    """One user-created recurring schedule. `rrule` is a 5-field cron expression
    like "0 9 * * 1" (cronsim-parseable). `next_run` is the next scheduled fire
    time; the loop advances it past `now` after each fire (coalesce policy)."""

    schedule_id: str = Field(default_factory=_new_schedule_id)
    conversation_id: str
    owner_id: str
    rrule: str  # cron expression, e.g. "*/2 * * * *"
    description: str
    timezone: str = "UTC"
    depth: str | None = None  # Deep Research depth tier
    model_override: str | None = None  # driver model override
    created_at: datetime = Field(default_factory=_now)
    enabled: bool = True
    next_run: datetime | None = None

    def to_store_row(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "conversation_id": self.conversation_id,
            "owner_id": self.owner_id,
            "rrule": self.rrule,
            "description": self.description,
            "timezone": self.timezone,
            "depth": self.depth,
            "model_override": self.model_override,
            "created_at": self.created_at.isoformat(),
            "enabled": self.enabled,
            "next_run": self.next_run.isoformat() if self.next_run else None,
        }

    @classmethod
    def from_store_row(cls, row: dict) -> Schedule:
        next_run = row.get("next_run")
        created_at = row.get("created_at")
        return cls(
            schedule_id=row["schedule_id"],
            conversation_id=row["conversation_id"],
            owner_id=row["owner_id"],
            rrule=row["rrule"],
            description=row["description"],
            # Explicit migration: legacy rows were evaluated as UTC cron, so a
            # missing value remains UTC rather than adopting the host timezone.
            timezone=str(row.get("timezone") or "UTC"),
            depth=row.get("depth"),
            model_override=row.get("model_override"),
            created_at=datetime.fromisoformat(created_at) if created_at else _now(),
            enabled=bool(row.get("enabled", 1)),
            next_run=datetime.fromisoformat(next_run) if next_run else None,
        )


class ScheduleRun(BaseModel):
    """Audit record for one completed schedule run."""

    run_id: str = Field(default_factory=_new_run_id)
    schedule_id: str
    conversation_id: str
    fired_at: datetime = Field(default_factory=_now)
    coalesced: bool = False

    def to_store_row(self) -> dict:
        return {
            "run_id": self.run_id,
            "schedule_id": self.schedule_id,
            "conversation_id": self.conversation_id,
            "fired_at": self.fired_at.isoformat(),
            "coalesced": self.coalesced,
        }


class NLParseResult(BaseModel):
    """Result of parsing a natural-language schedule expression.

    `success=True` means `rrule` holds a validated cron expression.
    `success=False` means `error` describes why the parse failed."""

    success: bool
    rrule: str | None = None  # validated cron expression when success=True
    description: str = ""  # human-readable description of the schedule
    error: str | None = None  # present when success=False


class CreateScheduleBody(BaseModel):
    """Request body for POST /api/conversations/{cid}/schedules."""

    rrule: str  # 5-field cron expression
    description: str = ""
    timezone: str = "UTC"
    depth: str | None = None
    model_override: str | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value


class PreviewScheduleBody(BaseModel):
    """Request body for POST /api/schedules/preview."""

    rrule: str
    n: int = 3  # number of upcoming run times to return
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

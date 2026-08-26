"""Sealed scheduled workflow runs.

This is additive to the RP-08 conversation schedules. Workflow schedules live in
JSON beside the workflow instance store so they can pin an approved workflow
instance digest and fire fresh sealed conversations instead of appending to an
existing conversation.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError
from disco.core.owners import install_owner_id
from disco.core.workflow import ScheduleSpec
from disco.tools.projects import ProjectStore
from pydantic import BaseModel, ConfigDict, Field

_LOG = logging.getLogger(__name__)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


def _now() -> datetime:
    return datetime.now(UTC)


def _new_schedule_id() -> str:
    return f"wfsched_{uuid.uuid4().hex}"


def _new_run_id() -> str:
    return f"wfsrun_{uuid.uuid4().hex}"


def _next_future_run(
    cron: str,
    after: datetime,
    *,
    timezone: str = "UTC",
) -> datetime | None:
    try:
        zone = ZoneInfo(timezone)
        it = CronSim(cron, after.astimezone(zone))
        for dt in it:
            if dt > after:
                return dt
        return None
    except (CronSimError, ZoneInfoNotFoundError, ValueError, TypeError, StopIteration):
        return None


def _was_coalesced(
    cron: str,
    now: datetime,
    next_run: datetime,
    *,
    timezone: str = "UTC",
) -> bool:
    try:
        zone = ZoneInfo(timezone)
        count = 0
        for dt in CronSim(cron, next_run.astimezone(zone)):
            if dt > now:
                break
            count += 1
            if count > 1:
                return True
        return False
    except (CronSimError, ZoneInfoNotFoundError, ValueError, TypeError):
        return False


def _json_safe_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


class WorkflowScheduleRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str = Field(default_factory=_new_schedule_id)
    owner_id: str = Field(default_factory=install_owner_id)
    spec: ScheduleSpec
    created_at: datetime = Field(default_factory=_now)
    next_run: datetime | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schedule_id": self.schedule_id,
            "owner_id": self.owner_id,
            "spec": self.spec.model_dump(mode="json"),
            "created_at": self.created_at.isoformat(),
            "next_run": _json_safe_dt(self.next_run),
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, object]) -> WorkflowScheduleRow:
        return cls(
            schedule_id=str(data["schedule_id"]),
            owner_id=str(data.get("owner_id") or install_owner_id()),
            spec=ScheduleSpec.model_validate(data["spec"]),
            created_at=_parse_dt(str(data.get("created_at") or "")) or _now(),
            next_run=_parse_dt(str(data["next_run"]) if data.get("next_run") is not None else None),
        )


class WorkflowScheduleRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(default_factory=_new_run_id)
    schedule_id: str
    run_cid: str
    fired_at: datetime = Field(default_factory=_now)
    terminal_state: str
    output_path: str
    verify_verdict: str
    coalesced: bool = False
    error: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        return dict(payload)


class JsonWorkflowScheduleStore:
    """Workflow schedule JSON store under ``<projects_root>/workflow_schedules``."""

    def __init__(self, projects_root: str | Path) -> None:
        self._root = Path(projects_root).expanduser()
        self._dir = self._root / "workflow_schedules"
        self._schedules_dir = self._dir / "schedules"
        self._runs_path = self._dir / "runs.json"

    @property
    def schedules_dir(self) -> Path:
        return self._schedules_dir

    @property
    def runs_path(self) -> Path:
        return self._runs_path

    def _path_for(self, schedule_id: str) -> Path:
        if not _SAFE_ID.fullmatch(schedule_id):
            raise ValueError(f"unsafe workflow schedule_id: {schedule_id!r}")
        return self._schedules_dir / f"{schedule_id}.json"

    def _load_schedule(self, path: Path) -> tuple[WorkflowScheduleRow, bool]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("workflow schedule must be a JSON object")
        raw_owner = raw.get("owner_id")
        legacy_unclaimed = not isinstance(raw_owner, str) or not raw_owner.strip()
        if legacy_unclaimed:
            raw["owner_id"] = install_owner_id()
        return WorkflowScheduleRow.from_json_dict(raw), legacy_unclaimed

    def create_schedule(
        self,
        spec: ScheduleSpec,
        *,
        owner_id: str | None = None,
        now: datetime | None = None,
    ) -> WorkflowScheduleRow:
        current = now or _now()
        resolved_owner = owner_id or install_owner_id()
        existing = [
            row
            for row in self.list_schedules(owner_id=resolved_owner)
            if row.spec.instance_id == spec.instance_id
        ]
        current_row = existing[0] if existing else None
        row = WorkflowScheduleRow(
            schedule_id=(
                current_row.schedule_id if current_row is not None else _new_schedule_id()
            ),
            owner_id=resolved_owner,
            spec=spec,
            created_at=(current_row.created_at if current_row is not None else current),
            next_run=_next_future_run(
                spec.cron,
                current,
                timezone=spec.timezone,
            ),
        )
        _write_json_atomic(self._path_for(row.schedule_id), row.to_json_dict())
        # One workflow has one editable schedule. Clean up any legacy duplicate
        # rows only after the replacement is durably published.
        for duplicate in existing[1:]:
            self._path_for(duplicate.schedule_id).unlink(missing_ok=True)
        return row

    def list_schedules(
        self,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> list[WorkflowScheduleRow]:
        if not self._schedules_dir.is_dir():
            return []
        rows: list[WorkflowScheduleRow] = []
        for path in sorted(self._schedules_dir.glob("*.json")):
            if not _SAFE_ID.fullmatch(path.stem):
                continue
            try:
                row, legacy_unclaimed = self._load_schedule(path)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if legacy_unclaimed and not include_unclaimed_legacy:
                continue
            if owner_id is None or row.owner_id == owner_id:
                rows.append(row)
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    def list_enabled_schedules(self) -> list[WorkflowScheduleRow]:
        return [
            row for row in self.list_schedules(include_unclaimed_legacy=True) if row.spec.enabled
        ]

    def update_next_run(self, schedule_id: str, next_run: datetime | None) -> None:
        path = self._path_for(schedule_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        row = WorkflowScheduleRow.from_json_dict(raw)
        updated = row.model_copy(update={"next_run": next_run})
        _write_json_atomic(path, updated.to_json_dict())

    def append_run(self, record: WorkflowScheduleRunRecord) -> None:
        rows = self.list_runs()
        rows.append(record)
        _write_json_atomic(self._runs_path, [row.to_json_dict() for row in rows])

    def list_runs(
        self,
        *,
        schedule_id: str | None = None,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
        limit: int = 100,
    ) -> list[WorkflowScheduleRunRecord]:
        if not self._runs_path.is_file():
            return []
        try:
            raw = json.loads(self._runs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(raw, list):
            return []
        rows: list[WorkflowScheduleRunRecord] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                rows.append(WorkflowScheduleRunRecord.model_validate(item))
            except ValueError:
                continue
        if owner_id is not None:
            owned_schedule_ids = {
                row.schedule_id
                for row in self.list_schedules(
                    owner_id=owner_id,
                    include_unclaimed_legacy=include_unclaimed_legacy,
                )
            }
            rows = [row for row in rows if row.schedule_id in owned_schedule_ids]
        if schedule_id is not None:
            rows = [row for row in rows if row.schedule_id == schedule_id]
        rows.sort(key=lambda r: r.fired_at, reverse=True)
        return rows[:limit]


class WorkflowScheduleRuntime(Protocol):
    def _project_store_now(self) -> ProjectStore: ...

    async def run_sealed_workflow_schedule(
        self,
        *,
        schedule_id: str,
        spec: ScheduleSpec,
        owner_id: str,
        coalesced: bool,
    ) -> WorkflowScheduleRunRecord: ...


class WorkflowScheduleManager:
    def __init__(
        self,
        runtime: WorkflowScheduleRuntime,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._now_fn = now_fn or _now

    def _store(self) -> JsonWorkflowScheduleStore:
        projects_root = self._runtime._project_store_now().root
        return JsonWorkflowScheduleStore(projects_root or "")

    def create_schedule(
        self,
        spec: ScheduleSpec,
        *,
        owner_id: str | None = None,
    ) -> WorkflowScheduleRow:
        return self._store().create_schedule(spec, owner_id=owner_id, now=self._now_fn())

    def list_schedules(
        self,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> list[WorkflowScheduleRow]:
        return self._store().list_schedules(
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
        )

    def list_runs(
        self,
        *,
        schedule_id: str | None = None,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
        limit: int = 100,
    ) -> list[WorkflowScheduleRunRecord]:
        return self._store().list_runs(
            schedule_id=schedule_id,
            owner_id=owner_id,
            include_unclaimed_legacy=include_unclaimed_legacy,
            limit=limit,
        )

    async def fire_now(
        self,
        schedule_id: str,
        *,
        owner_id: str | None = None,
        include_unclaimed_legacy: bool = False,
    ) -> WorkflowScheduleRunRecord | None:
        row = next(
            (
                candidate
                for candidate in self._store().list_schedules(
                    owner_id=owner_id,
                    include_unclaimed_legacy=include_unclaimed_legacy,
                )
                if candidate.schedule_id == schedule_id
            ),
            None,
        )
        if row is None:
            return None
        record = await self._execute_run(row, coalesced=False)
        return record

    async def tick(self) -> None:
        now = self._now_fn()
        store = self._store()
        for row in store.list_enabled_schedules():
            if row.next_run is None:
                continue
            next_run = row.next_run
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=UTC)
            if now < next_run:
                continue
            coalesced = _was_coalesced(
                row.spec.cron,
                now,
                next_run,
                timezone=row.spec.timezone,
            )
            await self._execute_run(row, coalesced=coalesced)
            store.update_next_run(
                row.schedule_id,
                _next_future_run(
                    row.spec.cron,
                    now,
                    timezone=row.spec.timezone,
                ),
            )

    async def _execute_run(
        self, row: WorkflowScheduleRow, *, coalesced: bool
    ) -> WorkflowScheduleRunRecord:
        try:
            record = await self._runtime.run_sealed_workflow_schedule(
                schedule_id=row.schedule_id,
                spec=row.spec,
                owner_id=row.owner_id,
                coalesced=coalesced,
            )
        except Exception as exc:  # noqa: BLE001 - scheduler must keep sweeping
            _LOG.exception("workflow schedule %s failed", row.schedule_id)
            record = WorkflowScheduleRunRecord(
                schedule_id=row.schedule_id,
                run_cid="",
                fired_at=self._now_fn(),
                terminal_state="ERROR",
                output_path="",
                verify_verdict="error",
                coalesced=coalesced,
                error=str(exc),
            )
        self._store().append_run(record)
        return record


__all__ = [
    "JsonWorkflowScheduleStore",
    "WorkflowScheduleManager",
    "WorkflowScheduleRow",
    "WorkflowScheduleRunRecord",
]

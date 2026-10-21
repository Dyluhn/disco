"""Owner-scoped schedule lifecycle, dispatch races, and durable run history."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from disco.agent_server.routes.schedules import make_schedules_router
from disco.agent_server.schedule_service import ScheduleService
from disco.agent_server.workflow_schedule import (
    JsonWorkflowScheduleStore,
    WorkflowScheduleManager,
    WorkflowScheduleRunRecord,
)
from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore
from disco.core.workflow import ScheduleSpec
from fastapi import FastAPI

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def spec(instance="wf_example", **changes):
    return ScheduleSpec(instance_id=instance, instance_digest="a" * 64, cron="* * * * *", **changes)


def record(schedule_id, **changes):
    return WorkflowScheduleRunRecord(
        schedule_id=schedule_id,
        run_cid="conv_test",
        terminal_state="FINISHED",
        output_path="report.txt",
        verify_verdict="pass",
        **changes,
    )


def test_pause_resume_preserves_identity_and_avoids_catchup(tmp_path):
    store = JsonWorkflowScheduleStore(tmp_path)
    row = store.create_schedule(spec(), owner_id="alice", now=NOW)
    paused = store.set_enabled(row.schedule_id, enabled=False, owner_id="alice", now=NOW)
    assert paused.spec.enabled is False and paused.next_run is None
    assert paused.spec.instance_digest == row.spec.instance_digest
    assert not store.list_enabled_schedules()
    # A delayed in-flight completion must not reactivate a paused schedule.
    store.update_next_run(row.schedule_id, NOW + timedelta(minutes=1))
    assert store.list_schedules(owner_id="alice")[0].next_run is None
    resumed_at = NOW + timedelta(days=3)
    resumed = store.set_enabled(row.schedule_id, enabled=True, owner_id="alice", now=resumed_at)
    assert resumed.schedule_id == row.schedule_id and resumed.created_at == row.created_at
    assert resumed.next_run == resumed_at + timedelta(minutes=1)
    assert (
        store.set_enabled(
            row.schedule_id, enabled=True, owner_id="alice", now=resumed_at + timedelta(seconds=30)
        )
        == resumed
    )
    assert JsonWorkflowScheduleStore(tmp_path).list_enabled_schedules() == [resumed]


def test_save_retries_do_not_duplicate_or_resume_paused_schedule(tmp_path):
    store = JsonWorkflowScheduleStore(tmp_path)
    row = store.create_schedule(spec(), owner_id="alice", now=NOW)
    assert store.create_schedule(spec(), owner_id="alice", now=NOW + timedelta(hours=1)) == row
    paused = store.set_enabled(row.schedule_id, enabled=False, owner_id="alice")
    assert store.create_schedule(spec(), owner_id="alice") == paused
    assert store.create_schedule(spec(), owner_id="bob").schedule_id != row.schedule_id
    assert len(store.list_schedules(owner_id="alice")) == 1


def test_history_retains_all_records_and_explicit_owner_without_schedule(tmp_path):
    store = JsonWorkflowScheduleStore(tmp_path)
    row = store.create_schedule(spec(), owner_id="alice")
    for _ in range(105):
        store.append_run(record(row.schedule_id, owner_id="alice"))
    store.append_run(record(row.schedule_id, owner_id="bob"))
    store.append_run(record(row.schedule_id))  # legacy needs surviving schedule authority
    store.set_enabled(row.schedule_id, enabled=False, owner_id="alice")
    assert len(store.list_runs(owner_id="alice", limit=200)) == 106
    (store.schedules_dir / f"{row.schedule_id}.json").unlink()
    assert len(store.list_runs(owner_id="alice", limit=200)) == 105
    assert len(store.list_runs(owner_id="bob", limit=200)) == 1
    assert store.list_runs(owner_id="mallory") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_running", [False, True])
async def test_paused_job_never_dispatches_and_pending_tick_rechecks_state(
    tmp_path, resume_running
):
    now = [NOW]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        return record(kwargs["schedule_id"])

    runtime = SimpleNamespace(
        _project_store_now=lambda: SimpleNamespace(root=tmp_path), run_sealed_workflow_schedule=run
    )
    manager = WorkflowScheduleManager(runtime, now_fn=lambda: now[0])
    waiting = manager.create_schedule(spec("wf_waiting"), owner_id="alice")
    now[0] += timedelta(seconds=1)
    first = manager.create_schedule(spec("wf_first"), owner_id="alice")
    now[0] += timedelta(minutes=2)
    task = asyncio.create_task(manager.tick())
    await asyncio.wait_for(entered.wait(), 2)
    assert calls[0]["schedule_id"] == first.schedule_id
    manager.set_enabled(waiting.schedule_id, enabled=False, owner_id="alice")
    manager.set_enabled(first.schedule_id, enabled=False, owner_id="alice")
    if resume_running:
        now[0] += timedelta(days=2)
        manager.set_enabled(first.schedule_id, enabled=True, owner_id="alice")
    release.set()
    await task
    await manager.tick()
    assert len(calls) == 1
    assert await manager.fire_now(waiting.schedule_id, owner_id="alice") is None
    history = manager.list_runs(owner_id="alice")
    assert len(history) == 1 and history[0].owner_id == "alice"
    state = {row.schedule_id: row for row in manager.list_schedules(owner_id="alice")}
    assert state[waiting.schedule_id].next_run is None
    expected = (now[0] + timedelta(minutes=1)).replace(second=0) if resume_running else None
    assert state[first.schedule_id].next_run == expected


@pytest.mark.asyncio
async def test_pause_api_owner_isolation_validation_and_persistence(tmp_path):
    db = SqliteEventStore(str(tmp_path / "events.db"))
    service = ScheduleService(
        db,
        workflow_runs=SimpleNamespace(run=AsyncMock()),
        project_access=SimpleNamespace(_project_store_now=lambda: SimpleNamespace(root=tmp_path)),
        recurring_control=SimpleNamespace(),
    )
    row = service.create_workflow_schedule(spec(), owner_id="alice")
    app = FastAPI()

    @app.middleware("http")
    async def test_identity(request, call_next):
        request.state.auth_session = AuthSession(
            owner_id=request.headers.get("x-test-owner", "alice"),
            csrf_token="test",
            session_id="test",
            expires_at=9999999999,
        )
        return await call_next(request)

    app.include_router(make_schedules_router(db, SimpleNamespace(schedules=service)))
    url = f"/api/workflows/schedules/{row['schedule_id']}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for owner in ("bob", "mallory"):
            assert (
                await client.patch(url, json={"enabled": False}, headers={"x-test-owner": owner})
            ).status_code == 404
        assert service.list_workflow_schedules(owner_id="alice")[0]["spec"]["enabled"]
        for invalid in ("bad%20id", "..", "a" * 121):
            assert (
                await client.patch(f"/api/workflows/schedules/{invalid}", json={"enabled": False})
            ).status_code == 404
        for body in ({"enabled": "false"}, {"enabled": False, "owner_id": "bob"}, {}):
            assert (await client.patch(url, json=body)).status_code == 422
        paused = await client.patch(url, json={"enabled": False})
        assert paused.status_code == 200 and paused.json()["next_run"] is None
        assert not (await client.get("/api/workflows/schedules")).json()["schedules"][0]["spec"][
            "enabled"
        ]
        assert (
            await client.get("/api/workflows/schedules", headers={"x-test-owner": "bob"})
        ).json()["schedules"] == []
        assert (await client.post(url + "/fire-now")).status_code == 404
        assert (await client.patch(url, json={"enabled": True})).json()["spec"]["enabled"]
    db.close()


def test_unclaimed_legacy_cannot_be_mutated_by_assuming_an_owner(tmp_path):
    store = JsonWorkflowScheduleStore(tmp_path)
    row = store.create_schedule(spec(), owner_id="alice")
    path = store.schedules_dir / f"{row.schedule_id}.json"
    legacy = row.to_json_dict()
    del legacy["owner_id"]
    path.write_text(json.dumps(legacy))
    original = path.read_bytes()
    for owner in ("alice", "local", "mallory"):
        assert store.set_enabled(row.schedule_id, enabled=False, owner_id=owner) is None
    assert path.read_bytes() == original

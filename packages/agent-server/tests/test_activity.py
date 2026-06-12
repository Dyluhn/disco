"""The background-task dashboard feed (GET /api/activity) + its store query.

Headless: a fake not-done "task" stands in for a live run (no real loop), and
schedule-run audit rows are written directly. Asserts the three things the
dashboard shows — what's running now, recent scheduled runs, and the global count —
plus owner-scoping and the done-task / other-owner exclusions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from perpleximanus.agent_server import ConversationRuntime, create_app
from perpleximanus.core import SqliteEventStore
from perpleximanus.core.llm import ConfigStore, RouterConfig


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg_store = ConfigStore(path=Path("/dev/null"))
    cfg_store.load = lambda: cfg  # type: ignore[method-assign]
    return ConversationRuntime(store, config=cfg, config_store=cfg_store)


class _FakeTask:
    """Stands in for an asyncio.Task; `done()` drives running_conversation_ids."""

    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


# ---- store: owner-scoped recent schedule runs -------------------------------


def test_list_recent_schedule_runs_owner_scoped_and_joined(store):
    # owner "alice" has a schedule + two runs; owner "bob" has his own — must not leak.
    store.create_conversation("c_alice", owner_id="alice", title="Nightly digest")
    store.create_conversation("c_bob", owner_id="bob", title="Bob's thing")
    store.create_schedule(
        {
            "schedule_id": "s_alice", "conversation_id": "c_alice", "owner_id": "alice",
            "rrule": "0 9 * * *", "description": "Morning brief", "depth": None,
            "model_override": None, "created_at": "2026-06-10T00:00:00Z",
            "enabled": True, "next_run": "2026-06-13T09:00:00Z",
        }
    )
    store.create_schedule(
        {
            "schedule_id": "s_bob", "conversation_id": "c_bob", "owner_id": "bob",
            "rrule": "0 9 * * *", "description": "Bob brief", "depth": None,
            "model_override": None, "created_at": "2026-06-10T00:00:00Z",
            "enabled": True, "next_run": "2026-06-13T09:00:00Z",
        }
    )
    store.create_schedule_run(
        {"run_id": "r1", "schedule_id": "s_alice", "conversation_id": "c_alice",
         "fired_at": "2026-06-11T09:00:00Z", "coalesced": False}
    )
    store.create_schedule_run(
        {"run_id": "r2", "schedule_id": "s_alice", "conversation_id": "c_alice",
         "fired_at": "2026-06-12T09:00:00Z", "coalesced": True}
    )
    store.create_schedule_run(
        {"run_id": "r3", "schedule_id": "s_bob", "conversation_id": "c_bob",
         "fired_at": "2026-06-12T09:00:00Z", "coalesced": False}
    )

    rows = store.list_recent_schedule_runs("alice", limit=50)
    assert [r["run_id"] for r in rows] == ["r2", "r1"]  # newest first, bob excluded
    assert rows[0]["description"] == "Morning brief"  # joined from schedules
    assert rows[0]["title"] == "Nightly digest"  # joined from conversations
    assert rows[0]["coalesced"] == 1


# ---- endpoint: running + recent + counts ------------------------------------


def test_activity_lists_running_tasks_and_count(store):
    runtime = _runtime(store)
    store.create_conversation("c_run", owner_id="local", title="A live task", surface="agent")
    store.create_conversation("c_idle", owner_id="local", title="Finished", surface="build")
    # one live task, one done task (excluded), and a cid with no conversation row
    runtime._tasks["c_run"] = _FakeTask(done=False)  # type: ignore[assignment]
    runtime._tasks["c_idle"] = _FakeTask(done=True)  # type: ignore[assignment]
    runtime._tasks["c_ghost"] = _FakeTask(done=False)  # type: ignore[assignment]

    client = TestClient(create_app(store, runtime=runtime))
    body = client.get("/api/activity").json()

    assert body["counts"]["running"] == 1  # only the live, owned task
    assert len(body["running"]) == 1
    row = body["running"][0]
    assert row["id"] == "c_run"
    assert row["title"] == "A live task"
    assert row["surface"] == "agent"


def test_activity_excludes_other_owners_running_tasks(store):
    runtime = _runtime(store)
    store.create_conversation("c_other", owner_id="someone_else", title="Not yours")
    runtime._tasks["c_other"] = _FakeTask(done=False)  # type: ignore[assignment]

    client = TestClient(create_app(store, runtime=runtime))
    body = client.get("/api/activity?owner_id=local").json()
    assert body["counts"]["running"] == 0  # owner-scoped: another owner's run is hidden
    assert body["running"] == []


def test_activity_includes_recent_runs(store):
    runtime = _runtime(store)
    store.create_conversation("c1", owner_id="local", title="Scheduled one")
    store.create_schedule(
        {
            "schedule_id": "s1", "conversation_id": "c1", "owner_id": "local",
            "rrule": "0 9 * * *", "description": "Daily", "depth": None,
            "model_override": None, "created_at": "2026-06-10T00:00:00Z",
            "enabled": True, "next_run": "2026-06-13T09:00:00Z",
        }
    )
    store.create_schedule_run(
        {"run_id": "r1", "schedule_id": "s1", "conversation_id": "c1",
         "fired_at": "2026-06-12T09:00:00Z", "coalesced": False}
    )
    client = TestClient(create_app(store, runtime=runtime))
    body = client.get("/api/activity").json()
    assert len(body["recent_runs"]) == 1
    assert body["recent_runs"][0]["description"] == "Daily"


def test_activity_empty_when_nothing_running(store):
    runtime = _runtime(store)
    client = TestClient(create_app(store, runtime=runtime))
    body = client.get("/api/activity").json()
    assert body == {"running": [], "recent_runs": [], "counts": {"running": 0}}

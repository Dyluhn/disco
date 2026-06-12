"""The /health readiness probe (plan Phase 8). Headless via Starlette TestClient.

It must: report 200/ok when the store is reachable, distinguish wire-only mode
(no runtime) from a wired runtime, and flip to 503/degraded when a dependency is
broken — so a systemd-timer / uptime check alerts on a dead store rather than a
silent half-up server (the failure mode that hid the agent-server dying mid-run).
"""

from __future__ import annotations

import pytest
from disco.agent_server import create_app
from disco.core import SqliteEventStore
from fastapi.testclient import TestClient


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


def test_health_ok_wire_only(store):
    """No runtime injected → liveness still ok, runtime reported absent (not an error)."""
    client = TestClient(create_app(store, runtime=None))
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["checks"]["store"] == "ok"
    assert body["checks"]["runtime"] == "absent (wire-only mode)"
    assert body["version"]  # version surfaced for deploy-drift detection


def test_health_degraded_when_store_unreachable(store, monkeypatch):
    """A broken store dependency → 503 degraded with the error surfaced, NOT a 200
    or a 500 crash. This is the signal an ops probe alerts on."""

    async def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "list_conversations", boom)
    client = TestClient(create_app(store, runtime=None))
    res = client.get("/health")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "degraded"
    assert "database is locked" in body["checks"]["store"]

"""`GET /api/diagnostics` — the in-process half of the doctor, redacted, no model call."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from disco.agent_server.routes.diagnostics import make_diagnostics_router
from disco.core.store.sqlite import SqliteEventStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_diagnostics_reports_build_health_sandbox_checks_and_redacted_env(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "disco.db"
    conn = sqlite3.connect(db); conn.execute("CREATE TABLE events (id TEXT)"); conn.commit(); conn.close()
    monkeypatch.setenv("DISCO_BUILD_TAG", "v9.9.9")
    monkeypatch.setenv("DISCO_BUILD_COMMIT", "deadbee")
    monkeypatch.setenv("DISCO_DB", str(db))
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISCO_OPENROUTER_API_KEY", "sk-live-secret")
    monkeypatch.setenv("DISCO_SECRET_KEY", "k")
    app = FastAPI()
    app.include_router(make_diagnostics_router(SqliteEventStore(":memory:"), None))

    body = TestClient(app).get("/api/diagnostics").json()

    assert body["version"] == "v9.9.9 (deadbee)" and body["build"]["source"] == "image"
    assert body["agent_server"]["checks"]["store"] == "ok"
    assert body["agent_server"]["checks"]["runtime"] == "absent (wire-only mode)"
    assert body["sandbox"] == {"reachable": False, "backend": "unknown", "detail": "runtime unavailable"}
    names = [c["name"] for c in body["checks"]]
    assert names == ["config", "data disk", "database", "secret key"]
    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["database"]["status"] == "PASS" and "quick_check ok" in by_name["database"]["detail"]
    assert by_name["secret key"]["status"] == "PASS"
    assert body["env"]["DISCO_OPENROUTER_API_KEY"] == "***REDACTED***"
    assert "sk-live-secret" not in str(body)

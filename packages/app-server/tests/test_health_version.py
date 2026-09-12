"""The app-server health body names the build, like the agent-server's."""

from __future__ import annotations

from disco.app_server.routes.health import make_health_router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_health_reports_version_and_build(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_BUILD_TAG", "v9.9.9")
    monkeypatch.setenv("DISCO_BUILD_COMMIT", "deadbee")
    app = FastAPI()
    app.include_router(make_health_router())
    body = TestClient(app).get("/api/health").json()
    assert body["status"] == "ok" and body["service"] == "app-server"
    assert body["version"] == "v9.9.9 (deadbee)"
    assert body["build"] == {"tag": "v9.9.9", "commit": "deadbee", "source": "image"}

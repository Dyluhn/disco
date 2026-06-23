"""W-48 — the sandbox connectivity PREFLIGHT probe route (POST /api/sandbox/test).

Builds the SAME backend the agent-server would and runs its `healthcheck()`,
returning a typed host-NAMING ProbeResult at HTTP 200 (never a 500). The process
backend ok-path is hermetic; the unreachable-path points the real docker-py client
at a closed localhost port (ECONNREFUSED, immediate) with a tight client timeout.
"""

from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.app_server.config.dtos import SandboxConfigDTO
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path) -> TestClient:
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
    return TestClient(create_app(SqliteEventStore(":memory:"), state))


def _dto(**over) -> dict:
    base = {
        "backend": "process",
        "docker_socket": "unix:///var/run/docker.sock",
        "podman_url": "http+ssh://sandbox@host/run/podman.sock",
        "runtime": "runsc",
        "image": "disco-sandbox:base",
        "workspace_root": "/opt/sandbox/workspaces",
    }
    base.update(over)
    return SandboxConfigDTO(**base).model_dump()


def test_sandbox_test_route_ok_for_process_backend(client):
    """The process (dev) backend is always reachable (writable workspace root) →
    a real ok=True verdict, HTTP 200."""
    r = client.post("/api/sandbox/test", json=_dto(backend="process"))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "ok"
    assert "process sandbox" in body["detail"]


def test_sandbox_test_route_typed_named_error_when_unreachable(client):
    """An unreachable gVisor endpoint → HTTP 200 with ok=False, status 'unreachable',
    and a detail that NAMES the endpoint (not a silent failure or a 500)."""
    r = client.post(
        "/api/sandbox/test",
        json=_dto(backend="gvisor", docker_socket="tcp://127.0.0.1:1"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] in ("unreachable", "error")
    assert "tcp://127.0.0.1:1" in body["detail"]  # the endpoint is NAMED

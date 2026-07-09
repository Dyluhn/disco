"""The AGENT-server sandbox reachability routes (GET /api/sandbox/health,
POST /api/sandbox/test).

These moved OFF the app-server on purpose (found live 2026-07-09): in a
split-container deploy only the agent-server owns the container socket, so the
app-server's co-located probe reported a FALSE "local sandbox unreachable"
banner while builds ran fine. The probe now runs HERE, against the SAME service
the run path builds. These tests are the regression guard for that contract:

  * the routes are served by the agent-server app,
  * they run a REAL healthcheck (process backend → reachable),
  * an unreachable endpoint is a RESULT at HTTP 200 (never a 500),
  * they degrade gracefully (not 500) when no runtime is wired.

The TestClient host is "testclient", which the agent auth middleware treats as
an admin session — so these admin-gated routes are reachable without a cookie
(same as the sibling TTS/image/MCP probe tests).
"""

from __future__ import annotations

import pytest
from disco.agent_server import create_app
from disco.agent_server.runtime import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import SandboxSettings
from disco.core.llm.config_store import ConfigStore
from fastapi.testclient import TestClient


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """Point the shared ConfigStore + SecretStore at temp files (mirrors the
    sibling probe-route tests)."""
    p = tmp_path / "config.json"
    monkeypatch.setenv("DISCO_CONFIG", str(p))
    monkeypatch.setenv("DISCO_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("DISCO_SECRET_KEY", "test-app-secret")
    return p


def _runtime_client(cfg_path) -> TestClient:
    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(store)
    return TestClient(create_app(rt._store, runtime=rt))


# ---- POST /api/sandbox/test (Settings "Test connection" preflight) -----------


def test_test_route_process_backend_is_reachable(cfg_path):
    """The process (dev) backend healthchecks its workspace root → ok=True. This
    proves the route runs a REAL probe (not a canned true), served by the
    agent-server."""
    client = _runtime_client(cfg_path)
    body = client.post(
        "/api/sandbox/test",
        json={"backend": "process", "workspace_root": str(cfg_path.parent / "ws")},
    ).json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["provider"] == "process"


def test_test_route_unreachable_gvisor_is_a_result_not_a_500(cfg_path):
    """An unreachable gVisor host → HTTP 200 with ok=False + a host-naming detail,
    never a 500. This is the whole reason the probe returns a RESULT."""
    client = _runtime_client(cfg_path)
    r = client.post(
        "/api/sandbox/test",
        json={"backend": "gvisor", "docker_socket": "ssh://sandbox@203.0.113.253:22"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["status"] in ("unreachable", "error")
    assert body["provider"] == "gvisor"
    # the detail NAMES what was probed, so the UI can point at the dead host
    assert "gvisor sandbox host" in body["detail"]


def test_test_route_without_runtime_degrades_not_500(cfg_path):
    """Wire the app with NO runtime → the route must still answer 200 (degraded),
    never crash the Settings panel."""
    client = TestClient(create_app(SqliteEventStore(":memory:")))
    r = client.post("/api/sandbox/test", json={"backend": "process"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["provider"] == "process"


# ---- GET /api/sandbox/health (the app-shell banner signal) -------------------


def test_health_route_reflects_active_backend(cfg_path, monkeypatch):
    """The banner signal probes the ACTIVE (persisted) backend. Persist a process
    backend (re-permitted for dev) → reachable=True with backend=process, proving
    the health route reads the SAME config the run path builds from."""
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "1")
    ConfigStore(cfg_path).save_sandbox(
        SandboxSettings(backend="process", workspace_root=str(cfg_path.parent / "ws"))
    )
    client = _runtime_client(cfg_path)
    body = client.get("/api/sandbox/health").json()
    assert body["reachable"] is True
    assert body["backend"] == "process"


def test_health_route_without_runtime_degrades_not_500(cfg_path):
    """No runtime wired → 200 reachable=False detail 'runtime unavailable'. The
    banner must never 500 the app shell on a headless/partial boot."""
    client = TestClient(create_app(SqliteEventStore(":memory:")))
    r = client.get("/api/sandbox/health")
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is False
    assert "runtime unavailable" in body["detail"]

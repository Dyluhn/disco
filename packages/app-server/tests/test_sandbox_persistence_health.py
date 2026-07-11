"""Sandbox per-backend connection PERSISTENCE + reachability HEALTH (2026-06-23 outage).

Root cause of the outage: the sandbox connection fields were SHARED across backends, so
toggling gVisor→local→gVisor blanked the gVisor `docker_socket` to a host-less
`ssh://sandbox@` and every run died with "ssh: Could not resolve hostname :".

These tests pin the fix:
  - switching the active backend PRESERVES every backend's connection (no field loss);
  - persisting a gvisor backend with an empty/host-less docker_socket is REJECTED (400);
  - the health probe reports reachable=False (with a host-naming detail) for an
    unreachable host and reachable=True for the local/process backend.
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
def store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def state(tmp_path, store) -> ConfigState:
    return ConfigState(
        store=store,
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )


@pytest.fixture
def client(state) -> TestClient:
    return TestClient(create_app(SqliteEventStore(":memory:"), state))


def _obj(**over) -> SandboxConfigDTO:
    base = {
        "backend": "process",
        "docker_socket": "unix:///var/run/docker.sock",
        "podman_url": "http+ssh://sandbox@203.0.113.47/run/podman.sock",
        "runtime": "runc",
        "image": "disco-sandbox:base",
        "workspace_root": "/opt/sandbox/workspaces",
    }
    base.update(over)
    return SandboxConfigDTO(**base)


def _dto(**over) -> dict:
    return _obj(**over).model_dump()


# ---- A. per-backend persistence: a switch never clears the others -------------


def test_switch_backend_preserves_other_backends_connection(state):
    """gvisor → local → gvisor must KEEP the gVisor docker_socket verbatim (the outage
    was that the round-trip blanked it). The store retains a per-backend block."""
    gvisor_sock = "ssh://sandbox@100.81.82.115"
    # 1. configure + save gVisor with a real remote host
    state.update_sandbox_config(_obj(backend="gvisor", docker_socket=gvisor_sock, runtime="runsc"))
    # 2. switch to local (its own docker_socket)
    state.update_sandbox_config(
        _obj(
            backend="local",
            docker_socket="unix:///var/run/docker.sock",
            runtime="runc",
        )
    )
    # 3. the persisted config remembers gVisor's connection block verbatim
    cfg = state.sandbox_config()
    assert cfg.backend == "local"
    assert "gvisor" in cfg.connections, "gVisor's saved block must survive the switch"
    assert cfg.connections["gvisor"].docker_socket == gvisor_sock
    assert cfg.connections["gvisor"].runtime == "runsc"


def test_active_flat_fields_track_the_active_backend(state):
    """The flat fields (what the live backend builder reads) reflect the ACTIVE backend,
    while inactive backends keep their own block — no cross-contamination."""
    state.update_sandbox_config(
        _obj(backend="gvisor", docker_socket="ssh://sandbox@host-a", runtime="runsc")
    )
    state.update_sandbox_config(
        _obj(
            backend="local",
            docker_socket="unix:///var/run/docker.sock",
            runtime="runc",
        )
    )
    cfg = state.sandbox_config()
    assert cfg.docker_socket == "unix:///var/run/docker.sock"  # active = local
    assert cfg.runtime == "runc"
    assert cfg.connections["gvisor"].docker_socket == "ssh://sandbox@host-a"  # inactive preserved


def test_no_field_loss_round_trip_via_http(client):
    """Full HTTP round-trip: PUT gvisor, PUT local, GET → gVisor's block is intact."""
    r1 = client.put(
        "/api/sandbox/config",
        json=_dto(backend="gvisor", docker_socket="ssh://sandbox@1.2.3.4", runtime="runsc"),
    )
    assert r1.status_code == 200
    r2 = client.put("/api/sandbox/config", json=_dto(backend="local"))
    assert r2.status_code == 200
    got = client.get("/api/sandbox/config").json()
    assert got["backend"] == "local"
    assert got["connections"]["gvisor"]["docker_socket"] == "ssh://sandbox@1.2.3.4"


def test_old_flat_config_migrates_active_only_clean_defaults_for_others(state, store):
    """P1 #2 migration: an OLD flat config (active gvisor + bad host-less socket, NO
    `connections` map) maps the flat block to connections[ACTIVE] ONLY — every OTHER
    backend gets a CLEAN backend-appropriate default, NOT the gvisor ssh socket. So
    switching to Local never inherits the bad socket."""
    from disco.core.llm import SandboxSettings

    # Simulate a legacy persisted config: flat fields set, `connections` empty.
    legacy = SandboxSettings(backend="gvisor", docker_socket="ssh://sandbox@", runtime="runsc")
    store.save(store.load().model_copy(update={"sandbox": legacy}))

    cfg = state.sandbox_config()
    # active backend reflects the (bad) flat block — shown so the user can fix it
    assert cfg.connections["gvisor"].docker_socket == "ssh://sandbox@"
    # Local did NOT inherit the gvisor ssh socket — it got a clean local docker socket
    assert "ssh://" not in cfg.connections["local"].docker_socket
    assert cfg.connections["local"].docker_socket == "unix:///var/run/docker.sock"
    assert cfg.connections["local"].runtime == "runc"
    # process likewise has no ssh socket bled in
    assert "ssh://" not in cfg.connections["process"].docker_socket


# ---- B. validation: the unrunnable state is rejected, not silently saved ------


@pytest.mark.parametrize(
    "bad_socket",
    [
        "ssh://sandbox@",
        "",
        "   ",
        "ssh://sandbox@ ",
        "ssh://sandbox@:22",  # P1 #1: host-less WITH a port
        "http+ssh://sandbox@:2222/run/docker.sock",
        "ssh://sandbox@/run/docker.sock",  # host-less with a path
    ],
)
def test_reject_gvisor_with_hostless_or_empty_socket(client, bad_socket):
    """The exact outage config — a gvisor backend with a host-less/empty docker_socket
    (INCLUDING host-less-with-a-port, the P1 miss) — is REJECTED 400 with a typed reason,
    never persisted."""
    r = client.put(
        "/api/sandbox/config",
        json=_dto(backend="gvisor", docker_socket=bad_socket, runtime="runsc"),
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["reason"] == "unrunnable_sandbox"


@pytest.mark.parametrize(
    "good_socket",
    [
        "ssh://sandbox@host",
        "ssh://sandbox@host:22",
        "ssh://sandbox@1.2.3.4:22/run/docker.sock",
    ],
)
def test_accept_gvisor_with_valid_host_and_port(client, good_socket):
    """A real host (with or without a port/path) is accepted."""
    r = client.put(
        "/api/sandbox/config",
        json=_dto(backend="gvisor", docker_socket=good_socket, runtime="runsc"),
    )
    assert r.status_code == 200, r.text


def test_reject_keeps_last_good_block(state):
    """A rejected gvisor save must NOT clobber the previously-saved good gVisor block."""
    state.update_sandbox_config(
        _obj(backend="gvisor", docker_socket="ssh://sandbox@good-host", runtime="runsc")
    )
    from disco.app_server.config_state import ConfigValidationError

    with pytest.raises(ConfigValidationError):
        state.update_sandbox_config(
            _obj(backend="gvisor", docker_socket="ssh://sandbox@", runtime="runsc")
        )
    cfg = state.sandbox_config()
    assert cfg.connections["gvisor"].docker_socket == "ssh://sandbox@good-host"


def test_reject_podman_with_empty_url(client):
    r = client.put("/api/sandbox/config", json=_dto(backend="podman", podman_url=""))
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "unrunnable_sandbox"


def test_process_and_local_with_socket_are_runnable(client):
    """Process needs nothing; local with the default unix socket is fine — both save."""
    assert client.put("/api/sandbox/config", json=_dto(backend="process")).status_code == 200
    assert client.put("/api/sandbox/config", json=_dto(backend="local")).status_code == 200


# ---- C. reachability health probe --------------------------------------------


def test_health_reachable_for_process_backend(client):
    """The process (dev) backend is always reachable (writable workspace root)."""
    client.put("/api/sandbox/config", json=_dto(backend="process"))
    r = client.get("/api/sandbox/health")
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["backend"] == "process"


def test_health_unreachable_names_the_host(state):
    """An unreachable gVisor endpoint → reachable=False with a host-naming detail (the
    same verdict the run path produces), so the banner can show the real reason."""
    import asyncio

    # save a reachable config first, then point the ACTIVE backend at a dead endpoint
    state.update_sandbox_config(
        _obj(backend="gvisor", docker_socket="tcp://127.0.0.1:1", runtime="runsc")
    )
    health = asyncio.run(state.sandbox_health())
    assert health.reachable is False
    assert health.backend == "gvisor"
    assert "tcp://127.0.0.1:1" in health.detail  # the endpoint is NAMED

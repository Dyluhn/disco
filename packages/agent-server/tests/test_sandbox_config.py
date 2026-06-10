"""Settings-driven sandbox backend selection — the round-trip the UI relies on.

Proves: SandboxSettings maps to the right backend; the agent-server reads the backend
from the SAME shared ConfigStore the Settings UI writes (so a selection genuinely changes
the active backend); a change is picked up per-request; and an explicit injection overrides.
"""

from __future__ import annotations

from perpleximanus.agent_server.runtime import ConversationRuntime, build_sandbox_service
from perpleximanus.core import SqliteEventStore
from perpleximanus.core.llm import ConfigStore, SandboxSettings
from perpleximanus.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    ProcessSandboxService,
)


def test_build_sandbox_service_maps_each_backend():
    def built(backend: str):
        return build_sandbox_service(SandboxSettings(backend=backend))

    assert isinstance(built("local"), LocalSandboxService)
    assert isinstance(built("gvisor"), GvisorSandboxService)
    assert isinstance(built("podman"), PodmanSandboxService)
    assert isinstance(built("process"), ProcessSandboxService)
    # connection detail flows into the built backend's config
    svc = build_sandbox_service(SandboxSettings(backend="local", image="custom:tag"))
    assert svc._cfg.image == "custom:tag"


def test_runtime_reads_backend_from_the_shared_store(tmp_path):
    # the app-server writes the selection; the agent-server reads the SAME store.
    store = ConfigStore(tmp_path / "config.json")
    store.save_sandbox(SandboxSettings(backend="gvisor", image="x:y"))
    rt = ConversationRuntime(SqliteEventStore(":memory:"), config_store=store)
    assert isinstance(rt._sandbox_service_now(), GvisorSandboxService)

    # flipping the selection is picked up on the next request (no restart).
    store.save_sandbox(SandboxSettings(backend="local"))
    assert isinstance(rt._sandbox_service_now(), LocalSandboxService)


def test_injected_sandbox_overrides_the_persisted_config(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save_sandbox(SandboxSettings(backend="gvisor"))
    rt = ConversationRuntime(
        SqliteEventStore(":memory:"), config_store=store, sandbox_service=ProcessSandboxService()
    )
    assert isinstance(rt._sandbox_service_now(), ProcessSandboxService)  # override wins


import pytest

class _FakeSession:
    def __init__(self, backend: str, url: str | None) -> None:
        self._service = type("S", (), {"name": backend})()
        self._url = url
        # The preview probe is PASSIVE: it reads _instance (the live box) and the
        # session manager's namespace — it must never create a sandbox itself.
        self._instance = self
        self.sessions = type("M", (), {"namespace": ""})()

    async def _ensure(self):
        return self

    def expose_port(self, port: int) -> str | None:
        return self._url


class _FakeExecutor:
    def __init__(self, session) -> None:
        self._sandbox = session


@pytest.mark.asyncio
async def test_preview_is_backend_aware_and_honest():
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    # no session yet → a clean reason, never a URL
    p = await rt.preview("c")
    assert p["available"] is False

    # session exists but no live instance → PASSIVE: a polled GET must not create
    # a sandbox as a side effect; it just says the box isn't running.
    idle = _FakeSession("local", None)
    idle._instance = None
    rt._executors["idle"] = _FakeExecutor(idle)
    p = await rt.preview("idle")
    assert p["available"] is False and "isn't running" in p["reason"]

    # Podman → the honest labeled stub (matches the UI), no fake URL
    rt._executors["pod"] = _FakeExecutor(_FakeSession("podman", "http://nope"))
    pod = await rt.preview("pod")
    assert pod["available"] is False and pod["stub"] is True

    # local with a reachable dev server → available (proxied through this origin); the
    # raw upstream is kept server-side (the browser hits the agent-server proxy).
    rt._executors["loc"] = _FakeExecutor(_FakeSession("local", "http://localhost:32768"))
    # Mock port_owner for this test
    import perpleximanus.agent_server.runtime as runtime_mod
    from perpleximanus.tools.sandbox.port_owner import PortOwner
    
    async def mock_port_owner(inst, port):
        return PortOwner(port=port, pid=1234, cmdline="test", session="preview")
        
    import unittest.mock
    with unittest.mock.patch("perpleximanus.agent_server.runtime.port_owner", side_effect=mock_port_owner):
        loc = await rt.preview("loc")
        assert loc["available"] is True and loc.get("proxy") is True and "url" not in loc
        assert rt.preview_upstream("loc") == "http://localhost:32768"

    # local with no dev server up → a reason, not a fake URL
    rt._executors["bare"] = _FakeExecutor(_FakeSession("local", None))
    with unittest.mock.patch("perpleximanus.agent_server.runtime.port_owner", return_value=None):
        bare = await rt.preview("bare")
        assert bare["available"] is False
        assert rt.preview_upstream("bare") is None


@pytest.mark.asyncio
async def test_ensure_preview_rematerializes_after_clean_finish_teardown():
    """BP-02 §E7 + the G safe-leak fix: a clean FINISH tears the sandbox down, but
    'Restart preview' must NOT become a dead affordance — with a snapshot on disk it
    re-materializes through the resume path (loop_for → rehydrate → ensure_preview)."""
    from perpleximanus.tools.projects.store import StorageStatus

    rt = ConversationRuntime(SqliteEventStore(":memory:"))

    session = _FakeSession("local", None)
    session.preview_started = False

    async def _fake_ensure_preview(port: int = 8000) -> bool:
        session.preview_started = True
        return True

    session.ensure_preview = _fake_ensure_preview

    calls: list[str] = []

    class _Record:
        files_missing = False

    class _Store:
        def status(self):
            return StorageStatus.OK

        def get(self, cid):
            return _Record() if cid == "fin" else None

    rt._project_store_now = lambda: _Store()

    def _fake_loop_for(cid):
        calls.append("loop_for")
        rt._executors[cid] = _FakeExecutor(session)

    rt._loop_for = _fake_loop_for

    async def _fake_rehydrate(cid):
        calls.append("rehydrate")

    rt._maybe_rehydrate = _fake_rehydrate

    # no executor + no snapshot record → False, and NO re-materialization
    assert await rt.ensure_preview("missing") is False
    assert calls == []

    # no executor + snapshot present → re-compose, rehydrate, start preview
    assert await rt.ensure_preview("fin") is True
    assert calls == ["loop_for", "rehydrate"]
    assert session.preview_started is True

    # executor already live → straight delegation, no second re-materialization
    session.preview_started = False
    assert await rt.ensure_preview("fin") is True
    assert calls == ["loop_for", "rehydrate"]
    assert session.preview_started is True


def test_sandbox_settings_round_trip_on_disk(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save_sandbox(
        SandboxSettings(backend="gvisor", docker_socket="ssh://sandbox@host", runtime="runsc")
    )
    reloaded = ConfigStore(tmp_path / "config.json").load()
    assert reloaded.sandbox.backend == "gvisor"
    assert reloaded.sandbox.docker_socket == "ssh://sandbox@host"
    assert reloaded.sandbox.runtime == "runsc"

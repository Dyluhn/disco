"""Settings-driven sandbox backend selection — the round-trip the UI relies on.

Proves: SandboxSettings maps to the right backend; the agent-server reads the backend
from the SAME shared ConfigStore the Settings UI writes (so a selection genuinely changes
the active backend); a change is picked up per-request; and an explicit injection overrides.
"""

from __future__ import annotations

import pytest
from disco.agent_server.runtime import ConversationRuntime, build_sandbox_service
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, DefaultLLMRouter, SandboxSettings
from disco.core.loop import RouterAgent
from disco.tools.sandbox import (
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
    # Mock port_owners for this test
    from disco.tools.sandbox.port_owner import PortOwner

    async def mock_port_owners(inst, ports):
        return {
            p: PortOwner(port=p, pid=1234, cmdline="test", session="pmx-preview")
            if p == 8000 else None
            for p in ports
        }

    import unittest.mock

    with unittest.mock.patch(
        "disco.agent_server.preview_service.port_owners", side_effect=mock_port_owners
    ):
        loc = await rt.preview("loc")
        assert loc["available"] is True and loc.get("proxy") is True and "url" not in loc
        assert rt.preview_upstream("loc") == "http://localhost:32768"

    # local with no dev server up → a reason, not a fake URL
    rt._executors["bare"] = _FakeExecutor(_FakeSession("local", None))
    with unittest.mock.patch("disco.agent_server.preview_service.port_owners", return_value={}):
        bare = await rt.preview("bare")
        assert bare["available"] is False
        assert rt.preview_upstream("bare") is None


@pytest.mark.asyncio
async def test_multi_port_upstream_resolution():
    # BP-10: port_upstream resolves curated USER_PORTS
    rt = ConversationRuntime(SqliteEventStore(":memory:"))

    class _MultiPortSession(_FakeSession):
        def expose_port(self, port: int) -> str | None:
            if port == 8000:
                return "http://h:8000"
            if port == 3000:
                return "http://h:3000"
            return None

    rt._executors["c"] = _FakeExecutor(_MultiPortSession("local", "http://h:8000"))
    assert rt.port_upstream("c", 8000) == "http://h:8000"
    assert rt.port_upstream("c", 3000) == "http://h:3000"
    assert rt.port_upstream("c", 9999) is None  # expose_port (backend) defends this


@pytest.mark.asyncio
async def test_port_proxy_route_auth_and_defense():
    # BP-10: app-route proxy defends USER_PORTS set
    import unittest.mock

    from disco.agent_server import create_app
    from disco.tools.sandbox._container import USER_PORTS
    from fastapi.testclient import TestClient

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    app = create_app(rt._store, runtime=rt)
    client = TestClient(app)

    # 404 for ports NOT in USER_PORTS (defense stays in the app layer)
    assert 9999 not in USER_PORTS
    assert client.get("/conversations/x/port/9999/").status_code == 404
    # 404 for INTERNAL_PORTS (8899)
    assert 8899 not in USER_PORTS
    assert client.get("/conversations/x/port/8899/").status_code == 404

    # 503 if the upstream isn't available (box down / no executor)
    assert client.get("/conversations/x/port/3000/").status_code == 503

    # 200 (proxied) if available. WALK-10: the route now wakes a suspended sandbox via
    # runtime.wake_for_preview (async) instead of the passive port_upstream, so stub that.
    with unittest.mock.patch.object(
        rt, "wake_for_preview", new=unittest.mock.AsyncMock(return_value="http://localhost:32769")
    ):
        # We need a real-ish response from the stubbed upstream or httpx will fail.
        # Use a mock for httpx.AsyncClient.get
        with unittest.mock.patch("httpx.AsyncClient.get") as mock_get:
            mock_get.return_value = unittest.mock.MagicMock(
                status_code=200, content=b"hello", headers={"content-type": "text/plain"}
            )
            resp = client.get("/conversations/x/port/3000/api/data")
            assert resp.status_code == 200
            assert resp.text == "hello"
            mock_get.assert_called_with("http://localhost:32769/api/data")


@pytest.mark.asyncio
async def test_ensure_preview_rematerializes_after_clean_finish_teardown():
    """BP-02 §E7 + the G safe-leak fix: a clean FINISH tears the sandbox down, but
    'Restart preview' must NOT become a dead affordance — with a snapshot on disk it
    re-materializes through the resume path (loop_for → rehydrate → ensure_preview)."""
    from disco.tools.projects.store import StorageStatus

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


@pytest.mark.asyncio
async def test_compose_build_loop_egress_modes():
    # BP-09: env-flag dispatch for open/filtered Build sandboxes.
    # BP-G10: the DEFAULT flipped from "open" to "filtered" (the E8 allowlisting
    # proxy is now wired on every backend — gVisor, podman, local — so the safe
    # default IS the allowlist). Open is now the EXPLICIT escape hatch, not the
    # default, so the "open" case here sets PMX_BUILD_EGRESS=open explicitly.
    import os
    from unittest import mock

    from disco.tools import REGISTRY_EGRESS_ALLOW, Capability

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)

    # 1. Default (filtered — BP-G10 default)
    with mock.patch.dict(os.environ, {}, clear=False):
        with mock.patch.object(rt, "_sandbox_service_now"):
            loop = rt._compose_build_loop("c1", router, agent)
            spec = loop.executor._sandbox.spec
            assert Capability.NETWORK not in spec.permitted
            assert spec.egress_allow == REGISTRY_EGRESS_ALLOW

    # 2. Explicit open (escape hatch via PMX_BUILD_EGRESS=open)
    with mock.patch.dict(os.environ, {"PMX_BUILD_EGRESS": "open"}):
        with mock.patch.object(rt, "_sandbox_service_now"):
            loop = rt._compose_build_loop("c2", router, agent)
            spec = loop.executor._sandbox.spec
            assert Capability.NETWORK in spec.permitted
            assert not spec.egress_allow

    # 3. Filtered is unchanged when set explicitly (regression guard for BP-09).
    with mock.patch.dict(os.environ, {"PMX_BUILD_EGRESS": "filtered"}):
        with mock.patch.object(rt, "_sandbox_service_now"):
            loop = rt._compose_build_loop("c3", router, agent)
            spec = loop.executor._sandbox.spec
            assert Capability.NETWORK not in spec.permitted
            assert spec.egress_allow == REGISTRY_EGRESS_ALLOW


def test_registry_egress_allow_semantics():
    # BP-09: REGISTRY_EGRESS_ALLOW matches exact and .suffix
    from disco.tools import REGISTRY_EGRESS_ALLOW, SandboxSpec

    spec = SandboxSpec(egress_allow=REGISTRY_EGRESS_ALLOW)
    assert spec.egress_allowed("registry.npmjs.org") is True
    assert spec.egress_allowed("pypi.org") is True
    assert spec.egress_allowed("anything.npmjs.org") is True
    assert spec.egress_allowed("example.com") is False
    assert spec.egress_allowed("raw.githubusercontent.com") is True

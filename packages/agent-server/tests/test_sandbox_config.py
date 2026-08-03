"""Settings-driven sandbox backend selection — the round-trip the UI relies on.

Proves: SandboxSettings maps to the right backend; the agent-server reads the backend
from the SAME shared ConfigStore the Settings UI writes (so a selection genuinely changes
the active backend); a change is picked up per-request; and an explicit injection overrides.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from disco.agent_server.preview_manager import PreviewSession, PreviewStatus
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


@pytest.fixture(autouse=True)
def _close_owned_event_stores(monkeypatch):
    """Close every externally owned event store created by this test module."""
    store_type = SqliteEventStore
    owned_stores = []

    def create_store(*args, **kwargs):
        store = store_type(*args, **kwargs)
        owned_stores.append(store)
        return store

    monkeypatch.setitem(globals(), "SqliteEventStore", create_store)
    yield
    for store in reversed(owned_stores):
        store.close()


def test_build_sandbox_service_maps_each_backend(monkeypatch):
    monkeypatch.delenv("DISCO_LOCAL_ENGINE", raising=False)

    def built(backend: str):
        return build_sandbox_service(SandboxSettings(backend=backend))

    assert isinstance(built("local"), LocalSandboxService)
    assert isinstance(built("gvisor"), GvisorSandboxService)
    assert isinstance(built("podman"), PodmanSandboxService)
    # EPIC H (P0): the process backend is now FAIL-CLOSED for Build/soak — building it
    # requires the explicit dev opt-out (else ProductionValidityError; see the gate tests).
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "1")
    assert isinstance(built("process"), ProcessSandboxService)
    # connection detail flows into the built backend's config
    svc = build_sandbox_service(SandboxSettings(backend="local", image="custom:tag"))
    assert svc._cfg.image == "custom:tag"


def test_compose_local_podman_identity_selects_native_transport(monkeypatch):
    settings = SandboxSettings(
        backend="local",
        docker_socket="unix:///var/run/docker.sock",
        podman_url="unix:///host-only/podman.sock",
    )

    monkeypatch.setenv("DISCO_LOCAL_ENGINE", "podman")
    service = build_sandbox_service(settings)
    assert isinstance(service, PodmanSandboxService)
    assert service._cfg.podman_url == "unix:///var/run/docker.sock"

    monkeypatch.setenv("DISCO_LOCAL_ENGINE", "docker")
    assert isinstance(build_sandbox_service(settings), LocalSandboxService)

    monkeypatch.setenv("DISCO_LOCAL_ENGINE", "unknown")
    with pytest.raises(ValueError, match="expected docker or podman"):
        build_sandbox_service(settings)


def test_production_gate_refuses_process_backend_by_default(monkeypatch):
    # EPIC H (P0) FAIL-CLOSED: the Build/soak builder refuses the unisolated process (dev)
    # backend BY DEFAULT — it shares the host PID + net namespace (the `kill <pid>` source).
    # No env needed; protection is the default now (the inversion of the old fail-open opt-in).
    from disco.tools.sandbox import ProductionValidityError

    monkeypatch.delenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", raising=False)
    monkeypatch.delenv("DISCO_REQUIRE_PRODUCTION_SANDBOX", raising=False)
    with pytest.raises(ProductionValidityError, match="dev-only"):
        build_sandbox_service(SandboxSettings(backend="process"))
    # a container backend always passes the gate
    assert isinstance(
        build_sandbox_service(SandboxSettings(backend="gvisor")), GvisorSandboxService
    )


def test_dev_opt_out_re_enables_process_backend(monkeypatch):
    # The ONLY way to run the process backend on the Build/soak path is the explicit
    # dev-only opt-out. This keeps plain local dev working while staying fail-closed.
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "1")
    assert isinstance(
        build_sandbox_service(SandboxSettings(backend="process")), ProcessSandboxService
    )
    # …and the old fail-OPEN var no longer has any effect (it was replaced).
    monkeypatch.delenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", raising=False)
    monkeypatch.setenv("DISCO_REQUIRE_PRODUCTION_SANDBOX", "1")
    from disco.tools.sandbox import ProductionValidityError

    with pytest.raises(ProductionValidityError):
        build_sandbox_service(SandboxSettings(backend="process"))


def test_runtime_reads_backend_from_the_shared_store(tmp_path):
    # the app-server writes the selection; the agent-server reads the SAME store.
    store = ConfigStore(tmp_path / "config.json")
    store.sections.save_sandbox(SandboxSettings(backend="gvisor", image="x:y"))
    rt = ConversationRuntime(SqliteEventStore(":memory:"), config_store=store)
    assert isinstance(rt._sandbox_service_now(), GvisorSandboxService)

    # flipping the selection is picked up on the next request (no restart).
    store.sections.save_sandbox(SandboxSettings(backend="local"))
    assert isinstance(rt._sandbox_service_now(), LocalSandboxService)


def test_injected_sandbox_overrides_the_persisted_config(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.sections.save_sandbox(SandboxSettings(backend="gvisor"))
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

    @property
    def sandbox(self):
        return self._sandbox


@pytest.mark.asyncio
async def test_preview_is_backend_aware_and_honest():
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    # no session yet → a clean reason, never a URL
    p = await rt.preview.preview("c")
    assert p["available"] is False

    # session exists but no live instance → PASSIVE: a polled GET must not create
    # a sandbox as a side effect; it just says the box isn't running.
    idle = _FakeSession("local", None)
    idle._instance = None
    rt._run_resources.set_executor("idle", _FakeExecutor(idle))
    p = await rt.preview.preview("idle")
    assert p["available"] is False and "isn't running" in p["reason"]

    # Podman is NOT special-cased anymore: availability keys on whether a dev server
    # is actually routable (expose_port/port_owners), exactly like the local backend —
    # rootless-podman previews for real, it is not a labeled dead stub. With no
    # routable dev server here it degrades HONESTLY (no fake URL, no backend-name stub).
    rt._run_resources.set_executor("pod", _FakeExecutor(_FakeSession("podman", "http://nope")))
    pod = await rt.preview.preview("pod")
    assert pod["available"] is False
    assert "stub" not in pod

    # local with a reachable dev server → available (proxied through this origin); the
    # raw upstream is kept server-side (the browser hits the agent-server proxy).
    rt._run_resources.set_executor(
        "loc", _FakeExecutor(_FakeSession("local", "http://localhost:32768"))
    )
    # Mock port_owners for this test
    from disco.tools.sandbox.port_owner import PortOwner

    async def mock_port_owners(inst, ports):
        return {
            p: PortOwner(port=p, pid=1234, cmdline="test", session="pmx-preview")
            if p == 8000
            else None
            for p in ports
        }

    import unittest.mock

    with unittest.mock.patch(
        "disco.agent_server.preview_service.port_owners", side_effect=mock_port_owners
    ):
        loc = await rt.preview.preview("loc")
        assert loc["available"] is True and loc.get("proxy") is True and "url" not in loc
        assert rt.preview.preview_upstream("loc") == "http://localhost:32768"

    # local with no dev server up → a reason, not a fake URL
    rt._run_resources.set_executor("bare", _FakeExecutor(_FakeSession("local", None)))
    with unittest.mock.patch("disco.agent_server.preview_service.port_owners", return_value={}):
        bare = await rt.preview.preview("bare")
        assert bare["available"] is False
        assert rt.preview.preview_upstream("bare") is None

    # An owned Build session with no managed launch has no canonical port. It must
    # remain in the honest Preparing state instead of guessing the retired auto-preview.
    owned = _FakeSession("local", None)
    owned._auto_preview_disabled = True
    rt.set_surface("owned", "build")
    rt._run_resources.set_executor("owned", _FakeExecutor(owned))
    with unittest.mock.patch("disco.agent_server.preview_service.port_owners", return_value={}):
        preparing = await rt.preview.preview("owned")
    assert rt.preview.preview_target_port("owned") is None
    assert rt.preview.preview_upstream("owned") is None
    assert preparing["available"] is False
    assert preparing["reason"] == (
        "Preparing preview: waiting for the platform-managed runtime to start."
    )

    # The same build remains manager-only after teardown/suspension, when there is no
    # live session flag to consult. Surface authority still forbids a guessed :8000.
    rt._run_resources.pop_executor("owned")
    assert rt.preview.preview_target_port("owned") is None


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

    rt._run_resources.set_executor("c", _FakeExecutor(_MultiPortSession("local", "http://h:8000")))
    assert rt.preview.port_upstream("c", 8000) == "http://h:8000"
    assert rt.preview.port_upstream("c", 3000) == "http://h:3000"
    assert rt.preview.port_upstream("c", 9999) is None  # expose_port defends this


@pytest.mark.asyncio
async def test_canonical_preview_follows_sole_managed_platform_port():
    """H333: a valid stop/start move to 5173 must not strand canonical preview on 8000."""
    rt = ConversationRuntime(SqliteEventStore(":memory:"))

    class _ManagedSession(_FakeSession):
        def expose_port(self, port: int) -> str | None:
            return f"http://managed:{port}" if port == 5173 else None

    session = _ManagedSession("gvisor", None)
    managed_preview = PreviewSession(
        name="web",
        port=5173,
        command="npm run dev -- --port 5173",
        exec_dir="/workspace",
        intent={"framework": "vite", "launch_kind": "framework"},
        projection_id="pv_generation_1",
        status=PreviewStatus.RUNNING,
        update_error="dependency refresh failed",
    )
    session._preview_manager = SimpleNamespace(
        canonical_port=lambda: 5173,
        canonical_lifecycle_session=lambda: managed_preview,
    )
    rt._run_resources.set_executor("managed", _FakeExecutor(session))

    from disco.tools.sandbox.port_owner import PortOwner

    async def mock_port_owners(inst, ports):  # noqa: ANN001
        return {
            p: PortOwner(port=p, pid=1234, cmdline="python3 server.py", session="disco-live")
            if p == 5173
            else None
            for p in ports
        }

    import unittest.mock

    with unittest.mock.patch(
        "disco.agent_server.preview_service.port_owners", side_effect=mock_port_owners
    ):
        preview = await rt.preview.preview("managed")

    assert rt.preview.preview_target_port("managed") == 5173
    assert rt.preview.preview_upstream("managed") == "http://managed:5173"
    assert preview["available"] is True
    assert preview["port"] == 5173
    assert preview["status"] == "running"
    assert preview["generation"] == "pv_generation_1"
    assert preview["launch_kind"] == "framework"
    assert preview["reload_strategy"] == "hmr"
    assert preview["update_error"] == "dependency refresh failed"


@pytest.mark.asyncio
async def test_canonical_preview_accepts_exact_shared_host_managed_port_only():
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    selected_port = 10_123

    class _ManagedHostSession(_FakeSession):
        shares_host_network = True

        def expose_port(self, port: int) -> str | None:
            return f"http://managed:{port}" if port == selected_port else None

    session = _ManagedHostSession("process", None)
    managed_preview = PreviewSession(
        name="web",
        port=selected_port,
        command=f"python3 -m http.server {selected_port}",
        exec_dir="/workspace",
        intent={"framework": "static", "launch_kind": "static"},
        projection_id="pv_managed_host_generation",
        status=PreviewStatus.RUNNING,
    )
    session._preview_manager = SimpleNamespace(
        canonical_port=lambda: selected_port,
        canonical_lifecycle_session=lambda: managed_preview,
    )
    rt._run_resources.set_executor("managed-host", _FakeExecutor(session))

    from disco.tools.sandbox.port_owner import PortOwner

    async def mock_port_owners(inst, ports):  # noqa: ANN001
        assert selected_port in ports
        return {
            port: (
                PortOwner(
                    port=port,
                    pid=1234,
                    cmdline="python3 -m http.server",
                    session="disco-managed-host",
                )
                if port == selected_port
                else None
            )
            for port in ports
        }

    import unittest.mock

    with unittest.mock.patch(
        "disco.agent_server.preview_service.port_owners",
        side_effect=mock_port_owners,
    ):
        preview = await rt.preview.preview("managed-host")

    assert rt.preview.preview_target_port("managed-host") == selected_port
    assert rt.preview.preview_upstream("managed-host") == f"http://managed:{selected_port}"
    assert preview["available"] is True
    assert preview["port"] == selected_port
    assert any(entry["port"] == selected_port for entry in preview["ports"])
    assert rt.preview.port_upstream("managed-host", selected_port + 1) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "detail", "reason_fragment"),
    [
        (PreviewStatus.STARTING, "", "Preparing preview"),
        (PreviewStatus.RESTARTING, "restart #1", "restarting"),
        (PreviewStatus.CRASHED, "process exited", "process exited"),
    ],
)
async def test_managed_preview_reports_exact_unready_lifecycle(status, detail, reason_fragment):
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    session = _FakeSession("gvisor", None)
    managed_preview = PreviewSession(
        name="web",
        port=5173,
        command="npm run dev -- --port 5173",
        exec_dir="/workspace",
        intent={"framework": "vite", "launch_kind": "framework"},
        projection_id="pv_unready_generation",
        status=status,
        detail=detail,
    )
    session._preview_manager = SimpleNamespace(
        canonical_port=lambda: None,
        canonical_lifecycle_session=lambda: managed_preview,
    )
    rt._run_resources.set_executor("managed-unready", _FakeExecutor(session))

    import unittest.mock

    with unittest.mock.patch("disco.agent_server.preview_service.port_owners", return_value={}):
        preview = await rt.preview.preview("managed-unready")

    assert preview["available"] is False
    assert reason_fragment in preview["reason"]
    assert preview["port"] == 5173
    assert preview["status"] == status.value
    assert preview["generation"] == "pv_unready_generation"
    assert preview["launch_kind"] == "framework"
    assert preview["reload_strategy"] == "hmr"


@pytest.mark.asyncio
async def test_managed_unexposable_preview_never_claims_available():
    rt = ConversationRuntime(SqliteEventStore(":memory:"))

    class _ManagedSession(_FakeSession):
        def expose_port(self, port: int) -> str | None:
            return None

    session = _ManagedSession("podman", None)
    managed_preview = PreviewSession(
        name="web",
        port=5173,
        command="npm run dev -- --port 5173",
        exec_dir="/workspace",
        intent={"framework": "vite", "launch_kind": "framework"},
        projection_id="pv_unexposable_generation",
        status=PreviewStatus.UNAVAILABLE,
        detail="Runtime is healthy, but this backend cannot expose it.",
    )
    session._preview_manager = SimpleNamespace(
        canonical_port=lambda: 5173,
        canonical_lifecycle_session=lambda: managed_preview,
    )
    rt._run_resources.set_executor("managed-unexposable", _FakeExecutor(session))

    from disco.tools.sandbox.port_owner import PortOwner

    async def mock_port_owners(inst, ports):  # noqa: ANN001
        return {
            port: PortOwner(port=port, pid=1234, cmdline="npm run dev", session="disco-web")
            if port == 5173
            else None
            for port in ports
        }

    import unittest.mock

    with unittest.mock.patch(
        "disco.agent_server.preview_service.port_owners", side_effect=mock_port_owners
    ):
        preview = await rt.preview.preview("managed-unexposable")

    assert preview["available"] is False
    assert preview["status"] == "unavailable"
    assert "cannot expose" in preview["reason"]
    assert preview["port"] == 5173
    assert preview["owner"]["pid"] == 1234


def test_canonical_preview_refuses_manager_without_healthy_selection():
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    session = _FakeSession("gvisor", None)
    session._preview_manager = SimpleNamespace(
        canonical_port=lambda: None,
        canonical_lifecycle_session=lambda: None,
    )
    rt._run_resources.set_executor("unhealthy", _FakeExecutor(session))

    assert rt.preview.preview_target_port("unhealthy") is None
    assert rt.preview.preview_upstream("unhealthy") is None


@pytest.mark.asyncio
async def test_port_proxy_route_auth_and_defense():
    # BP-10: app-route proxy defends USER_PORTS set
    import unittest.mock

    from disco.agent_server import create_app
    from disco.tools.sandbox._container import USER_PORTS
    from fastapi.testclient import TestClient

    store = SqliteEventStore(":memory:")
    cid = "conv_x"
    store.create_conversation(cid, owner_id="local")
    rt = ConversationRuntime(store)
    app = create_app(rt._store, runtime=rt)
    client = TestClient(app)

    # 404 for ports NOT in USER_PORTS (defense stays in the app layer)
    assert 9999 not in USER_PORTS
    assert client.get(f"/conversations/{cid}/port/9999/").status_code == 404
    # The broad managed range is canonical Preview authority, not a generic
    # user-selected service surface.
    assert 10123 not in USER_PORTS
    assert client.get(f"/conversations/{cid}/port/10123/").status_code == 404
    # 404 for INTERNAL_PORTS (8899)
    assert 8899 not in USER_PORTS
    assert client.get(f"/conversations/{cid}/port/8899/").status_code == 404

    # 503 if the upstream isn't available (box down / no executor)
    assert client.get(f"/conversations/{cid}/port/3000/").status_code == 503

    # 200 (proxied) if available. WALK-10: the route now wakes a suspended sandbox via
    # runtime.preview.wake_for_preview (async) instead of the passive port_upstream, so stub that.
    with unittest.mock.patch.object(
        rt.preview,
        "wake_for_preview",
        new=unittest.mock.AsyncMock(return_value="http://localhost:32769"),
    ):
        # We need a real-ish response from the stubbed upstream or httpx will fail.
        # Use a mock for httpx.AsyncClient.get
        with unittest.mock.patch("httpx.AsyncClient.get") as mock_get:
            mock_get.return_value = unittest.mock.MagicMock(
                status_code=200, content=b"hello", headers={"content-type": "text/plain"}
            )
            resp = client.get(f"/conversations/{cid}/port/3000/api/data")
            assert resp.status_code == 200
            assert resp.text == "hello"
            mock_get.assert_called_with("http://localhost:32769/api/data")


@pytest.mark.asyncio
async def test_non_build_legacy_preview_can_rematerialize_after_teardown():
    """Non-Build compatibility may still rehydrate the legacy static viewer."""
    from disco.tools.projects.store import StorageStatus

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    rt.set_surface("fin", "research")

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

    rt._projects.current_project_store = lambda: _Store()

    def _fake_loop_for(cid):
        calls.append("loop_for")
        rt._run_resources.set_executor(cid, _FakeExecutor(session))

    rt._loop_factory.loop_for = _fake_loop_for

    async def _fake_rehydrate(cid):
        calls.append("rehydrate")

    rt._lifecycle._maybe_rehydrate = _fake_rehydrate

    # no executor + no snapshot record → False, and NO re-materialization
    assert await rt.preview.ensure_preview("missing") is False
    assert calls == []

    # no executor + snapshot present → re-compose, rehydrate, start preview
    assert await rt.preview.ensure_preview("fin") is True
    assert calls == ["loop_for", "rehydrate"]
    assert session.preview_started is True

    # executor already live → straight delegation, no second re-materialization
    session.preview_started = False
    assert await rt.preview.ensure_preview("fin") is True
    assert calls == ["loop_for", "rehydrate"]
    assert session.preview_started is True


@pytest.mark.asyncio
async def test_restart_preview_uses_managed_intent_and_never_legacy_static() -> None:
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    rt.set_surface("managed-restart", "build")
    session = _FakeSession("local", None)
    legacy_calls = 0

    async def _legacy() -> bool:
        nonlocal legacy_calls
        legacy_calls += 1
        return True

    session.ensure_preview = _legacy
    restarted = SimpleNamespace(status=PreviewStatus.RUNNING)
    from unittest.mock import AsyncMock

    manager = SimpleNamespace(restart_canonical=AsyncMock(return_value=restarted))
    session._preview_manager = manager
    rt._run_resources.set_executor("managed-restart", _FakeExecutor(session))

    assert await rt.preview.ensure_preview("managed-restart") is True
    manager.restart_canonical.assert_awaited_once_with()
    assert legacy_calls == 0


@pytest.mark.asyncio
async def test_build_restart_without_managed_intent_never_fabricates_static_preview() -> None:
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    rt.set_surface("no-intent", "build")
    session = _FakeSession("local", None)
    legacy_calls = 0

    async def _legacy() -> bool:
        nonlocal legacy_calls
        legacy_calls += 1
        return True

    session.ensure_preview = _legacy
    rt._run_resources.set_executor("no-intent", _FakeExecutor(session))

    assert await rt.preview.ensure_preview("no-intent") is False
    assert legacy_calls == 0


def test_sandbox_settings_round_trip_on_disk(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.sections.save_sandbox(
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

    # 2. Explicit open compatibility alias (public-only, private denied)
    with mock.patch.dict(os.environ, {"PMX_BUILD_EGRESS": "open"}):
        with mock.patch.object(rt, "_sandbox_service_now"):
            loop = rt._compose_build_loop("c2", router, agent)
            spec = loop.executor._sandbox.spec
            assert Capability.NETWORK not in spec.permitted
            assert spec.public_web is True
            assert not spec.egress_allow

    # 3. Filtered is unchanged when set explicitly (regression guard for BP-09).
    with mock.patch.dict(os.environ, {"PMX_BUILD_EGRESS": "filtered"}):
        with mock.patch.object(rt, "_sandbox_service_now"):
            loop = rt._compose_build_loop("c3", router, agent)
            spec = loop.executor._sandbox.spec
            assert Capability.NETWORK not in spec.permitted
            assert spec.egress_allow == REGISTRY_EGRESS_ALLOW


@pytest.mark.asyncio
async def test_agent_artifact_mode_keeps_finite_build_egress():
    from unittest import mock

    from disco.tools import REGISTRY_EGRESS_ALLOW

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    rt.set_surface("artifact-agent", "agent")
    rt.set_artifact_mode("artifact-agent", True)
    router = mock.MagicMock(spec=DefaultLLMRouter)
    agent = mock.MagicMock(spec=RouterAgent)

    with mock.patch.object(rt, "_sandbox_service_now"):
        loop = rt._compose_build_loop("artifact-agent", router, agent)
    spec = loop.executor._sandbox.spec
    assert spec.public_web is False
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


def test_surface_egress_defaults_and_unknown_values_fail_closed(monkeypatch):
    from disco.tools import REGISTRY_EGRESS_ALLOW, Capability, SandboxSpec

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    rt._sandbox._sandbox_spec = SandboxSpec(
        permitted=frozenset({Capability.NETWORK}),
        egress_allow=frozenset({"stale.example"}),
    )
    for name in ("PMX_AGENT_EGRESS", "DISCO_AGENT_EGRESS"):
        monkeypatch.delenv(name, raising=False)
    agent = rt._build_sandbox_spec(surface="agent")
    assert agent.public_web is True
    assert Capability.NETWORK not in agent.permitted
    assert not agent.egress_allow

    for name in ("PMX_BUILD_EGRESS", "DISCO_BUILD_EGRESS"):
        monkeypatch.delenv(name, raising=False)
    build = rt._build_sandbox_spec(surface="build")
    assert build.public_web is False
    assert Capability.NETWORK not in build.permitted
    assert build.egress_allow == REGISTRY_EGRESS_ALLOW

    monkeypatch.setenv("PMX_AGENT_EGRESS", "typo-means-open-before-sw5")
    with pytest.raises(ValueError, match="invalid AGENT_EGRESS"):
        rt._build_sandbox_spec(surface="agent")


def test_surface_egress_sealed_and_explicit_raw(monkeypatch):
    from disco.tools import Capability

    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    monkeypatch.setenv("PMX_AGENT_EGRESS", "sealed")
    sealed = rt._build_sandbox_spec(surface="agent")
    assert not sealed.public_web and not sealed.egress_allow
    assert Capability.NETWORK not in sealed.permitted

    monkeypatch.setenv("PMX_AGENT_EGRESS", "raw")
    raw = rt._build_sandbox_spec(surface="agent")
    assert Capability.NETWORK not in raw.permitted
    assert raw.public_web and not raw.egress_allow

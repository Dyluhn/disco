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


class _FakeSession:
    def __init__(self, backend: str, url: str | None) -> None:
        self._service = type("S", (), {"name": backend})()
        self._url = url

    def expose_port(self, port: int) -> str | None:
        return self._url


class _FakeExecutor:
    def __init__(self, session) -> None:
        self._sandbox = session


def test_preview_is_backend_aware_and_honest():
    rt = ConversationRuntime(SqliteEventStore(":memory:"))
    # no session yet → a clean reason, never a URL
    assert rt.preview("c")["available"] is False

    # Podman → the honest labeled stub (matches the UI), no fake URL
    rt._executors["pod"] = _FakeExecutor(_FakeSession("podman", "http://nope"))
    pod = rt.preview("pod")
    assert pod["available"] is False and pod["stub"] is True

    # local with a reachable dev server → the iframe URL
    rt._executors["loc"] = _FakeExecutor(_FakeSession("local", "http://localhost:32768"))
    loc = rt.preview("loc")
    assert loc["available"] is True and loc["url"] == "http://localhost:32768"

    # local with no dev server up → a reason, not a fake URL
    rt._executors["bare"] = _FakeExecutor(_FakeSession("local", None))
    assert rt.preview("bare")["available"] is False


def test_sandbox_settings_round_trip_on_disk(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save_sandbox(
        SandboxSettings(backend="gvisor", docker_socket="ssh://sandbox@host", runtime="runsc")
    )
    reloaded = ConfigStore(tmp_path / "config.json").load()
    assert reloaded.sandbox.backend == "gvisor"
    assert reloaded.sandbox.docker_socket == "ssh://sandbox@host"
    assert reloaded.sandbox.runtime == "runsc"

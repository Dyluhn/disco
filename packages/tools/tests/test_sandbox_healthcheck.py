"""W-48 — sandbox connectivity PREFLIGHT (`SandboxService.healthcheck`).

Each backend's healthcheck probes its real endpoint and either returns None
(reachable + usable) or raises a TYPED `SandboxUnavailableError` whose message
NAMES the endpoint + reason — never a silent failure or a generic 500 later.

Hermetic + BOUNDED: the ok / runtime-missing paths use the injected fake Docker
client; the "unreachable" path points the REAL docker-py / podman client at a
closed localhost port (ECONNREFUSED, immediate) with a tight client timeout, and
every probe is wrapped in `asyncio.wait_for` so a regression can't hang the suite.
"""

from __future__ import annotations

import asyncio

import pytest
from disco.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    ProcessSandboxService,
    SandboxConfig,
    SandboxUnavailableError,
    service_from_config,
)

# Reuse the proven fake Docker client (ping/info/run/images/networks) from test_gvisor.
from test_gvisor import FakeDockerClient  # noqa: E402 — flat-test-module import (suite convention)

# Every probe must resolve well under this — a sentinel that the healthcheck is bounded.
_BOUND_S = 8.0


async def test_healthcheck_ok_when_reachable_and_runtime_present():
    client = FakeDockerClient(runtimes=("runsc", "runc"))
    svc = GvisorSandboxService(SandboxConfig(runtime="runsc"), client=client)
    # No raise == reachable + runsc registered.
    assert await asyncio.wait_for(svc.healthcheck(), _BOUND_S) is None


async def test_healthcheck_typed_named_error_when_runtime_missing():
    """Reachable host but the configured runtime isn't registered → a typed error
    that NAMES the runtime (the actionable reason), not a generic failure."""
    client = FakeDockerClient(runtimes=("runc",))  # no runsc
    svc = GvisorSandboxService(SandboxConfig(runtime="runsc"), client=client)
    with pytest.raises(SandboxUnavailableError) as ei:
        await asyncio.wait_for(svc.healthcheck(), _BOUND_S)
    assert "runsc" in str(ei.value)


async def test_healthcheck_typed_named_error_when_unreachable_and_bounded():
    """An UNREACHABLE Docker endpoint → SandboxUnavailableError whose message NAMES
    the endpoint + says 'unreachable', returned FAST (bounded), not a hang/500.

    No injected client → the real docker-py client attempts the (closed) localhost
    port and fails with ECONNREFUSED; client_timeout_s caps the socket."""
    cfg = SandboxConfig(
        backend="gvisor", docker_socket="tcp://127.0.0.1:1", client_timeout_s=2
    )
    svc = GvisorSandboxService(cfg)
    with pytest.raises(SandboxUnavailableError) as ei:
        await asyncio.wait_for(svc.healthcheck(), _BOUND_S)
    msg = str(ei.value)
    assert "tcp://127.0.0.1:1" in msg  # the endpoint is NAMED
    assert "unreachable" in msg.lower()


async def test_local_backend_healthcheck_checks_runtime_present():
    """The local container tier reuses the Docker healthcheck — reachable socket +
    its `runc` runtime present == ok (W-48: 'local = is the runtime present')."""
    client = FakeDockerClient(runtimes=("runc",))
    svc = LocalSandboxService(SandboxConfig(backend="local", runtime="runc"), client=client)
    assert await asyncio.wait_for(svc.healthcheck(), _BOUND_S) is None


async def test_podman_healthcheck_ok_with_injected_client():
    """Podman's healthcheck pings the native-remote socket; an injected client
    (test seam) short-circuits the real SSH transport and proves the ok path."""
    class _FakePodman:
        def ping(self) -> bool:
            return True

    svc = PodmanSandboxService(SandboxConfig(backend="podman"), client=_FakePodman())
    assert await asyncio.wait_for(svc.healthcheck(), _BOUND_S) is None


async def test_process_backend_healthcheck_ok(tmp_path):
    """The process (dev) backend has no remote endpoint — its healthcheck confirms
    the workspace root is writable and returns None."""
    svc = ProcessSandboxService(root=str(tmp_path))
    assert await asyncio.wait_for(svc.healthcheck(), _BOUND_S) is None


def test_service_from_config_maps_each_backend():
    assert service_from_config(SandboxConfig(backend="gvisor")).name == "gvisor"
    assert service_from_config(SandboxConfig(backend="local")).name == "local"
    assert service_from_config(SandboxConfig(backend="podman")).name == "podman"
    # Unknown / "process" → the dev backend.
    assert service_from_config(SandboxConfig(backend="process")).name == "process"
    assert service_from_config(SandboxConfig(backend="???")).name == "process"

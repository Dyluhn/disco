"""Backend-aware preview exposure — hermetic (the additive expose_port + dispatch).

Proves: only the dev-server port is exposed; the published host port becomes a reachable
URL; the URL host follows the backend (localhost local, the remote tailnet IP for gVisor);
the local backend publishes the port only when network is granted; Podman is a code stub.
"""

from __future__ import annotations

from perpleximanus.tools.anatomy import Capability
from perpleximanus.tools.sandbox._container import PREVIEW_PORT, ContainerInstance
from perpleximanus.tools.sandbox.base import SandboxSpec
from perpleximanus.tools.sandbox.gvisor import _preview_host


class _Container:
    def __init__(self, host_port: str | None) -> None:
        self.status = "running"
        ports = {f"{PREVIEW_PORT}/tcp": [{"HostPort": host_port}]} if host_port else {}
        self.attrs = {"NetworkSettings": {"Ports": ports}}

    def reload(self) -> None:
        pass


def _inst(container, preview_host: str = "localhost") -> ContainerInstance:
    return ContainerInstance(
        id="x",
        owner_id="o",
        conversation_id="c",
        spec=SandboxSpec(),
        container=container,
        container_workspace="/workspace",
        stop_timeout_s=5,
        preview_host=preview_host,
    )


def test_expose_port_returns_the_published_url():
    assert _inst(_Container("32768")).expose_port(PREVIEW_PORT) == "http://localhost:32768"


def test_only_the_dev_server_port_is_exposed():
    # containment: any other port is NOT reachable
    assert _inst(_Container("32768")).expose_port(9999) is None


def test_no_url_when_the_port_is_not_published():
    # a sealed session publishes nothing → no preview (not a fake URL)
    assert _inst(_Container(None)).expose_port(PREVIEW_PORT) is None


def test_preview_url_host_follows_the_backend():
    # gVisor: the remote Docker host's tailnet IP serves the published port
    assert _inst(_Container("8080"), preview_host="100.81.82.115").expose_port(PREVIEW_PORT) == (
        "http://100.81.82.115:8080"
    )


def test_gvisor_preview_host_parsing():
    assert _preview_host("ssh://sandbox@100.81.82.115") == "100.81.82.115"
    assert _preview_host("ssh://sandbox@vm201.tailnet:22") == "vm201.tailnet"
    assert _preview_host("unix:///var/run/docker.sock") == "localhost"


def test_local_publishes_only_when_network_granted():
    from conftest import FakeSandboxInstance  # noqa: F401 — ensures conftest path
    from perpleximanus.tools.sandbox import LocalSandboxService, SandboxConfig
    from test_local import FakeLocalClient  # reuse the local backend's fake docker client

    async def _run(spec) -> dict:
        client = FakeLocalClient()
        svc = LocalSandboxService(SandboxConfig(backend="local", runtime="runc"), client=client)
        await svc.create(spec, owner_id="o", conversation_id="c")
        return client.last.run_kwargs

    import asyncio

    granted = asyncio.run(_run(SandboxSpec(permitted=frozenset({Capability.NETWORK}))))
    assert granted["ports"] == {f"{PREVIEW_PORT}/tcp": None}  # dev-server port published
    sealed = asyncio.run(_run(SandboxSpec()))
    assert sealed["ports"] is None  # sealed box exposes nothing


def test_podman_expose_port_is_a_stub():
    # the Podman instance overrides expose_port to a labeled None (stub in this env)
    from perpleximanus.tools.sandbox.podman import PodmanSandboxInstance

    assert PodmanSandboxInstance.expose_port is not ContainerInstance.expose_port

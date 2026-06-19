"""Backend-aware preview exposure — hermetic (the additive expose_port + dispatch).

Proves: only the dev-server port is exposed; the published host port becomes a reachable
URL; the URL host follows the backend (localhost local, the remote tailnet IP for gVisor);
the local backend publishes the port only when network is granted; Podman is a code stub.
"""

from __future__ import annotations

from disco.tools.anatomy import Capability
from disco.tools.sandbox._container import (
    PREVIEW_PORT,
    PUBLISHED_PORTS,
    USER_PORTS,
    ContainerInstance,
)
from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.gvisor import _preview_host


class _Container:
    def __init__(self, port_map: dict[int, str | None] | str | None = None) -> None:
        self.status = "running"
        if isinstance(port_map, str) or port_map is None:
            # maintain compatibility with existing tests that pass a single host port string
            port_map = {PREVIEW_PORT: port_map} if port_map else {}

        ports = {f"{p}/tcp": [{"HostPort": hp}] for p, hp in port_map.items() if hp}
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


def test_expose_extra_user_ports():
    # BP-10: curated USER_PORTS are also exposed
    c = _Container({8000: "32768", 3000: "32769", 5173: "32770"})
    inst = _inst(c)
    assert inst.expose_port(8000) == "http://localhost:32768"
    assert inst.expose_port(3000) == "http://localhost:32769"
    assert inst.expose_port(5173) == "http://localhost:32770"


def test_internal_ports_are_never_exposed_to_user():
    # security-critical: INTERNAL_PORTS (8899) must NEVER become user URLs,
    # even if the backend publishes them.
    from disco.tools.sandbox._container import INTERNAL_PORTS

    assert 8899 in INTERNAL_PORTS

    c = _Container({8899: "32771"})
    inst = _inst(c)
    assert inst.expose_port(8899) is None


def test_only_curated_user_ports_are_exposed():
    # containment: any other port is NOT reachable as a user URL
    assert 9999 not in USER_PORTS
    assert _inst(_Container({9999: "32772"})).expose_port(9999) is None


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


def test_local_publishes_curated_port_set_when_network_granted():
    from disco.tools.sandbox import LocalSandboxService, SandboxConfig
    from test_local import FakeLocalClient  # reuse the local backend's fake docker client
    from tool_fakes import FakeSandboxInstance  # noqa: F401 — ensures conftest path

    async def _run(spec) -> dict:
        client = FakeLocalClient()
        svc = LocalSandboxService(SandboxConfig(backend="local", runtime="runc"), client=client)
        await svc.create(spec, owner_id="o", conversation_id="c")
        return client.last.run_kwargs

    import asyncio

    granted = asyncio.run(_run(SandboxSpec(permitted=frozenset({Capability.NETWORK}))))
    assert granted["ports"] == {f"{p}/tcp": None for p in sorted(PUBLISHED_PORTS)}
    sealed = asyncio.run(_run(SandboxSpec()))
    assert sealed["ports"] is None  # sealed box exposes nothing


def test_podman_expose_port_is_a_stub():
    # the Podman instance overrides expose_port to a labeled None (stub in this env)
    from disco.tools.sandbox.podman import PodmanSandboxInstance

    assert PodmanSandboxInstance.expose_port is not ContainerInstance.expose_port

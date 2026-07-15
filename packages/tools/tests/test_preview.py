"""Backend-aware preview exposure — hermetic (the additive expose_port + dispatch).

Proves: only curated user ports become preview URLs; internal control mappings never do;
daemon bindings must be exact loopback bindings; and sealed boxes publish only the kernel
control port through their sidecar while exposing no user preview.
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import Capability
from disco.tools.sandbox._container import (
    INTERNAL_PORTS,
    PREVIEW_PORT,
    PUBLISHED_PORTS,
    USER_PORTS,
    ContainerInstance,
    loopback_port_bindings,
)
from disco.tools.sandbox.base import SandboxError, SandboxSpec
from disco.tools.sandbox.gvisor import _preview_host


class _Container:
    def __init__(self, port_map: dict[int, str | None] | str | None = None) -> None:
        self.status = "running"
        if isinstance(port_map, str) or port_map is None:
            # maintain compatibility with existing tests that pass a single host port string
            port_map = {PREVIEW_PORT: port_map} if port_map else {}

        ports = {
            f"{p}/tcp": [{"HostIp": "127.0.0.1", "HostPort": hp}]
            for p, hp in port_map.items()
            if hp
        }
        self.attrs = {"NetworkSettings": {"Ports": ports}}

    def reload(self) -> None:
        pass


def _inst(
    container,
    preview_host: str = "localhost",
    spec: SandboxSpec | None = None,
) -> ContainerInstance:
    return ContainerInstance(
        id="x",
        owner_id="o",
        conversation_id="c",
        spec=spec or SandboxSpec(public_web=True),
        container=container,
        container_workspace="/workspace",
        stop_timeout_s=5,
        preview_host=preview_host,
    )


def test_expose_port_returns_the_published_url():
    assert _inst(_Container("32768")).expose_port(PREVIEW_PORT) == "http://127.0.0.1:32768"


def test_expose_extra_user_ports():
    # BP-10: curated USER_PORTS are also exposed
    c = _Container({8000: "32768", 3000: "32769", 5173: "32770"})
    inst = _inst(c)
    assert inst.expose_port(8000) == "http://127.0.0.1:32768"
    assert inst.expose_port(3000) == "http://127.0.0.1:32769"
    assert inst.expose_port(5173) == "http://127.0.0.1:32770"


def test_internal_ports_are_never_exposed_to_user():
    # security-critical: INTERNAL_PORTS (8899) must NEVER become user URLs,
    # even if the backend publishes them.
    from disco.tools.sandbox._container import INTERNAL_PORTS

    assert 8899 in INTERNAL_PORTS

    c = _Container({8899: "32771"})
    inst = _inst(c)
    assert inst.expose_port(8899) is None


def test_sealed_internal_mapping_is_available_but_inverse_port_gates_hold():
    inst = _inst(
        _Container({8899: "32771", 8000: "32772"}),
        spec=SandboxSpec(),
    )
    assert inst.internal_port_mapping(8899) == ("127.0.0.1", 32771)
    assert inst.internal_port_mapping(8000) is None
    assert inst.expose_port(8000) is None
    assert inst.expose_port(8899) is None


def test_mapping_rejects_stopped_or_ambiguously_bound_sidecar():
    sidecar = _Container({8899: "32771"})
    inst = _inst(_Container(), spec=SandboxSpec())
    inst._egress_sidecar = sidecar
    sidecar.status = "exited"
    assert inst.internal_port_mapping(8899) is None

    sidecar.status = "running"
    sidecar.attrs["NetworkSettings"]["Ports"]["8899/tcp"].append(
        {"HostIp": "0.0.0.0", "HostPort": "32772"}
    )
    assert inst.internal_port_mapping(8899) is None


@pytest.mark.parametrize(
    ("host_ip", "host_port"),
    [
        ("::1", "32771"),
        ("0.0.0.0", "32771"),
        ("127.0.0.1", None),
        ("127.0.0.1", "not-a-port"),
        ("127.0.0.1", "0"),
        ("127.0.0.1", "-1"),
        ("127.0.0.1", "65536"),
    ],
)
def test_mapping_rejects_noncanonical_or_dead_binding(host_ip, host_port):
    sidecar = _Container()
    sidecar.attrs = {
        "NetworkSettings": {"Ports": {"8899/tcp": [{"HostIp": host_ip, "HostPort": host_port}]}}
    }
    inst = _inst(_Container(), spec=SandboxSpec())
    inst._egress_sidecar = sidecar
    assert inst.internal_port_mapping(8899) is None


def test_loopback_bindings_can_select_only_curated_internal_ports():
    assert loopback_port_bindings(INTERNAL_PORTS) == {"8899/tcp": ("127.0.0.1", None)}
    assert set(loopback_port_bindings()) == {f"{port}/tcp" for port in PUBLISHED_PORTS}
    with pytest.raises(SandboxError, match="outside the curated"):
        loopback_port_bindings({8899, 65_000})


def test_only_curated_user_ports_are_exposed():
    # containment: any other port is NOT reachable as a user URL
    assert 9999 not in USER_PORTS
    assert _inst(_Container({9999: "32772"})).expose_port(9999) is None


def test_no_url_when_the_port_is_not_published():
    # no user-port binding means no preview URL (sealed control mapping is separate)
    assert _inst(_Container(None)).expose_port(PREVIEW_PORT) is None


def test_preview_url_uses_verified_loopback_binding_not_configured_remote_host():
    # A configured tailnet/public preview host cannot turn a loopback bind into a
    # sibling-reachable URL.
    assert _inst(_Container("8080"), preview_host="100.81.82.115").expose_port(PREVIEW_PORT) == (
        "http://127.0.0.1:8080"
    )


def test_gvisor_preview_host_parsing():
    assert _preview_host("ssh://sandbox@100.81.82.115") == "100.81.82.115"
    assert _preview_host("ssh://sandbox@vm201.tailnet:22") == "vm201.tailnet"
    assert _preview_host("unix:///var/run/docker.sock") == "localhost"


def test_local_publishes_curated_ports_only_on_policy_sidecar():
    from disco.tools.sandbox import LocalSandboxService, SandboxConfig
    from test_local import FakeLocalClient  # reuse the local backend's fake docker client
    from tool_fakes import FakeSandboxInstance  # noqa: F401 — ensures conftest path

    async def _run(spec):
        client = FakeLocalClient()
        svc = LocalSandboxService(SandboxConfig(backend="local", runtime="runc"), client=client)
        await svc.create(spec, owner_id="o", conversation_id="c")
        return client.last.run_kwargs, client.runs[0].run_kwargs

    import asyncio

    granted, sidecar = asyncio.run(_run(SandboxSpec(permitted=frozenset({Capability.NETWORK}))))
    assert granted["ports"] is None
    assert sidecar["ports"] == loopback_port_bindings()
    sealed, sealed_sidecar = asyncio.run(_run(SandboxSpec()))
    assert sealed["ports"] is None  # sealed guest never publishes directly
    assert sealed_sidecar["ports"] == loopback_port_bindings(INTERNAL_PORTS)


def test_podman_expose_port_inherits_shared_impl():
    # [P-B / FIX6 parity] the Podman instance NO LONGER overrides expose_port with a
    # labeled-None stub — it INHERITS the shared `_resolve_mapping`-backed impl (the
    # filtered box's preview is reached through the egress sidecar's published port +
    # inbound forwarder), exactly like gVisor.
    from disco.tools.sandbox.podman import PodmanSandboxInstance

    assert PodmanSandboxInstance.expose_port is ContainerInstance.expose_port

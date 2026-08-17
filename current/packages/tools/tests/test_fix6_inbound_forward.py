"""FIX6 — make a FILTERED-egress gVisor box's preview port HOST-REACHABLE without
breaking containment.

Root cause: a filtered box sits on an INTERNAL no-NAT net and runsc freezes its
netstack at boot (no NIC hot-plug), so the sandbox can NEVER publish a host port
without a NAT bridge (= raw egress = broken containment) — so today the preview is
unreachable (gvisor.py set `ports=None` for filtered boxes).

Live-proven fix: keep the sandbox internal-only; the dual-homed egress SIDECAR
(bridge+internal) PUBLISHES the preview ports on the host and runs a stdlib TCP
forwarder `host:PORT -> <sandbox_internal_ip>:PORT`. Host reaches the preview via
the sidecar's published port; the sandbox keeps zero direct egress.

These are the UNIT proofs (hermetic, fake docker client); a separate LIVE check on
real runsc (VM 202) is run by the orchestrator.

  (a) the SIDECAR is created WITH the published PUBLISHED_PORTS,
  (b) `inbound_forward.py` is injected + launched on the sidecar with the sandbox IP,
  (c) `_resolve_mapping` reads the SIDECAR binding for both filtered preview and
      sealed internal-control transport,
  (d) `inbound_forward.py` itself — arg parse + a real localhost forward round-trip.
"""

from __future__ import annotations

import socket
import threading

import pytest
from disco.tools.sandbox import (
    GvisorSandboxService,
    SandboxConfig,
    SandboxSpec,
    SandboxUnavailableError,
)
from disco.tools.sandbox import inbound_forward as inbf
from disco.tools.sandbox._container import (
    INTERNAL_PORTS,
    PUBLISHED_PORTS,
    USER_PORTS,
    loopback_port_bindings,
)
from test_gvisor import FakeContainer, FakeDockerClient


def _svc(tmp_path) -> tuple[GvisorSandboxService, FakeDockerClient]:
    cfg = SandboxConfig(workspace_root=str(tmp_path))
    client = FakeDockerClient()
    return GvisorSandboxService(cfg, client=client), client


# ---------------------------------------------------------------------------
# (a) the egress SIDECAR is created WITH the published preview ports.
# ---------------------------------------------------------------------------


async def test_filtered_sidecar_created_with_published_preview_ports(tmp_path):
    svc, client = _svc(tmp_path)
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})),
        owner_id="o",
        conversation_id="c",
    )
    # The sidecar is the FIRST container the service stands up (create-then-start),
    # the sandbox is the second (run). The sidecar must carry the published set —
    # it's the only member that can publish (it's on bridge; the sandbox is
    # internal-only and runsc can't hot-plug a NIC).
    sidecar = client.runs[0]
    published = sidecar.run_kwargs.get("ports")
    assert published == loopback_port_bindings(), published
    # The SANDBOX, by contrast, publishes NOTHING (containment).
    assert client.last.run_kwargs.get("ports") is None


# ---------------------------------------------------------------------------
# (b) inbound_forward.py is injected + launched on the SIDECAR with the sandbox IP.
# ---------------------------------------------------------------------------


async def test_inbound_forwarder_injected_and_launched_on_sidecar(tmp_path):
    svc, client = _svc(tmp_path)
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})),
        owner_id="o",
        conversation_id="c",
    )
    sidecar = client.runs[0]
    # [FIX6 live-found on real gVisor] the forwarder is delivered + launched in ONE
    # exec: the script is base64-embedded, decoded to /inbound_forward.py, then python3
    # `exec`s it — all in the same shell. A separate put_archive + later exec was proven
    # UNRELIABLE on a dual-homed runsc sidecar (put returned success but the file was not
    # visible to the next exec → forwarder died Errno 2 → preview silently unreachable).
    launched = [c for c in sidecar.exec_calls if any("inbound_forward.py" in str(a) for a in c)]
    assert launched, "inbound forwarder was not launched on the sidecar"
    joined = " ".join(launched[0])
    # delivered inline (base64 decode → file) and run in the same shell, not put_archive
    assert "base64 -d > /inbound_forward.py" in joined, joined
    assert "python3 /inbound_forward.py" in joined, joined
    # targets the SANDBOX's internal IP (172.28.0.5, the fake's synthetic IP) + ports
    assert "172.28.0.5" in joined, joined
    for port in sorted(PUBLISHED_PORTS):
        assert str(port) in joined, f"port {port} missing from forwarder launch: {joined}"


async def test_no_forwarder_without_sandbox_ip(tmp_path):
    """If the sandbox has no internal IP yet (reload returned nothing), the forwarder
    fails closed and every partially allocated resource is removed."""
    svc, client = _svc(tmp_path)

    # Force the sandbox container to report no internal IP.
    orig_run = client.run

    def _run_no_ip(**kwargs):
        c = orig_run(**kwargs)
        c.attrs = {}  # no NetworkSettings → no IP
        return c

    client.run = _run_no_ip  # type: ignore[method-assign]
    with pytest.raises(SandboxUnavailableError, match="no internal address"):
        await svc.create(
            SandboxSpec(egress_allow=frozenset({"api.example.com"})),
            owner_id="o",
            conversation_id="c",
        )
    sidecar = client.runs[0]
    assert not any("inbound_forward.py" in " ".join(call) for call in sidecar.exec_calls)
    assert all(container.removed for container in client.runs)
    assert client.networks.created[0].removed
    assert all(volume.removed for volume in client.volumes.objects.values())


# ---------------------------------------------------------------------------
# (c) _resolve_mapping reads the SIDECAR binding for filtered preview and sealed
#     internal control transport.
# ---------------------------------------------------------------------------


async def test_resolve_mapping_reads_sidecar_binding_when_filtered(tmp_path):
    svc, client = _svc(tmp_path)
    inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})),
        owner_id="o",
        conversation_id="c",
    )
    sidecar, sandbox = client.runs[0], client.runs[1]
    # The SIDECAR holds the real host-published mapping (it published the ports).
    sidecar.attrs = {
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49160"}]}}
    }
    # A decoy binding on the sandbox must be IGNORED for a filtered box.
    sandbox.attrs = {
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}]}}
    }

    url = inst.expose_port(8000)
    assert url is not None
    assert url == "http://127.0.0.1:49160"  # the SIDECAR's port, not the sandbox decoy


async def test_sealed_sandbox_maps_only_internal_control_port_from_sidecar(tmp_path):
    svc, client = _svc(tmp_path)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    sidecar, sandbox = client.runs
    sidecar.attrs = {
        "NetworkSettings": {"Ports": {"8899/tcp": [{"HostIp": "127.0.0.1", "HostPort": "38899"}]}}
    }
    sandbox.attrs = {
        "NetworkSettings": {
            "Ports": {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "33333"}],
                "8899/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}],
            }
        }
    }
    assert inst._egress_sidecar is sidecar
    assert inst.internal_port_mapping(8899) == ("127.0.0.1", 38899)
    assert inst.expose_port(8000) is None
    assert inst.expose_port(8899) is None
    assert inst.internal_port_mapping(next(iter(USER_PORTS))) is None
    assert sidecar.run_kwargs["ports"] == loopback_port_bindings(INTERNAL_PORTS)


async def test_resolve_mapping_refuses_nonloopback_daemon_binding(tmp_path):
    svc, client = _svc(tmp_path)
    inst = await svc.create(
        SandboxSpec(public_web=True),
        owner_id="o",
        conversation_id="c",
    )
    inst._egress_sidecar.attrs = {
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "33333"}]}}
    }
    assert inst.expose_port(8000) is None


async def test_resolve_mapping_returns_none_when_no_binding(tmp_path):
    """Wedge-guard preserved: a missing/absent binding yields None (no URL), never
    a raise — for both the sidecar (filtered) and the sandbox (sealed) source."""
    svc, client = _svc(tmp_path)
    inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})), owner_id="o", conversation_id="c"
    )
    client.runs[0].attrs = {"NetworkSettings": {"Ports": {}}}  # sidecar, no binding yet
    assert inst.expose_port(8000) is None
    assert inst.expose_port(9999) is None  # not a USER port at all


# ---------------------------------------------------------------------------
# (d) inbound_forward.py itself — pure stdlib.
# ---------------------------------------------------------------------------


def test_parse_args_ok_and_errors():
    assert inbf.parse_args(["172.28.0.5", "8000", "3000"]) == ("172.28.0.5", [8000, 3000])
    assert inbf.parse_args(["host", "5173"]) == ("host", [5173])
    with pytest.raises(ValueError):
        inbf.parse_args(["onlyhost"])  # too few args
    with pytest.raises(ValueError):
        inbf.parse_args([])
    with pytest.raises(ValueError):
        inbf.parse_args(["host", "notaport"])  # non-integer port


def test_main_bad_args_returns_nonzero():
    assert inbf.main(["onlyhost"]) == 2
    assert inbf.main([]) == 2


def test_localhost_forward_round_trip():
    """A real end-to-end TCP forward over loopback: forwarder listens on an
    ephemeral port and pipes to a backend echo server; bytes survive both ways."""
    # Backend echo server.
    backend = inbf._make_listener(0)
    backend_port = backend.getsockname()[1]

    def _echo() -> None:
        try:
            conn, _ = backend.accept()
        except OSError:
            return
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)

    threading.Thread(target=_echo, daemon=True).start()

    # Forwarder: listen on an ephemeral port, forward to the backend echo port.
    listener = inbf._make_listener(0)
    fwd_port = listener.getsockname()[1]
    threading.Thread(
        target=inbf._serve_forever,
        args=(listener, "127.0.0.1", backend_port),
        daemon=True,
    ).start()

    # Client hits the FORWARDER and must get its bytes echoed back through it.
    with socket.create_connection(("127.0.0.1", fwd_port), timeout=5) as sock:
        sock.sendall(b"hello-fix6")
        sock.settimeout(5)
        got = b""
        while len(got) < len(b"hello-fix6"):
            chunk = sock.recv(64)
            if not chunk:
                break
            got += chunk
    assert got == b"hello-fix6"
    listener.close()
    backend.close()


def test_published_ports_includes_user_preview_ports():
    """Sanity: the forwarder set covers the user preview ports (8000, vite 5173,
    noVNC 6080, …) so a dev server on any of them is reachable through the sidecar."""
    assert USER_PORTS <= PUBLISHED_PORTS
    assert 8000 in PUBLISHED_PORTS and 5173 in PUBLISHED_PORTS


# Keep a direct ref to FakeContainer so a future edit to the fake import doesn't
# silently drop coverage of the create()-path container shape.
assert FakeContainer is not None

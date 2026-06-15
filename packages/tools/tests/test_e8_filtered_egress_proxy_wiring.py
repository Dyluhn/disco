"""E8 — wire the allowlisting egress PROXY for the podman (and local) sandbox backend
so a FILTERED box gets a proxied allowlist instead of a total deny-all seal.

UNIT part only — the live VM 202 host-reach test is deferred. These tests assert
the backend's WIRING (the container create args / fake-client state) proves the
filtered posture is no longer the old `network_mode="none"` seal — it's a PROXIED
network (internal no-NAT net + sidecar + allowlist env), with the gVisor path
unchanged. The seal relaxes ONLY to the allowlist, never to an open bridge.

References:
  - podman.py `_setup_filtered_egress` (NEW) — the podman-py mirror of gvisor.py
  - local.py `_start_container` (rewired) — reuses the parent's `_setup_filtered_egress`
  - gvisor.py `_setup_filtered_egress` (UNCHANGED) — the original
  - _container.py `ContainerInstance` — shared teardown of the aux sidecar + net
"""

from __future__ import annotations

from disco.tools.anatomy import Capability
from disco.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    PodmanSandboxService,
    SandboxConfig,
    SandboxSpec,
    default_local_config,
    default_podman_config,
)
from test_gvisor import FakeDockerClient
from test_local import FakeLocalClient

# Reuse the fakes from the existing test modules — they're hermetic and well
# isolated, and importing them keeps the E8 surface minimal.
from test_podman import FakeCli, FakePodmanClient

# ---------------------------------------------------------------------------
# Test 1 — a FILTERED podman spec → the resolved network is PROXIED, NOT "none".
# ---------------------------------------------------------------------------


def _podman_svc() -> tuple[PodmanSandboxService, FakePodmanClient, FakeCli]:
    """A podman service with a populated fake client (networks + containers) and a
    CLI runner that accepts every exec (the sidecar's one-shots go through it,
    per `_sidecar_cli_run` in podman.py)."""
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(has_image=True, fs=fs)
    cli = FakeCli(fs)
    svc = PodmanSandboxService(
        default_podman_config(), client=client, cli_runner=cli
    )
    return svc, client, cli


async def test_filtered_podman_spec_yields_proxied_network_not_none():
    """The E8 acceptance: a FILTERED podman spec produces a PROXIED network
    (allowlist env present, sidecar created and attached to an internal no-NAT
    net), NOT the old `network_mode="none"` fail-safe seal. The relaxation is
    ONLY to the allowlist — never to an open bridge."""
    svc, client, _cli = _podman_svc()
    inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com", ".pypi.org"})),
        owner_id="o",
        conversation_id="c",
    )

    # (a) An INTERNAL no-NAT network was created — the containment substrate.
    assert len(client.networks.created) == 1
    net = client.networks.created[0]
    assert net.attrs.get("internal") is True, "filtered must use an internal (no-NAT) net"
    assert net.name.startswith("disco-egr-")

    # (b) A proxy SIDECAR was create-then-connect-then-started (the runsc-equivalent
    # ordering for podman). Two containers total: sidecar + sandbox.
    assert len(client.created) == 2, f"expected sidecar + sandbox, got {len(client.created)}"
    sidecar, sandbox = client.created[0], client.created[1]
    assert sidecar.started is True, "sidecar must be started (so both NICs exist)"
    assert net.connected and net.connected[0][0] is sidecar

    # (c) The SANDBOX is on the internal net (proxied) — NOT `network_mode="none"`
    # and NOT `network_mode="bridge"`. The relaxation is to the allowlist, never
    # to an open bridge.
    sb_kwargs = sandbox.create_kwargs
    assert "network_mode" not in sb_kwargs, (
        f"filtered must NOT be sealed with network_mode; got {sb_kwargs.get('network_mode')!r}"
    )
    assert "network_mode" not in sb_kwargs or sb_kwargs.get("network_mode") != "bridge", (
        "filtered must NOT be open bridge; relaxation is to the allowlist ONLY"
    )
    sb_networks = sb_kwargs.get("networks") or {}
    assert sb_networks, "sandbox must be attached to the internal net via `networks`"
    assert net.name in sb_networks, "sandbox must be on the egress internal net"

    # (d) The allowlist env is wired (defense in depth atop the no-route net).
    env = sb_kwargs["environment"]
    assert env.get("HTTPS_PROXY", "").startswith("http://"), env
    assert ":8888" in env["HTTPS_PROXY"], "proxy must point at the allowlist sidecar's port"
    # Both case-pairs set (different tools read different ones), per proxy_env().
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        assert var in env, f"missing proxy env var {var!r}"

    # (e) The aux refs are attached on the instance (so destroy() tears them down).
    assert inst._egress_network is net
    assert inst._egress_sidecar is sidecar

    # (f) destroy() tears down BOTH the sandbox and the egress aux (no orphans).
    await inst.destroy()
    assert sandbox.stopped and sandbox.removed
    assert sidecar.removed
    assert net.removed


async def test_filtered_podman_spec_never_relaxes_to_open_bridge():
    """The CRITICAL guard: a filtered spec MUST NOT silently get an open bridge.
    That was the original (pre-E8) false guarantee — the allowlist was a name on
    paper while the box had full network. E8 fixes it: the only paths are sealed,
    filtered (proxied allowlist), or open (explicit NETWORK capability). The
    relaxation is never to an open bridge from a filtered spec."""
    svc, client, _cli = _podman_svc()
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})),
        owner_id="o",
        conversation_id="c",
    )
    sandbox = client.created[-1]
    # The relaxation surface is binary: filtered -> proxied, open -> bridge.
    # A filtered spec must not produce the open-branch kwarg.
    assert sandbox.create_kwargs.get("network_mode") != "bridge"


# ---------------------------------------------------------------------------
# Test 2 — gVisor filtered path UNCHANGED (no regression).
# ---------------------------------------------------------------------------


def _gvisor_svc(
    tmp_path, *, with_image: bool = True
) -> tuple[GvisorSandboxService, FakeDockerClient]:
    cfg = SandboxConfig(workspace_root=str(tmp_path))
    client = FakeDockerClient(has_image=with_image)
    return GvisorSandboxService(cfg, client=client), client


async def test_filtered_gvisor_spec_unchanged_no_regression(tmp_path):
    """The E8 no-regression guard: a filtered gVisor spec keeps the existing
    behavior. This is a re-assertion of the gVisor filtered contract — the
    sidecar, the internal net, the proxy env — with the assertion that NOTHING
    has been silently altered. If this test breaks, an E8 change has leaked
    into the gVisor path (the most likely culprit would be the shared
    ContainerInstance teardown refactor, so we cover that path explicitly)."""
    svc, client = _gvisor_svc(tmp_path)
    inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com", ".pypi.org"})),
        owner_id="o",
        conversation_id="c",
    )

    # INTERNAL (no-NAT) network — unchanged.
    assert len(client.networks.created) == 1
    net = client.networks.created[0]
    assert net.attrs.get("internal") is True

    # Sidecar create-then-connect-then-start (verified-live invariant #1) — unchanged.
    assert len(client.runs) == 2
    sidecar = client.runs[0]
    assert sidecar.started is True
    assert net.connected and net.connected[0][0] is sidecar

    # Sidecar resolv.conf is repointed to a public resolver (verified-live #2) — unchanged.
    resolv = [c for c in sidecar.exec_calls if any("resolv.conf" in str(a) for a in c)]
    assert resolv, "sidecar resolv.conf was not repointed to a public resolver"
    assert any("1.1.1.1" in str(a) for a in resolv[0])

    # Proxy script was injected and launched with the allowlist — unchanged.
    assert any("egress_proxy.py" in p for p in sidecar.fs)
    launched = [c for c in sidecar.exec_calls if any("egress_proxy.py" in str(a) for a in c)]
    assert launched, "proxy was not launched in the sidecar"
    joined = " ".join(launched[0])
    assert "api.example.com" in joined and ".pypi.org" in joined

    # Sandbox: NOT full bridge, on the internal net, proxy env present — unchanged.
    sb_kw = client.last.run_kwargs
    assert "network_mode" not in sb_kw
    assert sb_kw["network"] == net.name
    assert sb_kw["environment"]["HTTPS_PROXY"].startswith("http://")

    # Aux teardown still works (E8 refactor: shared via ContainerInstance) — unchanged.
    assert inst._egress_network is net
    assert inst._egress_sidecar is sidecar
    await inst.destroy()
    assert sidecar.removed and net.removed


async def test_sealed_and_open_gvisor_paths_unchanged_no_regression(tmp_path):
    """E8 must not change the non-filtered paths. The default spec stays
    `network_mode="none"`; an explicit NETWORK capability stays `bridge`."""
    svc, client = _gvisor_svc(tmp_path)
    # Default (sealed) — unchanged.
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["network_mode"] == "none"
    # Explicit NETWORK capability (open) — unchanged.
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})),
        owner_id="o",
        conversation_id="c",
    )
    assert client.last.run_kwargs["network_mode"] == "bridge"


# ---------------------------------------------------------------------------
# BONUS — the local tier gets the same wiring (the E8 brief says "podman (and
# local)"). These are not in the strict E8 acceptance pair, but the local
# backend inherits from gVisorSandboxService so the wiring must work end-to-end
# there too — the regression guard would otherwise miss a local-only break.
# ---------------------------------------------------------------------------


def _local_svc() -> tuple[LocalSandboxService, FakeLocalClient]:
    client = FakeLocalClient()
    svc = LocalSandboxService(default_local_config(), client=client)
    return svc, client


async def test_filtered_local_spec_yields_proxied_network_not_none():
    """The local (lowest-isolation) tier also gets the proxied allowlist — the
    E8 brief says podman (and local). The local backend reuses the parent's
    `_setup_filtered_egress` (same docker-py client) so the wiring is the same
    shape, just on a named volume instead of a bind mount."""
    svc, client = _local_svc()
    inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})),
        owner_id="o",
        conversation_id="c",
    )

    # INTERNAL (no-NAT) net + sidecar.
    assert len(client.networks.created) == 1
    net = client.networks.created[0]
    assert net.attrs.get("internal") is True
    assert len(client.runs) == 2  # sidecar + sandbox
    sidecar = client.runs[0]
    assert sidecar.started is True
    assert net.connected and net.connected[0][0] is sidecar

    # Sandbox on the internal net (proxied), NOT sealed, NOT open bridge.
    sb_kw = client.last.run_kwargs
    assert "network_mode" not in sb_kw
    assert sb_kw.get("network") == net.name
    assert sb_kw["environment"]["HTTPS_PROXY"].startswith("http://")

    # Aux teardown attached.
    assert inst._egress_network is net
    assert inst._egress_sidecar is sidecar
    await inst.destroy()
    assert sidecar.removed and net.removed


async def test_sealed_and_open_local_paths_unchanged_no_regression():
    """The local tier's sealed / open paths stay exactly as before (the existing
    `test_sealed_default_open_when_granted` covers this, but we re-assert here
    so the E8 test file is the single regression hub for the local wiring)."""
    svc, client = _local_svc()
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["network_mode"] == "none"
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})),
        owner_id="o",
        conversation_id="c",
    )
    assert client.last.run_kwargs["network_mode"] == "bridge"

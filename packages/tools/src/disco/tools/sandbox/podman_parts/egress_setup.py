"""[E8 — live-verify on VM 202 pending] `PodmanSandboxService._setup_filtered_egress`.

Stand up the internal network plus policy/control sidecar and return its
topology.

SPIRIT identical to `gvisor.py _setup_filtered_egress` — the OLD binary
(none|bridge) silently gave an allowlisted box full network, which is the
false guarantee E8 closes. Now an INTERNAL (no-NAT) network is the
substrate: the box's ONLY route out is the proxy, and the proxy refuses
any host the allowlist doesn't name. The HTTP(S)_PROXY env is belt-and-
suspenders; the no-route property is the real guarantee.

Three podman-specific notes (mirror gVisor's invariants #1/#2/#3 in
gvisor.py; the podman host may not enforce the same boot-time netstack
freeze as runsc, but we keep the create-on-bridge → connect-internal →
start ordering so both NICs exist before any sandbox traffic):

1. The proxy script is `put_archive`'d to the sidecar (no image rebuild;
   base image's `python3` runs it; honors the never-pull rule).
2. `exec_run` on a podman-py remote container is broken (per the backend
   docstring), so the one-shot setup (resolv.conf + proxy launch) goes
   through the CLI runner, same as the sandbox's command path.
3. The sandbox's proxy env names the sidecar by its INTERNAL-NET IP (read
   from the sidecar's attrs after `reload()`). Missing or unverifiable IP
   state fails closed because embedded DNS is unavailable here.

``inbound_only`` omits resolver/proxy/relay setup and publishes only
INTERNAL_PORTS, preserving zero guest outbound transport.
"""

from __future__ import annotations

import io
import pathlib
import shlex
import tarfile
import time
from typing import TYPE_CHECKING, Any

from .._container import (
    EGRESS_PROXY_PORT,
    INTERNAL_PORTS,
    PUBLISHED_PORTS,
    bounded_sidecar_cap,
    collect_host_deny_ips,
    discover_remote_host_ips,
    egress_mode,
    format_allow,
    loopback_port_bindings,
    nofile_ulimits,
    proxy_env,
    proxy_readiness_argv,
    proxy_run_argv,
    sealed,
)
from ..base import SandboxUnavailableError
from ..capability_relay import parse_upstream

if TYPE_CHECKING:
    from ..base import SandboxSpec
    from ..podman import PodmanSandboxService

_CPU_PERIOD = 100_000  # cgroup CPU period (100ms); quota/period = cpus


def _compute_sidecar_caps(svc: PodmanSandboxService) -> tuple[float, int, int]:
    # EPIC H (P1) — runtime LAST GATE on the sidecar caps. SandboxConfig is hot-mutable, so
    # a post-construction `cfg.sidecar_cpu = 0` (etc.) bypasses the @field_validator and
    # would otherwise reach podman as UNLIMITED. Gate BEFORE creating any network/sidecar
    # so a mutated cap is refused with nothing stranded.
    sidecar_cpu = bounded_sidecar_cap("cpu", svc._cfg.sidecar_cpu)
    sidecar_memory_mb = int(bounded_sidecar_cap("memory_mb", svc._cfg.sidecar_memory_mb))
    sidecar_pids_limit = int(bounded_sidecar_cap("pids", svc._cfg.sidecar_pids_limit))
    return sidecar_cpu, sidecar_memory_mb, sidecar_pids_limit


def _compute_deny_ips(svc: PodmanSandboxService, inbound_only: bool):
    from .. import podman

    if inbound_only:
        return frozenset()
    return collect_host_deny_ips(
        svc._cfg.host_ip_blocklist,
        svc._cfg.preview_host or podman._preview_host(svc._cli_url),
        include_local_interfaces=not svc._cli_url.startswith("ssh://"),
        additional_host_ips=(
            discover_remote_host_ips(svc._cli_url)
            if svc._cli_url.startswith("ssh://") and svc._injected_client is None
            else frozenset()
        ),
    )


def _create_dual_homed_sidecar(
    svc: PodmanSandboxService,
    client: Any,
    spec: SandboxSpec,
    net_name: str,
    labels: dict[str, str],
    sidecar_cpu: float,
    sidecar_memory_mb: int,
    sidecar_pids_limit: int,
    resources: dict[str, Any],
) -> None:
    """Create the network + sidecar, writing each into ``resources`` as it is
    created (NOT only on full success) — a partial failure here (e.g.
    ``sidecar.start()`` raising) must still leave the caller able to see and
    tear down whatever already exists; see the leak-guard in
    `setup_filtered_egress`."""
    # The network carries the same label as the containers so the orphan
    # sweep can find it — its NAME is instance-keyed, not conversation-keyed.
    resources["network"] = client.networks.create(
        net_name, driver="bridge", internal=True, labels=labels
    )
    # (1) create on bridge → connect internal → start, so the sidecar's
    # `bridge` NIC (the route to the internet, the proxy's upstream) AND its
    # internal-net NIC BOTH exist before any sandbox traffic.
    # [FIX6 parity — live-verified on local podman 5.8.2] PUBLISH the preview
    # ports on the SIDECAR (it's on bridge → it CAN publish; the sandbox is
    # internal-only and can't). The sidecar's inbound forwarder (launched after
    # the sandbox starts, in `_start_container`) bridges each published host
    # port to the sandbox's internal IP — host reaches the preview, the sandbox
    # keeps zero direct egress. Ports must be declared at create() (podman can't
    # add mappings to a running container). podman-py accepts the SAME docker-py
    # `{"8000/tcp": None}` format AND reads it back as the same
    # `NetworkSettings.Ports` shape (verified live), so the shared
    # `_container._resolve_mapping` reads the binding unchanged.
    resources["sidecar"] = client.containers.create(
        image=svc._cfg.image,
        command=["sh", "-c", "exec sleep infinity"],
        # [FIX6 parity] dual-home the sidecar: a BRIDGE route (the proxy's
        # upstream + the host's path to the published preview ports) AND, after
        # `network.connect` below, the internal no-NAT net it shares with the
        # sandbox. gVisor passes docker-py's `network="bridge"`; the rootless
        # podman equivalent is `network_mode="bridge"` (verified live — a bare
        # create defaults to pasta, which an internal net cannot attach to).
        network_mode="bridge",
        ports=loopback_port_bindings(
            INTERNAL_PORTS if sealed(spec) else PUBLISHED_PORTS,
            podman=True,
        ),
        # EPIC H (P1): bound the sidecar on CPU + PIDs too, not just memory — a wedged
        # or compromised proxy must not be able to burn host CPU or fork-bomb host PIDs.
        # podman caps cpu via quota/period (mirrors the sandbox create path).
        mem_limit=f"{sidecar_memory_mb}m",
        cpu_quota=int(sidecar_cpu * _CPU_PERIOD),
        cpu_period=_CPU_PERIOD,
        pids_limit=sidecar_pids_limit,
        ulimits=nofile_ulimits(svc._cfg),
        cap_drop=["ALL"],
        no_new_privileges=True,
        sysctls={
            "net.ipv4.ip_forward": "0",
            "net.ipv6.conf.all.forwarding": "0",
        },
        detach=True,
        name=net_name,
        labels=labels,
    )
    resources["network"].connect(resources["sidecar"])
    resources["sidecar"].start()


def _read_sidecar_internal_address(sidecar: Any, net_name: str) -> tuple[bool, str]:
    # (3) read the sidecar's IP on the internal net; the sandbox proxies by IP.
    # Embedded DNS is unavailable in this topology, so a name fallback would
    # produce a silently dead proxy transport.
    try:
        sidecar.reload()
        sidecar_running = getattr(sidecar, "status", "") == "running"
        nets = sidecar.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
        proxy_ip = nets.get(net_name, {}).get("IPAddress", "")
    except Exception as exc:  # noqa: BLE001 — fixed diagnostic + outer cleanup
        raise SandboxUnavailableError(
            "sandbox policy sidecar internal address is unavailable; refusing sandbox start"
        ) from exc
    return sidecar_running, proxy_ip


def _configure_egress_proxy(
    svc: PodmanSandboxService,
    sidecar: Any,
    spec: SandboxSpec,
    deny_ips,
    net_name: str,
) -> tuple[dict[str, str], str | None]:
    from .. import podman

    # (2) replace the dead embedded resolver with public DNS over the (working) route.
    svc._sidecar_cli_run(
        sidecar.name,
        [
            "sh",
            "-c",
            'printf "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n" > /etc/resolv.conf',
        ],
        15,
    )
    # Inject the proxy script (put_archive works over the podman remote, per
    # the backend docstring). Native detached exec owns the long-lived
    # process; the shell is replaced by Python instead of abandoning an
    # ``&`` child that Podman may reap.
    script = pathlib.Path(podman._egress_proxy_mod.__file__).read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="egress_proxy.py")
        info.size = len(script)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(script))
    if not sidecar.put_archive("/", buf.getvalue()):
        raise SandboxUnavailableError("failed to inject egress proxy script into sidecar")
    allow = format_allow(spec.egress_allow)
    deny_hosts = (
        frozenset({parse_upstream(svc._cfg.host_service_upstream).host})
        if spec.host_services
        else frozenset()
    )
    argv = proxy_run_argv(
        allow,
        EGRESS_PROXY_PORT,
        public_only=egress_mode(spec) == "public",
        deny_ips=deny_ips,
        deny_hosts=deny_hosts,
    )
    svc._sidecar_cli_run(
        sidecar.name,
        [
            "sh",
            "-c",
            f"exec {shlex.join(argv)} >/var/log/egress.log 2>/var/log/egress.err.log",
        ],
        10,
        detach=True,
    )
    ready_rc, _ready_out, _ready_err = svc._sidecar_cli_run(
        sidecar.name, proxy_readiness_argv(EGRESS_PROXY_PORT), 10
    )
    if ready_rc != 0:
        raise SandboxUnavailableError(
            "egress policy proxy failed its readiness check; refusing sandbox start"
        )
    sidecar_running, proxy_ip = _read_sidecar_internal_address(sidecar, net_name)
    if not sidecar_running or not proxy_ip:
        raise SandboxUnavailableError(
            "sandbox policy sidecar internal address is unavailable; refusing sandbox start"
        )
    env = proxy_env(proxy_ip, EGRESS_PROXY_PORT)
    relay_url = svc._launch_capability_relay(sidecar, proxy_ip, spec)
    return env, relay_url


def setup_filtered_egress(
    svc: PodmanSandboxService,
    client: Any,
    spec: SandboxSpec,
    instance_id: str,
    conversation_id: str = "",
    *,
    inbound_only: bool = False,
) -> Any:
    from .. import podman

    net_name = f"{podman.EGR_NET_PREFIX}{instance_id}"
    labels = {podman.LABEL_CONV: conversation_id} if conversation_id else {}
    sidecar_cpu, sidecar_memory_mb, sidecar_pids_limit = _compute_sidecar_caps(svc)
    deny_ips = _compute_deny_ips(svc, inbound_only)
    # [P1 leak-guard] Setup runs BEFORE the guarded sandbox create in `_start_container`,
    # so a partial failure here (connect/start/proxy inject) would otherwise strand the
    # internal network + proxy sidecar. Own the cleanup: any exception after either
    # resource exists removes whatever was created before re-raising.
    # `resources` is written into incrementally by `_create_dual_homed_sidecar` (not
    # only returned on full success), so a partial failure (e.g. `sidecar.start()`
    # raising) still leaves whatever was already created visible to the cleanup below.
    resources: dict[str, Any] = {"network": None, "sidecar": None}
    try:
        _create_dual_homed_sidecar(
            svc,
            client,
            spec,
            net_name,
            labels,
            sidecar_cpu,
            sidecar_memory_mb,
            sidecar_pids_limit,
            resources,
        )
        network, sidecar = resources["network"], resources["sidecar"]
        if inbound_only:
            # Default sealed boxes get only the inbound control transport:
            # no resolver, egress proxy, proxy env, or relay is installed.
            return network, sidecar, {}, net_name, None
        env, relay_url = _configure_egress_proxy(svc, sidecar, spec, deny_ips, net_name)
        return network, sidecar, env, net_name, relay_url
    except Exception:
        # Partial setup must not leak: tear down whatever already exists, then re-raise.
        svc._best_effort_cleanup(
            egress_network=resources["network"], egress_sidecar=resources["sidecar"]
        )
        raise

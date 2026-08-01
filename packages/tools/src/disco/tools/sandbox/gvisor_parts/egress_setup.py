"""[HARDWARE-UNVERIFIED — live-verify on VM 201] `GvisorSandboxService._setup_filtered_egress`.

Stand up the internal network + policy/control sidecar and return its topology.

Containment design: an INTERNAL (no-NAT) network whose ONLY member with an
outside route is the proxy sidecar. The sandbox is attached to this network
alone, so its sole path to the internet is through the proxy — which refuses
any host the allowlist doesn't name. The HTTP(S)_PROXY env is belt-and-
suspenders; the no-route property is the real guarantee. ``inbound_only``
omits resolver/proxy/relay setup and publishes only INTERNAL_PORTS for
host-to-guest kernel control.

The proxy is the stdlib-only `egress_proxy.py`, injected into a sidecar built
from the SAME base image (its python3 runs it — no extra image, honoring the
never-pull rule).

THREE non-obvious details, each VERIFIED live on the gVisor host (VM 201);
get any wrong and containment silently breaks:

1. Two NICs AT BOOT. gVisor (runsc) freezes its netstack at sandbox-create;
   a `docker network connect` AFTER `run` adds the NIC at the Docker level
   but the runsc netstack never sees it (the sandbox can't reach the proxy →
   RST). So the sidecar is `create`d on bridge, `connect`ed to the internal
   net, and only THEN `start`ed — both interfaces exist before runsc boots.

2. A WORKING resolver. On a user-defined network Docker forces
   `nameserver 127.0.0.11` (its embedded DNS), and that resolver is
   unreachable from a dual-homed gVisor box → the proxy can't resolve any
   upstream. Routing to the internet by IP works fine, so we point the
   sidecar's resolv.conf straight at public resolvers.

3. Reach the proxy by IP, not name. The SANDBOX (on the internal net) also
   can't use the embedded DNS, so it can't resolve the sidecar's name. We
   read the sidecar's internal-net IP and hand the sandbox HTTP(S)_PROXY by
   IP.
"""

from __future__ import annotations

import pathlib
import shlex
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
    from ..gvisor import GvisorSandboxService


def _compute_sidecar_caps(svc: GvisorSandboxService) -> tuple[float, int, int]:
    # EPIC H (P1) — runtime LAST GATE on the sidecar caps. SandboxConfig is hot-mutable, so
    # a post-construction `cfg.sidecar_cpu = 0` (etc.) bypasses the @field_validator and
    # would otherwise reach Docker as UNLIMITED. Gate BEFORE creating any network/sidecar
    # so a mutated cap is refused with nothing stranded.
    sidecar_cpu = bounded_sidecar_cap("cpu", svc._cfg.sidecar_cpu)
    sidecar_memory_mb = int(bounded_sidecar_cap("memory_mb", svc._cfg.sidecar_memory_mb))
    sidecar_pids_limit = int(bounded_sidecar_cap("pids", svc._cfg.sidecar_pids_limit))
    return sidecar_cpu, sidecar_memory_mb, sidecar_pids_limit


def _compute_deny_ips(svc: GvisorSandboxService, inbound_only: bool):
    from .. import gvisor

    if inbound_only:
        return frozenset()
    return collect_host_deny_ips(
        svc._cfg.host_ip_blocklist,
        svc._cfg.preview_host or gvisor._preview_host(svc._cfg.docker_socket),
        include_local_interfaces=not svc._cfg.docker_socket.startswith("ssh://"),
        additional_host_ips=(
            discover_remote_host_ips(svc._cfg.docker_socket)
            if svc._cfg.docker_socket.startswith("ssh://") and svc._injected_client is None
            else frozenset()
        ),
    )


def _create_dual_homed_sidecar(
    svc: GvisorSandboxService,
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
    # (1) create on bridge → connect internal → start, so runsc sees BOTH NICs.
    # [FIX6] PUBLISH the preview ports on the SIDECAR (it's on bridge → it CAN
    # publish; the sandbox is internal-only and can't). The sidecar's inbound
    # forwarder (launched after the sandbox starts) bridges each published
    # host port to the sandbox's internal IP — host reaches the preview, the
    # sandbox keeps zero direct egress. Ports must be declared at create()
    # (docker can't add mappings to a running container).
    resources["sidecar"] = client.containers.create(
        image=svc._cfg.image,
        command=["sh", "-c", "exec sleep infinity"],
        runtime=svc._cfg.runtime,
        network="bridge",  # the route to the internet (the proxy's upstream)
        ports=loopback_port_bindings(INTERNAL_PORTS if sealed(spec) else PUBLISHED_PORTS),
        # EPIC H (P1): bound the sidecar on CPU + PIDs too, not just memory — a wedged
        # or compromised proxy must not be able to burn host CPU or fork-bomb host PIDs.
        mem_limit=f"{sidecar_memory_mb}m",
        nano_cpus=int(sidecar_cpu * 1_000_000_000),
        pids_limit=sidecar_pids_limit,
        ulimits=nofile_ulimits(svc._cfg),
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        sysctls={
            "net.ipv4.ip_forward": "0",
            "net.ipv6.conf.all.forwarding": "0",
        },
        detach=True,
        name=net_name,
        labels=labels,
    )
    # the internal net the sandbox shares with it
    resources["network"].connect(resources["sidecar"])
    resources["sidecar"].start()


def _read_sidecar_internal_address(sidecar: Any, net_name: str) -> tuple[bool, str]:
    # (3) read the sidecar's IP on the internal net; the sandbox proxies by IP.
    try:
        sidecar.reload()
        sidecar_running = getattr(sidecar, "status", "") == "running"
        proxy_ip = (
            sidecar.attrs.get("NetworkSettings", {})
            .get("Networks", {})
            .get(net_name, {})
            .get("IPAddress", "")
        )
    except Exception as exc:  # noqa: BLE001 — fixed diagnostic + outer cleanup
        raise SandboxUnavailableError(
            "sandbox policy sidecar internal address is unavailable; refusing sandbox start"
        ) from exc
    return sidecar_running, proxy_ip


def _configure_egress_proxy(
    svc: GvisorSandboxService,
    sidecar: Any,
    spec: SandboxSpec,
    deny_ips,
    net_name: str,
) -> tuple[dict[str, str], str | None]:
    from .. import gvisor

    # (2) replace the dead embedded resolver with public DNS over the (working) route.
    sidecar.exec_run(
        [
            "sh",
            "-c",
            'printf "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n" > /etc/resolv.conf',
        ]
    )
    # Inject the proxy script + launch it in the background on the sidecar.
    script = pathlib.Path(gvisor._egress_proxy_mod.__file__).read_bytes()
    gvisor._put_file(sidecar, "/", "egress_proxy.py", script)
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
    sidecar.exec_run(
        [
            "sh",
            "-c",
            f"exec {shlex.join(argv)} >/var/log/egress.log 2>/var/log/egress.err.log",
        ],
        detach=True,
    )
    ready = sidecar.exec_run(proxy_readiness_argv(EGRESS_PROXY_PORT), demux=True)
    if ready[0] != 0:
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
    svc: GvisorSandboxService,
    client: Any,
    spec: SandboxSpec,
    instance_id: str,
    conversation_id: str = "",
    *,
    inbound_only: bool = False,
) -> Any:
    from .. import gvisor

    net_name = f"{gvisor.EGR_NET_PREFIX}{instance_id}"
    labels = {gvisor.LABEL_CONV: conversation_id} if conversation_id else {}
    sidecar_cpu, sidecar_memory_mb, sidecar_pids_limit = _compute_sidecar_caps(svc)
    deny_ips = _compute_deny_ips(svc, inbound_only)
    # [P1 leak-guard] Setup runs BEFORE the guarded sandbox create in `_start_container`,
    # so if it creates the network/sidecar then fails partway (connect/start/proxy
    # inject), nothing downstream tears them down. Own the cleanup HERE: any exception
    # after either resource exists removes whatever was created before re-raising, so a
    # failed filtered-egress setup never strands an internal network or proxy sidecar.
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
            # Default sealed boxes need host-to-guest kernel control only.
            # No resolver, egress proxy, proxy environment, or relay is
            # installed, so the sandbox retains zero outbound transport.
            return network, sidecar, {}, net_name, None
        env, relay_url = _configure_egress_proxy(svc, sidecar, spec, deny_ips, net_name)
        return network, sidecar, env, net_name, relay_url
    except Exception:
        # Partial setup must not leak: tear down whatever already exists, then re-raise
        # so the caller surfaces the original failure.
        svc._best_effort_cleanup(resources["network"], resources["sidecar"])
        raise

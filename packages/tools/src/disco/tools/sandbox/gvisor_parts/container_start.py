"""`GvisorSandboxService._start_container` — the blocking Docker create path.

Maps infra failures to typed errors with the real cause. `host_workspace` is
a path on the DAEMON host (not necessarily local). Returns the container plus
its policy/control network, sidecar, workspace volume, and optional relay URL.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from .._container import (
    INTERNAL_PORTS,
    PUBLISHED_PORTS,
    _create_named_volume,
    _remove_container,
    _remove_volume,
    egress_mode,
    nofile_ulimits,
    resolve_bounds,
)
from .._effective_config import assert_effective_limits, assert_effective_runtime
from ..base import SandboxUnavailableError
from ..capability_relay import RelayConfigurationError, parse_upstream

if TYPE_CHECKING:
    from ..base import SandboxSpec
    from ..gvisor import GvisorSandboxService


def _validate_host_service_relay(svc: GvisorSandboxService, spec: SandboxSpec) -> None:
    if not spec.host_services:
        return
    try:
        relay_upstream = parse_upstream(svc._cfg.host_service_upstream)
    except RelayConfigurationError as exc:
        raise SandboxUnavailableError(str(exc)) from exc
    if relay_upstream.scheme != "https":
        raise SandboxUnavailableError(
            "container host-service relay requires a reachable HTTPS upstream; "
            "sidecar loopback is not the agent-server host"
        )


def _setup_egress_for_start(
    svc: GvisorSandboxService,
    client: Any,
    spec: SandboxSpec,
    instance_id: str,
    conversation_id: str,
    inbound_only: bool,
) -> tuple[Any, Any, dict[str, str], str, Any]:
    try:
        return svc._setup_filtered_egress(
            client,
            spec,
            instance_id,
            conversation_id,
            inbound_only=inbound_only,
        )
    except SandboxUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 — helper already removed partial resources
        operation = "inbound control" if inbound_only else "sandbox policy"
        raise SandboxUnavailableError(
            f"{operation} sidecar setup failed; refusing sandbox start"
        ) from exc


def _run_gvisor_container(
    svc: GvisorSandboxService,
    client: Any,
    vol_name: str,
    labels: dict[str, str],
    disk_mb: int,
    mem_mb: int,
    cpu: float,
    pids: int,
    environment: dict[str, str],
    container_name: str,
    net_kwargs: dict[str, Any],
) -> tuple[Any, Any]:
    from .. import gvisor

    volume = _create_named_volume(
        client.volumes,
        name=vol_name,
        labels=labels,
        disk_mb=disk_mb,
        workspace_uid=svc._cfg.workspace_uid,
    )
    container = client.containers.run(
        image=svc._cfg.image,
        command=gvisor._keepalive_command(),  # keepalive
        runtime=svc._cfg.runtime,  # gVisor
        ports=None,  # preview exposure (dev-server port only)
        mem_limit=f"{mem_mb}m",
        nano_cpus=int(cpu * 1_000_000_000),
        pids_limit=pids,  # EPIC H: cgroup pids.max — fork-bomb / host-PID guard
        ulimits=nofile_ulimits(svc._cfg),
        volumes={vol_name: {"bind": svc._cfg.container_workspace, "mode": "rw"}},
        # NO host env beyond capability-granted values. Filtered/public
        # boxes receive only proxy routing vars; sealed receives none.
        environment=environment,
        # Run every model-controlled process as the configured workspace
        # principal.  Merely owning the tmpfs mount as this UID is not
        # sufficient: without an explicit runtime user the image defaults
        # to root, and guest-side atomic-write staging turns every scaffold
        # back into a root-owned file.
        user=f"{svc._cfg.workspace_uid}:{svc._cfg.workspace_uid}",
        working_dir=svc._cfg.container_workspace,
        detach=True,
        name=container_name,
        labels=labels,
        **net_kwargs,
    )
    # `client.containers.run` reaches the SAME daemon the Podman backend does, and
    # `DISCO_LOCAL_ENGINE=docker` against a Podman compat socket is a documented,
    # reachable posture — so the pre-flight `_require_runtime` here is talking to
    # the transport that lies. Verify what was actually assigned, exactly as the
    # Podman create path does; the caller's cleanup removes the container on raise.
    # The keepalive command is all that has run at this point: no agent code.
    assert_effective_runtime(container, svc._cfg.runtime)
    assert_effective_limits(
        container,
        {
            "Memory": int(mem_mb) * 1024 * 1024,
            "NanoCpus": int(cpu * 1_000_000_000),
            "PidsLimit": int(pids),
        },
    )
    return volume, container


def _cleanup_failed_gvisor_start(
    svc: GvisorSandboxService,
    client: Any,
    container: Any,
    container_name: str,
    egress_network: Any,
    egress_sidecar: Any,
    volume: Any,
) -> None:
    if container is None:
        with contextlib.suppress(Exception):
            container = client.containers.get(container_name)
    with contextlib.suppress(Exception):
        _remove_container(container)
    # Don't leak the egress aux if the sandbox itself failed to start.
    svc._best_effort_cleanup(egress_network, egress_sidecar)
    with contextlib.suppress(Exception):
        _remove_volume(volume)


def start_container(
    svc: GvisorSandboxService,
    spec: SandboxSpec,
    instance_id: str,
    host_workspace: str,
    conversation_id: str = "",
) -> Any:
    from .. import gvisor

    client = svc._client()
    svc._require_runtime(client)
    svc._require_image(client)  # never-pull guard, before any run

    # NOTE: do NOT mkdir the workspace locally — the bind-mount source is a path
    # on the Docker DAEMON host, which Docker creates on demand. Making it here
    # would (wrongly) create it on whatever host runs the backend.
    # EPIC H (P1): the deployment config is the MAXIMUM, not a fallback. A
    # model-influenced spec may TIGHTEN cpu/mem/pids but can never loosen them above
    # the configured max nor disable the pids cap (pids=0 → default, never Docker's
    # "unlimited"); above-max is clamped down, negatives rejected. See resolve_bounds.
    cpu, mem_mb, pids, disk_mb = resolve_bounds(spec, svc._cfg)
    mode = egress_mode(spec)
    _validate_host_service_relay(svc, spec)

    # The sandbox never publishes directly. Every mode uses an internal
    # no-NAT network and a loopback-bound policy/control sidecar.
    labels = {gvisor.LABEL_CONV: conversation_id} if conversation_id else {}
    inbound_only = mode == "sealed" and not spec.host_services
    (
        egress_network,
        egress_sidecar,
        environment,
        net_name,
        host_service_relay_url,
    ) = _setup_egress_for_start(svc, client, spec, instance_id, conversation_id, inbound_only)
    # INTERNAL network: no bridge/NAT route is ever attached to the sandbox.
    net_kwargs = {"network": net_name}

    volume = None
    container = None
    container_name = f"{gvisor.SBX_NAME_PREFIX}{instance_id}"
    vol_name = f"{svc._cfg.workspace_volume_prefix}-{instance_id}"
    try:
        volume, container = _run_gvisor_container(
            svc,
            client,
            vol_name,
            labels,
            disk_mb,
            mem_mb,
            cpu,
            pids,
            environment,
            container_name,
            net_kwargs,
        )
        forward_ports = INTERNAL_PORTS if mode == "sealed" else PUBLISHED_PORTS
        svc._launch_inbound_forwarder(
            egress_sidecar,
            container,
            net_name,
            forward_ports,
        )
        return container, egress_network, egress_sidecar, volume, host_service_relay_url
    except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
        _cleanup_failed_gvisor_start(
            svc, client, container, container_name, egress_network, egress_sidecar, volume
        )
        if isinstance(exc, SandboxUnavailableError):
            raise
        raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

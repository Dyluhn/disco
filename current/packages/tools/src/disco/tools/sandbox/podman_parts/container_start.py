"""`PodmanSandboxService._start_container` — the blocking Podman work for `create`.

Image-by-load (never pull); limits via the socket; typed errors on failure.
Returns (container, name, egress_network, egress_sidecar, workspace_volume,
host_service_relay_url) — the aux refs cover sealed control-only and
filtered/public policy modes. `ContainerInstance.destroy()` tears them down
and removes the named workspace volume.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import tarfile
import time
from typing import TYPE_CHECKING, Any

from .._container import (
    INTERNAL_PORTS,
    PUBLISHED_PORTS,
    _create_named_volume,
    _remove_container,
    _remove_volume,
    egress_mode,
    inbound_readiness_argv,
    nofile_ulimits,
    resolve_bounds,
)
from ..base import SandboxUnavailableError
from ..capability_relay import RelayConfigurationError, parse_upstream

if TYPE_CHECKING:
    from ..base import SandboxSpec
    from ..podman import PodmanSandboxService

_CPU_PERIOD = 100_000  # cgroup CPU period (100ms); quota/period = cpus


def launch_inbound_forwarder(
    svc: PodmanSandboxService,
    sidecar: Any,
    container: Any,
    net_name: str,
    ports: frozenset[int],
) -> None:
    """[FIX6 parity — port of `gvisor._launch_inbound_forwarder`] Make a FILTERED
    box's preview ports host-reachable WITHOUT breaking containment. The sandbox is
    internal-only (no published port); the dual-homed egress SIDECAR publishes the
    preview ports (see `_setup_filtered_egress`) and runs a stdlib TCP forwarder that
    bridges each published host port to the sandbox's internal IP —
    `host:PORT -> <sandbox_ip>:PORT`. Transparent byte pipe → websockets / Vite HMR
    pass through; the sandbox keeps zero direct egress.

    Fail closed: a published sidecar binding without this forwarder is a false
    availability signal for both preview and the internal kernel transport.  The
    caller owns complete sandbox/sidecar/network/volume cleanup on any exception.

    Podman's archive API is the proven binary-safe injection path used by the
    egress proxy.  Use it here too: a live Podman remote accepted a 7.4-KiB
    inline-base64 detached exec and returned an exec id while silently never
    starting the command.  The subsequent short native ``exec --detach`` is
    bounded by an active listener-readiness probe.  A dead published transport
    fails sandbox creation instead of returning a knowingly broken preview/kernel
    path.  gVisor retains its separate docker ``exec_run(detach=True)`` delivery.
    """
    from .. import podman

    try:
        container.reload()
        sbx_ip = (
            container.attrs.get("NetworkSettings", {})
            .get("Networks", {})
            .get(net_name, {})
            .get("IPAddress", "")
        )
        if not sbx_ip:
            raise SandboxUnavailableError(
                "sandbox has no internal address; refusing dead inbound transport"
            )
        script = pathlib.Path(podman._inbound_forward_mod.__file__).read_bytes()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="inbound_forward.py")
            info.size = len(script)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(script))
        if not sidecar.put_archive("/", buf.getvalue()):
            raise SandboxUnavailableError(
                "failed to inject inbound transport forwarder; refusing sandbox start"
            )
        ports_arg = " ".join(str(p) for p in sorted(ports))
        launch_rc, _launch_out, _launch_err = svc._sidecar_cli_run(
            sidecar.name,
            [
                "sh",
                "-c",
                f"exec python3 /inbound_forward.py {sbx_ip} {ports_arg} "
                ">/var/log/inbound.log 2>/var/log/inbound.err.log",
            ],
            15,
            detach=True,
        )
        if launch_rc != 0:
            raise SandboxUnavailableError(
                "inbound transport forwarder launch failed; refusing sandbox start"
            )

        ready_rc, _ready_out, _ready_err = svc._sidecar_cli_run(
            sidecar.name, inbound_readiness_argv(ports), 7
        )
        if ready_rc != 0:
            raise SandboxUnavailableError(
                "inbound transport forwarder failed readiness; refusing sandbox start"
            )
    except SandboxUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 — fixed diagnostic, cleanup owned by caller
        raise SandboxUnavailableError(
            "inbound transport forwarder setup failed; refusing sandbox start"
        ) from exc


def _check_image_present(svc: PodmanSandboxService, client: Any) -> None:
    try:
        present = client.images.exists(svc._cfg.image)
    except Exception as exc:  # noqa: BLE001
        raise SandboxUnavailableError(f"could not query Podman images: {exc}") from exc
    if not present:
        raise SandboxUnavailableError(
            f"sandbox image {svc._cfg.image!r} is not present (load it with "
            "`podman load`; this backend never pulls from a registry)"
        )


def _validate_host_service_relay(svc: PodmanSandboxService, spec: SandboxSpec) -> None:
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
    svc: PodmanSandboxService,
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


def _run_podman_container(
    svc: PodmanSandboxService,
    client: Any,
    vol_name: str,
    labels: dict[str, str],
    disk_mb: int,
    mem_mb: int,
    cpu: float,
    pids: int,
    environment: dict[str, str],
    name: str,
    net_kwargs: dict[str, Any],
    resources: dict[str, Any],
) -> None:
    """Create the volume + container and start it, writing each into
    ``resources`` as it is created (NOT only on full success) — a partial
    failure here (e.g. ``container.start()`` raising) must still leave the
    caller able to see and tear down whatever already exists."""
    resources["volume"] = _create_named_volume(
        client.volumes,
        name=vol_name,
        labels=labels,
        disk_mb=disk_mb,
        workspace_uid=svc._cfg.workspace_uid,
    )
    resources["container"] = client.containers.create(
        image=svc._cfg.image,
        command=["sleep", "infinity"],  # keepalive
        mem_limit=f"{mem_mb}m",
        cpu_quota=int(cpu * _CPU_PERIOD),
        cpu_period=_CPU_PERIOD,
        pids_limit=pids,  # EPIC H: cgroup pids.max — fork-bomb / host-PID guard
        ulimits=nofile_ulimits(svc._cfg),
        **net_kwargs,
        volumes={vol_name: {"bind": svc._cfg.container_workspace, "mode": "rw"}},
        # NO host env leaks in. Filtered/public boxes receive only proxy
        # routing vars; sealed receives none.
        environment=environment,
        # The base image creates UID 1000 but intentionally has no USER
        # directive.  Pin the sandbox (not the privileged policy sidecar)
        # to the workspace principal so guest cp/mv staging preserves an
        # editable non-root ownership boundary.
        user=f"{svc._cfg.workspace_uid}:{svc._cfg.workspace_uid}",
        working_dir=svc._cfg.container_workspace,
        name=name,
        labels=labels,
        detach=True,
    )
    resources["container"].start()


def _cleanup_failed_podman_start(
    svc: PodmanSandboxService,
    client: Any,
    container: Any,
    name: str,
    egress_network: Any,
    egress_sidecar: Any,
    volume: Any,
) -> None:
    if container is None:
        with contextlib.suppress(Exception):
            container = client.containers.get(name)
    with contextlib.suppress(Exception):
        _remove_container(container)
    # Don't leak the egress aux if the sandbox itself failed to start
    # (E8: the parent class's teardown walks these refs).
    svc._best_effort_cleanup(egress_network=egress_network, egress_sidecar=egress_sidecar)
    with contextlib.suppress(Exception):
        _remove_volume(volume)


def start_container(
    svc: PodmanSandboxService, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
) -> tuple[Any, str, Any, Any, Any, str | None]:
    from .. import podman

    client = svc._client()
    _check_image_present(svc, client)

    # EPIC H (P1): config is the MAXIMUM, not a fallback. A model spec may tighten
    # cpu/mem/pids but never loosen them above the configured max nor disable the pids
    # cap (pids=0 → default, never "unlimited"). Enforced by the user@ systemd manager
    # via the socket (same path as mem/cpu). See resolve_bounds.
    cpu, mem_mb, pids, disk_mb = resolve_bounds(spec, svc._cfg)
    vol_name = f"{svc._cfg.workspace_volume_prefix}-{instance_id}"
    name = f"{podman.SBX_NAME_PREFIX}{instance_id}"

    # Per-mode network config — every guest uses an internal no-NAT network
    # and sidecar. Sealed uses control-only ingress; filtered/public also run
    # the policy proxy. No model-shaped spec reaches a raw guest bridge.
    mode = egress_mode(spec)
    _validate_host_service_relay(svc, spec)
    labels = {podman.LABEL_CONV: conversation_id} if conversation_id else {}
    inbound_only = mode == "sealed" and not spec.host_services
    (
        egress_network,
        egress_sidecar,
        environment,
        net_name,
        host_service_relay_url,
    ) = _setup_egress_for_start(svc, client, spec, instance_id, conversation_id, inbound_only)
    # podman-py's `containers.create` attaches the one internal network via
    # `networks`. `network_mode="bridge"` selects the rootless netns driver
    # (instead of pasta); it does not add the default bridge when an explicit
    # networks map is supplied. The guest's only interface is the internal
    # no-NAT network, including for sealed control-only boxes.
    net_kwargs = {"network_mode": "bridge", "networks": {net_name: {}}}

    resources: dict[str, Any] = {"volume": None, "container": None}
    try:
        _run_podman_container(
            svc,
            client,
            vol_name,
            labels,
            disk_mb,
            mem_mb,
            cpu,
            pids,
            environment,
            name,
            net_kwargs,
            resources,
        )
        volume, container = resources["volume"], resources["container"]
        # [FIX6 parity] sandbox is up + on the internal net — launch and prove
        # the inbound forwarder on the SIDECAR (host:PORT -> sandbox_ip:PORT).
        # A failure raises into this block's complete resource cleanup.
        forward_ports = INTERNAL_PORTS if mode == "sealed" else PUBLISHED_PORTS
        svc._launch_inbound_forwarder(
            egress_sidecar,
            container,
            net_name,
            forward_ports,
        )
        return (
            container,
            name,
            egress_network,
            egress_sidecar,
            volume,
            host_service_relay_url,
        )
    except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
        from podman.errors import ImageNotFound

        _cleanup_failed_podman_start(
            svc,
            client,
            resources["container"],
            name,
            egress_network,
            egress_sidecar,
            resources["volume"],
        )
        if isinstance(exc, SandboxUnavailableError):
            raise
        if isinstance(exc, ImageNotFound):
            raise SandboxUnavailableError(f"sandbox image {svc._cfg.image!r} not found") from exc
        raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

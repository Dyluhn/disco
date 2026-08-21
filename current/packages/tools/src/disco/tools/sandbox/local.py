"""Local container backend — the lowest-isolation tier (tool-sandbox-contract §5.1).

The proven Docker/OCI backend (`GvisorSandboxService`) pointed at a LOCAL container
socket with the standard `runc` runtime — no SSH, no remote host. The sandbox runs on
the same machine as the app: the simplest deployment (a stranger cloning the repo on
one Linux box) and the cross-platform target (Windows validation deferred).

This is almost entirely a config variant of the Docker backend — same docker-py client,
session model, exec/file-ops, capability sealing, never-pull, typed errors. The ONE
behavioral difference is the workspace: a per-run NAMED VOLUME rather than a remote-
daemon bind-mount. A named volume is portable across both local runtimes — rootless
Podman (reached via its Docker-compatible socket) can't bind-mount an auto-created host
path (the VM 202 lesson), and local Docker handles named volumes identically.

Isolation here is container-grade only (SHARED host kernel) — weaker than gVisor. See
`isolation.py`: this tier is honest about that and couples to a tighter confirmation
default, so the weak sandbox is never silently as permissive as the strong tier.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ._container import (
    INTERNAL_PORTS,
    PUBLISHED_PORTS,
    ContainerInstance,
    _create_named_volume,
    _remove_container,
    _remove_volume,
    egress_mode,
    nofile_ulimits,
    resolve_bounds,
)
from ._effective_config import assert_effective_limits, assert_effective_runtime
from .base import SandboxSpec, SandboxUnavailableError
from .capability_relay import RelayConfigurationError, parse_upstream
from .config import SandboxConfig, default_local_config
from .gvisor import GvisorSandboxService, _keepalive_command
from .naming import LABEL_CONV, SBX_NAME_PREFIX


class LocalSandboxInstance(ContainerInstance):
    """A running local container — shared ContainerInstance behavior. The workspace is
    a per-run named volume on the LOCAL host, removed during sandbox teardown."""


class LocalSandboxService(GvisorSandboxService):
    """[CONTRACT boundary] The local container position: the Docker/OCI backend on a
    LOCAL socket with `runc`. Lowest isolation (shared kernel) — `self.isolation`
    carries the honest label + the tighter, coupled confirmation default."""

    name = "local"
    _instance_cls = LocalSandboxInstance

    def __init__(self, config: SandboxConfig | None = None, *, client: Any | None = None) -> None:
        super().__init__(config or default_local_config(), client=client)

    def _start_container(
        self,
        spec: SandboxSpec,
        instance_id: str,
        host_workspace: str,
        conversation_id: str = "",
    ) -> Any:
        """Same Docker create as the parent, but the workspace is a per-run NAMED VOLUME
        (not the daemon bind-mount). `host_workspace` is unused on this tier — the
        volume is the portable choice across local Docker and rootless Podman.

        Egress posture (E8): mirrors the parent's three-way mode.
        • sealed   → internal no-NAT net + control-only sidecar; only
                     INTERNAL_PORTS are loopback-published.
        • filtered → allowlisting PROXY sidecar on an internal no-NAT net; the
                     allowlist is enforced by the proxy (not just a name). The
                     parent `_setup_filtered_egress` is reused unchanged (the
                     local tier drives the same docker-py client as gVisor).
        • public   → the same sidecar boundary with arbitrary public HTTP(S).
        Returns (container, egress_network, egress_sidecar) — the last two are
        non-None for filtered/public boxes; sealed gets (None, None)."""
        client = self._client()
        self._require_runtime(client)
        self._require_image(client)  # never-pull guard, before any run

        vol_name = f"{self._cfg.workspace_volume_prefix}-{instance_id}"
        # EPIC H (P1): config is the MAXIMUM — a model spec may tighten but never loosen
        # cpu/mem/pids above it, and pids=0 resolves to the default (never "unlimited").
        cpu, mem_mb, pids, disk_mb = resolve_bounds(spec, self._cfg)
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        mode = egress_mode(spec)
        self._validate_host_service_relay(spec)

        inbound_only = mode == "sealed" and not spec.host_services
        (
            egress_network,
            egress_sidecar,
            environment,
            net_name,
            host_service_relay_url,
        ) = self._setup_egress_for_start(client, spec, instance_id, conversation_id, inbound_only)
        net_kwargs = {"network": net_name}
        volume = None
        container = None
        container_name = f"{SBX_NAME_PREFIX}{instance_id}"
        try:
            volume = _create_named_volume(
                client.volumes,
                name=vol_name,
                labels=labels,
                disk_mb=disk_mb,
                workspace_uid=self._cfg.workspace_uid,
            )
            container = self._run_local_container(
                client, vol_name, mem_mb, cpu, pids, environment, container_name, labels, net_kwargs
            )
            forward_ports = INTERNAL_PORTS if mode == "sealed" else PUBLISHED_PORTS
            self._launch_inbound_forwarder(
                egress_sidecar,
                container,
                net_name,
                forward_ports,
            )
            return (
                container,
                egress_network,
                egress_sidecar,
                volume,
                host_service_relay_url,
            )
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            self._cleanup_failed_local_start(
                client, container, container_name, egress_sidecar, egress_network, volume
            )
            if isinstance(exc, SandboxUnavailableError):
                raise
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

    def _validate_host_service_relay(self, spec: SandboxSpec) -> None:
        if not spec.host_services:
            return
        try:
            relay_upstream = parse_upstream(self._cfg.host_service_upstream)
        except RelayConfigurationError as exc:
            raise SandboxUnavailableError(str(exc)) from exc
        if relay_upstream.scheme != "https":
            raise SandboxUnavailableError(
                "container host-service relay requires a reachable HTTPS upstream; "
                "sidecar loopback is not the agent-server host"
            )

    def _setup_egress_for_start(
        self,
        client: Any,
        spec: SandboxSpec,
        instance_id: str,
        conversation_id: str,
        inbound_only: bool,
    ) -> tuple[Any, Any, dict[str, str], str, Any]:
        try:
            return self._setup_filtered_egress(
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

    def _run_local_container(
        self,
        client: Any,
        vol_name: str,
        mem_mb: float,
        cpu: float,
        pids: int,
        environment: dict[str, str],
        container_name: str,
        labels: dict[str, str],
        net_kwargs: dict[str, Any],
    ) -> Any:
        # Bind `ports` at function-frame (the conditional expression above might
        # leave it unbound on an unrecognised mode — defense in depth).
        ports: dict[str, str | None] | None = None
        container = client.containers.run(
            image=self._cfg.image,
            # keepalive
            command=_keepalive_command(),
            runtime=self._cfg.runtime,  # runc (a value, not a branch)
            # The main container never publishes directly. Sealed boxes use
            # the sidecar only for INTERNAL_PORTS; other modes use the full
            # curated loopback set.
            ports=ports,
            # The limit goes through the LOCAL socket/daemon → it actually bites.
            mem_limit=f"{mem_mb}m",
            nano_cpus=int(cpu * 1_000_000_000),
            pids_limit=pids,  # EPIC H: cgroup pids.max — fork-bomb / host-PID guard
            ulimits=nofile_ulimits(self._cfg),
            volumes={vol_name: {"bind": self._cfg.container_workspace, "mode": "rw"}},
            # NO host env beyond capability-granted values. Filtered/public
            # boxes receive only proxy routing vars; sealed passes an empty
            # dict — no leaks.
            environment=environment,
            working_dir=self._cfg.container_workspace,
            detach=True,
            name=container_name,
            labels=labels,
            **net_kwargs,
        )
        # `DISCO_LOCAL_ENGINE=docker` + `DISCO_LOCAL_RUNTIME=runsc` against what is
        # actually Podman's compat socket is a documented, reachable posture, and
        # that transport advertises `runsc` in /info from a static candidate path
        # whether or not the binary exists — so the pre-flight `_require_runtime`
        # false-greens on exactly the engine that drops the field. Verify what was
        # really assigned, and that the cgroup bounds were really recorded.
        assert_effective_runtime(container, self._cfg.runtime)
        assert_effective_limits(
            container,
            {
                "Memory": int(mem_mb) * 1024 * 1024,
                "NanoCpus": int(cpu * 1_000_000_000),
                "PidsLimit": int(pids),
            },
        )
        return container

    def _cleanup_failed_local_start(
        self,
        client: Any,
        container: Any,
        container_name: str,
        egress_sidecar: Any,
        egress_network: Any,
        volume: Any,
    ) -> None:
        if container is None:
            with contextlib.suppress(Exception):
                container = client.containers.get(container_name)
        with contextlib.suppress(Exception):
            _remove_container(container)
        # Don't leak the egress aux if the sandbox itself failed to start (E8).
        if egress_sidecar is not None:
            try:
                _remove_container(egress_sidecar)
            except Exception:  # noqa: BLE001 — best-effort
                pass
        if egress_network is not None:
            try:
                egress_network.remove()
            except Exception:  # noqa: BLE001 — best-effort
                pass
        with contextlib.suppress(Exception):
            _remove_volume(volume)

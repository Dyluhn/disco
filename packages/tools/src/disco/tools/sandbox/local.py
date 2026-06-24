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

from typing import Any

from ._container import PUBLISHED_PORTS, ContainerInstance, egress_mode
from .base import SandboxSpec, SandboxUnavailableError
from .config import SandboxConfig, default_local_config
from .gvisor import GvisorSandboxService, _keepalive_command
from .naming import LABEL_CONV, SBX_NAME_PREFIX


class LocalSandboxInstance(ContainerInstance):
    """A running local container — shared ContainerInstance behavior. The workspace is
    a per-run named volume on the LOCAL host, persisting across the container."""


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
        • sealed   → `network_mode="none"`, no ports.
        • filtered → allowlisting PROXY sidecar on an internal no-NAT net; the
                     allowlist is enforced by the proxy (not just a name). The
                     parent `_setup_filtered_egress` is reused unchanged (the
                     local tier drives the same docker-py client as gVisor).
        • open     → `network_mode="bridge"` with the curated port set published.
        Returns (container, egress_network, egress_sidecar) — the last two are
        non-None ONLY for a filtered box; sealed/open get (None, None) and the
        inherited ContainerInstance teardown is a no-op on those refs."""
        client = self._client()
        self._require_runtime(client)
        self._require_image(client)  # never-pull guard, before any run

        vol_name = f"{self._cfg.workspace_volume_prefix}-{instance_id}"
        mem_mb = spec.memory_mb or self._cfg.default_memory_mb
        cpu = spec.cpu or self._cfg.default_cpu
        # EPIC H host-protection: cap container pids (cgroup pids.max) — fork-bomb guard.
        pids = spec.pids or self._cfg.default_pids_limit
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        mode = egress_mode(spec)
        # Per-mode network config (the three-way egress posture; egress_mode docstring).
        net_kwargs: dict[str, Any] = {}
        environment: dict[str, str] = {}
        egress_network = egress_sidecar = None
        net_name = ""  # set in the filtered branch; used by the Fix6 forwarder launch
        if mode == "filtered":
            # Reuse the gVisor proxy setup unchanged — same docker-py client, same
            # internal-network + sidecar pattern (the local tier just differs in its
            # workspace being a named volume, not a host bind).
            egress_network, egress_sidecar, environment, net_name = self._setup_filtered_egress(
                client, spec, instance_id, conversation_id
            )
            net_kwargs = {"network": net_name}
            ports: dict[str, str | None] | None = None  # inbound preview on internal net
        elif mode == "open":
            net_kwargs = {"network_mode": "bridge"}
            ports = {f"{p}/tcp": None for p in sorted(PUBLISHED_PORTS)}
        else:  # sealed
            net_kwargs = {"network_mode": "none"}
            ports = None
        # Bind `ports` at function-frame (the conditional expression above might
        # leave it unbound on an unrecognised mode — defense in depth).
        try:
            client.volumes.create(name=vol_name)  # auto-created; persists across the box
            container = client.containers.run(
                image=self._cfg.image,
                # keepalive
                command=_keepalive_command(),
                runtime=self._cfg.runtime,  # runc (a value, not a branch)
                # Publish the curated port set (declared at create — Docker can't
                # add mappings later). Only on `open`; the filtered and sealed
                # paths have no inbound preview (an internal net, or no net at all).
                ports=ports,
                # The limit goes through the LOCAL socket/daemon → it actually bites.
                mem_limit=f"{mem_mb}m",
                nano_cpus=int(cpu * 1_000_000_000),
                pids_limit=pids,  # EPIC H: cgroup pids.max — fork-bomb / host-PID guard
                volumes={vol_name: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                # NO host env beyond capability-granted values. For a filtered box
                # that's the proxy routing vars (defense in depth atop the no-route
                # network). Sealed / open pass an empty dict — no leaks.
                environment=environment,
                working_dir=self._cfg.container_workspace,
                detach=True,
                name=f"{SBX_NAME_PREFIX}{instance_id}",
                labels=labels,
                **net_kwargs,
            )
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            # Don't leak the egress aux if the sandbox itself failed to start (E8).
            if egress_sidecar is not None:
                try:
                    egress_sidecar.remove(force=True)
                except Exception:  # noqa: BLE001 — best-effort
                    pass
            if egress_network is not None:
                try:
                    egress_network.remove()
                except Exception:  # noqa: BLE001 — best-effort
                    pass
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc
        # [FIX6 podman-parity] The parent gVisor `_start_container` launches the inbound
        # preview forwarder on the sidecar after the sandbox starts; this override has
        # to do the same or a FILTERED box's published preview port forwards to nothing
        # (the sidecar publishes the port, but nothing listens on it → connection
        # refused → preview 502). Live-proven on the workstation's rootless podman: with
        # this call the host reaches the filtered box's preview via the sidecar; without
        # it, 502. Best-effort (never raises), same as the parent.
        if mode == "filtered" and egress_sidecar is not None:
            self._launch_inbound_forwarder(egress_sidecar, container, net_name)
        return container, egress_network, egress_sidecar

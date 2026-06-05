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

from ._container import ContainerInstance, sealed
from .base import SandboxSpec, SandboxUnavailableError
from .config import SandboxConfig, default_local_config
from .gvisor import GvisorSandboxService


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

    def _start_container(self, spec: SandboxSpec, instance_id: str, host_workspace: str) -> Any:
        """Same Docker create as the parent, but the workspace is a per-run NAMED VOLUME
        (not the daemon bind-mount). `host_workspace` is unused on this tier — the
        volume is the portable choice across local Docker and rootless Podman."""
        client = self._client()
        self._require_runtime(client)
        self._require_image(client)  # never-pull guard, before any run

        vol_name = f"{self._cfg.workspace_volume_prefix}-{instance_id}"
        mem_mb = spec.memory_mb or self._cfg.default_memory_mb
        cpu = spec.cpu or self._cfg.default_cpu

        try:
            client.volumes.create(name=vol_name)  # auto-created; persists across the box
            return client.containers.run(
                image=self._cfg.image,
                command=["sleep", "infinity"],  # keepalive: stays up for exec_shell
                runtime=self._cfg.runtime,  # runc (a value, not a branch)
                # Sealed by default: no network unless the capability set granted it.
                network_mode="none" if sealed(spec) else "bridge",
                # The limit goes through the LOCAL socket/daemon → it actually bites.
                mem_limit=f"{mem_mb}m",
                nano_cpus=int(cpu * 1_000_000_000),
                volumes={vol_name: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                environment={},  # NO host env leaks into the box
                working_dir=self._cfg.container_workspace,
                detach=True,
                name=f"pmx-sbx-{instance_id}",
            )
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

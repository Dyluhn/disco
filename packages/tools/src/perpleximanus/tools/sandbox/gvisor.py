"""gVisor sandbox backend — tool-sandbox-contract §5.1, driving a Docker host.

Fulfils `SandboxService`/`SandboxInstance` over Docker (docker-py), selecting the
`runsc` (gVisor) runtime. The session model + exec/file-op/lifecycle behavior is the
shared `ContainerInstance`; this module is just the gVisor `create` path.

Transport: the Docker endpoint is config-driven (`docker_socket`) — the LOCAL socket
when co-located, OR Docker-over-SSH (`ssh://user@host`, via the system ssh client,
e.g. keyless Tailscale SSH) when remote. File ops go through the container (exec/cp),
never a host path, so they work over either. docker-py is synchronous; every call is
run via `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import posixpath
import uuid
from typing import Any

from ._container import PREVIEW_PORT, ContainerInstance, sealed
from .base import SandboxInstance, SandboxSpec, SandboxUnavailableError
from .config import SandboxConfig, default_sandbox_config
from .isolation import IsolationProfile, isolation_for


def _keepalive_command(workspace: str, *, previewable: bool) -> list[str]:
    """The container's main process. Sealed → just a keepalive. Previewable → also start a
    static file server on PREVIEW_PORT serving the workspace, so whatever the agent writes
    is live in the preview immediately (the agent need not run a server itself)."""
    if not previewable:
        return ["sleep", "infinity"]
    serve = f"python3 -m http.server {PREVIEW_PORT} >/dev/null 2>&1"
    return ["sh", "-c", f"cd {workspace}; {serve} & exec sleep infinity"]


def _preview_host(docker_socket: str) -> str:
    """The host a published port is reachable at: localhost for a local socket; the
    remote Docker host (its tailnet IP) for Docker-over-SSH — that host serves the
    published port and is reachable over the proven keyless tailnet."""
    if docker_socket.startswith("ssh://"):
        host = docker_socket.removeprefix("ssh://").split("@")[-1]
        return host.split(":")[0].split("/")[0]  # strip any port / path
    return "localhost"


class GvisorSandboxInstance(ContainerInstance):
    """A running gVisor container — shared ContainerInstance behavior. The host
    bind-mount persists the workspace on the daemon host; the backend never touches
    that path directly (file ops go through the container)."""


class GvisorSandboxService:
    """[CONTRACT boundary] The Docker/OCI backend — creates instances on a Docker host
    via docker-py, selecting the configured runtime (gVisor `runsc` here). The local
    container backend (`LocalSandboxService`) reuses this whole class, differing only
    in its socket/runtime config and a named-volume workspace."""

    name = "gvisor"
    _instance_cls: type[ContainerInstance] = GvisorSandboxInstance

    def __init__(self, config: SandboxConfig | None = None, *, client: Any | None = None) -> None:
        self._cfg = config or default_sandbox_config()
        self._injected_client = client  # test seam: a fake docker client
        self._client_cache: Any | None = None
        self._instances: dict[str, ContainerInstance] = {}

    @property
    def isolation(self) -> IsolationProfile:
        """This backend's isolation profile (label + adversarial-safety + the
        isolation-coupled confirmation default), surfaced at the point of choice."""
        return isolation_for(self.name)

    def _client(self) -> Any:
        """The Docker client (lazy, cached). A connection failure is a typed
        infra error carrying the real cause — Docker isn't reachable."""
        if self._injected_client is not None:
            return self._injected_client
        if self._client_cache is None:
            try:
                import docker

                base_url = self._cfg.docker_socket
                kwargs: dict[str, Any] = {"base_url": base_url}
                if base_url.startswith("ssh://"):
                    # Docker-over-SSH: use the SYSTEM ssh client so the host's auth
                    # (e.g. keyless Tailscale SSH) applies, not docker-py's paramiko.
                    kwargs["use_ssh_client"] = True
                client = docker.DockerClient(**kwargs)
                client.ping()
            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
                raise SandboxUnavailableError(
                    f"Docker unreachable at {self._cfg.docker_socket}: {exc}"
                ) from exc
            self._client_cache = client
        return self._client_cache

    def _require_runtime(self, client: Any) -> None:
        """Fail loud if the configured runtime (runsc / runc / …) isn't registered on
        the host. A value, not a branch — the same check serves every Docker tier."""
        try:
            runtimes = client.info().get("Runtimes", {}) or {}
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailableError(f"could not query Docker runtimes: {exc}") from exc
        if self._cfg.runtime not in runtimes:
            raise SandboxUnavailableError(
                f"the {self._cfg.runtime!r} runtime is not configured on the Docker host"
            )

    def _require_image(self, client: Any) -> None:
        """Image is assumed PRESENT (load it first); a missing image fails loud and we
        NEVER pull. `containers.run` would otherwise silently pull from a registry —
        this guard turns that into a typed error before any run."""
        from docker.errors import ImageNotFound

        try:
            client.images.get(self._cfg.image)
        except ImageNotFound as exc:
            raise SandboxUnavailableError(
                f"sandbox image {self._cfg.image!r} not present on the host; this backend "
                f"never pulls (load it first)"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailableError(f"could not query the sandbox image: {exc}") from exc

    def _start_container(self, spec: SandboxSpec, instance_id: str, host_workspace: str) -> Any:
        """All the blocking Docker work for `create`, run in a thread. Maps infra
        failures to typed errors with the real cause. `host_workspace` is a path on
        the DAEMON host (not necessarily local)."""
        client = self._client()
        self._require_runtime(client)
        self._require_image(client)  # never-pull guard, before any run

        # NOTE: do NOT mkdir the workspace locally — the bind-mount source is a path
        # on the Docker DAEMON host, which Docker creates on demand. Making it here
        # would (wrongly) create it on whatever host runs the backend.
        mem_mb = spec.memory_mb or self._cfg.default_memory_mb
        cpu = spec.cpu or self._cfg.default_cpu
        # Publish ONLY the dev-server port, and only when network is granted (a sealed box
        # has no port to reach) — the preview is the forcing function for that posture.
        # Previewable boxes also auto-serve the workspace on PREVIEW_PORT so a built page is
        # live the moment the agent writes it (no need for the agent to run a server itself).
        ports = None if sealed(spec) else {f"{PREVIEW_PORT}/tcp": None}
        command = _keepalive_command(self._cfg.container_workspace, previewable=not sealed(spec))

        try:
            return client.containers.run(
                image=self._cfg.image,
                command=command,  # keepalive (+ a static preview server when previewable)
                runtime=self._cfg.runtime,  # gVisor
                # Sealed by default: no network unless the capability set granted it.
                network_mode="none" if sealed(spec) else "bridge",
                ports=ports,  # preview exposure (dev-server port only)
                mem_limit=f"{mem_mb}m",
                nano_cpus=int(cpu * 1_000_000_000),
                volumes={host_workspace: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                # NO host env: nothing from the agent-server's environment leaks in.
                # Only capability-granted values would be added here (none by default).
                environment={},
                working_dir=self._cfg.container_workspace,
                detach=True,
                name=f"pmx-sbx-{instance_id}",
            )
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        instance_id = f"sbx_{uuid.uuid4().hex}"
        host_workspace = posixpath.join(self._cfg.workspace_root, instance_id)
        container = await asyncio.to_thread(
            self._start_container, spec, instance_id, host_workspace
        )
        instance = self._instance_cls(
            id=instance_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
            container=container,
            container_workspace=self._cfg.container_workspace,
            stop_timeout_s=self._cfg.stop_timeout_s,
            workspace_uid=self._cfg.workspace_uid,
            preview_host=self._cfg.preview_host or _preview_host(self._cfg.docker_socket),
        )
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)

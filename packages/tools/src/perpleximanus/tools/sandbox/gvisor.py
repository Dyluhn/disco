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
import pathlib
import posixpath
import uuid
from typing import Any

from . import egress_proxy as _egress_proxy_mod
from ._container import (
    EGRESS_PROXY_PORT,
    PREVIEW_PORT,
    ContainerInstance,
    egress_mode,
    format_allow,
    proxy_env,
    proxy_run_argv,
    sealed,
)
from .base import SandboxInstance, SandboxSpec, SandboxUnavailableError
from .config import SandboxConfig, default_sandbox_config
from .isolation import IsolationProfile, isolation_for


def _keepalive_command() -> list[str]:
    """The container's main process. Always sleep infinity."""
    return ["sleep", "infinity"]


def _preview_host(docker_socket: str) -> str:
    """The host a published port is reachable at: localhost for a local socket; the
    remote Docker host (its tailnet IP) for Docker-over-SSH — that host serves the
    published port and is reachable over the proven keyless tailnet."""
    if docker_socket.startswith("ssh://"):
        host = docker_socket.removeprefix("ssh://").split("@")[-1]
        return host.split(":")[0].split("/")[0]  # strip any port / path
    return "localhost"


def _put_file(container: Any, dir_path: str, name: str, data: bytes) -> None:
    """`put_archive` a single file into a container dir — used to drop the
    stdlib-only egress proxy script into the sidecar (no image rebuild)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    if not container.put_archive(dir_path, buf.getvalue()):
        raise SandboxUnavailableError("failed to inject egress proxy script into sidecar")


class GvisorSandboxInstance(ContainerInstance):
    """A running gVisor container — shared ContainerInstance behavior. The host
    bind-mount persists the workspace on the daemon host; the backend never touches
    that path directly (file ops go through the container).

    Filtered-egress boxes also own an allowlisting proxy SIDECAR and an internal
    no-NAT network; both are torn down alongside the container so a run leaves no
    orphaned infra."""

    # Set by the backend for a "filtered" box; None otherwise.
    _egress_sidecar: Any | None = None
    _egress_network: Any | None = None

    async def destroy(self) -> None:
        # Tear down the main container first (shared logic), then the egress aux.
        await super().destroy()
        sidecar, network = self._egress_sidecar, self._egress_network
        if sidecar is None and network is None:
            return

        def _teardown_egress() -> None:
            if sidecar is not None:
                try:
                    sidecar.stop(timeout=2)
                except Exception:  # noqa: BLE001 — best-effort
                    pass
                try:
                    sidecar.remove(force=True)
                except Exception:  # noqa: BLE001 — already gone is fine
                    pass
            if network is not None:
                try:
                    network.remove()
                except Exception:  # noqa: BLE001 — already gone / still-attached is fine
                    pass

        await asyncio.to_thread(_teardown_egress)


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

    def _setup_filtered_egress(self, client: Any, spec: SandboxSpec, instance_id: str) -> Any:
        """[HARDWARE-UNVERIFIED — live-verify on VM 201] Stand up the allowlisting
        egress for a "filtered" box and return (network, sidecar, env, network_name).

        Containment design: an INTERNAL (no-NAT) network whose ONLY member with an
        outside route is the proxy sidecar. The sandbox is attached to this network
        alone, so its sole path to the internet is through the proxy — which refuses
        any host the allowlist doesn't name. The HTTP(S)_PROXY env is belt-and-
        suspenders; the no-route property is the real guarantee.

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
           IP."""
        net_name = f"pmx-egr-{instance_id}"
        network = client.networks.create(net_name, driver="bridge", internal=True)
        # (1) create on bridge → connect internal → start, so runsc sees BOTH NICs.
        sidecar = client.containers.create(
            image=self._cfg.image,
            command=["sh", "-c", "exec sleep infinity"],
            runtime=self._cfg.runtime,
            network="bridge",  # the route to the internet (the proxy's upstream)
            mem_limit="256m",
            detach=True,
            name=net_name,
        )
        network.connect(sidecar)  # the internal net the sandbox shares with it
        sidecar.start()
        # (2) replace the dead embedded resolver with public DNS over the (working) route.
        sidecar.exec_run(
            ["sh", "-c", 'printf "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n" > /etc/resolv.conf']
        )
        # Inject the proxy script + launch it in the background on the sidecar.
        script = pathlib.Path(_egress_proxy_mod.__file__).read_bytes()
        _put_file(sidecar, "/", "egress_proxy.py", script)
        allow = format_allow(spec.egress_allow)
        argv = proxy_run_argv(allow, EGRESS_PROXY_PORT)
        sidecar.exec_run(["sh", "-c", f"{' '.join(argv)} >/var/log/egress.log 2>&1 &"], detach=True)
        # (3) read the sidecar's IP on the internal net; the sandbox proxies by IP.
        sidecar.reload()
        proxy_ip = (
            sidecar.attrs.get("NetworkSettings", {})
            .get("Networks", {})
            .get(net_name, {})
            .get("IPAddress", "")
        ) or net_name  # fall back to the name (harmless for the fake/test path)
        env = proxy_env(proxy_ip, EGRESS_PROXY_PORT)
        return network, sidecar, env, net_name

    def _start_container(self, spec: SandboxSpec, instance_id: str, host_workspace: str) -> Any:
        """All the blocking Docker work for `create`, run in a thread. Maps infra
        failures to typed errors with the real cause. `host_workspace` is a path on
        the DAEMON host (not necessarily local). Returns (container, egress_aux),
        where egress_aux is (network, sidecar) for a filtered box else (None, None)."""
        client = self._client()
        self._require_runtime(client)
        self._require_image(client)  # never-pull guard, before any run

        # NOTE: do NOT mkdir the workspace locally — the bind-mount source is a path
        # on the Docker DAEMON host, which Docker creates on demand. Making it here
        # would (wrongly) create it on whatever host runs the backend.
        mem_mb = spec.memory_mb or self._cfg.default_memory_mb
        cpu = spec.cpu or self._cfg.default_cpu
        mode = egress_mode(spec)
        # Publish ONLY the dev-server port, and only when network is granted (a sealed box
        # has no port to reach) — the preview is the forcing function for that posture.
        ports = None if sealed(spec) else {f"{PREVIEW_PORT}/tcp": None}
        command = _keepalive_command()

        # Per-mode network config (the three-way egress posture; egress_mode docstring).
        net_kwargs: dict[str, Any] = {}
        environment: dict[str, str] = {}
        egress_network = egress_sidecar = None
        if mode == "filtered":
            egress_network, egress_sidecar, environment, net_name = self._setup_filtered_egress(
                client, spec, instance_id
            )
            net_kwargs = {"network": net_name}  # internal no-NAT net; proxy is the only route
            ports = None  # inbound preview not published on an internal net (live-verify TODO)
        elif mode == "open":
            net_kwargs = {"network_mode": "bridge"}  # explicit raw egress (NETWORK capability)
        else:  # sealed
            net_kwargs = {"network_mode": "none"}

        try:
            container = client.containers.run(
                image=self._cfg.image,
                command=command,  # keepalive
                runtime=self._cfg.runtime,  # gVisor
                ports=ports,  # preview exposure (dev-server port only)
                mem_limit=f"{mem_mb}m",
                nano_cpus=int(cpu * 1_000_000_000),
                volumes={host_workspace: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                # NO host env beyond capability-granted values. For a filtered box that's
                # the proxy routing vars (defense in depth atop the no-route network).
                environment=environment,
                working_dir=self._cfg.container_workspace,
                detach=True,
                name=f"pmx-sbx-{instance_id}",
                **net_kwargs,
            )
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            # Don't leak the egress aux if the sandbox itself failed to start.
            self._best_effort_cleanup(egress_network, egress_sidecar)
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc
        return container, egress_network, egress_sidecar

    @staticmethod
    def _best_effort_cleanup(network: Any, sidecar: Any) -> None:
        if sidecar is not None:
            try:
                sidecar.remove(force=True)
            except Exception:  # noqa: BLE001 — best-effort
                pass
        if network is not None:
            try:
                network.remove()
            except Exception:  # noqa: BLE001 — best-effort
                pass

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        instance_id = f"sbx_{uuid.uuid4().hex}"
        host_workspace = posixpath.join(self._cfg.workspace_root, instance_id)
        container, egress_network, egress_sidecar = await asyncio.to_thread(
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
        # Attach the filtered-egress aux so destroy() tears down the proxy + network.
        instance._egress_network = egress_network
        instance._egress_sidecar = egress_sidecar
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)

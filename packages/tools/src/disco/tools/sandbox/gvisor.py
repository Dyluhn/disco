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
import base64
import contextlib
import logging
import pathlib
import posixpath
import shlex
import uuid
from typing import Any

from . import egress_proxy as _egress_proxy_mod
from . import inbound_forward as _inbound_forward_mod
from ._container import (
    EGRESS_PROXY_PORT,
    PUBLISHED_PORTS,
    ContainerInstance,
    SshLoopbackTunnelManager,
    _create_named_volume,
    _remove_container,
    _remove_volume,
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
    resolve_bounds,
    sealed,
)
from .base import SandboxInstance, SandboxSpec, SandboxUnavailableError
from .config import SandboxConfig, default_sandbox_config
from .isolation import IsolationProfile, isolation_for
from .naming import (
    EGR_NET_PREFIX,
    LABEL_CONV,
    LABEL_CONV_KEYS,
    SBX_NAME_PREFIX,
    SBX_NAME_PREFIXES,
    conv_id_from_labels,
)

_LOG = logging.getLogger(__name__)


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


# [W-48 P1] Bound the docker-over-SSH preflight. docker-py's `use_ssh_client` path
# (transport.SSHSocket) builds its `ssh` command with NO ConnectTimeout/BatchMode, so
# an UNREACHABLE host leaves that ssh subprocess — and the `asyncio.to_thread` worker
# blocked reading its pipe — alive until ssh's OWN multi-minute TCP timeout, even
# though the asyncio `wait_for` around the probe returns bounded. Repeated bad-config
# probes then pile up leaked threads/subprocesses. We probe reachability with a
# bounded `ssh … true` BEFORE the docker-py call so the slow path can't leak.
_SSH_CONNECT_TIMEOUT_S = 8


def _ssh_probe_command(
    docker_socket: str, connect_timeout: int = _SSH_CONNECT_TIMEOUT_S
) -> list[str]:
    """Build a BOUNDED `ssh … true` reachability probe for an `ssh://user@host[:port]`
    Docker endpoint. Mirrors docker-py's own arg layout (`-l user`, `-p port`, `--`,
    host) but adds the two bounds docker-py's SSHSocket OMITS: `ConnectTimeout` (ssh
    itself gives up fast — no minutes-long hang) and `BatchMode=yes` (no interactive
    auth prompt that would hang the worker). Runs `true`, not the docker dial — a pure
    liveness check ahead of the leaky docker-py path."""
    rest = docker_socket.removeprefix("ssh://")
    user: str | None = None
    if "@" in rest:
        user, rest = rest.split("@", 1)
    host = rest.split("/", 1)[0]  # strip any trailing path
    port: str | None = None
    if ":" in host:
        host, port = host.split(":", 1)
    args = ["ssh", "-o", f"ConnectTimeout={connect_timeout}", "-o", "BatchMode=yes"]
    if user:
        args += ["-l", user]
    if port:
        args += ["-p", port]
    args += ["--", host, "true"]
    return args


def _probe_ssh_reachable(docker_socket: str, connect_timeout: int = _SSH_CONNECT_TIMEOUT_S) -> None:
    """Run the bounded SSH probe BEFORE handing the endpoint to docker-py, so a dead
    host raises a typed error in ~`connect_timeout`s and the leaky `use_ssh_client`
    path is never reached for it. `subprocess.run(timeout=…)` is a hard backstop: even
    if ssh ignored ConnectTimeout the child is killed and reaped, so the worker thread
    can't leak. Raises SandboxUnavailableError on any unreachable/failed probe."""
    import subprocess

    args = _ssh_probe_command(docker_socket, connect_timeout)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=connect_timeout + 4,  # backstop kill if ssh ignores ConnectTimeout
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailableError(
            f"Docker host unreachable over SSH at {docker_socket}: ssh probe timed out "
            f"after ~{connect_timeout}s (ConnectTimeout)"
        ) from exc
    except OSError as exc:  # ssh binary missing / spawn failure
        raise SandboxUnavailableError(
            f"could not run the SSH reachability probe for {docker_socket}: {exc}"
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        msg = tail[-1] if tail else f"ssh exited {proc.returncode}"
        raise SandboxUnavailableError(f"Docker host unreachable over SSH at {docker_socket}: {msg}")


def _put_file(container: Any, dir_path: str, name: str, data: bytes) -> None:
    """`put_archive` a single file into a container dir — used to drop the
    stdlib-only egress proxy script into the sidecar (no image rebuild)."""
    import io
    import tarfile
    import time

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        # [BP-00 root-cause] TarInfo defaults mtime to 0 (epoch 1970) — stamp reality.
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    if not container.put_archive(dir_path, buf.getvalue()):
        raise SandboxUnavailableError("failed to inject egress proxy script into sidecar")


class GvisorSandboxInstance(ContainerInstance):
    """A running gVisor container — shared ContainerInstance behavior. Its
    workspace is an ephemeral, quota-capped daemon volume; file ops reach it only
    through the container and teardown removes it.

    Filtered-egress boxes also own an allowlisting proxy SIDECAR and an internal
    no-NAT network; both are torn down alongside the container so a run leaves no
    orphaned infra. The aux teardown is inherited from ContainerInstance
    (`_teardown_egress_aux`); the service sets `instance._egress_sidecar` /
    `instance._egress_network` in create()."""


class GvisorSandboxService:
    """[CONTRACT boundary] The Docker/OCI backend — creates instances on a Docker host
    via docker-py, selecting the configured runtime (gVisor `runsc` here). The local
    container backend (`LocalSandboxService`) reuses this whole class, differing only
    in its socket/runtime config and a named-volume workspace."""

    name = "gvisor"
    # EPIC H (§1.4/§9.3): a container backend with its OWN PID + network namespace —
    # production / Build-Soak valid. LocalSandboxService (runc, shared host KERNEL but
    # still a private PID + net namespace) and PodmanSandboxService inherit/set the
    # same. Only the `process` dev backend is False.
    is_production_valid = True
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
                # Bound the socket timeout (Dispo #25): a hung daemon must fail
                # fast so its `to_thread` worker returns instead of leaking for
                # docker-py's 60s default. `reload()` keeps its tighter 0.5s guard.
                kwargs: dict[str, Any] = {
                    "base_url": base_url,
                    "timeout": self._cfg.client_timeout_s,
                    # This DockerClient is CACHED + SHARED across all concurrent sandboxes,
                    # and every op runs via asyncio.to_thread (many threads hit one client).
                    # docker-py's default urllib3 pool (max_pool_size=10) is too small for
                    # N concurrent builds × ops → connection contention surfaced as spurious
                    # 404 bursts on /containers/<id>/exec (misread as container death → needless
                    # recreate). Size the pool well above the build concurrency.
                    "max_pool_size": 64,
                }
                if base_url.startswith("ssh://"):
                    # Docker-over-SSH: use the SYSTEM ssh client so the host's auth
                    # (e.g. keyless Tailscale SSH) applies, not docker-py's paramiko.
                    # [W-48 P1] Probe reachability with a BOUNDED ssh first — docker-py's
                    # use_ssh_client path omits ConnectTimeout/BatchMode, so a dead host
                    # would leak the ssh subprocess + its to_thread worker for minutes.
                    _probe_ssh_reachable(base_url)
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

    def _setup_filtered_egress(
        self, client: Any, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
    ) -> Any:
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
        net_name = f"{EGR_NET_PREFIX}{instance_id}"
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        # EPIC H (P1) — runtime LAST GATE on the sidecar caps. SandboxConfig is hot-mutable, so
        # a post-construction `cfg.sidecar_cpu = 0` (etc.) bypasses the @field_validator and
        # would otherwise reach Docker as UNLIMITED. Gate BEFORE creating any network/sidecar
        # so a mutated cap is refused with nothing stranded.
        sidecar_cpu = bounded_sidecar_cap("cpu", self._cfg.sidecar_cpu)
        sidecar_memory_mb = int(bounded_sidecar_cap("memory_mb", self._cfg.sidecar_memory_mb))
        sidecar_pids_limit = int(bounded_sidecar_cap("pids", self._cfg.sidecar_pids_limit))
        deny_ips = collect_host_deny_ips(
            self._cfg.host_ip_blocklist,
            self._cfg.preview_host or _preview_host(self._cfg.docker_socket),
            include_local_interfaces=not self._cfg.docker_socket.startswith("ssh://"),
            additional_host_ips=(
                discover_remote_host_ips(self._cfg.docker_socket)
                if self._cfg.docker_socket.startswith("ssh://") and self._injected_client is None
                else frozenset()
            ),
        )
        # [P1 leak-guard] Setup runs BEFORE the guarded sandbox create in `_start_container`,
        # so if it creates the network/sidecar then fails partway (connect/start/proxy
        # inject), nothing downstream tears them down. Own the cleanup HERE: any exception
        # after either resource exists removes whatever was created before re-raising, so a
        # failed filtered-egress setup never strands an internal network or proxy sidecar.
        network: Any = None
        sidecar: Any = None
        try:
            # The network carries the same label as the containers so the orphan
            # sweep can find it — its NAME is instance-keyed, not conversation-keyed.
            network = client.networks.create(
                net_name, driver="bridge", internal=True, labels=labels
            )
            # (1) create on bridge → connect internal → start, so runsc sees BOTH NICs.
            # [FIX6] PUBLISH the preview ports on the SIDECAR (it's on bridge → it CAN
            # publish; the sandbox is internal-only and can't). The sidecar's inbound
            # forwarder (launched after the sandbox starts) bridges each published
            # host port to the sandbox's internal IP — host reaches the preview, the
            # sandbox keeps zero direct egress. Ports must be declared at create()
            # (docker can't add mappings to a running container).
            sidecar = client.containers.create(
                image=self._cfg.image,
                command=["sh", "-c", "exec sleep infinity"],
                runtime=self._cfg.runtime,
                network="bridge",  # the route to the internet (the proxy's upstream)
                ports=loopback_port_bindings(),
                # EPIC H (P1): bound the sidecar on CPU + PIDs too, not just memory — a wedged
                # or compromised proxy must not be able to burn host CPU or fork-bomb host PIDs.
                mem_limit=f"{sidecar_memory_mb}m",
                nano_cpus=int(sidecar_cpu * 1_000_000_000),
                pids_limit=sidecar_pids_limit,
                ulimits=nofile_ulimits(self._cfg),
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
            network.connect(sidecar)  # the internal net the sandbox shares with it
            sidecar.start()
            # (2) replace the dead embedded resolver with public DNS over the (working) route.
            sidecar.exec_run(
                [
                    "sh",
                    "-c",
                    'printf "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n" > /etc/resolv.conf',
                ]
            )
            # Inject the proxy script + launch it in the background on the sidecar.
            script = pathlib.Path(_egress_proxy_mod.__file__).read_bytes()
            _put_file(sidecar, "/", "egress_proxy.py", script)
            allow = format_allow(spec.egress_allow)
            argv = proxy_run_argv(
                allow,
                EGRESS_PROXY_PORT,
                public_only=egress_mode(spec) == "public",
                deny_ips=deny_ips,
            )
            sidecar.exec_run(
                ["sh", "-c", f"{shlex.join(argv)} >/var/log/egress.log 2>&1 &"], detach=True
            )
            ready = sidecar.exec_run(proxy_readiness_argv(EGRESS_PROXY_PORT), demux=True)
            if ready[0] != 0:
                raise SandboxUnavailableError(
                    "egress policy proxy failed its readiness check; refusing sandbox start"
                )
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
        except Exception:
            # Partial setup must not leak: tear down whatever already exists, then re-raise
            # so the caller surfaces the original failure.
            self._best_effort_cleanup(network, sidecar)
            raise

    def _launch_inbound_forwarder(self, sidecar: Any, container: Any, net_name: str) -> None:
        """[FIX6 — live-proven on real gVisor/runsc] Make a FILTERED box's preview
        ports host-reachable WITHOUT breaking containment. The sandbox is on an
        INTERNAL no-NAT net and runsc froze its netstack at boot (no NIC hot-plug),
        so it can NEVER publish a host port without a NAT bridge (= raw egress =
        broken containment). Instead the dual-homed egress SIDECAR (bridge+internal)
        already publishes the preview ports (see `_setup_filtered_egress`); here we
        run a stdlib TCP forwarder on it that bridges each published host port to
        the sandbox's internal IP — `host:PORT -> <sandbox_ip>:PORT`. Transparent
        byte pipe → websockets / Vite HMR pass through; the sandbox keeps zero
        direct egress.

        Best-effort: on ANY failure the preview is unreachable but the box still
        works (the agent-server surfaces the honest no-URL state), so we never
        raise here and leak the just-started sandbox.

        GOTCHA (found live): a detached `docker exec -d ... python3 -` drops stdin,
        so the script is delivered via `put_archive` (NOT piped on stdin) and only
        THEN launched detached."""
        try:
            container.reload()
            sbx_ip = (
                container.attrs.get("NetworkSettings", {})
                .get("Networks", {})
                .get(net_name, {})
                .get("IPAddress", "")
            )
            if not sbx_ip:
                _LOG.warning(
                    "filtered sandbox has no internal IP on %s; preview unreachable", net_name
                )
                return
            script = pathlib.Path(_inbound_forward_mod.__file__).read_bytes()
            # [FIX6 — live-found on real gVisor] Do NOT deliver the forwarder via a
            # separate `put_archive` + later `exec`: against a dual-homed runsc sidecar
            # that put intermittently returns success while the file is NOT visible to a
            # subsequently-exec'd process (the forwarder then dies with Errno 2 and the
            # preview is silently unreachable). Instead embed the script base64 and
            # decode+exec it in ONE shell — the write and the run share a process, so
            # there is no cross-exec gofer-visibility gap. `exec` makes python3 the
            # detached process itself (not a backgrounded child that gets reaped).
            b64 = base64.b64encode(script).decode("ascii")
            ports_arg = " ".join(str(p) for p in sorted(PUBLISHED_PORTS))
            sidecar.exec_run(
                [
                    "sh",
                    "-c",
                    f"echo {b64} | base64 -d > /inbound_forward.py && "
                    f"exec python3 /inbound_forward.py {sbx_ip} {ports_arg} "
                    f">/var/log/inbound.log 2>&1",
                ],
                detach=True,
            )
        except Exception:  # noqa: BLE001 — best-effort; preview unreachable, box still works
            _LOG.warning(
                "failed to launch the inbound preview forwarder on the egress sidecar",
                exc_info=True,
            )

    def _start_container(
        self, spec: SandboxSpec, instance_id: str, host_workspace: str, conversation_id: str = ""
    ) -> Any:
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
        # EPIC H (P1): the deployment config is the MAXIMUM, not a fallback. A
        # model-influenced spec may TIGHTEN cpu/mem/pids but can never loosen them above
        # the configured max nor disable the pids cap (pids=0 → default, never Docker's
        # "unlimited"); above-max is clamped down, negatives rejected. See resolve_bounds.
        cpu, mem_mb, pids, disk_mb = resolve_bounds(spec, self._cfg)
        mode = egress_mode(spec)
        # Publish the curated port set (Docker can't add mappings to a running
        # container, so the set is declared here), and only when network is
        # granted — a sealed box has no port to reach.
        ports = None if sealed(spec) else loopback_port_bindings()
        command = _keepalive_command()

        # Per-mode network config (the three-way egress posture; egress_mode docstring).
        net_kwargs: dict[str, Any] = {}
        environment: dict[str, str] = {}
        egress_network = egress_sidecar = None
        egress_net_name = ""
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        if mode in {"filtered", "public"}:
            egress_network, egress_sidecar, environment, net_name = self._setup_filtered_egress(
                client, spec, instance_id, conversation_id
            )
            egress_net_name = net_name
            net_kwargs = {"network": net_name}  # internal no-NAT net; proxy is the only route
            # The sandbox itself publishes NOTHING (it's on an internal no-NAT net and
            # runsc can't hot-plug a NIC). [FIX6] the preview is reached via the egress
            # SIDECAR's published ports + an inbound forwarder, launched below.
            ports = None
        else:  # sealed
            net_kwargs = {"network_mode": "none"}

        volume = None
        container = None
        container_name = f"{SBX_NAME_PREFIX}{instance_id}"
        vol_name = f"{self._cfg.workspace_volume_prefix}-{instance_id}"
        try:
            volume = _create_named_volume(
                client.volumes,
                name=vol_name,
                labels=labels,
                disk_mb=disk_mb,
                workspace_uid=self._cfg.workspace_uid,
            )
            container = client.containers.run(
                image=self._cfg.image,
                command=command,  # keepalive
                runtime=self._cfg.runtime,  # gVisor
                ports=ports,  # preview exposure (dev-server port only)
                mem_limit=f"{mem_mb}m",
                nano_cpus=int(cpu * 1_000_000_000),
                pids_limit=pids,  # EPIC H: cgroup pids.max — fork-bomb / host-PID guard
                ulimits=nofile_ulimits(self._cfg),
                volumes={vol_name: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                # NO host env beyond capability-granted values. For a filtered box that's
                # the proxy routing vars (defense in depth atop the no-route network).
                environment=environment,
                working_dir=self._cfg.container_workspace,
                detach=True,
                name=container_name,
                labels=labels,
                **net_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            if container is None:
                with contextlib.suppress(Exception):
                    container = client.containers.get(container_name)
            with contextlib.suppress(Exception):
                _remove_container(container)
            # Don't leak the egress aux if the sandbox itself failed to start.
            self._best_effort_cleanup(egress_network, egress_sidecar)
            with contextlib.suppress(Exception):
                _remove_volume(volume)
            if isinstance(exc, SandboxUnavailableError):
                raise
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc
        # [FIX6] sandbox is up and on the internal net — launch the inbound preview
        # forwarder on the SIDECAR (host:PORT -> sandbox_ip:PORT). Best-effort: never
        # raises, so it can't leak the just-started box.
        if mode in {"filtered", "public"} and egress_sidecar is not None:
            self._launch_inbound_forwarder(egress_sidecar, container, egress_net_name)
        return container, egress_network, egress_sidecar, volume

    @staticmethod
    def _best_effort_cleanup(network: Any, sidecar: Any) -> None:
        if sidecar is not None:
            try:
                _remove_container(sidecar)
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
        started = await asyncio.to_thread(
            self._start_container, spec, instance_id, host_workspace, conversation_id
        )
        if len(started) == 3:
            container, egress_network, egress_sidecar = started
            workspace_volume = None
        else:
            container, egress_network, egress_sidecar, workspace_volume = started
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
            # Wedge-guard timeout (Dispo #25). Snapshotted from the live config
            # at create. Hot-apply: mutate `cfg.reload_timeout_s` BEFORE creating
            # new instances and they pick up the new value; for live-instance
            # updates, the service caller can also assign `inst._reload_timeout_s
            # = new_value` directly.
            reload_timeout_s=self._cfg.reload_timeout_s,
            workspace_volume=workspace_volume,
            loopback_tunnel=(
                SshLoopbackTunnelManager(self._cfg.docker_socket)
                if self._cfg.docker_socket.startswith("ssh://") and self._injected_client is None
                else None
            ),
        )
        # Attach the filtered-egress aux so destroy() tears down the proxy + network.
        instance._egress_network = egress_network
        instance._egress_sidecar = egress_sidecar
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)

    async def healthcheck(self) -> None:
        """[W-48] Connectivity preflight: ping the configured Docker endpoint
        (`docker_socket` — a local socket OR `ssh://user@host` over keyless Tailscale
        SSH) and confirm the selected runtime (`runsc` for gVisor, `runc` for the
        local tier) is registered on that host. Raises a typed
        ``SandboxUnavailableError`` NAMING the endpoint + reason on failure
        (`_client()` → "Docker unreachable at <endpoint>: …"; `_require_runtime` →
        "the '<runtime>' runtime is not configured on the Docker host"); returns None
        on success. The blocking docker-py calls run in a thread and are bounded by
        `client_timeout_s`, so an unreachable host fails fast instead of hanging."""

        def _probe() -> None:
            client = self._client()  # typed: "Docker unreachable at <endpoint>: …"
            self._require_runtime(client)  # typed: "<runtime> not configured …"

        await asyncio.to_thread(_probe)

    async def list_live_instances(self) -> list[str]:
        """Return conversation_ids of all live pmx-sbx-* containers on this backend.

        Side effect (documented, deliberate): a pmx-sbx-* container WITHOUT a
        pmx.conversation_id label predates the label scheme — it cannot be
        correlated to any conversation, so it is unreachable garbage by
        construction (session handles are in-memory; recreation always builds a
        fresh instance). These legacy orphans are reaped on sight — they are
        exactly the population that OOM-wedged VM-201 (34 unlabeled containers,
        2026-06-10); a label-only sweep would skip every one of them."""

        def _list() -> list[str]:
            try:
                client = self._client()
                # Dual-read: sweep BOTH the current `disco-sbx-` and legacy
                # `pmx-sbx-` name prefixes so a rename never strands a container
                # started under the old scheme. Dedup by container id (a name
                # filter is a substring match, so the two queries can't overlap
                # here, but the dedup keeps this robust to filter semantics).
                seen_ids: set[str] = set()
                containers = []
                for prefix in SBX_NAME_PREFIXES:
                    for c in client.containers.list(filters={"name": prefix}):
                        if c.id not in seen_ids:
                            seen_ids.add(c.id)
                            containers.append(c)
                result = []
                for c in containers:
                    cid = conv_id_from_labels(c.labels)
                    if cid:
                        result.append(cid)
                        continue
                    try:  # legacy/unlabeled: reap on sight (see docstring)
                        _LOG.info("reaping unlabeled legacy sandbox container %s", c.name)
                        c.stop(timeout=2)
                        _remove_container(c)
                    except Exception:  # noqa: BLE001 — best-effort
                        pass
                return result
            except Exception:  # noqa: BLE001 — docker unreachable at startup is non-fatal
                return []

        return await asyncio.to_thread(_list)

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Destroy all pmx-sbx-* and pmx-egr-* containers labelled with this conversation_id."""

        def _destroy() -> None:
            try:
                client = self._client()
                # Dual-read: match BOTH the current `disco.conversation_id` and
                # legacy `pmx.conversation_id` label keys (union, dedup by id) so
                # a container/network started under the old label scheme is still
                # torn down after the rename.
                seen_ids: set[str] = set()
                workspace_volume_names: set[str] = set()
                for key in LABEL_CONV_KEYS:
                    for c in client.containers.list(
                        all=True,
                        filters={"label": f"{key}={conversation_id}"},
                    ):
                        if c.id in seen_ids:
                            continue
                        seen_ids.add(c.id)
                        name = str(getattr(c, "name", "") or "").lstrip("/")
                        for prefix in SBX_NAME_PREFIXES:
                            if name.startswith(prefix):
                                workspace_volume_names.add(
                                    f"{self._cfg.workspace_volume_prefix}-{name[len(prefix) :]}"
                                )
                                break
                        try:
                            c.stop(timeout=2)
                        except Exception:  # noqa: BLE001 — already stopped is fine
                            pass
                        try:
                            _remove_container(c)
                        except Exception:  # noqa: BLE001 — already gone is fine
                            pass
                # Clean up the egress internal network(s) by LABEL — networks are
                # named disco-egr-{instance_id}, which has NO relation to the
                # conversation_id, so a name match can never work. Labels are set
                # at create time (same conversation label as the containers);
                # containers were removed above, so the network is detachable.
                seen_nets: set[str] = set()
                for key in LABEL_CONV_KEYS:
                    for net in client.networks.list(filters={"label": f"{key}={conversation_id}"}):
                        if net.id in seen_nets:
                            continue
                        seen_nets.add(net.id)
                        try:
                            net.remove()
                        except Exception:  # noqa: BLE001 — in-use or gone
                            pass
                # LocalSandboxService inherits this release path and uses per-run
                # named workspace volumes. Remove labeled volumes after containers
                # have gone so no Podman/Docker volume lock survives release.
                volumes = getattr(client, "volumes", None)
                if volumes is not None:
                    seen_vols: set[str] = set()
                    for key in LABEL_CONV_KEYS:
                        try:
                            candidates = volumes.list(filters={"label": f"{key}={conversation_id}"})
                        except Exception:  # noqa: BLE001 — client lacks volume listing
                            continue
                        for vol in candidates:
                            name = getattr(vol, "name", "") or getattr(vol, "id", "")
                            if name in seen_vols:
                                continue
                            seen_vols.add(name)
                            try:
                                _remove_volume(vol)
                            except Exception:  # noqa: BLE001 — already gone / in use
                                pass
                    for name in workspace_volume_names:
                        if name in seen_vols:
                            continue
                        try:
                            vol = volumes.get(name)
                        except Exception:  # noqa: BLE001 — missing or unsupported
                            continue
                        seen_vols.add(name)
                        try:
                            _remove_volume(vol)
                        except Exception:  # noqa: BLE001 — already gone / in use
                            pass
            except Exception:  # noqa: BLE001 — docker unreachable: best-effort
                pass

        await asyncio.to_thread(_destroy)

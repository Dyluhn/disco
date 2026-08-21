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
import socket
import urllib.parse
import uuid
from typing import Any

# MODULE SURFACE, PRESERVED (Epic 10-B) ------------------------------------------
#
# `gvisor_parts` resolves this module's names through the parent at call time (the
# standing §13 rule, which keeps test monkeypatches effective) — e.g.
# `gvisor.EGR_NET_PREFIX`, `gvisor.LABEL_CONV_KEYS`, `gvisor.SBX_NAME_PREFIXES` and
# `gvisor._egress_proxy_mod.__file__`. Ruff cannot see those uses, so every such
# name is bound here in the redundant-alias re-export form (`X as X`), which is
# ruff's sanctioned re-export marker and needs no per-line suppression. The
# `._container` / `.capability_relay` re-exports below are this module's inherited
# surface, preserved unchanged across the extraction.
from . import capability_relay as _capability_relay_mod
from . import egress_proxy as egress_proxy
from . import inbound_forward as _inbound_forward_mod
from ._container import EGRESS_PROXY_PORT as EGRESS_PROXY_PORT
from ._container import INTERNAL_PORTS as INTERNAL_PORTS
from ._container import (
    PUBLISHED_PORTS,
    ContainerInstance,
    SshLoopbackTunnelManager,
    _remove_container,
    inbound_readiness_argv,
    reconcile_orphan_aux_resources,
)
from ._container import _create_named_volume as _create_named_volume
from ._container import _remove_volume as _remove_volume
from ._container import bounded_sidecar_cap as bounded_sidecar_cap
from ._container import collect_host_deny_ips as collect_host_deny_ips
from ._container import discover_remote_host_ips as discover_remote_host_ips
from ._container import egress_mode as egress_mode
from ._container import format_allow as format_allow
from ._container import loopback_port_bindings as loopback_port_bindings
from ._container import nofile_ulimits as nofile_ulimits
from ._container import proxy_env as proxy_env
from ._container import proxy_readiness_argv as proxy_readiness_argv
from ._container import proxy_run_argv as proxy_run_argv
from ._container import resolve_bounds as resolve_bounds
from ._container import sealed as sealed
from ._effective_config import cgroup_enforcement_warning
from .base import SandboxInstance, SandboxSpec, SandboxUnavailableError
from .capability_relay import (
    HOST_SERVICE_RELAY_PORT,
    relay_readiness_argv,
    relay_run_argv,
)
from .capability_relay import RelayConfigurationError as RelayConfigurationError
from .capability_relay import parse_upstream as parse_upstream
from .config import SandboxConfig, default_sandbox_config
from .gvisor_parts import container_start as _container_start_part
from .gvisor_parts import conversation_cleanup as _conversation_cleanup_part
from .gvisor_parts import egress_setup as _egress_setup_part
from .isolation import IsolationProfile, isolation_for
from .naming import EGR_NET_PREFIX as EGR_NET_PREFIX
from .naming import LABEL_CONV as LABEL_CONV
from .naming import LABEL_CONV_KEYS as LABEL_CONV_KEYS
from .naming import SBX_NAME_PREFIX as SBX_NAME_PREFIX
from .naming import (
    SBX_NAME_PREFIXES,
    conv_id_from_labels,
)

# This module's long-standing private alias for the egress-proxy module;
# `gvisor_parts.egress_setup` reads `gvisor._egress_proxy_mod.__file__` at call time.
# Bound from the re-export above so neither name needs a suppression.
_egress_proxy_mod = egress_proxy

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


def _probe_local_socket_reachable(docker_socket: str, timeout_s: float) -> None:
    """Own the local UDS connect attempt before DockerClient's constructor does.

    docker-py's UnixHTTPConnection creates a socket inside ``connect`` and raises
    before retaining it when the daemon path is unavailable.  That orphan cannot
    be closed by a caller because DockerClient construction itself never returns.
    A context-managed preflight prevents that SDK path from being entered for an
    unreachable local daemon.
    """
    parsed = urllib.parse.urlparse(docker_socket)
    if parsed.scheme not in {"unix", "http+unix"}:
        return
    path = urllib.parse.unquote(parsed.path or parsed.netloc)
    if not path:
        raise SandboxUnavailableError(f"Docker local socket endpoint has no path: {docker_socket}")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout_s)
            probe.connect(path)
    except OSError as exc:
        raise SandboxUnavailableError(
            f"Docker local socket unreachable at {docker_socket}: {exc}"
        ) from exc


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

    Every box also owns an internal no-NAT network and a hardened SIDECAR. Sealed
    boxes use it only for loopback-bound kernel control ingress; filtered/public
    boxes additionally run the egress policy proxy. Both resources are torn down
    alongside the container so a run leaves no orphaned infra. Aux teardown is
    inherited from ContainerInstance
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
            client: Any | None = None
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
                else:
                    _probe_local_socket_reachable(base_url, float(self._cfg.client_timeout_s))
                client = docker.DockerClient(**kwargs)
                client.ping()
            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.close()
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
        # A runtime registered with `--runtime-flag ignore-cgroups` records every
        # requested cap and enforces none, so neither the create call nor the
        # post-create HostConfig inspection can see it — the registered arguments
        # are the only place that posture is visible. WARN rather than refuse: this
        # project's own self-host guide documents the flag as the rootless-runsc
        # workaround, so failing closed would break installs it told people to make.
        warning = cgroup_enforcement_warning(runtimes, self._cfg.runtime)
        if warning:
            _LOG.error(
                "sandbox.cgroup_enforcement_disabled runtime=%s engine=%s: %s",
                self._cfg.runtime,
                self._cfg.docker_socket,
                warning,
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
        self,
        client: Any,
        spec: SandboxSpec,
        instance_id: str,
        conversation_id: str = "",
        *,
        inbound_only: bool = False,
    ) -> Any:
        """[HARDWARE-UNVERIFIED — live-verify on VM 201] Stand up the internal
        network + policy/control sidecar and return its topology; see
        `gvisor_parts.egress_setup` for the full contract (the three
        VM-201-verified non-obvious details live there)."""
        return _egress_setup_part.setup_filtered_egress(
            self, client, spec, instance_id, conversation_id, inbound_only=inbound_only
        )

    def _launch_capability_relay(
        self, sidecar: Any, sidecar_ip: str, spec: SandboxSpec
    ) -> str | None:
        """Launch the capability-only listener on the existing dual-homed sidecar.

        The relay script owns the strict route/framing policy and one fixed upstream.
        Any inject, launch, or readiness failure aborts provisioning (fail closed).
        """
        if not spec.host_services:
            return None
        script = pathlib.Path(_capability_relay_mod.__file__).read_bytes()
        _put_file(sidecar, "/", "capability_relay.py", script)
        authority = f"{sidecar_ip}:{HOST_SERVICE_RELAY_PORT}"
        argv = relay_run_argv(self._cfg.host_service_upstream, authority)
        sidecar.exec_run(
            ["sh", "-c", f"exec {shlex.join(argv)} >/dev/null 2>/dev/null"], detach=True
        )
        ready = sidecar.exec_run(relay_readiness_argv(sidecar_ip), demux=True)
        if ready[0] != 0:
            raise SandboxUnavailableError(
                "host-service capability relay failed readiness; refusing sandbox start"
            )
        return f"http://{authority}"

    def _launch_inbound_forwarder(
        self,
        sidecar: Any,
        container: Any,
        net_name: str,
        ports: frozenset[int] = PUBLISHED_PORTS,
    ) -> None:
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

        Fail closed: a missing internal address, launch failure, or listener
        readiness failure aborts provisioning. The caller owns complete cleanup.

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
                raise SandboxUnavailableError(
                    "sandbox has no internal address; refusing dead inbound transport"
                )
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
            ports_arg = " ".join(str(p) for p in sorted(ports))
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
            ready = sidecar.exec_run(inbound_readiness_argv(ports), demux=True)
            if ready[0] != 0:
                raise SandboxUnavailableError(
                    "inbound transport forwarder failed readiness; refusing sandbox start"
                )
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — fixed diagnostic, cleanup owned by caller
            raise SandboxUnavailableError(
                "inbound transport forwarder setup failed; refusing sandbox start"
            ) from exc

    def _start_container(
        self, spec: SandboxSpec, instance_id: str, host_workspace: str, conversation_id: str = ""
    ) -> Any:
        """All the blocking Docker work for `create`, run in a thread; see
        `gvisor_parts.container_start` for the full contract."""
        return _container_start_part.start_container(
            self, spec, instance_id, host_workspace, conversation_id
        )

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
        host_service_relay_url = None
        if len(started) == 3:
            container, egress_network, egress_sidecar = started
            workspace_volume = None
        elif len(started) == 4:
            container, egress_network, egress_sidecar, workspace_volume = started
        else:
            (
                container,
                egress_network,
                egress_sidecar,
                workspace_volume,
                host_service_relay_url,
            ) = started
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
        # Attach the policy/control aux so destroy() tears down sidecar + network.
        instance._egress_network = egress_network
        instance._egress_sidecar = egress_sidecar
        instance._host_service_relay_url = host_service_relay_url
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
        """Return conversation IDs for live current/legacy sandbox containers.

        Side effect (documented, deliberate): a pmx-sbx-* container WITHOUT a
        pmx.conversation_id label predates the label scheme — it cannot be
        correlated to any conversation, so it is unreachable garbage by
        construction (session handles are in-memory; recreation always builds a
        fresh instance). These legacy orphans are reaped on sight — they are
        exactly the population that OOM-wedged VM-201 (34 unlabeled containers,
        2026-06-10); a label-only sweep would skip every one of them. H220 also
        reconciles aged, detached, product-labeled auxiliary networks/volumes
        whose sandbox and sidecar containers no longer exist."""

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
                reconcile_orphan_aux_resources(
                    client,
                    workspace_volume_prefix=self._cfg.workspace_volume_prefix,
                )
                return result
            except Exception:  # noqa: BLE001 — docker unreachable at startup is non-fatal
                return []

        return await asyncio.to_thread(_list)

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Destroy all pmx-sbx-* and pmx-egr-* containers labelled with this
        conversation_id; see `gvisor_parts.conversation_cleanup` for the full
        contract."""
        await _conversation_cleanup_part.destroy_by_conversation(self, conversation_id)

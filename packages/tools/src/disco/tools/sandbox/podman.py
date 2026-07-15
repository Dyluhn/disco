"""Podman sandbox backend — tool-sandbox-contract §5.1, the remote/SSH-class path.

Fulfils `SandboxService`/`SandboxInstance` over Podman's NATIVE REMOTE, reached
keyless over Tailscale SSH. Same interface as the gVisor backend — second transport
(SSH) + runtime (crun). Shares the lifecycle/jail with `ContainerInstance`.

Three load-bearing Podman constraints:

  1. NATIVE REMOTE over the socket, NEVER a bare-SSH `podman run`. Driving Podman
     THROUGH THE SOCKET means limits are enforced by the `user@` systemd manager; a
     `podman run` over a non-login SSH session silently falls back to cgroupfs and
     limits DON'T apply (verified live: a socket-run container is OOM-bounded). Every
     op here goes through the socket.

  2. IMAGE BY LOAD, NEVER PULL — present-or-fail-loud (SandboxUnavailableError), and
     `containers.create` (not `run`), so nothing ever pulls from a registry.

  3. Workspace = a per-run NAMED VOLUME (Podman doesn't auto-create bind sources and
     rootless can't write root-owned host paths). Auto-created and removed on sandbox
     teardown.

Transport split (a real podman-py-vs-docker-py difference, verified live): podman-py
drives create/lifecycle/volumes/images/`put_archive` cleanly, but its `exec_run`
does NOT work over the remote API — it returns no output, doesn't block, and gives a
bogus exit code. So command execution + read/list go through the podman CLI's native
remote (`podman --url ssh://…//socket exec`), which captures stdout/stderr, waits,
and reports the real exit code — still socket-mediated (limits apply), not bare SSH.
The host running this backend needs the `podman` CLI + system `ssh` on PATH.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import pathlib
import posixpath
import shlex
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import capability_relay as _capability_relay_mod
from . import egress_proxy as _egress_proxy_mod
from . import inbound_forward as _inbound_forward_mod
from ._container import (
    EGRESS_PROXY_PORT,
    PUBLISHED_PORTS,
    SANDBOX_READ_TIMEOUT_S,
    TIMEOUT_EXIT_CODES,
    ContainerInstance,
    SshLoopbackTunnelManager,
    _create_named_volume,
    _remove_container,
    _remove_volume,
    bounded_exec_argv,
    bounded_read_argv,
    bounded_read_result,
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
from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxSpec,
    SandboxUnavailableError,
    raise_read_error,
)
from .capability_relay import (
    HOST_SERVICE_RELAY_PORT,
    RelayConfigurationError,
    parse_upstream,
    relay_readiness_argv,
    relay_run_argv,
)
from .config import SandboxConfig, default_podman_config
from .naming import (
    EGR_NET_PREFIX,
    LABEL_CONV,
    LABEL_CONV_KEYS,
    SBX_NAME_PREFIX,
    SBX_NAME_PREFIXES,
    conv_id_from_labels,
)

# `podman exec` stderr markers that mean the CONTAINER is gone (not the inner command
# failing) — used to type a mid-session death as SandboxUnavailableError.
_PODMAN_DEAD_MARKERS = ("no such container", "no container with", "is not running", "improper")
# Re-verify inspect retry: under concurrent build load `podman inspect` is ITSELF
# intermittently refused, and a single failed inspect would conservatively type a
# LIVE container dead → a needless recreate (the inspect-unavailable recreate residual).
# A real death (a successful inspect with a terminal status) returns immediately; only
# the UNVERIFIABLE case (rc!=0 / exception) is retried.
_INSPECT_RETRIES = 3
_INSPECT_BACKOFF_S = 0.15

_CPU_PERIOD = 100_000  # cgroup CPU period (100ms); quota/period = cpus

_LOG = logging.getLogger(__name__)

# A CLI runner: argv -> (exit_code, stdout, stderr). Injectable for hermetic tests.
CliRunner = Callable[[list[str], float], "tuple[int, bytes, bytes]"]


def _preview_host(cli_url: str) -> str:
    """The host a published preview port is reachable at, derived from the podman
    CLI url (mirrors `gvisor._preview_host` for the docker socket). For the remote
    Podman native remote (`ssh://user@host//run/.../podman.sock`) that's the remote
    host's tailnet IP — it runs the published port and is reachable over the proven
    keyless tailnet. A LOCAL socket url (`unix://…`) → localhost."""
    if cli_url.startswith("ssh://"):
        host = cli_url.removeprefix("ssh://").split("@")[-1]
        return host.split("/")[0].split(":")[0]  # strip any socket path / port
    return "localhost"


def _default_cli_runner(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)  # noqa: S603
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or b"", (exc.stderr or b"") + b"\n[backend: exec timed out]"
    except FileNotFoundError as exc:
        raise SandboxError(f"the `podman` CLI is required on PATH for exec: {exc}") from exc


class PodmanSandboxInstance(ContainerInstance):
    """A running Podman (crun, rootless) container. Lifecycle/jail are shared; exec
    and read/list use the podman CLI native remote (podman-py's exec is unusable
    over the remote API), while write uses podman-py `put_archive`."""

    def __init__(
        self,
        *,
        id: str,
        owner_id: str,
        conversation_id: str,
        spec: SandboxSpec,
        container: Any,
        container_workspace: str,
        stop_timeout_s: int,
        cli_url: str,
        container_name: str,
        cli_runner: CliRunner,
        workspace_uid: int = 1000,
        preview_host: str = "localhost",
        reload_timeout_s: float = 0.5,
        workspace_volume: Any | None = None,
        loopback_tunnel: SshLoopbackTunnelManager | None = None,
    ) -> None:
        super().__init__(
            id=id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
            container=container,
            container_workspace=container_workspace,
            stop_timeout_s=stop_timeout_s,
            workspace_uid=workspace_uid,
            preview_host=preview_host,
            reload_timeout_s=reload_timeout_s,
            workspace_volume=workspace_volume,
            loopback_tunnel=loopback_tunnel,
        )
        self._cli_url = cli_url
        self._name = container_name
        self._runner = cli_runner

    # [FIX6 parity] `expose_port` is INHERITED from `ContainerInstance` (the shared
    # `_resolve_mapping` reads the SIDECAR's published binding for a filtered box,
    # the sandbox's for sealed/open) — the old podman-only STUB that returned None is
    # gone, exactly like gvisor. The inbound forwarder (below) makes the sidecar's
    # published port actually pipe to the internal sandbox, so the URL is real.

    def _exec(self, argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        return self._runner(["podman", "--url", self._cli_url, "exec", self._name, *argv], timeout)

    def _guest_run(self, argv: list[str]) -> tuple[int, bytes]:
        """[P2] Guest exec for the symlink-resolution jail, via the CLI native remote —
        podman-py's `exec_run` is unusable over the remote API (see the module docstring),
        so the shared `_resolve_guest_path` (ContainerInstance) drives the guest through
        this override, exactly as read/list/exec do."""
        rc, out, _err = self._exec(argv, 30)
        return rc, out

    def _inspect_state(self) -> tuple[str, str]:
        """Best-effort `podman inspect` → (status, reason). `status` is `State.Status`
        ("running" / "exited" / "" if unverifiable); `reason` attributes a death
        (OOMKilled / exit / runtime error). A dead-but-not-yet-removed container still has
        its record, so this usually resolves. NEVER raises — runs on an already-failing
        path and must not mask the original error.

        RETRIES the inspect a few times when the COMMAND itself fails (rc!=0 / exception):
        under concurrent load `podman inspect` is itself intermittently refused, and a
        single failed inspect would conservatively type a LIVE container dead (the
        inspect-unavailable recreate residual). A successful inspect — even one reporting a
        terminal status like `exited` — is a REAL answer and returns immediately; only the
        unverifiable case is retried."""
        for attempt in range(_INSPECT_RETRIES):
            try:
                rc, out, _e = self._runner(
                    [
                        "podman",
                        "--url",
                        self._cli_url,
                        "inspect",
                        "--format",
                        "status={{.State.Status}} OOMKilled={{.State.OOMKilled}} "
                        "exit={{.State.ExitCode}} reason={{.State.Error}}",
                        self._name,
                    ],
                    10,
                )
                if rc == 0:
                    text = out.decode("utf-8", "replace").strip()
                    status = ""
                    for tok in text.split():
                        if tok.startswith("status="):
                            status = tok[len("status=") :]
                            break
                    return status, (text or "state-empty")
                # rc != 0 → the inspect command itself failed (not a container verdict).
                # Retry — this is the transient case the residual was about.
            except Exception:  # noqa: BLE001 — diagnostic only, must never mask the error
                pass
            if attempt < _INSPECT_RETRIES - 1:
                time.sleep(_INSPECT_BACKOFF_S)
        return "", "reason-unavailable"

    def _raise_if_dead(self, rc: int, err: bytes) -> None:
        """A `podman exec` failing at the CONTAINER level (rc 125 + a dead marker) is usually
        a gone/exited box → SandboxUnavailableError so the session RE-CREATES. BUT under
        concurrent build load podman intermittently refuses an exec on a LIVE container
        ("can only create exec sessions on running containers … improper"), and the broad
        "improper" marker would misread that as death → a needless recreate that derails the
        build. So RE-VERIFY via `podman inspect`: if State.Status=="running" the container is
        ALIVE → this is a TRANSIENT exec error, raise a retryable per-op SandboxError (NO
        recreate). Only a NOT-running / unverifiable container is typed dead. The inspect API
        is reliable here (it's the EXEC that's transiently refused, not inspect), so this
        re-verify is load-bearing under sustained concurrency. Mirrors the docker-py path's
        re-verify (`_confidently_alive`)."""
        if rc == 0:
            return
        msg = err.decode("utf-8", "replace")
        if not any(m in msg.lower() for m in _PODMAN_DEAD_MARKERS):
            return
        status, reason = self._inspect_state()
        # "running" = a TRANSIENT exec refusal on a live box (no recreate). Also treat the
        # TEARDOWN states {stopping, removing, paused} as transient: an exec racing the
        # NORMAL end-of-build container teardown is NOT a mid-session death — recreating
        # (or WARN-logging "died") there is harmless noise on an already-finishing build.
        if status in ("running", "stopping", "removing", "paused"):
            _LOG.info(
                "sandbox container %s: transient podman exec error, container %s (%s): %s",
                self._name,
                status or "alive",
                reason,
                msg.strip(),
            )
            raise SandboxError(f"transient sandbox exec error in {self._name}: {msg.strip()}")
        _LOG.warning(
            "sandbox container %s died mid-session (%s): %s",
            self._name,
            reason,
            msg.strip(),
        )
        raise SandboxUnavailableError(
            f"sandbox container died mid-session ({reason}): {msg.strip()}"
        )

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        """Run `cmd` via the CLI native remote — the timeout is enforced in-container
        (`timeout` coreutil) with an outer subprocess backstop; a killed command is
        reported with `timed_out=True`, partial output preserved."""
        self._alive()
        argv = bounded_exec_argv(cmd, timeout_s)
        rc, out, err = await asyncio.to_thread(self._exec, argv, timeout_s + 15)
        self._raise_if_dead(rc, err)
        return ExecResult(
            exit_code=rc,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=rc in TIMEOUT_EXIT_CODES,
        )

    async def read_file(self, path: str) -> bytes:
        self._alive()
        target = await asyncio.to_thread(self._resolve_guest_path, path)  # lexical+symlink (P2)
        relpath = posixpath.relpath(target, self._ws)
        rc, out, err = await asyncio.to_thread(
            self._exec,
            bounded_read_argv(self._ws, relpath),
            SANDBOX_READ_TIMEOUT_S,
        )
        self._raise_if_dead(rc, err)
        return bounded_read_result(path, rc, out, err)

    async def file_exists(self, path: str) -> bool:
        """[B4] Existence check via the CLI native remote (`test -f` in-container),
        matching read_file/list_dir's transport (podman-py exec is unusable over
        the remote API). False on a missing file or a jail-escaping path; a dead
        container is surfaced as a typed SandboxUnavailableError (→ recreate)."""
        self._alive()
        try:
            target = await asyncio.to_thread(self._resolve_guest_path, path)  # symlink jail (P2)
        except SandboxError:
            return False
        rc, _out, err = await asyncio.to_thread(
            self._exec,
            ["sh", "-c", 'test -f "$1" && test ! -L "$1"', "disco", target],
            30,
        )
        if rc != 0:
            self._raise_if_dead(rc, err)
        return rc == 0

    async def list_dir(self, path: str) -> list[str]:
        self._alive()
        target = await asyncio.to_thread(self._resolve_guest_path, path)  # symlink jail (P2)
        rc, out, err = await asyncio.to_thread(self._exec, ["ls", "-1A", "--", target], 30)
        if rc != 0:
            self._raise_if_dead(rc, err)
            raise_read_error(path, err, op="list_dir")  # W1: typed missing-dir error
        return sorted(n for n in out.decode("utf-8", "replace").splitlines() if n)

    async def write_file(self, path: str, data: bytes) -> None:
        """Binary-safe write through the inherited staged atomic path.

        Direct podman-py put_archive into the workspace creates SELinux labels
        which rootless guest processes cannot later rename or unlink.
        """
        await self.atomic_write(path, data)


class PodmanSandboxService:
    """[CONTRACT boundary] Creates Podman-backed instances over the native remote."""

    name = "podman"
    # EPIC H (§1.4/§9.3): a container backend with its own PID + network namespace —
    # production / Build-Soak valid (only the `process` dev backend is False).
    is_production_valid = True

    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        client: Any | None = None,
        cli_runner: CliRunner | None = None,
    ) -> None:
        self._cfg = config or default_podman_config()
        self._injected_client = client  # test seam: a fake podman client
        self._cli_runner = cli_runner or _default_cli_runner  # test seam: a fake CLI
        self._client_cache: Any | None = None
        self._instances: dict[str, PodmanSandboxInstance] = {}

    @property
    def _cli_url(self) -> str:
        # podman-py uses `http+ssh://…`; the podman CLI wants `ssh://…`.
        return self._cfg.podman_url.removeprefix("http+")

    def _client(self) -> Any:
        """The podman-py client (lazy, cached) on the native remote socket. The SSH
        leg uses the system ssh client (tailnet keyless auth applies)."""
        if self._injected_client is not None:
            return self._injected_client
        if self._client_cache is None:
            # podman-py's UDSConnection allocates a socket and, when connect()
            # itself fails, raises before assigning that socket to the connection.
            # Neither Session.close nor PodmanClient.close can then reach it.  Use
            # the already-required native CLI for the availability probe, so an
            # unreachable daemon is rejected before the SDK opens a transport.
            try:
                rc, _out, _err = self._cli_runner(
                    ["podman", "--url", self._cli_url, "info", "--format", "json"],
                    float(self._cfg.client_timeout_s),
                )
            except Exception as exc:  # noqa: BLE001 — normalize the availability boundary
                raise SandboxUnavailableError(
                    f"Podman unreachable at {self._cfg.podman_url}: "
                    "native client probe could not run"
                ) from exc
            if rc != 0:
                raise SandboxUnavailableError(
                    f"Podman unreachable at {self._cfg.podman_url}: native client probe exited {rc}"
                )
            client: Any | None = None
            try:
                from podman import PodmanClient

                client = PodmanClient(base_url=self._cfg.podman_url)
            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
                # Construction is normally transport-lazy after the native probe,
                # but close any partially initialized client defensively.
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.close()
                raise SandboxUnavailableError(
                    f"Podman unreachable at {self._cfg.podman_url}: {exc}"
                ) from exc
            self._client_cache = client
        return self._client_cache

    def _sidecar_cli_run(
        self, sidecar_name: str, argv: list[str], timeout: float
    ) -> tuple[int, bytes, bytes]:
        """[E8] One-shot CLI exec on the proxy SIDECAR. Reuses the same CLI runner
        as the sandbox (`self._cli_runner`) so the sidecar goes through the proven
        `podman --url ssh://…//socket exec` path — podman-py's native `exec_run`
        is broken over the remote API (per the backend's transport-split note in
        its module docstring), so the CLI is the only correct way to do one-shot
        commands on a remote podman container. The sandbox instance has a
        `self._exec` helper; the sidecar lives one container over, so we just
        call the runner directly with the sidecar's name."""
        return self._cli_runner(
            ["podman", "--url", self._cli_url, "exec", sidecar_name, *argv], timeout
        )

    @staticmethod
    def _best_effort_cleanup(*, egress_network: Any, egress_sidecar: Any) -> None:
        """Tear down a (possibly partial) filtered-egress aux — sidecar first (it's on the
        internal net), then the network. Both refs may be None. Best-effort: each removal
        is independently guarded so a failure on one still attempts the other."""
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

    def _setup_filtered_egress(
        self, client: Any, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
    ) -> Any:
        """[E8 — live-verify on VM 202 pending] Stand up the allowlisting egress for
        a "filtered" box and return (network, sidecar, env, net_name).

        SPIRIT identical to `gvisor.py _setup_filtered_egress` — the OLD binary
        (none|bridge) silently gave an allowlisted box full network, which is the
        false guarantee E8 closes. Now an INTERNAL (no-NAT) network is the
        substrate: the box's ONLY route out is the proxy, and the proxy refuses
        any host the allowlist doesn't name. The HTTP(S)_PROXY env is belt-and-
        suspenders; the no-route property is the real guarantee.

        Three podman-specific notes (mirror gVisor's invariants #1/#2/#3 in
        gvisor.py; the podman host may not enforce the same boot-time netstack
        freeze as runsc, but we keep the create-on-bridge → connect-internal →
        start ordering so both NICs exist before any sandbox traffic):

        1. The proxy script is `put_archive`'d to the sidecar (no image rebuild;
           base image's `python3` runs it; honors the never-pull rule).
        2. `exec_run` on a podman-py remote container is broken (per the backend
           docstring), so the one-shot setup (resolv.conf + proxy launch) goes
           through the CLI runner, same as the sandbox's command path.
        3. The sandbox's proxy env names the sidecar by its INTERNAL-NET IP (read
           from the sidecar's attrs after `reload()`), with a name-fallback for
           the fake/test path."""
        net_name = f"{EGR_NET_PREFIX}{instance_id}"
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        # EPIC H (P1) — runtime LAST GATE on the sidecar caps. SandboxConfig is hot-mutable, so
        # a post-construction `cfg.sidecar_cpu = 0` (etc.) bypasses the @field_validator and
        # would otherwise reach podman as UNLIMITED. Gate BEFORE creating any network/sidecar
        # so a mutated cap is refused with nothing stranded.
        sidecar_cpu = bounded_sidecar_cap("cpu", self._cfg.sidecar_cpu)
        sidecar_memory_mb = int(bounded_sidecar_cap("memory_mb", self._cfg.sidecar_memory_mb))
        sidecar_pids_limit = int(bounded_sidecar_cap("pids", self._cfg.sidecar_pids_limit))
        deny_ips = collect_host_deny_ips(
            self._cfg.host_ip_blocklist,
            self._cfg.preview_host or _preview_host(self._cli_url),
            include_local_interfaces=not self._cli_url.startswith("ssh://"),
            additional_host_ips=(
                discover_remote_host_ips(self._cli_url)
                if self._cli_url.startswith("ssh://") and self._injected_client is None
                else frozenset()
            ),
        )
        # [P1 leak-guard] Setup runs BEFORE the guarded sandbox create in `_start_container`,
        # so a partial failure here (connect/start/proxy inject) would otherwise strand the
        # internal network + proxy sidecar. Own the cleanup: any exception after either
        # resource exists removes whatever was created before re-raising.
        network: Any = None
        sidecar: Any = None
        try:
            # The network carries the same label as the containers so the orphan
            # sweep can find it — its NAME is instance-keyed, not conversation-keyed.
            network = client.networks.create(
                net_name, driver="bridge", internal=True, labels=labels
            )
            # (1) create on bridge → connect internal → start, so the sidecar's
            # `bridge` NIC (the route to the internet, the proxy's upstream) AND its
            # internal-net NIC BOTH exist before any sandbox traffic.
            # [FIX6 parity — live-verified on local podman 5.8.2] PUBLISH the preview
            # ports on the SIDECAR (it's on bridge → it CAN publish; the sandbox is
            # internal-only and can't). The sidecar's inbound forwarder (launched after
            # the sandbox starts, in `_start_container`) bridges each published host
            # port to the sandbox's internal IP — host reaches the preview, the sandbox
            # keeps zero direct egress. Ports must be declared at create() (podman can't
            # add mappings to a running container). podman-py accepts the SAME docker-py
            # `{"8000/tcp": None}` format AND reads it back as the same
            # `NetworkSettings.Ports` shape (verified live), so the shared
            # `_container._resolve_mapping` reads the binding unchanged.
            sidecar = client.containers.create(
                image=self._cfg.image,
                command=["sh", "-c", "exec sleep infinity"],
                # [FIX6 parity] dual-home the sidecar: a BRIDGE route (the proxy's
                # upstream + the host's path to the published preview ports) AND, after
                # `network.connect` below, the internal no-NAT net it shares with the
                # sandbox. gVisor passes docker-py's `network="bridge"`; the rootless
                # podman equivalent is `network_mode="bridge"` (verified live — a bare
                # create defaults to pasta, which an internal net cannot attach to).
                network_mode="bridge",
                ports=None if sealed(spec) else loopback_port_bindings(podman=True),
                # EPIC H (P1): bound the sidecar on CPU + PIDs too, not just memory — a wedged
                # or compromised proxy must not be able to burn host CPU or fork-bomb host PIDs.
                # podman caps cpu via quota/period (mirrors the sandbox create path).
                mem_limit=f"{sidecar_memory_mb}m",
                cpu_quota=int(sidecar_cpu * _CPU_PERIOD),
                cpu_period=_CPU_PERIOD,
                pids_limit=sidecar_pids_limit,
                ulimits=nofile_ulimits(self._cfg),
                cap_drop=["ALL"],
                no_new_privileges=True,
                sysctls={
                    "net.ipv4.ip_forward": "0",
                    "net.ipv6.conf.all.forwarding": "0",
                },
                detach=True,
                name=net_name,
                labels=labels,
            )
            network.connect(sidecar)
            sidecar.start()
            # (2) replace the dead embedded resolver with public DNS over the (working) route.
            self._sidecar_cli_run(
                sidecar.name,
                [
                    "sh",
                    "-c",
                    'printf "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n" > /etc/resolv.conf',
                ],
                15,
            )
            # Inject the proxy script (put_archive works over the podman remote, per
            # the backend docstring). Launch it in the background via the CLI runner
            # — podman-py's `exec_run` is broken, the CLI is the correct path.
            script = pathlib.Path(_egress_proxy_mod.__file__).read_bytes()
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name="egress_proxy.py")
                info.size = len(script)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(script))
            if not sidecar.put_archive("/", buf.getvalue()):
                raise SandboxUnavailableError("failed to inject egress proxy script into sidecar")
            allow = format_allow(spec.egress_allow)
            deny_hosts = (
                frozenset({parse_upstream(self._cfg.host_service_upstream).host})
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
            self._sidecar_cli_run(
                sidecar.name,
                ["sh", "-c", f"{shlex.join(argv)} >/var/log/egress.log 2>&1 &"],
                10,
            )
            ready_rc, _ready_out, _ready_err = self._sidecar_cli_run(
                sidecar.name, proxy_readiness_argv(EGRESS_PROXY_PORT), 10
            )
            if ready_rc != 0:
                raise SandboxUnavailableError(
                    "egress policy proxy failed its readiness check; refusing sandbox start"
                )
            # (3) read the sidecar's IP on the internal net; the sandbox proxies by IP.
            try:
                sidecar.reload()
                nets = sidecar.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
                proxy_ip = nets.get(net_name, {}).get("IPAddress", "")
            except Exception:  # noqa: BLE001 — fake/test path or hung client
                proxy_ip = ""
            proxy_ip = proxy_ip or net_name  # fall back to the name (harmless for fake/test)
            env = proxy_env(proxy_ip, EGRESS_PROXY_PORT)
            relay_url = self._launch_capability_relay(sidecar, proxy_ip, spec)
            return network, sidecar, env, net_name, relay_url
        except Exception:
            # Partial setup must not leak: tear down whatever already exists, then re-raise.
            self._best_effort_cleanup(egress_network=network, egress_sidecar=sidecar)
            raise

    def _launch_capability_relay(
        self, sidecar: Any, sidecar_ip: str, spec: SandboxSpec
    ) -> str | None:
        """Launch the strict host-service relay; failure aborts sandbox creation."""
        if not spec.host_services:
            return None
        script = pathlib.Path(_capability_relay_mod.__file__).read_bytes()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="capability_relay.py")
            info.size = len(script)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(script))
        if not sidecar.put_archive("/", buf.getvalue()):
            raise SandboxUnavailableError("failed to inject host-service capability relay")
        authority = f"{sidecar_ip}:{HOST_SERVICE_RELAY_PORT}"
        argv = relay_run_argv(self._cfg.host_service_upstream, authority)
        self._sidecar_cli_run(
            sidecar.name,
            ["sh", "-c", f"{shlex.join(argv)} >/dev/null 2>&1 &"],
            10,
        )
        ready_rc, _out, _err = self._sidecar_cli_run(
            sidecar.name, relay_readiness_argv(sidecar_ip), 10
        )
        if ready_rc != 0:
            raise SandboxUnavailableError(
                "host-service capability relay failed readiness; refusing sandbox start"
            )
        return f"http://{authority}"

    def _launch_inbound_forwarder(self, sidecar: Any, container: Any, net_name: str) -> None:
        """[FIX6 parity — port of `gvisor._launch_inbound_forwarder`] Make a FILTERED
        box's preview ports host-reachable WITHOUT breaking containment. The sandbox is
        internal-only (no published port); the dual-homed egress SIDECAR publishes the
        preview ports (see `_setup_filtered_egress`) and runs a stdlib TCP forwarder that
        bridges each published host port to the sandbox's internal IP —
        `host:PORT -> <sandbox_ip>:PORT`. Transparent byte pipe → websockets / Vite HMR
        pass through; the sandbox keeps zero direct egress.

        Best-effort: on ANY failure the preview is unreachable but the box still works
        (the agent-server surfaces the honest no-URL state), so we never raise here and
        leak the just-started sandbox.

        Delivery mirrors gVisor's FIX6 (base64 ONE-SHELL — write + run share a process,
        so there is no cross-exec gofer-visibility gap), but launched via the CLI runner
        (`_sidecar_cli_run`): podman-py's `exec_run` is broken over the remote API, so the
        `podman --url … exec` path is the only correct one-shot. It is backgrounded with
        `&` (the podman exec returns, the forwarder keeps running) — the same launch
        pattern the egress proxy uses above; gVisor instead relies on docker's
        `exec_run(detach=True)`."""
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
            b64 = base64.b64encode(script).decode("ascii")
            ports_arg = " ".join(str(p) for p in sorted(PUBLISHED_PORTS))
            self._sidecar_cli_run(
                sidecar.name,
                [
                    "sh",
                    "-c",
                    f"echo {b64} | base64 -d > /inbound_forward.py && "
                    f"python3 /inbound_forward.py {sbx_ip} {ports_arg} "
                    f">/var/log/inbound.log 2>&1 &",
                ],
                15,
            )
        except Exception:  # noqa: BLE001 — best-effort; preview unreachable, box still works
            _LOG.warning(
                "failed to launch the inbound preview forwarder on the egress sidecar",
                exc_info=True,
            )

    def _start_container(
        self, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
    ) -> tuple[Any, str, Any, Any, Any, str | None]:
        """The blocking Podman work for `create`, in a thread. Image-by-load (never
        pull); limits via the socket; typed errors on failure. Returns
        (container, name, egress_network, egress_sidecar, workspace_volume) —
        the egress refs are non-None ONLY for a "filtered" box (the allowlisting
        proxy aux), None for sealed / open. The shared
        `ContainerInstance.destroy()` teardown walks the aux refs and tears them
        down (E8 wiring) and removes the named workspace volume."""
        client = self._client()

        try:
            present = client.images.exists(self._cfg.image)
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailableError(f"could not query Podman images: {exc}") from exc
        if not present:
            raise SandboxUnavailableError(
                f"sandbox image {self._cfg.image!r} is not present (load it with "
                "`podman load`; this backend never pulls from a registry)"
            )

        # EPIC H (P1): config is the MAXIMUM, not a fallback. A model spec may tighten
        # cpu/mem/pids but never loosen them above the configured max nor disable the pids
        # cap (pids=0 → default, never "unlimited"). Enforced by the user@ systemd manager
        # via the socket (same path as mem/cpu). See resolve_bounds.
        cpu, mem_mb, pids, disk_mb = resolve_bounds(spec, self._cfg)
        vol_name = f"{self._cfg.workspace_volume_prefix}-{instance_id}"
        name = f"{SBX_NAME_PREFIX}{instance_id}"

        # Per-mode network config — mirror gVisor's three-way posture. Filtered
        # and public boxes get a policy sidecar on an internal no-NAT network;
        # no model-shaped spec reaches a raw bridge.
        mode = egress_mode(spec)
        if spec.host_services:
            try:
                relay_upstream = parse_upstream(self._cfg.host_service_upstream)
            except RelayConfigurationError as exc:
                raise SandboxUnavailableError(str(exc)) from exc
            if relay_upstream.scheme != "https":
                raise SandboxUnavailableError(
                    "container host-service relay requires a reachable HTTPS upstream; "
                    "sidecar loopback is not the agent-server host"
                )
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        net_kwargs: dict[str, Any] = {}
        environment: dict[str, str] = {}
        egress_network = egress_sidecar = None
        egress_net_name = ""
        host_service_relay_url = None
        if mode in {"filtered", "public"} or spec.host_services:
            (
                egress_network,
                egress_sidecar,
                environment,
                net_name,
                host_service_relay_url,
            ) = self._setup_filtered_egress(client, spec, instance_id, conversation_id)
            egress_net_name = net_name
            # podman-py's `containers.create` attaches user-defined networks via
            # `networks={name: per-net-config}`; the sandbox's ONLY interface
            # will be the internal no-NAT net (the proxy is the only route out).
            # `network_mode="bridge"` sets the netns nsmode (rootless podman defaults
            # netns to pasta, which REFUSES a `networks=` list → 500; verified live).
            # It declares the namespace TYPE only — `networks` still pins the box to
            # the single internal net (no default bridge attached → no egress).
            net_kwargs = {"network_mode": "bridge", "networks": {net_name: {}}}
        else:  # sealed
            net_kwargs = {"network_mode": "none"}

        volume = None
        container = None
        try:
            volume = _create_named_volume(
                client.volumes,
                name=vol_name,
                labels=labels,
                disk_mb=disk_mb,
                workspace_uid=self._cfg.workspace_uid,
            )
            container = client.containers.create(
                image=self._cfg.image,
                command=["sleep", "infinity"],  # keepalive
                mem_limit=f"{mem_mb}m",
                cpu_quota=int(cpu * _CPU_PERIOD),
                cpu_period=_CPU_PERIOD,
                pids_limit=pids,  # EPIC H: cgroup pids.max — fork-bomb / host-PID guard
                ulimits=nofile_ulimits(self._cfg),
                **net_kwargs,
                volumes={vol_name: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                # NO host env leaks in. For a filtered box the proxy routing vars
                # (defense in depth atop the no-route network) are passed — that's
                # the ONLY exception.
                environment=environment,
                # The base image creates UID 1000 but intentionally has no USER
                # directive.  Pin the sandbox (not the privileged policy sidecar)
                # to the workspace principal so guest cp/mv staging preserves an
                # editable non-root ownership boundary.
                user=f"{self._cfg.workspace_uid}:{self._cfg.workspace_uid}",
                working_dir=self._cfg.container_workspace,
                name=name,
                labels=labels,
                detach=True,
            )
            container.start()
            # [FIX6 parity] sandbox is up + on the internal net — launch the inbound
            # preview forwarder on the SIDECAR (host:PORT -> sandbox_ip:PORT).
            # Best-effort: never raises, so it can't leak the just-started box.
            if mode in {"filtered", "public"} and egress_sidecar is not None:
                self._launch_inbound_forwarder(egress_sidecar, container, egress_net_name)
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

            if container is None:
                with contextlib.suppress(Exception):
                    container = client.containers.get(name)
            with contextlib.suppress(Exception):
                _remove_container(container)
            # Don't leak the egress aux if the sandbox itself failed to start
            # (E8: the parent class's teardown walks these refs).
            self._best_effort_cleanup(egress_network=egress_network, egress_sidecar=egress_sidecar)
            with contextlib.suppress(Exception):
                _remove_volume(volume)
            if isinstance(exc, SandboxUnavailableError):
                raise
            if isinstance(exc, ImageNotFound):
                raise SandboxUnavailableError(
                    f"sandbox image {self._cfg.image!r} not found"
                ) from exc
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        instance_id = f"sbx_{uuid.uuid4().hex}"
        (
            container,
            name,
            egress_network,
            egress_sidecar,
            workspace_volume,
            host_service_relay_url,
        ) = await asyncio.to_thread(self._start_container, spec, instance_id, conversation_id)
        instance = PodmanSandboxInstance(
            id=instance_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
            container=container,
            container_workspace=self._cfg.container_workspace,
            stop_timeout_s=self._cfg.stop_timeout_s,
            workspace_uid=self._cfg.workspace_uid,
            cli_url=self._cli_url,
            container_name=name,
            cli_runner=self._cli_runner,
            # [FIX6 parity] the host a published preview port is reachable at: the
            # remote podman host's tailnet IP (from the CLI url), or an explicit
            # config override — mirrors gvisor's preview_host wiring.
            preview_host=self._cfg.preview_host or _preview_host(self._cli_url),
            # Wedge-guard timeout (Dispo #25, E5 wiring) — untouched in E8.
            reload_timeout_s=self._cfg.reload_timeout_s,
            workspace_volume=workspace_volume,
            loopback_tunnel=(
                SshLoopbackTunnelManager(self._cli_url)
                if self._cli_url.startswith("ssh://") and self._injected_client is None
                else None
            ),
        )
        # E8: attach the filtered-egress aux so the inherited ContainerInstance
        # teardown tears down the proxy sidecar + internal network. For sealed /
        # open these are None (the defaults on the class), so the teardown is a
        # no-op.
        instance._egress_network = egress_network
        instance._egress_sidecar = egress_sidecar
        instance._host_service_relay_url = host_service_relay_url
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)

    async def healthcheck(self) -> None:
        """[W-48] Connectivity preflight for the Podman native remote: ping the
        rootless socket over the (keyless Tailscale SSH) transport. Raises a typed
        ``SandboxUnavailableError`` NAMING the URL + reason on failure ("Podman
        unreachable at <url>: …"); returns None on success. Runs in a thread so the
        blocking SSH/socket call can't block the event loop."""

        def _probe() -> None:
            self._client()  # native probe + lazy PodmanClient, typed on failure

        await asyncio.to_thread(_probe)

    async def list_live_instances(self) -> list[str]:
        """Return conversation_ids of all live pmx-sbx-* containers on this Podman backend.

        Side effect (documented, deliberate — mirrors the gVisor backend): an
        unlabeled pmx-sbx-* container predates the label scheme and cannot be
        correlated to any conversation — unreachable garbage by construction,
        reaped on sight."""

        def _list() -> list[str]:
            try:
                client = self._client()
                # Dual-read: sweep BOTH the current `disco-sbx-` and legacy
                # `pmx-sbx-` name prefixes so a rename never strands a container.
                seen_ids: set[str] = set()
                containers = []
                for prefix in SBX_NAME_PREFIXES:
                    for c in client.containers.list(filters={"name": prefix}):
                        if c.id not in seen_ids:
                            seen_ids.add(c.id)
                            containers.append(c)
                result = []
                for c in containers:
                    cid = conv_id_from_labels(getattr(c, "labels", None))
                    if cid:
                        result.append(cid)
                        continue
                    try:  # legacy/unlabeled: reap on sight (see docstring)
                        c.stop(timeout=2)
                        _remove_container(c)
                    except Exception:  # noqa: BLE001 — best-effort
                        pass
                return result
            except Exception:  # noqa: BLE001 — Podman unreachable: non-fatal
                return []

        return await asyncio.to_thread(_list)

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Destroy all disco-sbx-*/pmx-sbx-* containers labelled with this conversation_id."""

        def _destroy() -> None:
            try:
                client = self._client()
                # Dual-read: match BOTH the current and legacy conversation label
                # keys (union, dedup) so an old-scheme container is still removed.
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
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            _remove_container(c)
                        except Exception:  # noqa: BLE001
                            pass
                # [P2] Clean up the filtered-egress internal network(s) by LABEL too —
                # mirrors the gVisor path. The network NAME is disco-egr-{instance_id}
                # (instance-keyed, NOT conversation-keyed), so only the conversation
                # label finds it; a crash/restart with filtered podman boxes would
                # otherwise strand the labeled `disco-egr-*` networks forever. The
                # containers above are already removed, so the network is detachable.
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
                # Remove the per-sandbox named workspace volume(s) labeled with
                # this conversation. This is the release path used by soak kill;
                # container.remove(v=True) covers attached/anonymous volumes, but
                # named volumes may need explicit removal by the SDK.
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
            except Exception:  # noqa: BLE001 — best-effort
                pass

        await asyncio.to_thread(_destroy)

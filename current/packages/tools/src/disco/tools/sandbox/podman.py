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
import contextlib
import io
import json
import logging
import os
import pathlib
import posixpath
import shlex
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from typing import Any

# MODULE SURFACE, PRESERVED (Epic 10-B) ------------------------------------------
#
# `podman_parts` resolves this module's names through the parent at call time (the
# standing §13 rule, which keeps test monkeypatches effective) — e.g.
# `podman.EGR_NET_PREFIX`, `podman.LABEL_CONV_KEYS`, `podman.SBX_NAME_PREFIX`,
# `podman._egress_proxy_mod.__file__` and `podman._inbound_forward_mod.__file__`.
# Ruff cannot see those uses, so every such name is bound here in the
# redundant-alias re-export form (`X as X`), ruff's sanctioned re-export marker,
# which needs no per-line suppression. The `._container` / `.capability_relay`
# re-exports are this module's inherited surface, preserved across the extraction.
from . import capability_relay as _capability_relay_mod
from . import egress_proxy as egress_proxy
from . import inbound_forward as inbound_forward
from ._container import EGRESS_PROXY_PORT as EGRESS_PROXY_PORT
from ._container import INTERNAL_PORTS as INTERNAL_PORTS
from ._container import (
    PUBLISHED_PORTS,
    SANDBOX_READ_TIMEOUT_S,
    TIMEOUT_EXIT_CODES,
    ContainerInstance,
    SshLoopbackTunnelManager,
    _remove_container,
    bounded_exec_argv,
    bounded_list_argv,
    bounded_list_result,
    bounded_read_argv,
    bounded_read_result,
    reconcile_orphan_aux_resources,
)
from ._container import _create_named_volume as _create_named_volume
from ._container import _remove_volume as _remove_volume
from ._container import bounded_sidecar_cap as bounded_sidecar_cap
from ._container import collect_host_deny_ips as collect_host_deny_ips
from ._container import discover_remote_host_ips as discover_remote_host_ips
from ._container import egress_mode as egress_mode
from ._container import format_allow as format_allow
from ._container import inbound_readiness_argv as inbound_readiness_argv
from ._container import loopback_port_bindings as loopback_port_bindings
from ._container import nofile_ulimits as nofile_ulimits
from ._container import proxy_env as proxy_env
from ._container import proxy_readiness_argv as proxy_readiness_argv
from ._container import proxy_run_argv as proxy_run_argv
from ._container import resolve_bounds as resolve_bounds
from ._container import sealed as sealed
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
    relay_readiness_argv,
    relay_run_argv,
)
from .capability_relay import RelayConfigurationError as RelayConfigurationError
from .capability_relay import parse_upstream as parse_upstream
from .config import SandboxConfig, default_podman_config
from .naming import EGR_NET_PREFIX as EGR_NET_PREFIX
from .naming import LABEL_CONV as LABEL_CONV
from .naming import LABEL_CONV_KEYS as LABEL_CONV_KEYS
from .naming import SBX_NAME_PREFIX as SBX_NAME_PREFIX
from .naming import (
    SBX_NAME_PREFIXES,
    conv_id_from_labels,
)
from .podman_parts import container_start as _container_start_part
from .podman_parts import conversation_cleanup as _conversation_cleanup_part
from .podman_parts import egress_setup as _egress_setup_part
from .podman_parts import workspace_export_script as _workspace_export_script_part

# This module's long-standing private aliases for the egress-proxy and
# inbound-forward modules; `podman_parts` reads `podman._egress_proxy_mod.__file__`
# and `podman._inbound_forward_mod.__file__` at call time. Bound from the
# re-exports above so neither name needs a suppression.
_egress_proxy_mod = egress_proxy
_inbound_forward_mod = inbound_forward

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
_WORKSPACE_EXPORT_TIMEOUT_S = 120
_WORKSPACE_EXPORT_META = "__DISCO_WORKSPACE_EXPORT_META__"

# One guest process streams the bounded workspace as tar.  Keeping the filter in
# the guest prevents runtime credential files from crossing the sandbox boundary;
# archive.py validates every member again before it reaches durable storage.
# The script text itself lives in `podman_parts.workspace_export_script` (moved
# purely to shed module-size budget — embedded script text counts as logical
# source) and is re-imported here unchanged, so `podman._WORKSPACE_EXPORT_SCRIPT`
# keeps resolving for callers/tests that read it off this module.
_WORKSPACE_EXPORT_SCRIPT = _workspace_export_script_part.WORKSPACE_EXPORT_SCRIPT

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


async def _stream_workspace_archive(
    argv: list[str], destination: pathlib.Path, timeout: float
) -> tuple[int, bytes]:
    """Stream binary stdout to a private file and always reap the CLI process."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=output,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise SandboxError(
                    f"the `podman` CLI is required for workspace export: {exc}"
                ) from exc
            try:
                _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                _stdout, stderr = await process.communicate()
                return 124, (stderr or b"") + b"\n[backend: workspace export timed out]"
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.communicate()
                raise
            returncode = process.returncode
            if returncode is None:
                raise SandboxError("podman workspace export did not report an exit status")
            return returncode, stderr or b""
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


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

    # `expose_port` is inherited from ContainerInstance. The resolver reads the
    # SIDECAR binding: filtered/public boxes may expose curated user ports, while
    # sealed boxes publish only INTERNAL_PORTS and remain inverse-gated from user
    # preview. The inbound forwarder makes each accepted mapping real.

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

    async def list_dir_bounded(self, path: str, limit: int) -> tuple[list[tuple[str, str]], bool]:
        self._alive()
        target = await asyncio.to_thread(self._resolve_guest_path, path)
        rc, out, err = await asyncio.to_thread(
            self._exec,
            bounded_list_argv(target, limit),
            30,
        )
        self._raise_if_dead(rc, err)
        return bounded_list_result(path, rc, out, err)

    async def export_workspace_archive(
        self,
        destination: pathlib.Path,
        *,
        max_depth: int,
        max_file_bytes: int,
    ) -> tuple[list[str], list[str]]:
        """Export the jailed workspace in one bounded guest process.

        The generic snapshot walker needs multiple native-remote execs per node.
        Under concurrent Podman builds that left FINISHED work uncommitted for more
        than a minute.  This streams regular, non-secret, non-cache files once; the
        host archive reader independently validates every member before publication.
        """

        self._alive()
        rc, err = await _stream_workspace_archive(
            [
                "podman",
                "--url",
                self._cli_url,
                "exec",
                "--workdir",
                "/",
                self._name,
                "python3",
                "-I",
                "-c",
                _WORKSPACE_EXPORT_SCRIPT,
                self._ws,
                str(max_depth),
                str(max_file_bytes),
            ],
            destination,
            _WORKSPACE_EXPORT_TIMEOUT_S,
        )
        self._raise_if_dead(rc, err)
        if rc != 0:
            detail = err.decode("utf-8", "replace").strip()
            raise SandboxError(f"workspace archive export failed: {detail or f'exit {rc}'}")
        metadata: dict[str, Any] | None = None
        for line in reversed(err.decode("utf-8", "replace").splitlines()):
            if line.startswith(_WORKSPACE_EXPORT_META):
                try:
                    decoded = json.loads(line.removeprefix(_WORKSPACE_EXPORT_META))
                except json.JSONDecodeError as exc:
                    raise SandboxError(
                        "workspace archive export returned invalid metadata"
                    ) from exc
                if isinstance(decoded, dict):
                    metadata = decoded
                break
        if metadata is None:
            raise SandboxError("workspace archive export returned no completion metadata")
        skipped = metadata.get("skipped", [])
        preserve = metadata.get("preserve", [])
        if not isinstance(skipped, list) or not all(isinstance(item, str) for item in skipped):
            raise SandboxError("workspace archive export returned invalid skipped metadata")
        if not isinstance(preserve, list) or not all(isinstance(item, str) for item in preserve):
            raise SandboxError("workspace archive export returned invalid preserve metadata")
        return skipped, preserve

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
        self,
        sidecar_name: str,
        argv: list[str],
        timeout: float,
        *,
        detach: bool = False,
    ) -> tuple[int, bytes, bytes]:
        """CLI exec on the proxy sidecar, optionally using Podman's native detach.

        Reuses the same CLI runner
        as the sandbox (`self._cli_runner`) so the sidecar goes through the proven
        `podman --url ssh://…//socket exec` path — podman-py's native `exec_run`
        is broken over the remote API. Long-lived daemons MUST use native
        ``exec --detach``: shell ``&`` returns after spawning a child whose
        lifetime is not owned by the Podman exec session and can be reaped.
        """
        command = ["podman", "--url", self._cli_url, "exec"]
        if detach:
            command.append("--detach")
        command.extend([sidecar_name, *argv])
        return self._cli_runner(command, timeout)

    @staticmethod
    def _best_effort_cleanup(*, egress_network: Any, egress_sidecar: Any) -> None:
        """Tear down a possibly partial policy/control aux — sidecar first (it's on
        the internal net), then the network. Both refs may be None. Best-effort: each removal
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
        self,
        client: Any,
        spec: SandboxSpec,
        instance_id: str,
        conversation_id: str = "",
        *,
        inbound_only: bool = False,
    ) -> Any:
        """[E8 — live-verify on VM 202 pending] Stand up the internal network plus
        policy/control sidecar and return its topology; see
        `podman_parts.egress_setup` for the full contract (spirit-identical to
        `gvisor.py`'s, with the podman-specific CLI-exec + put_archive transport)."""
        return _egress_setup_part.setup_filtered_egress(
            self, client, spec, instance_id, conversation_id, inbound_only=inbound_only
        )

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
            ["sh", "-c", f"exec {shlex.join(argv)} >/dev/null 2>/dev/null"],
            10,
            detach=True,
        )
        ready_rc, _out, _err = self._sidecar_cli_run(
            sidecar.name, relay_readiness_argv(sidecar_ip), 10
        )
        if ready_rc != 0:
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
        """[FIX6 parity — port of `gvisor._launch_inbound_forwarder`] Make a FILTERED
        box's preview ports host-reachable WITHOUT breaking containment; see
        `podman_parts.container_start` for the full contract."""
        _container_start_part.launch_inbound_forwarder(self, sidecar, container, net_name, ports)

    def _start_container(
        self, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
    ) -> tuple[Any, str, Any, Any, Any, str | None]:
        """The blocking Podman work for `create`, in a thread; see
        `podman_parts.container_start` for the full contract. Returns
        (container, name, egress_network, egress_sidecar, workspace_volume,
        host_service_relay_url) — the aux refs cover sealed control-only and
        filtered/public policy modes. `ContainerInstance.destroy()` tears them
        down and removes the named workspace volume."""
        return _container_start_part.start_container(self, spec, instance_id, conversation_id)

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
        # Attach the policy/control aux so inherited teardown removes the sidecar
        # and internal network for every container mode.
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
        """Return conversation IDs for live current/legacy Podman sandboxes.

        Side effect (documented, deliberate — mirrors the gVisor backend): an
        unlabeled pmx-sbx-* container predates the label scheme and cannot be
        correlated to any conversation — unreachable garbage by construction,
        reaped on sight. H220 also reconciles aged, detached, product-labeled
        auxiliary networks/volumes with no surviving sandbox or sidecar."""

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
                reconcile_orphan_aux_resources(
                    client,
                    workspace_volume_prefix=self._cfg.workspace_volume_prefix,
                )
                return result
            except Exception:  # noqa: BLE001 — Podman unreachable: non-fatal
                return []

        return await asyncio.to_thread(_list)

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Destroy all disco-sbx-*/pmx-sbx-* containers labelled with this
        conversation_id; see `podman_parts.conversation_cleanup` for the full
        contract."""
        await _conversation_cleanup_part.destroy_by_conversation(self, conversation_id)

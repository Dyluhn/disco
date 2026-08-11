"""Shared container-backend logic — the part identical across the gVisor (docker-py)
and Podman (podman-py) backends, factored out so each service is just its create
path (tool-sandbox §5.1; the interface is unchanged).

Both SDKs expose a duck-type-compatible Container: `exec_run(cmd, demux, workdir)`,
`put_archive(path, data)`, `stop(timeout)`, `remove(force)`. So one `ContainerInstance`
drives both. File ops go through the container (exec/cp), never a host path, so they
work over any transport (local socket or Docker/Podman-over-SSH).
"""

from __future__ import annotations

# MODULE SURFACE, PRESERVED (Epic 10-B) ------------------------------------------
#
# Extraction into `_container_parts/` moved the *callers* of many of these names
# out of this module, but the names themselves are this module's surface and are
# restored here as explicit re-exports in the redundant-alias form (`X as X`),
# which is ruff's sanctioned re-export marker and needs no per-line suppression.
#
# Two of them are load-bearing rather than cosmetic. `subprocess`, `socket` and
# `psutil` must stay reachable as `_container.subprocess` / `_container.socket` /
# `_container.psutil`: `test_sw5_isolation_bounds.py` patches attributes *on those
# shared module objects* (`_container.subprocess.Popen`,
# `_container.psutil.net_if_addrs`, `_container.socket.create_connection`) to make
# host discovery deterministic. `host_discovery` binds the same module objects
# under its own `import`, so the patch is visible there — but the patch target
# expression itself still has to resolve here. Deleting them as "unused" (which
# `ruff --fix` offers to do) breaks those tests at patch time.
import asyncio
import contextlib as contextlib
import errno
import hashlib as hashlib
import io as io
import ipaddress as ipaddress
import json as json
import logging
import math
import posixpath
import secrets as secrets
import socket as socket
import subprocess as subprocess
import tarfile as tarfile
import threading as threading
import time as time
import urllib as urllib
from collections.abc import Iterable
from datetime import UTC as UTC
from datetime import datetime as datetime
from typing import TYPE_CHECKING, Any

import psutil as psutil

from ..anatomy import Capability
from ._container_parts import atomic_write as _atomic_write_part
from ._container_parts import aux_reconcile as _aux_reconcile_part
from ._container_parts import failure_classification as _failure_classification_part
from ._container_parts import file_batch as _file_batch_part
from ._container_parts import guest_scripts as _guest_scripts_part
from ._container_parts import host_discovery as _host_discovery_part
from ._container_parts import lifecycle as _lifecycle_part
from ._container_parts import port_mapping as _port_mapping_part
from ._container_parts.aux_reconcile import _AUX_RECONCILE_GRACE_S as _AUX_RECONCILE_GRACE_S
from ._container_parts.aux_reconcile import _resource_created_epoch as _resource_created_epoch
from ._container_parts.aux_reconcile import _resource_labels as _resource_labels
from ._container_parts.failure_classification import (
    _DEATH_REVERIFY_BACKOFF_S as _DEATH_REVERIFY_BACKOFF_S,
)
from ._container_parts.failure_classification import (
    _DEATH_REVERIFY_WINDOW_S as _DEATH_REVERIFY_WINDOW_S,
)
from ._container_parts.host_discovery import _ssh_endpoint_argv as _ssh_endpoint_argv
from .base import (
    ExecResult,
    SandboxError,
    SandboxPermissionError,
    SandboxSpec,
    raise_read_error,
    strip_redundant_workspace_prefix,
)
from .base import SandboxUnavailableError as SandboxUnavailableError
from .file_batch import SandboxFileBatchResult, SandboxFileMutation

if TYPE_CHECKING:
    from .config import SandboxConfig

_LOG = logging.getLogger(__name__)


# EPIC H (P1) — the deployment config is the MAXIMUM, not a fallback ---------------
#
# A SandboxSpec carries model-influenced resource fields (cpu / memory_mb / pids). The
# old `spec.X or cfg.default_X` resolution treated the config as a mere FALLBACK, so a
# spec could set ANY value — including `pids=0` (which `or` then read as "unset" and a
# raw value of 0 to Docker means UNLIMITED pids: the model could DISABLE the fork-bomb
# cap) or `cpu=64` (above the deployment's intent). These helpers make the config the
# hard ceiling: a spec may TIGHTEN a bound (request less) but can never loosen one above
# the configured maximum, and can never disable a limit.

# The internal "unset" sentinel for a spec resource field: SandboxSpec defaults pids to
# 0, and a 0 cpu/memory likewise means "the spec didn't choose" — resolved to the
# configured default (which equals the max).
_UNSET = 0


def _bounded(name: str, value: float, maximum: float) -> float:
    """Clamp a spec-supplied resource `value` to `[>0 .. maximum]`. 0 is the unset
    sentinel → the configured default/maximum. A negative value is REJECTED (invalid —
    a model must not be able to smuggle a negative through to the runtime). Anything
    ABOVE the maximum is clamped DOWN to it (a spec tightens, never loosens)."""
    # EPIC H (P1 hardening): the configured MAXIMUM must itself be finite and positive.
    # SandboxConfig's validators reject a bad max at construction, but resolve_bounds is
    # the last gate before the runtime, so it refuses defense-in-depth too: a non-finite
    # (NaN/inf) or <=0 maximum would make the clamp below a no-op and a 0 reach Docker as
    # "unlimited" — a silently-disabled host-protection cap. Reject it loudly instead.
    if not math.isfinite(maximum) or maximum <= 0:
        raise SandboxError(
            f"sandbox config maximum for {name}={maximum!r} is invalid "
            "(must be a finite positive number); a non-finite or <=0 maximum would "
            "disable the host-protection cap"
        )
    # A non-finite spec value (NaN/inf) can never be a valid request — reject before clamp
    # (min(NaN, max) is order-dependent and could smuggle NaN through to the runtime).
    if not math.isfinite(value):
        raise SandboxError(f"sandbox spec {name}={value!r} is invalid (must be a finite number)")
    if value < 0:
        raise SandboxError(
            f"sandbox spec {name}={value!r} is invalid (must be >= 0); "
            "resource bounds may only tighten the deployment maximum, never go negative"
        )
    if value == _UNSET:
        return maximum  # unset → the deployment default (== the configured max)
    return min(value, maximum)  # clamp above-max DOWN; a spec can only tighten


def resolve_bounds(spec: SandboxSpec, cfg: SandboxConfig) -> tuple[float, int, int, int]:
    """Resolve effective `(cpu, memory_mb, pids, disk_mb)` for a container create from the
    SPEC and the deployment CONFIG, treating the config as the hard MAXIMUM (EPIC H P1).

    Closes the spec-overridable-limits hole: `pids=0` resolves to the configured default
    (never Docker's "unlimited"); `pids`/`cpu`/`memory_mb` ABOVE the configured max are
    clamped down to it; a NEGATIVE value is rejected. A model-shaped spec therefore can
    never loosen `default_cpu` / `default_memory_mb` / `default_pids_limit`."""
    cpu = _bounded("cpu", spec.cpu, cfg.default_cpu)
    mem = int(_bounded("memory_mb", spec.memory_mb, cfg.default_memory_mb))
    pids = int(_bounded("pids", spec.pids, cfg.default_pids_limit))
    disk = int(_bounded("disk_mb", spec.disk_mb, cfg.default_disk_mb))
    return cpu, mem, pids, disk


def bounded_sidecar_cap(name: str, value: float) -> float:
    """Runtime LAST GATE for a policy/control SIDECAR resource cap (cpu / memory_mb
    / pids), the sidecar analogue of `_bounded`/`resolve_bounds` for the main sandbox.

    Unlike the main-sandbox path there is no model-shaped spec — the deployment config value
    IS the cap (the max). But `SandboxConfig` is intentionally hot-mutable (ConfigDict with
    no assignment validation, for Settings hot-apply), so the construction-time
    `@field_validator` does NOT protect against a POST-construction mutation
    (`cfg.sidecar_cpu = 0`). This is the last gate before the value is handed to
    Docker/Podman at create, where a 0 / negative / non-finite cap reads as UNLIMITED
    (`mem_limit` / `nano_cpus` / `cpu_quota` / `pids_limit` of 0 = no cap) — a silently
    DISABLED host-protection bound on the sidecar. Refuse it loudly instead of passing
    an unbounded sidecar through."""
    if not math.isfinite(value) or value <= 0:
        raise SandboxError(
            f"sandbox sidecar cap {name}={value!r} is invalid (must be a finite positive "
            "number); a 0/negative/non-finite sidecar cap would disable the host-protection "
            "limit (unlimited CPU/memory/PIDs on the egress proxy)"
        )
    return value


def nofile_ulimits(cfg: SandboxConfig) -> list[Any]:
    """Runtime-last-gate for the container file-descriptor ceiling."""
    soft = int(cfg.default_nofile_soft)
    hard = int(cfg.default_nofile_hard)
    if soft <= 0 or hard <= 0 or soft > hard:
        raise SandboxError(
            f"invalid nofile limits soft={soft} hard={hard}; limits must be positive "
            "and soft must not exceed hard"
        )
    # docker-py recognizes its Ulimit type directly; podman-py treats it as the
    # uppercase-key dict its payload renderer expects. A lowercase plain dict
    # works in Docker but crashes podman-py before the API call.
    from docker.types import Ulimit

    return [Ulimit(name="nofile", soft=soft, hard=hard)]


# Exit codes the `timeout` coreutil reports when it fires (SIGTERM / then SIGKILL).
TIMEOUT_EXIT_CODES = frozenset({124, 137})

SANDBOX_READ_TIMEOUT_S = 20

# The bounded exec/read/delete/list guest-script constants and their argv
# builders/result parsers live in `_container_parts.guest_scripts` (moved
# purely to shed module-size budget — embedded script text counts as logical
# source); re-imported here unchanged so every existing import path
# (`from ._container import bounded_exec_argv`, `_container._BOUNDED_EXEC_HELPER`,
# etc.) keeps resolving.
EXEC_CAPTURE_HEAD_BYTES = _guest_scripts_part.EXEC_CAPTURE_HEAD_BYTES
EXEC_CAPTURE_TAIL_BYTES = _guest_scripts_part.EXEC_CAPTURE_TAIL_BYTES
EXEC_CAPTURE_RETURN_BYTES = _guest_scripts_part.EXEC_CAPTURE_RETURN_BYTES
EXEC_CAPTURE_SPILL_BYTES = _guest_scripts_part.EXEC_CAPTURE_SPILL_BYTES
MAX_SANDBOX_READ_BYTES = _guest_scripts_part.MAX_SANDBOX_READ_BYTES
MAX_SANDBOX_LIST_ENTRIES = _guest_scripts_part.MAX_SANDBOX_LIST_ENTRIES
_BOUNDED_EXEC_HELPER = _guest_scripts_part._BOUNDED_EXEC_HELPER
_BOUNDED_READ_HELPER = _guest_scripts_part._BOUNDED_READ_HELPER
_BOUNDED_DELETE_HELPER = _guest_scripts_part._BOUNDED_DELETE_HELPER
_BOUNDED_LIST_HELPER = _guest_scripts_part._BOUNDED_LIST_HELPER
bounded_exec_argv = _guest_scripts_part.bounded_exec_argv
bounded_read_argv = _guest_scripts_part.bounded_read_argv
bounded_read_result = _guest_scripts_part.bounded_read_result
bounded_delete_argv = _guest_scripts_part.bounded_delete_argv
bounded_delete_result = _guest_scripts_part.bounded_delete_result
bounded_list_argv = _guest_scripts_part.bounded_list_argv
bounded_list_result = _guest_scripts_part.bounded_list_result



# Default upper bound on a docker-py / podman-py `reload()` call (Dispo #25 wedge-guard).
# A healthy reload is sub-ms; this is ~500x that — large enough to absorb a brief
# scheduler hiccup, small enough that a dead daemon can't wedge a session. The
# value is overridable per-instance (constructor kwarg, typically sourced from
# `SandboxConfig.reload_timeout_s` for hot-apply via the Settings layer). Never
# `None` (a missing config must still keep the loop bounded).
_DEFAULT_RELOAD_TIMEOUT_S: float = 0.5

# The user-facing dev-server port (the primary preview) plus a curated set of
# framework-default ports a build may legitimately bind (API on 3000, Vite's
# native 5173, …). Docker cannot add mappings to a RUNNING container, so the
# publishable set is declared at create time. Containment: nothing outside
# PUBLISHED_PORTS is ever reachable, and INTERNAL_PORTS (agent-server plumbing,
# e.g. the BP-08 kernel gateway) are never handed out as user URLs.
PREVIEW_PORT = 8000
# noVNC websockify port — the WebSocket-to-VNC bridge that the frontend's iframe
# connects to. VNC itself (port 5901) is loopback-bound inside the sandbox and is
# NOT in USER_PORTS; only the websockify bridge (NOVNC_PORT) is exposed via the
# existing per-conversation preview proxy (same auth/jail as the dev-server preview).
NOVNC_PORT = 6080
USER_PORTS: frozenset[int] = frozenset({8000, 3000, 5173, 8080, 5000, 4321, NOVNC_PORT})
INTERNAL_PORTS: frozenset[int] = frozenset({8899})
PUBLISHED_PORTS: frozenset[int] = USER_PORTS | INTERNAL_PORTS


def loopback_port_bindings(
    ports: Iterable[int] = PUBLISHED_PORTS,
    *,
    podman: bool = False,
) -> dict[str, tuple[str, None] | dict[str, str]]:
    """Publish curated ports on daemon loopback only, with random host ports.

    This closes sibling/tailnet/public-IP hairpins. INTERNAL_PORTS remain mapped
    solely for the host-side kernel client, but never on a non-loopback address.
    """
    selected = frozenset(ports)
    if not selected <= PUBLISHED_PORTS:
        raise SandboxError("refusing to publish a port outside the curated sandbox set")
    if podman:
        # podman-py rejects Docker's (host_ip, None) random-port tuple. Its
        # native payload shape omits host_port while retaining host_ip.
        return {f"{port}/tcp": {"ip": "127.0.0.1"} for port in sorted(selected)}
    return {f"{port}/tcp": ("127.0.0.1", None) for port in sorted(selected)}


# The SINGLE source of truth for which sandbox backends can actually run the noVNC
# live-view stack (Xvfb + x11vnc + websockify). Those binaries ship ONLY in the
# container image (deploy/sandbox/Dockerfile) AND the live-jail security model (P5:
# loopback-bound x11vnc, view-only, per-conversation jail, gated published port) was
# designed/accepted for the strong-isolation gVisor backend. So gVisor is the only
# backend that gets the live stream: the `process` dev backend has no stack at all
# (Xvfb isn't on the host → live_start fails — the "live_start_failed" bug); the
# `local` shared-kernel backend and the `podman` deployment stub do not stream here.
# Everything reads THIS set — the runtime live-ready/live-url honesty (preview.py via
# SandboxSession.supports_live_view), the Settings enable-guard (config_state), and the
# frontend's BACKEND_META mirror — so the answer can never diverge across surfaces.
LIVE_VIEW_BACKENDS: frozenset[str] = frozenset({"gvisor"})


def sealed(spec: SandboxSpec) -> bool:
    """Network is SEALED unless the capability set grants it (§7 deny-by-default):
    a non-empty egress allowlist, or NETWORK in `permitted`. Default => sealed."""
    return (
        not spec.egress_allow and not spec.public_web and Capability.NETWORK not in spec.permitted
    )


# The single egress proxy port a filtered box reaches its allowlisting sidecar on.
EGRESS_PROXY_PORT = 8888


def egress_mode(spec: SandboxSpec) -> str:
    """Resolve a spec's egress posture — the fix for the old
    binary (none|bridge) that silently gave an allowlisted box full network:

      • "sealed"   — no allowlist, no NETWORK cap → an internal no-NAT guest
                     network plus a hardened loopback-only control sidecar. The
                     guest has no proxy route; only INTERNAL_PORTS flow host→guest.
      • "filtered" — a non-empty `egress_allow` → an allowlisting PROXY sidecar
                     enforces the list per-connection; the box's only route out is
                     the proxy (internal, no-NAT network). THIS is what makes the
                     allowlist real instead of a false guarantee.
      • "public"   — arbitrary public HTTP(S), but non-global/host/sibling IPs
                     are denied by the isolated proxy sidecar.
        Legacy NETWORK grants resolve to the same public-only boundary; there is
        no model-shaped route to the daemon's raw bridge.

    `filtered` takes precedence: an allowlist always means "only these", even if the
    NETWORK capability is also present."""
    if spec.egress_allow:
        return "filtered"
    if spec.public_web or Capability.NETWORK in spec.permitted:
        return "public"
    return "sealed"


def format_allow(entries: frozenset[str]) -> str:
    """Render an allowlist as the comma string the proxy CLI (`--allow`) consumes.
    Sorted for determinism (stable container args → reproducible runs)."""
    return ",".join(sorted(e.strip() for e in entries if e.strip()))


def proxy_env(proxy_host: str, port: int = EGRESS_PROXY_PORT) -> dict[str, str]:
    """The HTTP(S)_PROXY environment a filtered sandbox gets so proxy-aware clients
    (curl, pip, npm, requests) route through the allowlisting sidecar. Defense in
    depth only — the no-NAT network is the real containment; a client that ignores
    these vars still has no route except the proxy. Both cases (lower/upper) are set
    because different tools read different ones."""
    url = f"http://{proxy_host}:{port}"
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        # never proxy loopback (a dev server talking to itself)
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
    }


def proxy_run_argv(
    allow: str,
    port: int = EGRESS_PROXY_PORT,
    *,
    public_only: bool = False,
    deny_ips: frozenset[str] = frozenset(),
    deny_hosts: frozenset[str] = frozenset(),
) -> list[str]:
    """The command that runs the allowlisting proxy inside the sidecar. The proxy
    script is `put_archive`'d to /egress_proxy.py first (it's stdlib-only, so the
    base image's python3 runs it with no install)."""
    argv = ["python3", "/egress_proxy.py", "--port", str(port), "--allow", allow]
    if public_only:
        argv.append("--public-only")
    if deny_ips:
        argv.extend(["--deny-ip", ",".join(sorted(deny_ips))])
    if deny_hosts:
        argv.extend(["--deny-host", ",".join(sorted(deny_hosts))])
    return argv


# Host-address discovery (`collect_host_deny_ips`, `discover_local_host_ips`,
# `discover_remote_host_ips`) and `SshLoopbackTunnelManager` live in
# `_container_parts.host_discovery` (moved to shed module-size budget);
# re-imported here unchanged so every existing import path keeps resolving.
# `subprocess` stays imported above (even though nothing in THIS module calls
# it directly anymore) because a test patches `_container.subprocess.Popen` —
# mutating the shared stdlib module object, which every importer (including
# host_discovery's own `import subprocess`) observes identically regardless
# of which file's code runs.
collect_host_deny_ips = _host_discovery_part.collect_host_deny_ips
discover_local_host_ips = _host_discovery_part.discover_local_host_ips
discover_remote_host_ips = _host_discovery_part.discover_remote_host_ips
SshLoopbackTunnelManager = _host_discovery_part.SshLoopbackTunnelManager

def proxy_readiness_argv(port: int = EGRESS_PROXY_PORT) -> list[str]:
    """Bounded sidecar probe; setup fails if the policy process did not bind."""
    script = (
        "import socket,sys,time; p=int(sys.argv[1]); "
        "ok=False; "
        "\nfor _ in range(50):\n"
        " s=socket.socket(); s.settimeout(.1)\n"
        " try:\n  ok=s.connect_ex(('127.0.0.1',p))==0\n"
        " finally:\n  s.close()\n"
        " if ok: break\n time.sleep(.1)\n"
        "raise SystemExit(0 if ok else 1)"
    )
    return ["python3", "-c", script, str(port)]


def inbound_readiness_argv(ports: Iterable[int]) -> list[str]:
    """Bounded proof that every selected inbound sidecar listener is live."""

    selected = sorted(frozenset(ports))
    if not selected or not set(selected) <= PUBLISHED_PORTS:
        raise SandboxError("invalid inbound transport readiness port set")
    script = (
        "import socket,sys,time\n"
        f"ports={selected!r}\n"
        "deadline=time.monotonic()+5\n"
        "def ready():\n"
        "    for port in ports:\n"
        "        with socket.socket() as sock:\n"
        "            sock.settimeout(0.2)\n"
        "            if sock.connect_ex(('127.0.0.1', port)) != 0:\n"
        "                return False\n"
        "    return True\n"
        "while time.monotonic() < deadline:\n"
        "    if ready():\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.1)\n"
        "sys.exit(1)\n"
    )
    return ["python3", "-c", script]


class ContainerInstance:
    """A running container (gVisor or Podman). Tools execute against it; the
    workspace is reached only through the file methods (exec/cp), never a path."""

    #: Network-ISOLATED backend (bridge/no-NAT; preview ports are PUBLISHED, not
    #: shared). Inside the box 127.0.0.1:8000 IS the build's own app, so control
    #: ports are NOT reserved and 8000 is the canonical verifiable preview port.
    shares_host_network: bool = False

    # Set by the service for every container mode: sealed boxes own a control-only
    # sidecar + internal net; filtered/public boxes own a policy sidecar + internal
    # net. Class attrs keep teardown uniform and provide fail-safe defaults for
    # partial/test instances. create() shadows them with real instance resources.
    _egress_sidecar: Any | None = None
    _egress_network: Any | None = None
    _host_service_relay_url: str | None = None

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
        workspace_uid: int = 1000,
        preview_host: str = "localhost",
        reload_timeout_s: float = _DEFAULT_RELOAD_TIMEOUT_S,
        workspace_volume: Any | None = None,
        loopback_tunnel: SshLoopbackTunnelManager | None = None,
    ) -> None:
        self.id = id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec
        self._container = container
        self._ws = container_workspace
        self._stop_timeout_s = stop_timeout_s
        # the host the published preview port is reachable at (localhost for a local
        # backend; the remote Docker host's tailnet IP for gVisor).
        self._preview_host = preview_host
        # The image's run-user uid (contract: `agent` = 1000). Written files are owned
        # by it so the sandbox user can EDIT them — put_archive defaults to uid 0 (root),
        # which a non-root container user can read but not modify.
        self._workspace_uid = workspace_uid
        # Local/Podman workspaces are explicit named volumes. Keep the object so
        # instance teardown can remove it even when the runtime only removes
        # anonymous attached volumes from container.remove(v=True).
        self._workspace_volume = workspace_volume
        self._loopback_tunnel = loopback_tunnel
        # Wedge-guard bound for `_safe_reload()` (Dispo #25). A hung or failing
        # docker/podman client must never block the event loop. The service sources
        # this from `SandboxConfig.reload_timeout_s` at create time (hot-apply).
        self._reload_timeout_s = reload_timeout_s
        self._destroyed = False
        # [P2 symlink-jail] cached guest-resolved workspace root (the mount itself is
        # never a symlink, so this is stable for the box's life). None until first probe.
        self._ws_real: str | None = None

    def _alive(self) -> None:
        if self._destroyed:
            raise SandboxError(f"sandbox instance {self.id} has been destroyed")

    @property
    def host_service_relay_url(self) -> str | None:
        """Capability-only internal URL; never includes the bearer credential."""
        return self._host_service_relay_url

    def _safe_reload(self, obj: Any = None) -> bool:
        """Bounded wrapper around `reload()`; NEVER wedges the event loop; see
        `_container_parts.failure_classification` for the full contract
        (the VM 201/202 incident this guards against)."""
        return _failure_classification_part.safe_reload(self, obj)

    def _classify_failure_sync(self, exc: Exception) -> SandboxError:
        """Sync classify: dead box vs per-op failure; see
        `_container_parts.failure_classification` for the full contract."""
        return _failure_classification_part.classify_failure_sync(self, exc)

    async def _confidently_alive(self) -> bool:
        """STRICT liveness re-verify for a death verdict; see
        `_container_parts.failure_classification` for the full contract."""
        return await _failure_classification_part.confidently_alive(self)

    async def _classify_failure_async(self, exc: Exception) -> SandboxError:
        """Classify a raw op throw, RE-VERIFYING before accepting a death
        verdict; see `_container_parts.failure_classification` for the full
        contract."""
        return await _failure_classification_part.classify_failure_async(self, exc)

    def _death_reason_from_attrs(self) -> str:
        """Best-effort read of the container's State for OOMKilled/ExitCode/
        Error; see `_container_parts.failure_classification` for the full
        contract."""
        return _failure_classification_part.death_reason_from_attrs(self)

    async def _guarded(self, fn: Any) -> Any:
        """Run a blocking container op in a thread; let already-typed SandboxErrors
        (file-not-found, path-escape) pass through, but classify a raw backend throw
        (which usually means the box died) as available/unavailable."""
        try:
            return await asyncio.to_thread(fn)
        except SandboxError:
            raise  # explicit, correctly-typed already
        except Exception as exc:  # noqa: BLE001
            raise await self._classify_failure_async(exc) from exc

    @property
    def workspace_path(self) -> str | None:
        """W5 — container workspaces live INSIDE the container, not on the host FS.
        Returns None so C18 file_exists and C1c DoD degrade to no-op gracefully
        (they check `getattr(sbx, 'workspace_path', None)` and skip when None)."""
        return None

    def _container_path(self, path: str) -> str:
        """Resolve `path` to an absolute path INSIDE the workspace, rejecting escapes
        (../, absolute). LEXICAL only — see `_resolve_guest_path` for the symlink jail
        the file ops actually use; this is the first (cheap, no-exec) gate."""
        path = strip_redundant_workspace_prefix(path)  # ROOT-2: workspace/foo → foo
        target = posixpath.normpath(posixpath.join(self._ws, path))
        if target != self._ws and not target.startswith(self._ws + "/"):
            # W1/codex round-6: a path escaping the jail is an ACCESS denial → typed so the
            # loop's bookkeeping handlers (except PermissionError/OSError) catch it.
            raise SandboxPermissionError(f"path escapes workspace: {path!r}")
        return target

    def _guest_run(self, argv: list[str]) -> tuple[int, bytes]:
        """Run a short argv IN the guest, returning (exit_code, stdout). Backend-specific
        transport — docker-py `exec_run` here; the Podman subclass overrides it to use the
        CLI native remote (podman-py exec is unusable over the remote API). Used only by
        the symlink-resolution guard, which MUST run inside the guest namespace."""
        res = self._container.exec_run(argv, demux=True)
        out = (res[1][0] if res[1] else b"") or b""
        rc = res[0] if res[0] is not None else -1
        return rc, out

    def _guest_realpath(self, path: str) -> str | None:
        """Resolve `path` to its REAL location inside the guest (`realpath -m`, which
        follows symlink components but does NOT require the final path to exist, so a
        not-yet-written file resolves too). Returns None if realpath is unavailable or
        errors — the caller then fails CLOSED (refuses the op): a path whose real
        location can't be verified is treated as outside the workspace, never trusted to
        the lexical jail (which is blind to guest symlinks)."""
        try:
            rc, out = self._guest_run(["realpath", "-m", "--", path])
        except Exception:  # noqa: BLE001 — resolution is best-effort hardening
            return None
        if rc != 0:
            return None
        line = out.decode("utf-8", "replace").strip()
        return line or None

    def _guest_ws_real(self) -> str | None:
        """The guest-resolved workspace root, cached (the mount is never a symlink)."""
        if self._ws_real is None:
            self._ws_real = self._guest_realpath(self._ws)
        return self._ws_real

    def _resolve_guest_path(self, path: str) -> str:
        """[P2] The REAL workspace jail the file ops use. `_container_path` is lexical —
        `posixpath.normpath` keeps `/workspace/../x` in bounds but is BLIND to guest
        symlinks, so a sandbox could `ln -s /etc /workspace/out` and then read
        `out/passwd` (lexically `/workspace/out/passwd`, really `/etc/passwd`). Here we
        additionally resolve the target IN THE GUEST (`realpath -m`) and require the
        resolved real path to stay under the resolved workspace, else fail closed
        (SandboxPermissionError). Returns the lexical container path to use for the op
        (the guest follows the now-verified-safe symlink itself)."""
        target = self._container_path(path)  # lexical gate first (rejects ../, absolute)
        real = self._guest_realpath(target)
        ws_real = self._guest_ws_real()
        if real is None or ws_real is None:
            # FAIL CLOSED: the real location couldn't be verified (realpath missing/broken,
            # or the guest exec failed). The lexical path is BLIND to guest symlinks, so
            # trusting it here would reopen the symlink escape. Refuse instead.
            raise SandboxPermissionError(
                f"cannot verify real path stays in workspace (realpath unavailable): {path!r}"
            )
        ws_prefix = ws_real.rstrip("/") + "/"
        if real != ws_real and not real.startswith(ws_prefix):
            raise SandboxPermissionError(f"path escapes workspace via symlink: {path!r}")
        return target

    async def atomic_write(self, path: str, data: bytes) -> None:
        """CD-TOOLS-3/4: atomically commit `data` to `path` IN THE GUEST; see
        `_container_parts.atomic_write` for the full contract (the staged,
        SELinux-safe commit-via-rename)."""
        await _atomic_write_part.atomic_write(self, path, data)

    async def _commit_file_batch(
        self,
        mutations: Iterable[SandboxFileMutation],
        *,
        commit_last: Iterable[str] = (),
    ) -> SandboxFileBatchResult:
        """Commit a deterministic file plan through one container crossing."""
        return await _file_batch_part.commit_file_batch(
            self,
            tuple(mutations),
            commit_last=tuple(commit_last),
        )

    async def resolve_relpath(self, path: str) -> str:
        """CD-TOOLS-4: the REAL (symlink-followed) guest path RELATIVE to the workspace root,
        forward-slashed — so the governed-artifact guard sees through a symlink that reaches INTO
        `.disco/`. Reuses the P2 guest-realpath jail; fails closed (SandboxPermissionError) when
        the real location can't be verified, exactly like the file ops."""
        self._alive()

        def _resolve() -> str:
            self._resolve_guest_path(path)  # enforce the jail (raises on escape / unverifiable)
            real = self._guest_realpath(self._container_path(path))
            ws_real = self._guest_ws_real()
            if real is None or ws_real is None:
                raise SandboxPermissionError(
                    f"cannot verify real path stays in workspace: {path!r}"
                )
            rel = posixpath.relpath(real, ws_real)
            return "." if rel == "." else rel

        return await self._guarded(_resolve)

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        """Run `cmd` in the live container, capturing stdout/stderr/exit code. The
        timeout is enforced INSIDE the container (the `timeout` coreutil), with an
        outer backstop in case the exec call hangs. A killed command is reported with
        `timed_out=True`, not raised — partial output is preserved."""
        self._alive()
        wrapped = bounded_exec_argv(cmd, timeout_s)

        def _run() -> Any:
            return self._container.exec_run(wrapped, demux=True, workdir=self._ws)

        try:
            res = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 15)
        except TimeoutError:
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"command exceeded its {timeout_s}s timeout",
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 — classify: dead box vs per-op failure
            raise await self._classify_failure_async(exc) from exc

        exit_code = res[0] if res[0] is not None else -1
        out, err = res[1] if res[1] is not None else (None, None)
        return ExecResult(
            exit_code=exit_code,
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
            timed_out=exit_code in TIMEOUT_EXIT_CODES,
        )

    async def read_file(self, path: str) -> bytes:
        """Read one bounded, regular, non-symlink workspace file."""
        self._alive()

        def _read() -> bytes:
            target = self._resolve_guest_path(path)  # lexical + symlink jail (P2)
            relpath = posixpath.relpath(target, self._ws)
            res = self._container.exec_run(
                bounded_read_argv(self._ws, relpath), demux=True, workdir=self._ws
            )
            out, err = res[1] if res[1] is not None else (None, None)
            return bounded_read_result(path, int(res[0] or 0), out or b"", err or b"")

        try:
            return await asyncio.wait_for(self._guarded(_read), timeout=SANDBOX_READ_TIMEOUT_S)
        except TimeoutError as exc:
            raise OSError(
                errno.ETIMEDOUT,
                f"read_file {path!r}: exceeded the {SANDBOX_READ_TIMEOUT_S}s read timeout",
            ) from exc

    async def file_exists(self, path: str) -> bool:
        """[B4] Existence check INSIDE the container — `test -f` in the box, never
        a host `Path` check (the host has no view of the container FS; that host
        check is exactly the C18 false-positive this fixes). A missing file is
        False (the non-zero `test` exit, not a raise); a path that escapes the
        workspace jail is False. A genuinely dead box still surfaces through
        `_guarded` as a typed SandboxError, which C18 treats as unverifiable."""
        self._alive()

        def _test() -> bool:
            try:
                target = self._resolve_guest_path(path)  # lexical + symlink jail (P2)
            except SandboxError:
                return False
            res = self._container.exec_run(
                ["sh", "-c", 'test -f "$1" && test ! -L "$1"', "disco", target]
            )
            return res[0] == 0

        return await self._guarded(_test)

    async def write_file(self, path: str, data: bytes) -> None:
        """Binary-safe workspace write through the atomic SELinux-safe path."""
        await self.atomic_write(path, data)

    async def delete_file(self, path: str) -> None:
        """Unlink one regular file through guest-side no-follow directory descriptors."""
        self._alive()

        def _delete() -> None:
            target = self._container_path(path)  # lexical jail; helper enforces no-follow
            relpath = posixpath.relpath(target, self._ws)
            result = self._container.exec_run(bounded_delete_argv(self._ws, relpath), demux=True)
            err = (result[1][1] if result[1] else b"") or b""
            bounded_delete_result(path, int(result[0] or 0), err)

        await self._guarded(_delete)

    async def list_dir(self, path: str) -> list[str]:
        """List a workspace dir via `exec ls`."""
        self._alive()

        def _list() -> list[str]:
            target = self._resolve_guest_path(path)  # lexical + symlink jail (P2)
            res = self._container.exec_run(["ls", "-1A", "--", target], demux=True)
            if res[0] != 0:
                err = (res[1][1] if res[1] else b"") or b""
                raise_read_error(path, err, op="list_dir")  # W1: typed missing-dir error
            out = (res[1][0] if res[1] else b"") or b""
            return sorted(n for n in out.decode("utf-8", "replace").splitlines() if n)

        return await self._guarded(_list)

    async def list_dir_bounded(self, path: str, limit: int) -> tuple[list[tuple[str, str]], bool]:
        """List a bounded directory prefix entirely inside the guest."""

        self._alive()

        def _list() -> tuple[list[tuple[str, str]], bool]:
            target = self._resolve_guest_path(path)
            res = self._container.exec_run(bounded_list_argv(target, limit), demux=True)
            out = (res[1][0] if res[1] else b"") or b""
            err = (res[1][1] if res[1] else b"") or b""
            return bounded_list_result(path, int(res[0] or 0), out, err)

        return await self._guarded(_list)

    def display_url(self) -> str | None:
        return None  # no noVNC display on these backends (yet)

    def expose_port(self, port: int) -> str | None:
        """Return a browser-reachable URL for a published USER port (containment:
        only the curated USER_PORTS set is ever exposed; INTERNAL_PORTS are the
        agent-server's own plumbing and never become user URLs)."""
        if sealed(self.spec) or port not in USER_PORTS:
            return None
        mapping = self._resolve_mapping(port)
        if not mapping:
            return None
        return f"http://{mapping[0]}:{mapping[1]}"

    def internal_port_mapping(self, port: int) -> tuple[str, int] | None:
        """Resolve the published host mapping for an INTERNAL port only (BP-08).
        SECURITY: it must REFUSE USER_PORTS (the inverse gate) so it can never
        become a backdoor preview path."""
        if port not in INTERNAL_PORTS:
            return None
        return self._resolve_mapping(port)

    def _resolve_mapping(self, port: int) -> tuple[str, int] | None:
        """Read the Docker/Podman port binding off the sidecar (or the
        sandbox fallback); see `_container_parts.port_mapping` for the full
        contract."""
        return _port_mapping_part.resolve_mapping(self, port)

    def _teardown_egress_aux(self) -> None:
        """Best-effort teardown of the policy/control sidecar + internal
        network; see `_container_parts.lifecycle` for the full contract."""
        _lifecycle_part.teardown_egress_aux(self)

    async def destroy(self) -> None:
        """Stop + remove the container, then tear down the policy/control aux
        (if any); see `_container_parts.lifecycle` for the full contract."""
        await _lifecycle_part.destroy(self)


def _remove_container(container: Any, *, force: bool = True, volumes: bool = True) -> None:
    """Remove a docker/podman container and ask the runtime to drop attached volumes.

    docker-py and podman-py both expose the Docker API's ``v`` flag; some tests and
    old compatibility fakes only accept ``force``. Teardown is best-effort, so fall
    back only for signature incompatibility.
    """
    if container is None:
        return
    try:
        container.remove(force=force, v=volumes)
        return
    except TypeError:
        pass
    try:
        container.remove(force=force, volumes=volumes)
        return
    except TypeError:
        pass
    container.remove(force=force)


def _remove_volume(volume: Any, *, force: bool = True) -> None:
    """Best-effort named-volume remove across docker-py/podman-py and fakes."""
    if volume is None:
        return
    try:
        volume.remove(force=force)
        return
    except TypeError:
        pass
    volume.remove()


# `reconcile_orphan_aux_resources` (the dual-prefix orphan network/volume
# sweep) lives in `_container_parts.aux_reconcile` — moved to shed both its
# own logical-size and McCabe budget; re-imported here unchanged.
reconcile_orphan_aux_resources = _aux_reconcile_part.reconcile_orphan_aux_resources

def _create_named_volume(
    volumes: Any,
    *,
    name: str,
    labels: dict[str, str],
    disk_mb: int,
    workspace_uid: int,
) -> Any:
    """Create a real size-capped tmpfs workspace volume.

    A plain named volume or overlay ``storage_opt`` does not cap the mounted
    workspace. The local-volume tmpfs driver makes ``df /workspace`` and writes
    observe the declared cap on Docker and Podman, and volume removal destroys
    the ephemeral workspace.
    """
    opts = {
        "driver": "local",
        "driver_opts": {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": (
                f"size={disk_mb}m,uid={workspace_uid},gid={workspace_uid},mode=0750,nosuid,nodev"
            ),
        },
    }
    try:
        return volumes.create(name=name, labels=labels, **opts)
    except TypeError:
        # Compatibility fakes/older clients may not accept labels, but a real
        # backend must still accept the quota options; never fall back to an
        # uncapped volume.
        return volumes.create(name=name, **opts)

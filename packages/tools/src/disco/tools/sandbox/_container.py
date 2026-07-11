"""Shared container-backend logic — the part identical across the gVisor (docker-py)
and Podman (podman-py) backends, factored out so each service is just its create
path (tool-sandbox §5.1; the interface is unchanged).

Both SDKs expose a duck-type-compatible Container: `exec_run(cmd, demux, workdir)`,
`put_archive(path, data)`, `stop(timeout)`, `remove(force)`. So one `ContainerInstance`
drives both. File ops go through the container (exec/cp), never a host path, so they
work over any transport (local socket or Docker/Podman-over-SSH).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import ipaddress
import json
import logging
import math
import posixpath
import secrets
import socket
import subprocess
import tarfile
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

import psutil

from ..anatomy import Capability
from .base import (
    ExecResult,
    SandboxError,
    SandboxPermissionError,
    SandboxSpec,
    SandboxUnavailableError,
    raise_read_error,
    strip_redundant_workspace_prefix,
)

if TYPE_CHECKING:
    from .config import SandboxConfig

_LOG = logging.getLogger(__name__)

# Re-verify cadence/window for confirming a container is REALLY dead (vs a transient
# docker/podman API 404 burst, which under concurrent load can last ~1s) before accepting a
# death verdict + recreating. The window must OUTLAST a typical burst so a fresh probe can read
# the container running again; it is fully async (asyncio.sleep) so the event loop never blocks.
_DEATH_REVERIFY_BACKOFF_S = 0.25
_DEATH_REVERIFY_WINDOW_S = 2.0


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
    """Runtime LAST GATE for a filtered-egress PROXY SIDECAR resource cap (cpu / memory_mb
    / pids), the sidecar analogue of `_bounded`/`resolve_bounds` for the main sandbox.

    Unlike the main-sandbox path there is no model-shaped spec — the deployment config value
    IS the cap (the max). But `SandboxConfig` is intentionally hot-mutable (ConfigDict with
    no assignment validation, for Settings hot-apply), so the construction-time
    `@field_validator` does NOT protect against a POST-construction mutation
    (`cfg.sidecar_cpu = 0`). This is the last gate before the value is handed to
    Docker/Podman at create, where a 0 / negative / non-finite cap reads as UNLIMITED
    (`mem_limit` / `nano_cpus` / `cpu_quota` / `pids_limit` of 0 = no cap) — a silently
    DISABLED host-protection bound on the egress proxy. Refuse it loudly instead of passing
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

# S-W5 D3: the sandbox-to-host boundary is bounded before docker-py/podman-py
# materializes a response. The in-guest helper streams arbitrary command output
# into fixed-size head/tail buffers and a bounded workspace spill; only that
# bounded projection crosses the container API.
EXEC_CAPTURE_HEAD_BYTES = 64 * 1024
EXEC_CAPTURE_TAIL_BYTES = 64 * 1024
EXEC_CAPTURE_RETURN_BYTES = EXEC_CAPTURE_HEAD_BYTES + EXEC_CAPTURE_TAIL_BYTES
EXEC_CAPTURE_SPILL_BYTES = 1024 * 1024
MAX_SANDBOX_READ_BYTES = 32 * 1024 * 1024
SANDBOX_READ_TIMEOUT_S = 20

_BOUNDED_EXEC_HELPER = r"""
import os
import signal
import subprocess
import sys
import threading

timeout_s = float(sys.argv[1])
command = sys.argv[2]
token = sys.argv[3]
head_cap = int(sys.argv[4])
tail_cap = int(sys.argv[5])
return_cap = int(sys.argv[6])
spill_cap = int(sys.argv[7])
spill_dir = os.path.join(os.getcwd(), ".disco", "spills")
os.makedirs(spill_dir, mode=0o700, exist_ok=True)


class Capture:
    def __init__(self, kind):
        self.kind = kind
        self.total = 0
        self.small = bytearray()
        self.head = bytearray()
        self.tail = bytearray()
        self.relpath = os.path.join(".disco", "spills", token + "." + kind + ".log")
        self.path = os.path.join(os.getcwd(), self.relpath)
        self.spilled = 0
        self.file = open(self.path, "wb")

    def feed(self, chunk):
        self.total += len(chunk)
        if len(self.small) <= return_cap:
            room = return_cap + 1 - len(self.small)
            self.small.extend(chunk[:room])
        if len(self.head) < head_cap:
            self.head.extend(chunk[: head_cap - len(self.head)])
        self.tail.extend(chunk)
        if len(self.tail) > tail_cap:
            del self.tail[:-tail_cap]
        if self.spilled < spill_cap:
            part = chunk[: spill_cap - self.spilled]
            self.file.write(part)
            self.spilled += len(part)

    def finish(self):
        self.file.close()
        if self.total <= return_cap:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            return bytes(self.small[:self.total])
        dropped = max(0, self.total - len(self.head) - len(self.tail))
        marker = (
            "\n[disco: output truncated at sandbox boundary; "
            + str(self.total)
            + " bytes total, "
            + str(dropped)
            + " omitted; bounded prefix saved at "
            + self.relpath
            + " (max "
            + str(spill_cap)
            + " bytes)]\n"
        ).encode()
        return bytes(self.head) + marker + bytes(self.tail)


def drain(stream, capture):
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        capture.feed(chunk)


out = Capture("stdout")
err = Capture("stderr")
proc = subprocess.Popen(
    command,
    shell=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
)
t_out = threading.Thread(target=drain, args=(proc.stdout, out), daemon=True)
t_err = threading.Thread(target=drain, args=(proc.stderr, err), daemon=True)
t_out.start()
t_err.start()
timed_out = False
try:
    rc = proc.wait(timeout=timeout_s)
except subprocess.TimeoutExpired:
    timed_out = True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        rc = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        rc = 137
t_out.join(timeout=5)
t_err.join(timeout=5)
out_bytes = out.finish()
err_bytes = err.finish()
try:
    os.rmdir(spill_dir)
    os.rmdir(os.path.dirname(spill_dir))
except OSError:
    pass
sys.stdout.buffer.write(out_bytes)
sys.stderr.buffer.write(err_bytes)
raise SystemExit(124 if timed_out else rc)
"""


def bounded_exec_argv(command: str, timeout_s: int, *, python: str = "python3") -> list[str]:
    """Return an argv whose stdout/stderr crossing the runtime API is bounded."""
    return [
        python,
        "-c",
        _BOUNDED_EXEC_HELPER,
        str(timeout_s),
        command,
        secrets.token_hex(8),
        str(EXEC_CAPTURE_HEAD_BYTES),
        str(EXEC_CAPTURE_TAIL_BYTES),
        str(EXEC_CAPTURE_RETURN_BYTES),
        str(EXEC_CAPTURE_SPILL_BYTES),
    ]


_BOUNDED_READ_HELPER = r"""
import errno
import os
import stat
import sys

root, relpath, cap_raw = sys.argv[1:4]
cap = int(cap_raw)
parts = [part for part in relpath.split("/") if part not in ("", ".")]


def fail(kind, detail, code):
    sys.stderr.write("DISCO_READ_" + kind + ":" + detail[:512])
    raise SystemExit(code)


if not parts:
    fail("DENIED", "invalid relative path", 45)

dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
opened = []
try:
    current = os.open(root, dir_flags)
    opened.append(current)
    for part in parts[:-1]:
        if part == "..":
            if len(opened) == 1:
                fail("DENIED", "path escapes workspace", 45)
            os.close(opened.pop())
            current = opened[-1]
            continue
        current = os.open(part, dir_flags, dir_fd=current)
        opened.append(current)
    if parts[-1] == "..":
        fail("DENIED", "directory paths are not readable files", 45)
    fd = os.open(parts[-1], file_flags, dir_fd=current)
    opened.append(fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        fail("DENIED", "only regular, non-symlink files may be read", 45)
    if info.st_size > cap:
        fail("TOO_LARGE", str(info.st_size), 46)
    remaining = cap + 1
    chunks = []
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > cap:
        fail("TOO_LARGE", "file grew while being read", 46)
    sys.stdout.buffer.write(data)
except FileNotFoundError as exc:
    fail("MISSING", str(exc), 44)
except OSError as exc:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
        fail("DENIED", str(exc), 45)
    fail("ERROR", str(exc), 47)
finally:
    for descriptor in reversed(opened):
        try:
            os.close(descriptor)
        except OSError:
            pass
"""


def bounded_read_argv(workspace: str, relpath: str, *, python: str = "python3") -> list[str]:
    """Open and read a workspace file through no-follow directory descriptors."""
    return [python, "-c", _BOUNDED_READ_HELPER, workspace, relpath, str(MAX_SANDBOX_READ_BYTES)]


def bounded_read_result(path: str, rc: int, out: bytes, err: bytes) -> bytes:
    """Map the guest helper's bounded result to the sandbox read contract."""
    detail = err.decode("utf-8", "replace")
    if rc == 0:
        return out
    if detail.startswith("DISCO_READ_MISSING:"):
        raise FileNotFoundError(path)
    if detail.startswith("DISCO_READ_DENIED:"):
        raise SandboxPermissionError(
            f"read_file {path!r}: only regular, non-symlink workspace files may be read"
        )
    if detail.startswith("DISCO_READ_TOO_LARGE:"):
        raise OSError(
            errno.EFBIG,
            f"read_file {path!r} exceeds the {MAX_SANDBOX_READ_BYTES}-byte transfer cap",
        )
    raise OSError(f"read_file {path!r} failed safely: {detail or f'exit {rc}'}")


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


def loopback_port_bindings(*, podman: bool = False) -> dict[str, tuple[str, None] | dict[str, str]]:
    """Publish curated ports on daemon loopback only, with random host ports.

    This closes sibling/tailnet/public-IP hairpins. INTERNAL_PORTS remain mapped
    solely for the host-side kernel client, but never on a non-loopback address.
    """
    if podman:
        # podman-py rejects Docker's (host_ip, None) random-port tuple. Its
        # native payload shape omits host_port while retaining host_ip.
        return {f"{port}/tcp": {"ip": "127.0.0.1"} for port in sorted(PUBLISHED_PORTS)}
    return {f"{port}/tcp": ("127.0.0.1", None) for port in sorted(PUBLISHED_PORTS)}


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

      • "sealed"   — no allowlist, no NETWORK cap → no network at all.
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


def collect_host_deny_ips(
    configured: list[str],
    endpoint_host: str,
    *,
    include_local_interfaces: bool,
    additional_host_ips: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Resolve host-owned addresses before a sandbox network is created.

    The daemon endpoint is always load-bearing: failure to identify it aborts
    setup. For a local daemon, hostname and outbound-interface discovery add the
    host's other visible addresses. Explicit entries cover additional interfaces
    on remote multi-homed hosts.
    """

    result: set[str] = set()
    for raw in [*configured, *additional_host_ips]:
        try:
            result.add(str(ipaddress.ip_address(raw.strip())))
        except ValueError as exc:
            raise SandboxError(f"invalid host_ip_blocklist address {raw!r}") from exc

    def _resolve(host: str, *, required: bool) -> None:
        clean = host.strip().strip("[]")
        if not clean:
            if required:
                raise SandboxError("sandbox daemon host address is empty")
            return
        try:
            result.add(str(ipaddress.ip_address(clean)))
            return
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(clean, 0, type=socket.SOCK_STREAM)
        except OSError as exc:
            if required:
                raise SandboxError(
                    f"cannot resolve sandbox daemon host {clean!r} for egress denial"
                ) from exc
            return
        for _family, _kind, _proto, _canon, sockaddr in infos:
            result.add(str(ipaddress.ip_address(sockaddr[0])))

    _resolve(endpoint_host, required=True)
    if include_local_interfaces:
        result.update(discover_local_host_ips())
    return frozenset(result)


def discover_local_host_ips() -> frozenset[str]:
    """Inventory every address on the local sandbox-daemon host.

    Resolving the hostname and probing the default route each reveal only a
    subset on a multi-homed machine. The deny policy must also include secondary
    public, VPN, and tailnet interfaces, so an unavailable/empty inventory fails
    closed instead of silently omitting a host-owned global address.
    """
    try:
        interfaces = psutil.net_if_addrs()
    except Exception as exc:  # noqa: BLE001 — policy construction must fail closed
        raise SandboxError("could not inventory local sandbox host interfaces") from exc
    addresses: set[str] = set()
    for entries in interfaces.values():
        for entry in entries:
            if entry.family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            raw = str(entry.address).split("%", 1)[0]
            try:
                addresses.add(str(ipaddress.ip_address(raw)))
            except ValueError as exc:
                raise SandboxError(
                    f"local sandbox host returned invalid interface address {entry.address!r}"
                ) from exc
    if not addresses:
        raise SandboxError("local sandbox host interface inventory was empty")
    return frozenset(addresses)


def _ssh_endpoint_argv(endpoint: str, *, connect_timeout: int = 8) -> list[str]:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "ssh" or not parsed.hostname:
        raise SandboxError(f"not an SSH sandbox endpoint: {endpoint!r}")
    argv = [
        "ssh",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "BatchMode=yes",
    ]
    if parsed.username:
        argv.extend(["-l", urllib.parse.unquote(parsed.username)])
    if parsed.port:
        argv.extend(["-p", str(parsed.port)])
    argv.extend(["--", parsed.hostname])
    return argv


def discover_remote_host_ips(endpoint: str) -> frozenset[str]:
    """Inventory every interface address on a trusted remote daemon host.

    The policy cannot claim to deny host-owned public addresses based only on
    the SSH endpoint (often a tailnet address). Inventory is refreshed for every
    sandbox so a newly-added public interface cannot hide behind a stale cache.
    If inventory cannot be obtained, sandbox creation fails closed instead of
    starting a public-egress surface.
    """
    argv = [*_ssh_endpoint_argv(endpoint), "ip", "-j", "address", "show"]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, trusted configured host
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxError(
            f"could not inventory remote sandbox host interfaces at {endpoint!r}"
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SandboxError(
            "remote sandbox host interface inventory failed: "
            + (detail[-1] if detail else f"ssh exited {proc.returncode}")
        )
    try:
        payload = json.loads(proc.stdout)
        addresses = {
            str(ipaddress.ip_address(info["local"]))
            for interface in payload
            for info in interface.get("addr_info", [])
            if info.get("local")
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SandboxError("remote sandbox host returned invalid interface inventory") from exc
    if not addresses:
        raise SandboxError("remote sandbox host interface inventory was empty")
    return frozenset(addresses)


class SshLoopbackTunnelManager:
    """Expose remote daemon-loopback mappings only on local loopback."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._lock = threading.Lock()
        self._forwards: dict[int, tuple[int, subprocess.Popen[bytes]]] = {}

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def forward(self, remote_port: int) -> tuple[str, int]:
        with self._lock:
            existing = self._forwards.get(remote_port)
            if existing is not None and existing[1].poll() is None:
                return "127.0.0.1", existing[0]
            for _attempt in range(3):
                local_port = self._available_port()
                # SSH options must precede the destination. `_ssh_endpoint_argv`
                # ends in `-- host`, so move the forwarding options before it.
                base = _ssh_endpoint_argv(self._endpoint)
                destination = base[-2:]
                argv = [
                    *base[:-2],
                    "-N",
                    "-T",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-o",
                    "ServerAliveInterval=15",
                    "-o",
                    "ServerAliveCountMax=2",
                    "-L",
                    f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                    *destination,
                ]
                proc = subprocess.Popen(  # noqa: S603 — fixed argv, configured SSH endpoint
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        with socket.create_connection(("127.0.0.1", local_port), timeout=0.1):
                            self._forwards[remote_port] = (local_port, proc)
                            return "127.0.0.1", local_port
                    except OSError:
                        time.sleep(0.05)
                with contextlib.suppress(Exception):
                    proc.terminate()
                    proc.wait(timeout=1)
            raise SandboxError(
                f"could not establish local SSH forwarding for remote sandbox port {remote_port}"
            )

    def close(self) -> None:
        with self._lock:
            forwards = list(self._forwards.values())
            self._forwards.clear()
        for _port, proc in forwards:
            if proc.poll() is not None:
                continue
            with contextlib.suppress(Exception):
                proc.terminate()
                proc.wait(timeout=2)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=1)


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


class ContainerInstance:
    """A running container (gVisor or Podman). Tools execute against it; the
    workspace is reached only through the file methods (exec/cp), never a path."""

    #: Network-ISOLATED backend (bridge/no-NAT; preview ports are PUBLISHED, not
    #: shared). Inside the box 127.0.0.1:8000 IS the build's own app, so control
    #: ports are NOT reserved and 8000 is the canonical verifiable preview port.
    shares_host_network: bool = False

    # Set by the service for a "filtered" box (allowlist + proxy sidecar + internal
    # net); None otherwise. Class attrs (not __init__ params) so the destroy()
    # teardown is inherited uniformly across every backend that supports the
    # allowlisting aux (gVisor / Podman / local). The service mutates the INSTANCE
    # attr in create() — that shadows the class attr with a real object.
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
        """Bounded wrapper around `reload()` (docker-py / podman-py) on `obj`, which
        defaults to `self._container`. [FIX6] passing the egress SIDECAR lets
        `_resolve_mapping` refresh the sidecar's published-port bindings under the
        SAME wedge-guard (the filtered-box preview is published on the sidecar).

        A hung or failing client must NEVER wedge the event loop. The original
        VM 201/202 incident that motivated this guard: a docker daemon stall
        caused the agent loop to block indefinitely inside `reload()`; the
        whole conversation hung waiting for an HTTP call that never returned.

        Mechanism: run the blocking call in a daemon thread, then wait on a
        `threading.Event` with `_reload_timeout_s`. The thread is `daemon=True`
        so even if it never returns, it cannot block process exit (the main
        thread bails out cleanly, the test fake's blocker is released by the
        test's `finally:` clause). Returning status: True if the reload
        succeeded AND the container reports 'running'; False on either a
        non-running state or a raised error.

        On ANY of:
          - the daemon thread didn't finish in `_reload_timeout_s` (hung client)
          - the daemon thread raised (dead daemon, connection refused, etc.)
        we raise a typed `SandboxUnavailableError`. The caller handles it
        uniformly — `_classify_failure` types the box as dead (session
        re-creates), `_resolve_mapping` returns None (no URL). The loop is
        NEVER blocked.

        Cost on a HEALTHY client: one thread spawn + one Event wait per call.
        A healthy reload is sub-ms, the guard's overhead is on the same order
        — measured: 1000 healthy reloads complete in <100ms on any CI runner
        (no measurable added latency).
        """
        target = self._container if obj is None else obj
        done = threading.Event()
        # Result slot: written by the worker thread, read by the main thread
        # AFTER `done.wait()` returns. A small dict keeps the assignment atomic
        # under the GIL (no lock needed for the simple `setattr` we do here).
        result: dict[str, Any] = {"exc": None, "status": "unknown"}

        def _runner() -> None:
            try:
                target.reload()
                result["status"] = getattr(target, "status", "unknown")
            except Exception as exc:  # noqa: BLE001 — surface as typed infra error
                result["exc"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=_runner, daemon=True, name="disco-sbx-reload")
        thread.start()
        if not done.wait(timeout=self._reload_timeout_s):
            # Hung: the daemon thread is still running (we can't safely
            # interrupt a blocking C call in docker-py / podman-py), but
            # daemon=True means it can't block process exit, and we return
            # control to the event loop with a typed error. The caller
            # treats it as "box is dead / client is wedged" — uniformly.
            raise SandboxUnavailableError(
                f"sandbox client hung on reload() (>{self._reload_timeout_s}s); "
                f"the container state is unverifiable"
            )
        if result["exc"] is not None:
            raise SandboxUnavailableError(f"sandbox client failed on reload(): {result['exc']}")
        return result["status"] == "running"

    def _classify_failure_sync(self, exc: Exception) -> SandboxError:
        """Sync classify: a container op threw. If the box is no longer running (a SINGLE
        `_safe_reload` miss), type it dead (SandboxUnavailableError → session RE-CREATES);
        else a per-op SandboxError. Runs OFF the event loop (via to_thread from the async
        wrapper). Does NOT log the death warning — `_classify_failure_async` logs it ONLY
        after re-verify CONFIRMS death, so a TRANSIENT API 404 never emits a false death
        diagnostic. (Dispo #25 wedge-guard bounds the probe.)"""
        alive = False
        try:
            alive = self._safe_reload()
        except SandboxUnavailableError:
            alive = False
        if not alive:
            return SandboxUnavailableError(f"sandbox container died mid-session: {exc}")
        return SandboxError(f"sandbox op failed in {self.id}: {exc}")

    async def _confidently_alive(self) -> bool:
        """STRICT liveness re-verify for a death verdict: FRESH probes across a bounded
        ~`_DEATH_REVERIFY_WINDOW_S` window (long enough to OUTLAST a transient docker/podman
        404 burst, ~1s under concurrent load). Returns True ONLY when a probe reads
        status=="running" (exactly what `_safe_reload` returns). ANY ambiguity — a raise,
        non-running, or the box dying for the whole window — returns False ⇒ the death stands.
        We override a death to "transient" only when we POSITIVELY CONFIRM running, because a
        real death misclassified as transient surfaces as repeated op failures (worse than a
        clean recreate). Off-loads each probe to a thread; sleeps are async so the event loop is
        never blocked (a real death just takes up to the window to confirm + recreate)."""
        deadline = time.monotonic() + _DEATH_REVERIFY_WINDOW_S
        while time.monotonic() < deadline:
            await asyncio.sleep(_DEATH_REVERIFY_BACKOFF_S)
            try:
                if await asyncio.to_thread(self._safe_reload):
                    return True
            except SandboxUnavailableError:
                pass
        return False

    async def _classify_failure_async(self, exc: Exception) -> SandboxError:
        """Classify a raw op throw, RE-VERIFYING before accepting a death verdict. A transient
        docker/podman API 404 (a simultaneous burst under concurrent load) momentarily fails the
        reload on a container that is actually ALIVE — declaring death there triggers a needless
        sandbox RECREATE that derails the build (live-captured: 4 deaths, 0 OOM, all exit=0). So
        when the sync classify says death, re-probe; ONLY downgrade to a retryable per-op error if
        `_confidently_alive()` CONFIRMS running. Else the death stands + is logged with its
        attributed reason. Used by BOTH op paths (`_guarded`, `exec_shell`)."""
        base = await asyncio.to_thread(self._classify_failure_sync, exc)
        if not isinstance(base, SandboxUnavailableError):
            return base
        if await self._confidently_alive():
            _LOG.info(
                "sandbox container %s: transient API error, container running on re-verify: %s",
                self.id,
                exc,
            )
            return SandboxError(f"transient sandbox API error in {self.id}: {exc}")
        # Death CONFIRMED across the re-verify window — attribute it (OOMKilled/exit) in BOTH
        # the WARNING log and the returned error so a recreate is no longer opaque.
        reason = self._death_reason_from_attrs()
        _LOG.warning("sandbox container %s died mid-session (%s): %s", self.id, reason, exc)
        return SandboxUnavailableError(f"sandbox container died mid-session ({reason}): {exc}")

    def _death_reason_from_attrs(self) -> str:
        """Best-effort read of the container's State (refreshed by _safe_reload) for
        OOMKilled / ExitCode / Error. NEVER raises (diagnostic on a failing path)."""
        try:
            state = (getattr(self._container, "attrs", {}) or {}).get("State", {}) or {}
            return (
                f"OOMKilled={state.get('OOMKilled')} exit={state.get('ExitCode')} "
                f"reason={state.get('Error') or ''}".strip()
            )
        except Exception:  # noqa: BLE001 — diagnostic only
            return "reason-unavailable"

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
        """CD-TOOLS-3/4: atomically commit `data` to `path` IN THE GUEST — stage to a RANDOM tmp
        sibling (so a model can't pre-create a predictable symlink there) then `mv -f` over the
        target (atomic on the same filesystem; mv replaces a symlinked target rather than following
        it). The .disco/ governed guard already ran in the tool, so the target is never governed."""
        self._alive()

        def _aw() -> None:
            self._resolve_guest_path(path)  # enforce the jail (raises on escape / unverifiable)
            # Replace the RESOLVED real target (follow a final symlink), matching ProcessSandbox's
            # _resolve().resolve() + os.replace and the backend's own write_file — so atomic_write
            # and write_file have identical symlink semantics (codex round-5 parity).
            # _guest_realpath is jail-checked by _resolve_guest_path above; for a new
            # file it is the lexical path.
            real_target = self._guest_realpath(self._container_path(path))
            if real_target is None:
                raise SandboxError(f"atomic_write {path!r}: cannot resolve real target")
            target = real_target
            parent = posixpath.dirname(target) or self._ws
            tmp_name = f".disco-tmp-{secrets.token_hex(8)}"
            self._guest_run(["mkdir", "-p", "--", parent])
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=tmp_name)
                info.size = len(data)
                info.uid = info.gid = self._workspace_uid
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
            if not self._container.put_archive(parent, buf.getvalue()):
                raise SandboxError(f"atomic_write {path!r} staging failed")
            tmp_path = posixpath.join(parent, tmp_name)
            # -T (--no-target-directory): a TRUE rename/replace. Without it, if `target` is an
            # existing directory or a symlink-to-directory, `mv` would move tmp INTO it (succeeding
            # while leaving target unchanged + stranding tmp). -T makes mv replace the target as a
            # non-directory, or fail (rc!=0) if it is a real dir — which we surface (codex round-4).
            rc, _ = self._guest_run(["mv", "-fT", "--", tmp_path, target])
            if rc != 0:
                self._guest_run(["rm", "-f", "--", tmp_path])
                raise SandboxError(f"atomic_write {path!r} rename failed (rc={rc})")

        await self._guarded(_aw)

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
        """Write a workspace file via `cp` (put_archive) — binary-safe."""
        self._alive()

        def _write() -> None:
            target = self._resolve_guest_path(path)  # lexical + symlink jail (P2)
            parent = posixpath.dirname(target) or self._ws
            name = posixpath.basename(target)
            self._container.exec_run(["mkdir", "-p", "--", parent])
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                # Own the file as the container's run-user (not root) so it's editable.
                info.uid = info.gid = self._workspace_uid
                # [BP-00 root-cause] TarInfo defaults mtime to 0 (epoch 1970), and
                # put_archive preserves it. Every agent write then lands with an
                # mtime that NEVER changes, so anything mtime-based lies: HTTP
                # If-Modified-Since answers 304 forever (the browser re-renders its
                # first cached copy of an edited file), make/vite see "unchanged".
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
            if not self._container.put_archive(parent, buf.getvalue()):
                raise SandboxError(f"write_file {path!r} failed")

        await self._guarded(_write)

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
        if sealed(self.spec) or port not in INTERNAL_PORTS:
            return None
        return self._resolve_mapping(port)

    def _resolve_mapping(self, port: int) -> tuple[str, int] | None:
        """Internal helper to read the Docker/Podman port binding. Goes through
        `_safe_reload` so a hung/raising client returns None (no URL) instead of
        wedging the loop. (Dispo #25 wedge-guard.)

        [FIX6] For a FILTERED box (an egress sidecar is attached) the published
        mapping lives on the SIDECAR, not the sandbox: the sandbox is on an
        internal no-NAT net and publishes nothing, while the dual-homed sidecar
        publishes the preview ports and forwards them inward. Sealed / open boxes
        (no sidecar) keep the original sandbox-binding path."""
        if port not in PUBLISHED_PORTS:
            return None
        source = self._container if self._egress_sidecar is None else self._egress_sidecar
        try:
            self._safe_reload(source)  # bounded; raises SandboxUnavailableError on hang/raise
            ports = source.attrs.get("NetworkSettings", {}).get("Ports") or {}
            binding = ports.get(f"{port}/tcp")
            if not binding:
                return None
            host_ip = str(binding[0].get("HostIp") or "")
            if host_ip not in {"127.0.0.1", "::1"}:
                # Fail closed if the daemon did not honor the loopback bind.
                return None
            if self._loopback_tunnel is not None:
                return self._loopback_tunnel.forward(int(binding[0]["HostPort"]))
            return host_ip, int(binding[0]["HostPort"])
        except Exception:  # noqa: BLE001 — no mapping yet / box gone / wedged client
            return None

    def _teardown_egress_aux(self) -> None:
        """Best-effort teardown of the filtered-egress aux (proxy sidecar + internal
        network). Both refs default to None for sealed/open boxes, so this is a
        no-op on those modes — only the filtered path sets them in the service's
        create(). The order MATTERS: the sidecar is on the internal net, so we
        remove the sidecar first, then the net (else the net remove errors out on
        an attached endpoint)."""
        sidecar, network = self._egress_sidecar, self._egress_network
        if sidecar is None and network is None:
            return
        if sidecar is not None:
            try:
                sidecar.stop(timeout=2)
            except Exception:  # noqa: BLE001 — best-effort; force-remove next
                pass
            try:
                _remove_container(sidecar)
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
        if network is not None:
            try:
                network.remove()
            except Exception:  # noqa: BLE001 — still-attached (we removed the sidecar
                # above, but the client may not see it yet) or already gone
                pass

    async def destroy(self) -> None:
        """Stop + remove the container, then tear down the policy-egress aux (if
        any). Every container backend uses an ephemeral, quota-capped workspace
        volume which is explicitly removed during teardown. The aux
        teardown is shared across every backend that supports the allowlisting
        proxy (E8) — see `_teardown_egress_aux`."""
        if self._destroyed:
            return
        self._destroyed = True

        def _teardown() -> None:
            try:
                self._container.stop(timeout=self._stop_timeout_s)
            except Exception:  # noqa: BLE001 — best-effort stop; force-remove next
                pass
            try:
                _remove_container(self._container)
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
            try:
                _remove_volume(self._workspace_volume)
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
            if self._loopback_tunnel is not None:
                self._loopback_tunnel.close()

        await asyncio.to_thread(_teardown)
        # Aux AFTER the sandbox: the sandbox is on the internal net; removing the
        # net while the sandbox still references it would error out.
        await asyncio.to_thread(self._teardown_egress_aux)


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

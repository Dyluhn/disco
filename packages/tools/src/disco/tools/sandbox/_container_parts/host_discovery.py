"""Host-address discovery + SSH loopback forwarding for the egress deny-list.

`collect_host_deny_ips` resolves host-owned addresses before a sandbox
network is created, so the egress proxy can refuse them even under a
"public" posture; `discover_local_host_ips`/`discover_remote_host_ips` do the
actual interface inventory (local psutil vs. a trusted `ssh ... ip -j address
show`). `SshLoopbackTunnelManager` exposes a remote daemon's loopback-bound
port mappings on local loopback only, for Docker/Podman-over-SSH.

`subprocess` is used here via a plain `import subprocess` (not
`from subprocess import ...`): a test monkeypatches `Popen` directly on the
shared `subprocess` module object via `_container.subprocess.Popen`, and that
patch reaches every importer of the real module identically regardless of
which file's code calls it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import socket
import subprocess
import threading
import time
import urllib.parse

import psutil

from ..base import SandboxError


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

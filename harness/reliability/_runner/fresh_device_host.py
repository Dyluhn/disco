"""Host preflight and command boundary for fresh-device certification."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.reliability.state import FAIL, INFRA, PASS


class FreshDeviceError(RuntimeError):
    status = FAIL


class InfraError(FreshDeviceError):
    status = INFRA


class ProductError(FreshDeviceError):
    status = FAIL


def _machine_fingerprint() -> str:
    parts: list[bytes] = []
    for candidate in (
        Path("/etc/machine-id"),
        Path("/var/lib/dbus/machine-id"),
        Path("/sys/class/dmi/id/product_uuid"),
    ):
        with contextlib.suppress(OSError):
            value = candidate.read_bytes().strip()
            if value:
                parts.append(value)
    if not parts:
        raise InfraError("machine has no stable machine-id or product UUID")
    return hashlib.sha256(b"\0".join(parts)).hexdigest()[:24]


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _safe_device_label(value: str) -> str:
    import re

    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:32] or "device"


@dataclass(frozen=True)
class Engine:
    binary: str
    compose: tuple[str, ...]
    sandbox_socket: str


class CommandRunner:
    def __init__(self, out: Path) -> None:
        self.out = out

    def run(
        self,
        name: str,
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 600,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        log_path = self.out / f"{name}.log"
        try:
            result = subprocess.run(
                list(command),
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            raise InfraError(f"{name} could not run: {type(exc).__name__}: {exc}") from exc
        log_path.write_text(result.stdout or "", encoding="utf-8")
        if check and result.returncode != 0:
            tail = (result.stdout or "")[-2_000:].strip()
            raise ProductError(f"{name} exited {result.returncode}: {tail}")
        return result


def _detect_engine(runner: CommandRunner) -> Engine:
    forced = os.environ.get("DISCO_FRESH_CONTAINER_CLI", "").strip()
    candidates = [forced] if forced else ["docker", "podman"]
    for binary in candidates:
        if not binary or shutil.which(binary) is None:
            continue
        info = runner.run(
            f"preflight-{binary}-info",
            [binary, "info"],
            timeout=60,
            check=False,
        )
        if info.returncode != 0:
            continue
        compose = runner.run(
            f"preflight-{binary}-compose",
            [binary, "compose", "version"],
            timeout=60,
            check=False,
        )
        if compose.returncode != 0:
            continue
        if binary == "docker":
            socket_path = "/var/run/docker.sock"
        else:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            socket_path = f"{runtime}/podman/podman.sock"
            if not Path(socket_path).exists() and shutil.which("systemctl"):
                runner.run(
                    "preflight-podman-socket-start",
                    ["systemctl", "--user", "start", "podman.socket"],
                    timeout=60,
                    check=False,
                )
        if not Path(socket_path).exists():
            raise InfraError(f"{binary} API socket is absent: {socket_path}")
        return Engine(
            binary=binary,
            compose=(binary, "compose"),
            sandbox_socket=socket_path,
        )
    raise InfraError("neither Docker Compose nor Podman Compose is installed and reachable")


def _object_lines(
    runner: CommandRunner,
    engine: Engine,
    name: str,
    args: list[str],
) -> list[str]:
    result = runner.run(name, [engine.binary, *args], timeout=60, check=False)
    if result.returncode != 0:
        raise InfraError(f"container-engine inventory failed: {name}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _disco_artifacts(
    containers: list,
    images: list,
    volumes: list,
    ports: tuple[int, int, int],
) -> tuple:
    """Return disco containers/images/volumes/data_paths/occupied_ports."""
    disco_containers = [name for name in containers if "disco" in name.lower()]
    disco_images = [name for name in images if name.lower().startswith("disco-")]
    disco_volumes = [name for name in volumes if "disco" in name.lower()]
    data_paths = [
        path
        for path in (
            Path.home() / ".local/share/disco",
            Path.home() / ".config/disco",
        )
        if path.exists()
    ]
    occupied = [port for port in ports if not _port_available(port)]
    return disco_containers, disco_images, disco_volumes, data_paths, occupied


def _assert_pristine(
    runner: CommandRunner,
    engine: Engine,
    *,
    ports: tuple[int, int, int],
) -> dict[str, Any]:
    containers = _object_lines(
        runner,
        engine,
        "preflight-containers",
        ["ps", "-a", "--format", "{{.Names}}"],
    )
    images = _object_lines(
        runner,
        engine,
        "preflight-images",
        ["images", "--format", "{{.Repository}}:{{.Tag}}"],
    )
    volumes = _object_lines(
        runner,
        engine,
        "preflight-volumes",
        ["volume", "ls", "--format", "{{.Name}}"],
    )
    disco_containers, disco_images, disco_volumes, data_paths, occupied = _disco_artifacts(
        containers, images, volumes, ports
    )
    if disco_containers or disco_images or disco_volumes or data_paths or occupied:
        raise InfraError(
            "device is not pristine: "
            f"containers={disco_containers}, images={disco_images}, "
            f"volumes={disco_volumes}, data_paths={[str(p) for p in data_paths]}, "
            f"occupied_ports={occupied}"
        )
    return {
        "containers": len(containers),
        "images": len(images),
        "volumes": len(volumes),
        "checked_ports": list(ports),
    }


__all__ = [
    "FAIL",
    "INFRA",
    "PASS",
    "CommandRunner",
    "Engine",
    "FreshDeviceError",
    "InfraError",
    "ProductError",
    "_assert_pristine",
    "_detect_engine",
    "_machine_fingerprint",
    "_object_lines",
    "_port_available",
    "_safe_device_label",
]

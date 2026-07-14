"""Destructive acceptance for a machine with no prior Disco installation.

This program is meant to be copied/run *on the candidate device*. It proves a
real clone -> compose build -> first pairing -> configured model -> Build/Search
journeys -> restart -> source upgrade -> persisted export -> uninstall sequence.
It refuses to count a host that already has Disco containers, images, volumes,
data, or occupied default ports. The resulting machine fingerprint is used by
the campaign ledger, so ten labels for one host cannot satisfy ten-device proof.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.cookiejar
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
INFRA = "INFRA"


class FreshDeviceError(RuntimeError):
    status = FAIL


class InfraError(FreshDeviceError):
    status = INFRA


class ProductError(FreshDeviceError):
    status = FAIL


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
        return Engine(binary=binary, compose=(binary, "compose"), sandbox_socket=socket_path)
    raise InfraError("neither Docker Compose nor Podman Compose is installed and reachable")


def _object_lines(runner: CommandRunner, engine: Engine, name: str, args: list[str]) -> list[str]:
    result = runner.run(name, [engine.binary, *args], timeout=60, check=False)
    if result.returncode != 0:
        raise InfraError(f"container-engine inventory failed: {name}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _assert_pristine(
    runner: CommandRunner,
    engine: Engine,
    *,
    ports: tuple[int, int, int],
) -> dict[str, Any]:
    containers = _object_lines(
        runner, engine, "preflight-containers", ["ps", "-a", "--format", "{{.Names}}"]
    )
    images = _object_lines(
        runner,
        engine,
        "preflight-images",
        ["images", "--format", "{{.Repository}}:{{.Tag}}"],
    )
    volumes = _object_lines(
        runner, engine, "preflight-volumes", ["volume", "ls", "--format", "{{.Name}}"]
    )
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


class ApiSession:
    def __init__(self, *, app_base: str, agent_base: str, origin: str) -> None:
        self.app_base = app_base.rstrip("/")
        self.agent_base = agent_base.rstrip("/")
        self.origin = origin.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: Any | None = None,
        timeout: float = 60,
    ) -> tuple[int, bytes, dict[str, str]]:
        body = None if data is None else json.dumps(data).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": self.origin}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method in {"POST", "PUT", "PATCH", "DELETE"} and self.csrf:
            headers["X-Disco-CSRF"] = self.csrf
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers.items())

    def pair(self) -> None:
        status, raw, _ = self._request("GET", f"{self.app_base}/api/auth/pairing-token")
        if status != 200:
            raise ProductError(f"pairing-token endpoint returned HTTP {status}")
        token = str(json.loads(raw).get("pairing_token") or "")
        status, raw, _ = self._request(
            "POST", f"{self.app_base}/api/auth/mint", data={"pairing_token": token}
        )
        if status != 200:
            raise ProductError(f"first pairing returned HTTP {status}: {raw[:500]!r}")
        self.csrf = str(json.loads(raw).get("csrf_token") or "")
        if not self.csrf or not any(cookie.name == "disco_session" for cookie in self.jar):
            raise ProductError("pairing returned no CSRF token/session cookie")

    def json(self, method: str, base: str, path: str, *, data: Any | None = None) -> Any:
        status, raw, _ = self._request(method, f"{base.rstrip('/')}{path}", data=data)
        if not 200 <= status < 300:
            raise ProductError(f"{method} {path} returned HTTP {status}: {raw[:800]!r}")
        return json.loads(raw) if raw else None

    def bytes(self, base: str, path: str) -> bytes:
        status, raw, _ = self._request("GET", f"{base.rstrip('/')}{path}", timeout=120)
        if status != 200:
            raise ProductError(f"GET {path} returned HTTP {status}: {raw[:800]!r}")
        return raw


def _wait_url(url: str, *, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - loopback
                if 200 <= response.status < 300:
                    return response.read()
        except (OSError, urllib.error.URLError) as exc:
            last = str(exc)
        time.sleep(2)
    raise ProductError(f"service did not become ready at {url}: {last}")


def _compose_command(engine: Engine, project: str, *args: str) -> list[str]:
    return [*engine.compose, "-p", project, *args]


def _configure_driver(session: ApiSession, *, base_url: str, model: str, api_key: str) -> None:
    model_id = "fresh-device-driver"
    key_name = "DISCO_FRESH_DEVICE_DRIVER_API_KEY"
    if api_key:
        session.json(
            "PUT",
            session.app_base,
            f"/api/secrets/{key_name}",
            data={"value": api_key},
        )
    payload = {
        "id": model_id,
        "model_id": model,
        "base_url": base_url,
        "api_key_env": key_name if api_key else None,
        "context_window": int(os.environ.get("DISCO_FRESH_DRIVER_CONTEXT", "32768")),
        "quantization": None,
        "capabilities": ["tool_calling", "json_mode", "long_context"],
        "price_in_per_m": 0,
        "price_out_per_m": 0,
        "pricing_mode": "unknown",
    }
    status, raw, _ = session._request("POST", f"{session.app_base}/api/models", data=payload)
    if status == 400:
        status, raw, _ = session._request(
            "PUT", f"{session.app_base}/api/models/{model_id}", data=payload
        )
    if not 200 <= status < 300:
        raise ProductError(f"driver model configuration failed HTTP {status}: {raw[:800]!r}")
    session.json(
        "PUT",
        session.app_base,
        "/api/models/assignments",
        data={
            "default_model": model_id,
            "roles": {
                "rag_answerer": model_id,
                "query_rewriter": model_id,
                "summarizer": model_id,
            },
        },
    )


def _assert_verify_output(output: str, *, grounding: bool) -> None:
    for name in ("config", "completion", "tool-calling"):
        if not re.search(rf"\bPASS\s+{re.escape(name)}\b", output):
            raise ProductError(f"disco-verify did not PASS {name}: {output[-2_000:]}")
    if grounding and not re.search(r"\bPASS\s+grounding\b", output):
        raise ProductError(
            "fresh-device internet/grounding proof was not a PASS (SKIP never counts): "
            + output[-2_000:]
        )


def _project_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
        raise ProductError("project list response has the wrong shape")
    return [
        str(item.get("id"))
        for item in payload["projects"]
        if isinstance(item, dict) and item.get("id") and not item.get("files_missing")
    ]


def _validate_zip(data: bytes, target: Path) -> list[str]:
    target.write_bytes(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [item.filename for item in archive.infolist() if not item.is_dir()]
            if not names:
                raise ProductError("project export zip contains no files")
            bad = archive.testzip()
            if bad:
                raise ProductError(f"project export zip has a corrupt member: {bad}")
            return names
    except zipfile.BadZipFile as exc:
        raise ProductError(f"project export is not a ZIP: {exc}") from exc


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Disco on a demonstrably clean device")
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo-url", default=os.environ.get("DISCO_FRESH_REPO_URL", ""))
    parser.add_argument("--base-ref", default=os.environ.get("DISCO_FRESH_BASE_REF", ""))
    parser.add_argument("--upgrade-ref", default=os.environ.get("DISCO_FRESH_UPGRADE_REF", ""))
    parser.add_argument(
        "--expected-upgrade-commit",
        default=os.environ.get("DISCO_FRESH_EXPECTED_UPGRADE_COMMIT", ""),
    )
    parser.add_argument("--keep-on-failure", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    result_path = out / "fresh-device-result.json"
    started_at = _utc_now()
    fingerprint = "unknown"
    checks: list[dict[str, Any]] = []
    checkout = out / "fresh-clone"
    runner = CommandRunner(out)
    engine: Engine | None = None
    compose_env: dict[str, str] | None = None
    project = ""
    status = INFRA
    reason = "fresh-device run did not start"

    def passed(check_id: str, detail: str, **evidence: Any) -> None:
        checks.append({"id": check_id, "status": PASS, "detail": detail, "evidence": evidence})

    try:
        required = {
            "repo URL": args.repo_url,
            "base ref": args.base_ref,
            "upgrade ref": args.upgrade_ref,
            "expected upgrade commit": args.expected_upgrade_commit,
            "driver base URL": os.environ.get("DISCO_FRESH_DRIVER_BASE_URL", ""),
            "driver model": os.environ.get("DISCO_FRESH_DRIVER_MODEL", ""),
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise InfraError(f"missing fresh-device inputs: {', '.join(missing)}")

        fingerprint = _machine_fingerprint()
        device_label = os.environ.get("DISCO_FRESH_DEVICE_ID", fingerprint)
        engine = _detect_engine(runner)
        ui_port = int(os.environ.get("DISCO_FRESH_UI_PORT", "8088"))
        app_port = int(os.environ.get("DISCO_FRESH_APP_PORT", "8800"))
        agent_port = int(os.environ.get("DISCO_FRESH_AGENT_PORT", "8000"))
        pristine = _assert_pristine(runner, engine, ports=(ui_port, app_port, agent_port))
        passed(
            "pristine-device",
            "no prior Disco containers, images, volumes, data paths, or bound ports",
            device_label=device_label,
            fingerprint=fingerprint,
            engine=engine.binary,
            **pristine,
        )

        runner.run(
            "clone",
            ["git", "clone", "--filter=blob:none", "--no-checkout", args.repo_url, str(checkout)],
            timeout=900,
        )
        runner.run(
            "fetch-refs",
            ["git", "fetch", "--force", "origin", args.base_ref, args.upgrade_ref],
            cwd=checkout,
            timeout=900,
        )
        runner.run("checkout-base", ["git", "checkout", "--detach", args.base_ref], cwd=checkout)
        base_commit = runner.run(
            "base-revision", ["git", "rev-parse", "HEAD"], cwd=checkout
        ).stdout.strip()
        upgrade_commit = runner.run(
            "upgrade-revision",
            ["git", "rev-parse", f"{args.upgrade_ref}^{{commit}}"],
            cwd=checkout,
        ).stdout.strip()
        if base_commit == upgrade_commit:
            raise InfraError("base and upgrade refs resolve to the same commit")
        if upgrade_commit != args.expected_upgrade_commit:
            raise InfraError(
                "upgrade ref does not resolve to the campaign revision: "
                f"expected {args.expected_upgrade_commit}, got {upgrade_commit}"
            )
        passed(
            "clean-clone",
            "candidate was cloned into a new directory and two distinct refs resolved",
            base_commit=base_commit,
            upgrade_commit=upgrade_commit,
        )

        project = f"discofresh-{_safe_device_label(device_label)}-{fingerprint[:8]}"
        secret = secrets.token_urlsafe(48)
        compose_env = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": project,
            "DISCO_BIND": "127.0.0.1",
            "DISCO_UI_PORT": str(ui_port),
            "DISCO_APP_PORT": str(app_port),
            "DISCO_AGENT_PORT": str(agent_port),
            "DISCO_PUBLIC_UI_URL": f"http://127.0.0.1:{ui_port}",
            "DISCO_SECRET_KEY": secret,
            "DISCO_SANDBOX_SOCKET": engine.sandbox_socket,
            "DISCO_INSPECT": "1",
            "DISCO_LOG_JSON": "1",
        }
        runner.run(
            "compose-config-base",
            _compose_command(engine, project, "config"),
            cwd=checkout,
            env=compose_env,
        )
        runner.run(
            "install-build-up",
            _compose_command(engine, project, "up", "-d", "--build"),
            cwd=checkout,
            env=compose_env,
            timeout=7_200,
        )
        front = f"http://127.0.0.1:{ui_port}"
        app = f"http://127.0.0.1:{app_port}"
        agent = f"http://127.0.0.1:{agent_port}"
        _wait_url(f"{front}/env.js", timeout=600)
        _wait_url(f"{app}/api/health", timeout=600)
        _wait_url(f"{agent}/health", timeout=600)
        index = _wait_url(front, timeout=60).decode("utf-8", errors="replace")
        env_js = _wait_url(f"{front}/env.js", timeout=60).decode("utf-8", errors="replace")
        if "root" not in index or "/svc/app" not in env_js or "/svc/agent" not in env_js:
            raise ProductError("packaged frontend or runtime API routing is incomplete")
        boot_logs = runner.run(
            "initial-app-logs",
            _compose_command(engine, project, "logs", "--no-color", "app-server"),
            cwd=checkout,
            env=compose_env,
        ).stdout
        if "First-run admin pairing" not in boot_logs or str(ui_port) not in boot_logs:
            raise ProductError("boot logs did not print first-run pairing guidance and UI URL")
        passed(
            "install-and-boot",
            "compose built all images and the packaged front door plus both APIs became healthy",
            ui=front,
        )

        api = ApiSession(app_base=app, agent_base=agent, origin=front)
        api.pair()
        _configure_driver(
            api,
            base_url=os.environ["DISCO_FRESH_DRIVER_BASE_URL"].strip(),
            model=os.environ["DISCO_FRESH_DRIVER_MODEL"].strip(),
            api_key=os.environ.get("DISCO_FRESH_DRIVER_API_KEY", "").strip(),
        )
        assignments = api.json("GET", app, "/api/models/assignments")
        if assignments.get("default_model") != "fresh-device-driver":
            raise ProductError("driver assignment did not round-trip through Settings API")
        passed(
            "first-pair-and-config",
            "fresh browser-equivalent session paired and configured a driver",
        )

        quick = runner.run(
            "initial-model-verify",
            _compose_command(
                engine,
                project,
                "exec",
                "-T",
                "agent-server",
                "disco-verify",
                "--quick",
            ),
            cwd=checkout,
            env=compose_env,
            timeout=1_200,
        )
        _assert_verify_output(quick.stdout, grounding=False)
        grounded = runner.run(
            "initial-grounding-verify",
            _compose_command(engine, project, "exec", "-T", "agent-server", "disco-verify"),
            cwd=checkout,
            env=compose_env,
            timeout=1_800,
        )
        _assert_verify_output(grounded.stdout, grounding=True)
        passed(
            "model-and-internet",
            "configured model completed text/tool calls and the real grounded Internet path passed",
        )

        scenarios = runner.run(
            "initial-surface-scenarios",
            _compose_command(
                engine,
                project,
                "exec",
                "-T",
                "agent-server",
                "python",
                "-m",
                "disco.agent_server.verify.scenarios_run",
                "--only",
                "app_from_build",
                "research_report_export",
            ),
            cwd=checkout,
            env=compose_env,
            timeout=2_400,
        )
        if (
            "[PASS] app_from_build" not in scenarios.stdout
            or "[PASS] research_report_export" not in scenarios.stdout
        ):
            raise ProductError("fresh Build/Search scenarios did not both pass")
        runner.run(
            "copy-initial-dossiers",
            _compose_command(
                engine,
                project,
                "cp",
                "agent-server:/app/test-record/disco-verify",
                str(out / "initial-dossiers"),
            ),
            cwd=checkout,
            env=compose_env,
            timeout=300,
        )
        projects_payload = api.json("GET", agent, "/api/projects")
        projects_before = _project_ids(projects_payload)
        if not projects_before:
            raise ProductError("Build scenario produced no persisted project")
        export_before = api.bytes(agent, f"/api/projects/{projects_before[0]}/download")
        members_before = _validate_zip(export_before, out / "project-before-upgrade.zip")
        passed(
            "build-search-export",
            "fresh Build and Deep Search finished; report and nonempty project ZIP were exported",
            project_count=len(projects_before),
            zip_members=len(members_before),
        )

        runner.run(
            "restart-stack",
            _compose_command(engine, project, "restart"),
            cwd=checkout,
            env=compose_env,
            timeout=600,
        )
        _wait_url(f"{agent}/health", timeout=600)
        _wait_url(f"{front}/env.js", timeout=600)
        api.pair()
        if _project_ids(api.json("GET", agent, "/api/projects")) != projects_before:
            raise ProductError("project identity changed across a full stack restart")
        passed("cold-restart", "session re-paired and projects persisted across service restart")

        runner.run(
            "stop-before-upgrade",
            _compose_command(engine, project, "down", "--remove-orphans"),
            cwd=checkout,
            env=compose_env,
            timeout=600,
        )
        runner.run(
            "checkout-upgrade",
            ["git", "checkout", "--detach", upgrade_commit],
            cwd=checkout,
            timeout=300,
        )
        runner.run(
            "upgrade-build-up",
            _compose_command(engine, project, "up", "-d", "--build"),
            cwd=checkout,
            env=compose_env,
            timeout=7_200,
        )
        _wait_url(f"{front}/env.js", timeout=600)
        _wait_url(f"{app}/api/health", timeout=600)
        _wait_url(f"{agent}/health", timeout=600)
        api.pair()
        if _project_ids(api.json("GET", agent, "/api/projects")) != projects_before:
            raise ProductError("upgrade lost or rewrote persisted projects")
        if (
            api.json("GET", app, "/api/models/assignments").get("default_model")
            != "fresh-device-driver"
        ):
            raise ProductError("upgrade lost the configured driver assignment")
        post_upgrade = runner.run(
            "post-upgrade-model-verify",
            _compose_command(
                engine,
                project,
                "exec",
                "-T",
                "agent-server",
                "disco-verify",
                "--quick",
            ),
            cwd=checkout,
            env=compose_env,
            timeout=1_200,
        )
        _assert_verify_output(post_upgrade.stdout, grounding=False)
        edge = runner.run(
            "post-upgrade-edge-scenario",
            _compose_command(
                engine,
                project,
                "exec",
                "-T",
                "agent-server",
                "python",
                "-m",
                "disco.agent_server.verify.scenarios_run",
                "--only",
                "missing_file_sandbox_error",
            ),
            cwd=checkout,
            env=compose_env,
            timeout=900,
        )
        if "[PASS] missing_file_sandbox_error" not in edge.stdout:
            raise ProductError("post-upgrade build edge scenario failed")
        export_after = api.bytes(agent, f"/api/projects/{projects_before[0]}/download")
        members_after = _validate_zip(export_after, out / "project-after-upgrade.zip")
        passed(
            "upgrade-in-place",
            "distinct source upgrade retained configuration/projects and completed "
            "a new edge build",
            base_commit=base_commit,
            upgrade_commit=upgrade_commit,
            zip_members=len(members_after),
        )

        runner.run(
            "final-compose-logs",
            _compose_command(engine, project, "logs", "--no-color"),
            cwd=checkout,
            env=compose_env,
            timeout=300,
            check=False,
        )
        runner.run(
            "uninstall",
            _compose_command(
                engine,
                project,
                "down",
                "--volumes",
                "--remove-orphans",
                "--rmi",
                "all",
            ),
            cwd=checkout,
            env=compose_env,
            timeout=1_200,
        )
        remaining = _object_lines(
            runner,
            engine,
            "post-uninstall-containers",
            [
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
        )
        remaining_volumes = _object_lines(
            runner,
            engine,
            "post-uninstall-volumes",
            [
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
        )
        if remaining or remaining_volumes:
            raise ProductError(f"uninstall left containers={remaining} volumes={remaining_volumes}")
        shutil.rmtree(checkout)
        if checkout.exists():
            raise ProductError("uninstall left the cloned application directory")
        passed("uninstall", "containers, networks, volumes, local images, and clone were removed")
        status = PASS
        reason = "fresh install, live use, restart, upgrade, export, and uninstall all passed"
    except FreshDeviceError as exc:
        status = exc.status
        reason = str(exc)
        checks.append({"id": "failure", "status": status, "detail": reason, "evidence": {}})
    except Exception as exc:  # noqa: BLE001 - preserve unexpected harness failures as INFRA
        status = INFRA
        reason = f"unexpected harness error: {type(exc).__name__}: {exc}"
        checks.append({"id": "failure", "status": INFRA, "detail": reason, "evidence": {}})
    finally:
        if engine is not None and compose_env is not None and project and checkout.exists():
            with contextlib.suppress(Exception):
                runner.run(
                    "failure-compose-logs",
                    _compose_command(engine, project, "logs", "--no-color"),
                    cwd=checkout,
                    env=compose_env,
                    timeout=180,
                    check=False,
                )
            if status != PASS and not args.keep_on_failure:
                with contextlib.suppress(Exception):
                    runner.run(
                        "failure-cleanup",
                        _compose_command(
                            engine,
                            project,
                            "down",
                            "--volumes",
                            "--remove-orphans",
                            "--rmi",
                            "all",
                        ),
                        cwd=checkout,
                        env=compose_env,
                        timeout=1_200,
                        check=False,
                    )
                shutil.rmtree(checkout, ignore_errors=True)
        payload = {
            "schema_version": 1,
            "status": status,
            "reason": reason,
            "device_fingerprint": fingerprint,
            "device_label": os.environ.get("DISCO_FRESH_DEVICE_ID", fingerprint),
            "started_at": started_at,
            "finished_at": _utc_now(),
            "checks": checks,
        }
        _write_result(result_path, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if status == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run a command against a disposable, restartable Disco App + Agent stack."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from disco.core.llm import ConfigStore, ProjectStorageSettings


def _child_environment(command: list[str], source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a command environment without secrets or contradictory color controls."""
    environment = dict(os.environ if source is None else source)
    for key in (
        "DISCO_RELIABILITY_SEED_SECRET_KEY",
        "DISCO_SECRET_KEY",
        "PMX_SECRET_KEY",
        "DISCO_AUTH_SECRET",
        "PMX_AUTH_SECRET",
    ):
        environment.pop(key, None)
    if any(Path(argument).name.lower() == "playwright" for argument in command):
        environment.pop("NO_COLOR", None)
    return environment


@contextlib.contextmanager
def _temporary_environment(source: dict[str, str], keys: tuple[str, ...]):
    """Apply the stack's signed-state environment while preparing its config."""
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ[key] = source[key]
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _wait_http(url: str, *, timeout_s: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - local URL
                if 200 <= response.status < 300:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last = exc
        time.sleep(0.25)
    raise TimeoutError(f"service did not become healthy at {url}: {last}")


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


class StackManager:
    def __init__(
        self,
        *,
        repo: Path,
        root: Path,
        agent_port: int,
        app_port: int,
        ui_port: int,
        seed_config: Path | None,
        seed_secrets: Path | None,
        seed_approvals: Path | None,
        preserve_seed_sandbox: bool = False,
    ) -> None:
        self.repo = repo
        self.root = root
        self.agent_port = agent_port
        self.app_port = app_port
        self.ui_port = ui_port
        self.python = repo / ".venv" / "bin" / "python3"
        self.agent: subprocess.Popen[bytes] | None = None
        self.app: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self.preserve_seed_sandbox = preserve_seed_sandbox
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "projects").mkdir()
        (self.root / "skills").mkdir()
        self.config_path = self.root / "disco-config.json"
        self.secrets_path = self.root / "secrets.json"
        self.approvals_path = self.root / "disco-approved-origins.json"
        self.db_path = self.root / "disco.db"
        if seed_config is not None:
            shutil.copy2(seed_config, self.config_path)
        if seed_secrets is not None:
            shutil.copy2(seed_secrets, self.secrets_path)
        if seed_approvals is not None:
            shutil.copy2(seed_approvals, self.approvals_path)
        self.env = self._environment()
        self._prepare_config()

    @property
    def agent_url(self) -> str:
        return f"http://127.0.0.1:{self.agent_port}"

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.app_port}"

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "DISCO_DATA_DIR": str(self.root),
                "DISCO_DB": str(self.db_path),
                "DISCO_CONFIG": str(self.config_path),
                "DISCO_SECRETS": str(self.secrets_path),
                "DISCO_APPROVALS": str(self.approvals_path),
                "DISCO_SKILLS_DIR": str(self.root / "skills"),
                "DISCO_PROJECTS_ROOT": str(self.root / "projects"),
                "DISCO_SECRET_KEY": os.environ.get("DISCO_RELIABILITY_SEED_SECRET_KEY")
                or base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
                "DISCO_AUTH_DEV_AUTO_PAIR": "1",
                "DISCO_BIND": "127.0.0.1",
                "DISCO_PUBLIC_UI_URL": f"http://127.0.0.1:{self.ui_port}",
                "DISCO_INSPECT": "1",
                "DISCO_LOG_JSON": "1",
                "DISCO_LOG_LEVEL": os.environ.get("DISCO_RELIABILITY_LOG_LEVEL", "INFO"),
                # Settings/workflow tests need a safe local workspace, not a
                # dependency on the operator's container socket.
                "DISCO_SANDBOX": "process",
                "DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV": "1",
                # Do not inherit an ambient production/dev override and do not
                # silently widen the product default. A campaign that explicitly
                # needs another posture must name it through the reliability-only
                # control, making the exception visible in its recorded command.
                "DISCO_BUILD_EGRESS": os.environ.get("DISCO_RELIABILITY_BUILD_EGRESS", "filtered"),
            }
        )
        return env

    def _prepare_config(self) -> None:
        if not self.config_path.exists():
            subprocess.run(
                [str(self.python), str(self.repo / "scripts" / "seed_config.py")],
                cwd=self.repo,
                env=self.env,
                check=True,
            )
        # ConfigStore security migrations verify the separate origin ledger and
        # secret refs during load. Run that preparation under the same signed
        # state the child servers receive; otherwise the parent wrapper can
        # silently rewrite a valid seed as "untrusted" before either server boots.
        with _temporary_environment(
            self.env,
            ("DISCO_CONFIG", "DISCO_SECRETS", "DISCO_APPROVALS", "DISCO_SECRET_KEY"),
        ):
            store = ConfigStore(self.config_path)
            config = store.load()
            sandbox = config.sandbox
            if not self.preserve_seed_sandbox:
                sandbox = sandbox.model_copy(
                    update={
                        "backend": "process",
                        "docker_socket": "",
                        "runtime": "runc",
                        "workspace_root": str(self.root / "sandbox-workspaces"),
                    }
                )
            config = config.model_copy(
                update={
                    "projects": ProjectStorageSettings(projects_root=str(self.root / "projects")),
                    "sandbox": sandbox,
                    "live_browser": config.live_browser.model_copy(update={"enabled": False}),
                    # Never inherit an operator's global MCP connections into a
                    # disposable stack. The MCP suite adds its own fixture server.
                    "mcp": config.mcp.model_copy(update={"enabled": False, "servers": {}}),
                }
            )
            store.save(config)

    def _spawn(self, module: str, port: int, log_name: str) -> subprocess.Popen[bytes]:
        env = {**self.env, "DISCO_HOST": "127.0.0.1", "DISCO_PORT": str(port)}
        log = (self.root / log_name).open("ab", buffering=0)
        try:
            return subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
                [str(self.python), "-m", module],
                cwd=self.repo,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        self._generation += 1
        self.app = self._spawn("disco.app_server", self.app_port, "app-server.log")
        try:
            _wait_http(f"{self.app_url}/api/health")
            self.agent = self._spawn("disco.agent_server", self.agent_port, "agent-server.log")
            _wait_http(f"{self.agent_url}/health", timeout_s=90)
        except Exception:
            _terminate(self.agent)
            _terminate(self.app)
            raise
        self._write_manifest()

    def restart(self) -> None:
        with self._lock:
            _terminate(self.agent)
            _terminate(self.app)
            self.agent = None
            self.app = None
            self._start_locked()

    def close(self) -> None:
        with self._lock:
            _terminate(self.agent)
            _terminate(self.app)
            self.agent = None
            self.app = None
            self._write_manifest()

    def _write_manifest(self) -> None:
        payload = {
            "generation": self._generation,
            "agent_url": self.agent_url,
            "app_url": self.app_url,
            "agent_pid": self.agent.pid if self.agent and self.agent.poll() is None else None,
            "app_pid": self.app.pid if self.app and self.app.poll() is None else None,
            "config": str(self.config_path),
            "approvals": str(self.approvals_path),
            "db": str(self.db_path),
            "projects": str(self.root / "projects"),
        }
        target = self.root / "stack.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(target)


class _ControlHandler(BaseHTTPRequestHandler):
    server_version = "DiscoReliabilityControl/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def manager(self) -> StackManager:
        return self.server.manager  # type: ignore[attr-defined,no-any-return]

    def _authorized(self) -> bool:
        token = self.server.control_token  # type: ignore[attr-defined]
        return secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}")

    def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "reason": "unauthorized"})
            return
        if self.path != "/restart":
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "reason": "not_found"})
            return
        try:
            self.manager.restart()
        except Exception as exc:  # noqa: BLE001 - returned to the private harness caller
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "reason": f"{type(exc).__name__}: {exc}"},
            )
            return
        self._send(
            HTTPStatus.OK,
            {
                "ok": True,
                "agent_url": self.manager.agent_url,
                "app_url": self.manager.app_url,
            },
        )


def _control_server(manager: StackManager) -> tuple[ThreadingHTTPServer, str, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlHandler)
    server.manager = manager  # type: ignore[attr-defined]
    token = secrets.token_urlsafe(32)
    server.control_token = token  # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, url, token


def _seed_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{name} is not a file: {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a command against an isolated Disco stack")
    parser.add_argument("--root", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--agent-port", type=int, default=18000)
    parser.add_argument("--app-port", type=int, default=18800)
    parser.add_argument("--ui-port", type=int, default=5274)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="ignore seed config/secrets variables and start from generated defaults",
    )
    parser.add_argument(
        "--preserve-seed-sandbox",
        action="store_true",
        help="retain the seeded sandbox backend (required for real gVisor proof)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    repo = Path(__file__).resolve().parents[2]
    root = Path(args.root).expanduser().resolve()
    manager = StackManager(
        repo=repo,
        root=root,
        agent_port=args.agent_port,
        app_port=args.app_port,
        ui_port=args.ui_port,
        seed_config=None if args.clean else _seed_path("DISCO_RELIABILITY_SEED_CONFIG"),
        seed_secrets=None if args.clean else _seed_path("DISCO_RELIABILITY_SEED_SECRETS"),
        seed_approvals=(None if args.clean else _seed_path("DISCO_RELIABILITY_SEED_APPROVALS")),
        preserve_seed_sandbox=args.preserve_seed_sandbox,
    )
    control: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    try:
        manager.start()
        control, control_url, token = _control_server(manager)
        thread = threading.Thread(target=control.serve_forever, daemon=True)
        thread.start()
        child_env = _child_environment(command)
        child_env.update(
            {
                "DISCO_RELIABILITY_ISOLATED_STACK": "1",
                "DISCO_RELIABILITY_AGENT_URL": manager.agent_url,
                "DISCO_RELIABILITY_APP_URL": manager.app_url,
                "DISCO_RELIABILITY_STACK_CONTROL_URL": control_url,
                "DISCO_RELIABILITY_STACK_CONTROL_TOKEN": token,
                "DISCO_RELIABILITY_STACK_ROOT": str(root),
                "DISCO_RELIABILITY_PYTHON": str(manager.python),
                "VITE_AGENT_BASE": manager.agent_url,
                "VITE_API_BASE": manager.app_url,
                "LIVE_PORT": str(args.ui_port),
                "LIVE_WORKERS": os.environ.get("LIVE_WORKERS", "1"),
            }
        )
        completed = subprocess.run(  # noqa: S603 - campaign matrix controls argv
            command,
            cwd=Path(args.cwd).expanduser().resolve(),
            env=child_env,
            check=False,
        )
        return completed.returncode
    finally:
        if control is not None:
            control.shutdown()
            control.server_close()
        if thread is not None:
            thread.join(timeout=5)
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())

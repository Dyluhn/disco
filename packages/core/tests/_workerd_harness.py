from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPMessage
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from types import TracebackType
from typing import TextIO

_LOCAL_HOST = "127.0.0.1"
_MAX_OUTPUT_BYTES = 64_000
_NPM_INSTALL_TIMEOUT_S = 300.0
_D1_INIT_TIMEOUT_S = 120.0
_BOOT_TIMEOUT_S = 60.0
_HTTP_TIMEOUT_S = 10.0
_KILL_TIMEOUT_S = 10.0
_WRANGLER_METRICS_ENV = "WRANGLER_SEND_METRICS"


class WorkerdAppError(RuntimeError):
    """Raised when the local workerd harness cannot complete a lifecycle step."""


class CookieJar:
    """Minimal test cookie jar for Worker session-cookie round trips."""

    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def header(self) -> str | None:
        if not self._cookies:
            return None
        return "; ".join(f"{name}={value}" for name, value in sorted(self._cookies.items()))

    def store(self, set_cookie_headers: Sequence[str]) -> None:
        for header in set_cookie_headers:
            cookie = SimpleCookie()
            try:
                cookie.load(header)
            except CookieError:
                continue
            for name, morsel in cookie.items():
                if morsel["max-age"] == "0" or morsel.value == "":
                    self._cookies.pop(name, None)
                else:
                    self._cookies[name] = morsel.value


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


def wrangler_available() -> bool:
    """Return True when a wrangler executable is visible to this test process."""
    return _find_wrangler(Path.cwd()) is not None


class WorkerdApp:
    """Materialize and drive a generated AppKit Worker under local wrangler/workerd."""

    def __init__(self, tree: Mapping[str, str], admin_token: str) -> None:
        self._tree = dict(tree)
        self._admin_token = admin_token
        self._app_dir: Path | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._port: int | None = None
        self._dev_log_handle: TextIO | None = None
        self._dev_log_path: Path | None = None
        self._cookies = CookieJar()

    @property
    def app_dir(self) -> Path:
        if self._app_dir is None:
            raise WorkerdAppError("WorkerdApp has not been entered yet")
        return self._app_dir

    def __enter__(self) -> WorkerdApp:
        self._app_dir = Path(tempfile.mkdtemp(prefix="disco-workerd-app-"))
        try:
            _materialize_tree(self._tree, self.app_dir)
            _stub_dist(self.app_dir)
            (self.app_dir / ".dev.vars").write_text(
                f"ADMIN_TOKEN={self._admin_token}\n", encoding="utf-8"
            )
            _run_checked(
                ["npm", "install"],
                cwd=self.app_dir,
                timeout_s=_NPM_INSTALL_TIMEOUT_S,
                label="npm install",
            )
            self._init_local_d1()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.kill()
        if self._app_dir is not None:
            shutil.rmtree(self._app_dir, ignore_errors=True)
            self._app_dir = None

    def boot(self, port: int) -> None:
        """Start `wrangler dev` and wait until `/api/leads` reaches the Worker."""
        if self._proc is not None and self._proc.poll() is None:
            raise WorkerdAppError("wrangler dev is already running")
        wrangler = self._wrangler_bin()
        self._port = port
        self._dev_log_path = self.app_dir / "wrangler-dev.log"
        self._dev_log_handle = self._dev_log_path.open("w", encoding="utf-8")
        env = _command_env()
        argv = [
            wrangler,
            "dev",
            "--port",
            str(port),
            "--ip",
            _LOCAL_HOST,
            "--local",
        ]
        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=str(self.app_dir),
                env=env,
                stdout=self._dev_log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_dev_log()
            raise WorkerdAppError(f"failed to launch wrangler dev: {exc}") from exc
        try:
            self._wait_ready(port)
        except BaseException:
            self.kill()
            raise

    def kill(self) -> None:
        """Terminate wrangler dev, reap it, and ensure the last bound port is down."""
        proc = self._proc
        port = self._port
        if proc is not None:
            _terminate_process(proc)
            self._proc = None
        self._close_dev_log()
        if port is not None:
            _wait_port_down(port, timeout_s=_KILL_TIMEOUT_S)

    def cookie_jar(self) -> CookieJar:
        return CookieJar()

    def post_json(
        self,
        path: str,
        obj: Mapping[str, object],
        *,
        token: str | None = None,
        cookie_jar: CookieJar | None = None,
    ) -> tuple[int, str]:
        body = json.dumps(obj).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self._request(
            "POST",
            path,
            body=body,
            headers=headers,
            timeout_s=_HTTP_TIMEOUT_S,
            cookie_jar=cookie_jar,
        )

    def get(
        self,
        path: str,
        token: str | None = None,
        *,
        cookie_jar: CookieJar | None = None,
    ) -> tuple[int, str]:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self._request(
            "GET",
            path,
            body=None,
            headers=headers,
            timeout_s=_HTTP_TIMEOUT_S,
            cookie_jar=cookie_jar,
        )

    def is_down(self) -> bool:
        """Return True if the most recently used port is not accepting HTTP requests."""
        return self._port is None or _port_is_down(self._port)

    def _init_local_d1(self) -> None:
        db_name = _d1_database_name(self.app_dir / "wrangler.toml")
        _run_checked(
            [
                self._wrangler_bin(),
                "d1",
                "execute",
                db_name,
                "--local",
                "--file=./schema.sql",
                "--yes",
            ],
            cwd=self.app_dir,
            timeout_s=_D1_INIT_TIMEOUT_S,
            label="wrangler d1 execute --local",
        )

    def _wrangler_bin(self) -> str:
        found = _find_wrangler(self.app_dir)
        if found is None:
            raise WorkerdAppError("wrangler executable is not available")
        return str(found)

    def _wait_ready(self, port: int) -> None:
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise WorkerdAppError(
                    f"wrangler dev exited early with code {proc.returncode}\n"
                    f"{self._dev_log()}"
                )
            try:
                self._request(
                    "GET",
                    "/api/leads",
                    body=None,
                    headers={},
                    timeout_s=1.0,
                    port=port,
                    cookie_jar=None,
                )
                return
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(0.25)
        raise WorkerdAppError(
            f"wrangler dev was not ready within {_BOOT_TIMEOUT_S:g}s"
            f" (last error: {last_error})\n{self._dev_log()}"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout_s: float,
        port: int | None = None,
        cookie_jar: CookieJar | None = None,
    ) -> tuple[int, str]:
        target_port = self._port if port is None else port
        if target_port is None:
            raise WorkerdAppError("wrangler dev has not been booted")
        url = _url(target_port, path)
        req_headers = dict(headers)
        jar = self._cookies if cookie_jar is None else cookie_jar
        cookie_header = jar.header()
        if cookie_header is not None and "Cookie" not in req_headers:
            req_headers["Cookie"] = cookie_header
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as res:
                jar.store(_set_cookie_headers(res.headers))
                text = res.read().decode("utf-8", errors="replace")
                return res.status, text
        except urllib.error.HTTPError as exc:
            jar.store(_set_cookie_headers(exc.headers))
            text = exc.read().decode("utf-8", errors="replace")
            return exc.code, text

    def _dev_log(self) -> str:
        if self._dev_log_handle is not None:
            self._dev_log_handle.flush()
        if self._dev_log_path is None or not self._dev_log_path.exists():
            return ""
        return _read_capped_file(self._dev_log_path)

    def _close_dev_log(self) -> None:
        if self._dev_log_handle is not None:
            self._dev_log_handle.close()
            self._dev_log_handle = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _bin_name(name: str) -> str:
    return f"{name}.cmd" if os.name == "nt" else name


def _find_wrangler(cwd: Path) -> Path | None:
    local = cwd / "node_modules" / ".bin" / _bin_name("wrangler")
    if _is_executable(local):
        return local
    repo_local = _repo_root() / "node_modules" / ".bin" / _bin_name("wrangler")
    if _is_executable(repo_local):
        return repo_local
    global_bin = shutil.which("wrangler")
    return Path(global_bin) if global_bin is not None else None


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _materialize_tree(tree: Mapping[str, str], root: Path) -> None:
    root_resolved = root.resolve()
    for rel, contents in tree.items():
        target = (root / rel).resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise WorkerdAppError(f"generated path escapes app dir: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


def _stub_dist(root: Path) -> None:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>appkit test</title>\n", encoding="utf-8")


def _d1_database_name(wrangler_toml: Path) -> str:
    config = tomllib.loads(wrangler_toml.read_text(encoding="utf-8"))
    databases = config.get("d1_databases")
    if not isinstance(databases, list) or not databases:
        raise WorkerdAppError("wrangler.toml does not declare a D1 database")
    first = databases[0]
    if not isinstance(first, dict):
        raise WorkerdAppError("wrangler.toml has an invalid D1 database entry")
    name = first.get("database_name")
    if not isinstance(name, str) or not name:
        raise WorkerdAppError("wrangler.toml D1 database entry has no database_name")
    return name


def _command_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CI"] = "true"
    env[_WRANGLER_METRICS_ENV] = "false"
    env.setdefault("NO_COLOR", "1")
    return env


def _run_checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    label: str,
) -> _CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=_command_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_output(exc.stdout)
        stderr = _coerce_output(exc.stderr)
        raise WorkerdAppError(
            f"{label} exceeded {timeout_s:g}s and was killed\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        ) from exc
    except OSError as exc:
        raise WorkerdAppError(f"failed to launch {label}: {exc}") from exc
    result = _CommandResult(
        returncode=completed.returncode,
        stdout=_cap_text(completed.stdout),
        stderr=_cap_text(completed.stderr),
    )
    if result.returncode != 0:
        raise WorkerdAppError(
            f"{label} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        proc.wait(timeout=0)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=_KILL_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        proc.kill()
    proc.wait(timeout=_KILL_TIMEOUT_S)


def _wait_port_down(port: int, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _port_is_down(port):
            return
        time.sleep(0.1)
    raise WorkerdAppError(f"port {port} was still accepting connections after kill")


def _port_is_down(port: int) -> bool:
    try:
        with socket.create_connection((_LOCAL_HOST, port), timeout=0.25):
            return False
    except OSError:
        return True


def _url(port: int, path: str) -> str:
    suffix = path if path.startswith("/") else f"/{path}"
    return f"http://{_LOCAL_HOST}:{port}{suffix}"


def _set_cookie_headers(headers: HTTPMessage) -> list[str]:
    values = headers.get_all("Set-Cookie")
    return list(values) if values is not None else []


def _coerce_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return _cap_text(value.decode("utf-8", errors="replace"))
    return _cap_text(value)


def _cap_text(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_BYTES:
        return text
    return f"{text[:_MAX_OUTPUT_BYTES]}\n...[output truncated at {_MAX_OUTPUT_BYTES} bytes]"


def _read_capped_file(path: Path) -> str:
    with path.open("rb") as fh:
        data = fh.read(_MAX_OUTPUT_BYTES + 1)
    if len(data) <= _MAX_OUTPUT_BYTES:
        return data.decode("utf-8", errors="replace")
    text = data[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return f"{text}\n...[output truncated at {_MAX_OUTPUT_BYTES} bytes]"

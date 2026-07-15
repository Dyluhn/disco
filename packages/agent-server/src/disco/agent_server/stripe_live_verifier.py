"""Host-owned live verifier for the AppKit ``stripe`` security primitive.

The callback is wired into every ``DefaultToolExecutor`` created by
``ConversationRuntime`` (see ``runtime.py``).  When a ``verify_appkit_app`` run
hits a primitive whose ``live_verify_id`` is ``stripe.security.v1``, this
runner stands up a real generated Worker + local D1 + authenticated host-service
bus in an isolated temporary root, exercises the five mandatory adversarial
checks, and tears everything down.

All runtime secrets stay in a sealed inherited memory descriptor with no
filesystem name and are never written to the project tree. The host stores
used by the bus are temporary, so a live verification run never mutates the
server's production Stripe configuration.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets as _py_secrets
import select
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, TextIO, cast

import uvicorn
from disco.core.appkit.primitives import PrimitiveVerifyResult, VerifyCheck
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secrets import SecretBox, SecretStore
from disco.core.quota import SqliteQuotaStore
from disco.core.store.sqlite import SqliteEventStore
from disco.core.stripe_host_service import (
    PAYMENTS_CHECKOUT_SERVICE_NAME,
    PAYMENTS_READY_SERVICE_NAME,
    STRIPE_API_URL,
    STRIPE_SECRET_REF,
    StripeAppConfigStore,
    StripeConfigurationError,
    configure_stripe_restricted_key,
    configure_stripe_webhook_secret,
    ensure_stripe_binding_secret,
)
from fastapi import FastAPI

from .host_service_bus import make_host_service_bus_router
from .host_token_store import HostTokenStore

_STRIPE_LIVE_VERIFY_ID = "stripe.security.v1"

_LOG = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 64_000
_NPM_CI_TIMEOUT_S = 300.0
_D1_INIT_TIMEOUT_S = 120.0
_DEPLOY_TIMEOUT_S = 120.0
_BOOT_TIMEOUT_S = 60.0
_HTTP_TIMEOUT_S = 10.0
_KILL_TIMEOUT_S = 10.0
_SIGNATURE_TOLERANCE_S = 300
_MAX_WEBHOOK_BYTES = 256 * 1024
_WRANGLER_METRICS_ENV = "WRANGLER_SEND_METRICS"

_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS = 1033
_F_SEAL_SEAL = 0x0001
_F_SEAL_SHRINK = 0x0002
_F_SEAL_GROW = 0x0004
_F_SEAL_WRITE = 0x0008

_SECRET_PATTERNS = (r"sk_live", r"sk_test", r"whsec_")


class StripeLiveVerifierError(RuntimeError):
    """The live verifier could not complete one or more checks."""


class _CookieJar:
    """Minimal cookie jar for Worker session round-trips."""

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


class _LiveWorker(Protocol):
    """The real local Worker operations needed by the exploit checks."""

    async def request_async(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]: ...

    async def post_json_async(
        self, path: str, obj: Mapping[str, object], *, token: str | None = None
    ) -> tuple[int, str]: ...

    async def get_async(self, path: str, *, token: str | None = None) -> tuple[int, str]: ...

    async def d1_count_async(self, table: str) -> int: ...


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


class _SilentServer(uvicorn.Server):
    """Uvicorn server that does not capture process signal handlers."""

    def install_signal_handlers(self) -> None:
        pass


class _HostBusServer:
    """A minimal loopback host-service bus exposing only ``/_disco/svc/*``."""

    def __init__(
        self,
        store: SqliteEventStore,
        secret_store: SecretStore,
        config_store: ConfigStore,
        token_store: HostTokenStore,
        stripe_config_store: StripeAppConfigStore,
        port: int,
        cert_path: Path,
        key_path: Path,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._config_store = config_store
        self._token_store = token_store
        self._quota_store = SqliteQuotaStore()
        self._stripe_config_store = stripe_config_store
        self._port = port
        self._cert_path = cert_path
        self._key_path = key_path
        self._ssl_context = ssl.create_default_context(cafile=str(cert_path))
        self._server: _SilentServer | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def origin(self) -> str:
        return f"https://127.0.0.1:{self._port}"

    async def __aenter__(self) -> _HostBusServer:
        app = FastAPI()
        app.include_router(
            make_host_service_bus_router(
                self._store,
                cast(Any, _FakeRuntime(self._secret_store, self._config_store, self._store)),
                self._token_store,
                self._quota_store,
                self._stripe_config_store,
            )
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self._port,
            loop="asyncio",
            log_level="warning",
            ws="websockets-sansio",
            ssl_certfile=str(self._cert_path),
            ssl_keyfile=str(self._key_path),
        )
        self._server = _SilentServer(config)
        self._task = asyncio.create_task(self._server.serve())
        await self._wait_ready()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.should_exit = True
            if self._task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(self._task, timeout=_KILL_TIMEOUT_S)
                self._task = None
            self._server = None
        self._quota_store.close()

    async def _wait_ready(self) -> None:
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._task is not None and self._task.done():
                exc = self._task.exception()
                raise StripeLiveVerifierError(f"host bus server exited early: {exc}")
            if not _port_is_down(self._port):
                return
            await asyncio.sleep(0.05)
        raise StripeLiveVerifierError(
            f"host bus server was not ready within {_BOOT_TIMEOUT_S:g}s (port {self._port})"
        )

    async def _request(
        self,
        method: str,
        service: str,
        body: bytes | None,
        headers: Mapping[str, str],
        token: str | None,
    ) -> tuple[int, str]:
        req_headers = dict(headers)
        if token is not None:
            req_headers["Authorization"] = f"Bearer {token}"
        url = f"{self.origin}/_disco/svc/{service}"
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)

        def _sync_call() -> tuple[int, str]:
            try:
                with urllib.request.urlopen(
                    req, timeout=_HTTP_TIMEOUT_S, context=self._ssl_context
                ) as res:
                    return res.status, res.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8", errors="replace")

        return await asyncio.to_thread(_sync_call)

    async def _post(
        self,
        service: str,
        payload: Mapping[str, object],
        *,
        token: str | None = None,
    ) -> tuple[int, str]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return await self._request(
            "POST", service, body, {"Content-Type": "application/json"}, token
        )


class _FakeRuntime:
    """Minimum runtime duck type for ``make_host_service_context_factory``."""

    def __init__(
        self,
        secret_store: SecretStore,
        config_store: ConfigStore,
        store: SqliteEventStore,
    ) -> None:
        self._secret_store = secret_store
        self._config_store = config_store
        self._store = store


class _WorkerdApp:
    """Materialize and drive a generated AppKit Worker under local wrangler/workerd."""

    def __init__(
        self,
        tree: Mapping[str, str],
        env_vars: Mapping[str, str],
        tmp_root: Path,
        ca_cert: Path,
    ) -> None:
        self._tree = dict(tree)
        self._env_vars = dict(env_vars)
        self._tmp_root = tmp_root
        self._ca_cert = ca_cert
        self._app_dir: Path | None = None
        self._env_fd: int | None = None
        self._env_path: str | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._port: int | None = None
        self._persist_dir: Path | None = None
        self._bundle_dir: Path | None = None
        self._dev_log_path: Path | None = None
        self._dev_log_handle: TextIO | None = None
        self._cookies = _CookieJar()

    @property
    def app_dir(self) -> Path:
        if self._app_dir is None:
            raise StripeLiveVerifierError("WorkerdApp has not been entered")
        return self._app_dir

    @property
    def bundle_dir(self) -> Path | None:
        return self._bundle_dir

    @property
    def dev_log_path(self) -> Path | None:
        return self._dev_log_path

    def __enter__(self) -> _WorkerdApp:
        self._app_dir = self._tmp_root / "app"
        self._app_dir.mkdir(parents=True, exist_ok=True)
        try:
            _materialize_tree(self._tree, self._app_dir)
            _stub_dist(self._app_dir)
            self._env_fd, self._env_path = _open_memory_env(self._env_vars)
            self._ensure_lockfile()
            self._run_checked(
                ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                timeout_s=_NPM_CI_TIMEOUT_S,
                label="npm ci",
            )
            self._init_local_d1()
            self._bundle_dir = self._app_dir / ".wrangler" / "live-bundle"
            self._run_checked(
                [
                    str(self._wrangler_bin()),
                    "deploy",
                    "--dry-run",
                    "--outdir",
                    str(self._bundle_dir),
                    "--env-file",
                    self._env_path,
                ],
                timeout_s=_DEPLOY_TIMEOUT_S,
                label="wrangler deploy --dry-run",
                pass_env_fd=True,
            )
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.kill()
        finally:
            if self._env_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._env_fd)
                self._env_fd = None
                self._env_path = None
            if self._dev_log_handle is not None:
                self._dev_log_handle.close()
                self._dev_log_handle = None

    def boot(self, port: int) -> None:
        if self._proc is not None and self._proc.poll() is None:
            raise StripeLiveVerifierError("wrangler dev is already running")
        wrangler = self._wrangler_bin()
        self._port = port
        self._dev_log_path = self.app_dir / "wrangler-dev.log"
        self._dev_log_handle = self._dev_log_path.open("w", encoding="utf-8")
        env = _command_env(self._tmp_root, extra_ca_cert=self._ca_cert)
        argv = [
            str(wrangler),
            "dev",
            "--port",
            str(port),
            "--ip",
            "127.0.0.1",
            "--local",
            "--env-file",
            self._env_path or "",
            "--persist-to",
            str(self._persist_dir),
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
                pass_fds=(self._env_fd,) if self._env_fd is not None else (),
            )
        except OSError as exc:
            self._close_dev_log()
            raise StripeLiveVerifierError(f"failed to launch wrangler dev: {exc}") from exc
        try:
            self._wait_ready(port)
        except BaseException:
            self.kill()
            raise

    def kill(self) -> None:
        proc = self._proc
        port = self._port
        if proc is not None:
            _terminate_process(proc)
            self._proc = None
        self._close_dev_log()
        if port is not None:
            _wait_port_down(port, timeout_s=_KILL_TIMEOUT_S)

    def is_down(self) -> bool:
        return self._port is None or _port_is_down(self._port)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]:
        if self._port is None:
            raise StripeLiveVerifierError("wrangler dev has not been booted")
        req_headers = dict(headers or {})
        if token is not None:
            req_headers["Authorization"] = f"Bearer {token}"
        cookie_header = self._cookies.header()
        if cookie_header is not None and "Cookie" not in req_headers:
            req_headers["Cookie"] = cookie_header
        suffix = path if path.startswith("/") else f"/{path}"
        url = f"http://127.0.0.1:{self._port}{suffix}"
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as res:
                self._cookies.store(_set_cookie_headers(res.headers))
                return res.status, res.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            self._cookies.store(_set_cookie_headers(exc.headers))
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def post_json(
        self,
        path: str,
        obj: Mapping[str, object],
        *,
        token: str | None = None,
    ) -> tuple[int, str]:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return self.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
            token=token,
        )

    def get(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return self.request("GET", path, token=token)

    async def request_async(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]:
        """Avoid blocking the loop that serves the local authenticated bus."""
        return await asyncio.to_thread(self.request, method, path, body, headers, token)

    async def post_json_async(
        self,
        path: str,
        obj: Mapping[str, object],
        *,
        token: str | None = None,
    ) -> tuple[int, str]:
        return await asyncio.to_thread(self.post_json, path, obj, token=token)

    async def get_async(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return await asyncio.to_thread(self.get, path, token=token)

    async def d1_count_async(self, table: str) -> int:
        return await asyncio.to_thread(self.d1_count, table)

    def dev_log_tail(self) -> str:
        if self._dev_log_handle is not None:
            self._dev_log_handle.flush()
        if self._dev_log_path is None or not self._dev_log_path.exists():
            return ""
        return _read_capped_file(self._dev_log_path)

    @property
    def env_path(self) -> str:
        if self._env_path is None:
            raise StripeLiveVerifierError("Worker runtime env was not initialized")
        return self._env_path

    @property
    def env_fd(self) -> int:
        if self._env_fd is None:
            raise StripeLiveVerifierError("Worker runtime env was not initialized")
        return self._env_fd

    @property
    def ca_cert(self) -> Path:
        return self._ca_cert

    def _close_dev_log(self) -> None:
        if self._dev_log_handle is not None:
            self._dev_log_handle.close()
            self._dev_log_handle = None

    def _ensure_lockfile(self) -> None:
        if not (self.app_dir / "package-lock.json").is_file():
            raise StripeLiveVerifierError(
                "generated AppKit tree has no trusted package-lock.json (fail-closed)"
            )

    def _init_local_d1(self) -> None:
        db_name = _d1_database_name(self.app_dir / "wrangler.toml")
        self._persist_dir = self.app_dir / ".wrangler" / "live-state"
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._run_checked(
            [
                str(self._wrangler_bin()),
                "d1",
                "execute",
                db_name,
                "--local",
                "--persist-to",
                str(self._persist_dir),
                "--file=./schema.sql",
                "--yes",
            ],
            timeout_s=_D1_INIT_TIMEOUT_S,
            label="wrangler d1 execute --local",
        )

    def _wrangler_bin(self) -> Path:
        found = _find_wrangler(self.app_dir)
        if found is None:
            raise StripeLiveVerifierError("wrangler executable is not available")
        return found

    def _wait_ready(self, port: int) -> None:
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise StripeLiveVerifierError(
                    f"wrangler dev exited early with code {proc.returncode}\n{self.dev_log_tail()}"
                )
            try:
                self.get("/api/leads")
                return
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(0.05)
        raise StripeLiveVerifierError(
            f"wrangler dev was not ready within {_BOOT_TIMEOUT_S:g}s ({last_error})\n"
            f"{self.dev_log_tail()}"
        )

    def _run_checked(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        label: str,
        pass_env_fd: bool = False,
    ) -> _CommandResult:
        return _run_checked(
            argv,
            cwd=self.app_dir,
            private_root=self._tmp_root,
            extra_ca_cert=self._ca_cert,
            timeout_s=timeout_s,
            label=label,
            pass_fds=(self._env_fd,) if pass_env_fd and self._env_fd is not None else (),
        )

    def d1_count(self, table: str) -> int:
        """Count rows in a D1 table via the local wrangler CLI."""
        db_name = _d1_database_name(self.app_dir / "wrangler.toml")
        result = self._run_checked(
            [
                str(self._wrangler_bin()),
                "d1",
                "execute",
                db_name,
                "--local",
                "--persist-to",
                str(self._persist_dir),
                "--command",
                f"SELECT COUNT(*) AS c FROM {table};",
                "--json",
            ],
            timeout_s=_D1_INIT_TIMEOUT_S,
            label=f"wrangler d1 count {table}",
        )
        return _parse_d1_count_json(result.stdout)


class _MiniflareRuntimeProbe:
    """Run the trusted bundle in local workerd with outbound delivery to FastAPI.

    Wrangler's local runtime does not permit outbound loopback fetches. This
    companion workerd process routes only the configured bus URL through
    Miniflare's host-owned outbound adapter, which performs the real HTTPS
    request to the pre-bound FastAPI bus. No response is fabricated.
    """

    def __init__(self, worker: _WorkerdApp, tmp_root: Path, control_token: str) -> None:
        self._worker = worker
        self._tmp_root = tmp_root
        self._control_token = control_token
        self._port: int | None = None
        self._control_port: int | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._script: Path | None = None
        self._cookies = _CookieJar()

    def __enter__(self) -> _MiniflareRuntimeProbe:
        bundle = _bundle_entry(self._worker.bundle_dir)
        miniflare = _miniflare_entry(self._worker._wrangler_bin())
        node = shutil.which("node")
        if node is None:
            raise StripeLiveVerifierError("node executable is required for local workerd probe")
        self._port = _free_port()
        self._control_port = _free_port()
        self._script = self._tmp_root / "runtime-probe.mjs"
        self._script.write_text(
            _miniflare_probe_script(
                bundle,
                miniflare,
                self._port,
                self._control_port,
                self._worker.app_dir / "schema.sql",
                self._worker.ca_cert,
            ),
            encoding="utf-8",
        )
        try:
            self._proc = subprocess.Popen(
                [node, f"--env-file={self._worker.env_path}", str(self._script)],
                cwd=str(self._tmp_root),
                env=_command_env(self._tmp_root, extra_ca_cert=self._worker.ca_cert),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
                pass_fds=(self._worker.env_fd,),
            )
        except OSError as exc:
            raise StripeLiveVerifierError(
                f"failed to start local workerd runtime probe: {exc}"
            ) from exc
        assert self._proc.stdout is not None
        readable, _, _ = select.select([self._proc.stdout], [], [], _BOOT_TIMEOUT_S)
        if not readable:
            self.__exit__(None, None, None)
            raise StripeLiveVerifierError("local workerd runtime probe did not start")
        first_line = self._proc.stdout.readline().strip()
        if first_line != "READY":
            # Node may leave its event loop alive after emitting an asynchronous
            # startup exception.  Drain what has already been written before
            # terminating it so this fail-closed verifier retains the actual
            # runtime diagnostic rather than only its first stack-frame line.
            lines = [first_line]
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                readable, _, _ = select.select([self._proc.stdout], [], [], 0.1)
                if readable:
                    line = self._proc.stdout.readline()
                    if line:
                        lines.append(line.strip())
                        continue
                if self._proc.poll() is not None:
                    break
            _terminate_process(self._proc)
            try:
                _stdout, _stderr = self._proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                _stdout = ""
            self.__exit__(None, None, None)
            raise StripeLiveVerifierError(
                "local workerd runtime probe did not become ready: "
                + _cap_text("\n".join(lines) + "\n" + _stdout)
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc is not None:
            _terminate_process(self._proc)
            self._proc = None

    def payments_ready(self, admin_token: str) -> bool:
        if self._port is None:
            raise StripeLiveVerifierError("local workerd runtime probe has not been started")
        try:
            status, body = self.request("GET", "/api/stripe/runtime-probe", token=admin_token)
            return status == 200 and json.loads(body) == {"ready": True}
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            return False

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]:
        if self._port is None:
            raise StripeLiveVerifierError("local workerd runtime probe has not been started")
        req_headers = dict(headers or {})
        if token is not None:
            req_headers["Authorization"] = f"Bearer {token}"
        cookie_header = self._cookies.header()
        if cookie_header is not None and "Cookie" not in req_headers:
            req_headers["Cookie"] = cookie_header
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=body,
            headers=req_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as response:
                self._cookies.store(_set_cookie_headers(response.headers))
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            self._cookies.store(_set_cookie_headers(exc.headers))
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def post_json(
        self, path: str, obj: Mapping[str, object], *, token: str | None = None
    ) -> tuple[int, str]:
        return self.request(
            "POST",
            path,
            json.dumps(obj, separators=(",", ":")).encode("utf-8"),
            {"Content-Type": "application/json"},
            token,
        )

    def get(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return self.request("GET", path, token=token)

    async def request_async(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]:
        return await asyncio.to_thread(self.request, method, path, body, headers, token)

    async def post_json_async(
        self, path: str, obj: Mapping[str, object], *, token: str | None = None
    ) -> tuple[int, str]:
        return await asyncio.to_thread(self.post_json, path, obj, token=token)

    async def get_async(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return await asyncio.to_thread(self.get, path, token=token)

    def d1_count(self, table: str) -> int:
        if table not in {"stripe_events", "stripe_fulfillments", "user_role_grants"}:
            raise StripeLiveVerifierError("invalid local D1 table request")
        if self._control_port is None:
            raise StripeLiveVerifierError("local D1 control service has not been started")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._control_port}/count/{table}",
            headers={"Authorization": f"Bearer {self._control_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise StripeLiveVerifierError("could not query local D1 state") from exc
        count = payload.get("count") if isinstance(payload, dict) else None
        if not isinstance(count, int):
            raise StripeLiveVerifierError("local D1 count response was invalid")
        return count

    async def d1_count_async(self, table: str) -> int:
        return await asyncio.to_thread(self.d1_count, table)


def _parse_d1_count_json(stdout: str) -> int:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return -1
    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        if isinstance(first, dict):
            results = first.get("results")
            if isinstance(results, list) and results:
                row = results[0]
                if isinstance(row, dict):
                    count = row.get("c")
                    if isinstance(count, int):
                        return count
    return -1


def _find_wrangler(cwd: Path) -> Path | None:
    local = cwd / "node_modules" / ".bin" / _bin_name("wrangler")
    if _is_executable(local):
        return local
    repo_local = _repo_root() / "node_modules" / ".bin" / _bin_name("wrangler")
    if _is_executable(repo_local):
        return repo_local
    global_bin = shutil.which("wrangler")
    return Path(global_bin) if global_bin is not None else None


def _bundle_entry(bundle_dir: Path | None) -> Path:
    if bundle_dir is None:
        raise StripeLiveVerifierError("wrangler dry-run did not produce a bundle directory")
    entries = [*bundle_dir.rglob("*.js"), *bundle_dir.rglob("*.mjs")]
    if len(entries) != 1:
        raise StripeLiveVerifierError("wrangler dry-run did not produce one Worker bundle entry")
    return entries[0]


def _miniflare_entry(wrangler_bin: Path) -> Path:
    root = wrangler_bin.resolve().parents[1]
    candidates = (
        root / "node_modules" / "miniflare" / "dist" / "src" / "index.js",
        root.parent / "miniflare" / "dist" / "src" / "index.js",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise StripeLiveVerifierError("Wrangler's local workerd runtime is not available")


def _miniflare_probe_script(
    bundle: Path,
    miniflare: Path,
    port: int,
    control_port: int,
    schema: Path,
    ca_cert: Path,
) -> str:
    """Return a non-secret Node harness for the real Worker runtime probe."""
    return f"""import {{ Miniflare }} from {json.dumps(miniflare.as_uri())};
import https from "node:https";
import http from "node:http";
import {{ readFileSync }} from "node:fs";
const required = [
  "ADMIN_TOKEN", "DISCO_SVC_BUS", "DISCO_SVC_TOKEN", "STRIPE_WEBHOOK_SECRET",
  "STRIPE_APP_BINDING_SECRET", "STRIPE_RUNTIME_READY",
];
for (const name of required) {{
  if (typeof process.env[name] !== "string" || process.env[name].length === 0) {{
    throw new Error(`missing runtime binding ${{name}}`);
  }}
}}
const bus = new URL(process.env.DISCO_SVC_BUS);
const busCa = readFileSync({json.dumps(str(ca_cert))});
async function forwardToHostBus(request) {{
  const target = new URL(request.url);
  const body = Buffer.from(await request.arrayBuffer());
  return await new Promise((resolve, reject) => {{
    const outbound = https.request({{
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port,
      path: target.pathname + target.search,
      method: request.method,
      headers: Object.fromEntries(request.headers),
      ca: busCa,
      rejectUnauthorized: true,
    }}, (response) => {{
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {{
        const headers = new Headers();
        for (const [name, value] of Object.entries(response.headers)) {{
          if (typeof value === "string") headers.set(name, value);
          else if (Array.isArray(value)) headers.set(name, value.join(","));
        }}
        resolve(new Response(Buffer.concat(chunks), {{
          status: response.statusCode || 502,
          headers,
        }}));
      }});
    }});
    outbound.on("error", reject);
    outbound.end(body);
  }});
}}
const mf = new Miniflare({{
  host: "127.0.0.1",
  port: {port},
  workers: [{{
    name: "stripe-live-runtime-probe",
    modules: true,
    scriptPath: {json.dumps(str(bundle))},
    compatibilityDate: "2025-01-01",
    d1Databases: {{ DB: "stripe-live-runtime-probe" }},
    outboundService: async (request) => {{
      const target = new URL(request.url);
      if (target.origin !== bus.origin || !target.pathname.startsWith("/_disco/svc/")) {{
        return new Response("blocked outbound", {{ status: 403 }});
      }}
      try {{
        return await forwardToHostBus(request);
      }} catch (error) {{
        return new Response("host bus unavailable", {{ status: 502 }});
      }}
    }},
    bindings: Object.fromEntries(required.map((name) => [name, process.env[name]])),
  }}],
}});
await mf.ready;
const db = await mf.getD1Database("DB");
const schema = readFileSync({json.dumps(str(schema))}, "utf8")
  .split("\\n")
  .filter((line) => !line.trimStart().startsWith("--"))
  .join("\\n");
// D1's Miniflare binding executes one statement per exec call.  The generated,
// trusted schema contains no SQL string literals, so semicolons delimit its DDL
// unambiguously once line comments have been removed above.
for (const statement of schema.split(";")) {{
  if (statement.trim()) {{
    try {{
      await db.prepare(statement).run();
    }} catch (error) {{
      const detail = JSON.stringify(statement);
      throw new Error(
        `generated D1 schema statement failed: ${{detail}}`,
        {{ cause: error }},
      );
    }}
  }}
}}
const controlToken = process.env.DISCO_LIVE_D1_CONTROL_TOKEN;
const tables = new Set(["stripe_events", "stripe_fulfillments", "user_role_grants"]);
const control = http.createServer(async (request, response) => {{
  const table = request.url?.match(/^\\/count\\/([a-z_]+)$/)?.[1];
  if (request.method !== "GET" || request.headers.authorization !== `Bearer ${{controlToken}}`
      || table === undefined || !tables.has(table)) {{
    response.writeHead(403).end();
    return;
  }}
  try {{
    const row = await db.prepare(`SELECT COUNT(*) AS c FROM ${{table}}`).first();
    const count = typeof row?.c === "number" ? row.c : -1;
    response.writeHead(200, {{ "Content-Type": "application/json" }});
    response.end(JSON.stringify({{ count }}));
  }} catch {{
    response.writeHead(500).end();
  }}
}});
await new Promise((resolve) => control.listen({control_port}, "127.0.0.1", resolve));
console.log("READY");
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, async () => {{
  await new Promise((resolve) => control.close(resolve));
  await mf.dispose();
  process.exit(0);
}});
setInterval(() => {{}}, 1000);
"""


def _repo_root() -> Path:
    # packages/agent-server/src/disco/agent_server/stripe_live_verifier.py
    return Path(__file__).resolve().parents[6]


def _bin_name(name: str) -> str:
    return f"{name}.cmd" if os.name == "nt" else name


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _make_loopback_tls_pair(private_root: Path) -> tuple[Path, Path]:
    """Create a short-lived loopback certificate for the real local host bus."""
    openssl = shutil.which("openssl")
    if openssl is None:
        raise StripeLiveVerifierError("openssl is required for the local HTTPS host bus")
    cert = private_root / "host-bus-cert.pem"
    key = private_root / "host-bus-key.pem"
    try:
        completed = subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            env=_command_env(private_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StripeLiveVerifierError("could not create local HTTPS host-bus certificate") from exc
    if completed.returncode != 0 or not cert.is_file() or not key.is_file():
        raise StripeLiveVerifierError("could not create local HTTPS host-bus certificate")
    key.chmod(0o600)
    return cert, key


def _command_env(private_root: Path, *, extra_ca_cert: Path | None = None) -> dict[str, str]:
    """Return the narrowly allowlisted base environment for child tooling."""
    home = private_root / "command-home"
    temp = private_root / "command-tmp"
    home.mkdir(mode=0o700, exist_ok=True)
    temp.mkdir(mode=0o700, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "TMP": str(temp),
        "TEMP": str(temp),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "CI": "true",
        _WRANGLER_METRICS_ENV: "false",
        "NO_COLOR": "1",
    }
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    if extra_ca_cert is not None:
        env["NODE_EXTRA_CA_CERTS"] = str(extra_ca_cert)
    elif value := os.environ.get("NODE_EXTRA_CA_CERTS"):
        env["NODE_EXTRA_CA_CERTS"] = value
    return env


def _run_checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    private_root: Path,
    extra_ca_cert: Path | None,
    timeout_s: float,
    label: str,
    pass_fds: tuple[int, ...] = (),
) -> _CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=_command_env(private_root, extra_ca_cert=extra_ca_cert),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_output(exc.stdout)
        stderr = _coerce_output(exc.stderr)
        raise StripeLiveVerifierError(
            f"{label} exceeded {timeout_s:g}s and was killed\nstdout:\n{stdout}\nstderr:\n{stderr}"
        ) from exc
    except OSError as exc:
        raise StripeLiveVerifierError(f"failed to launch {label}: {exc}") from exc
    result = _CommandResult(
        returncode=completed.returncode,
        stdout=_cap_text(completed.stdout),
        stderr=_cap_text(completed.stderr),
    )
    if result.returncode != 0:
        raise StripeLiveVerifierError(
            f"{label} failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _materialize_tree(tree: Mapping[str, str], root: Path) -> None:
    root_resolved = root.resolve()
    for rel, contents in tree.items():
        target = (root / rel).resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise StripeLiveVerifierError(f"generated path escapes app dir: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


def _stub_dist(root: Path) -> None:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>appkit test</title>\n", encoding="utf-8"
    )


def _d1_database_name(wrangler_toml: Path) -> str:
    config = tomllib.loads(wrangler_toml.read_text(encoding="utf-8"))
    databases = config.get("d1_databases")
    if not isinstance(databases, list) or not databases:
        raise StripeLiveVerifierError("wrangler.toml does not declare a D1 database")
    first = databases[0]
    if not isinstance(first, dict):
        raise StripeLiveVerifierError("wrangler.toml has an invalid D1 database entry")
    name = first.get("database_name")
    if not isinstance(name, str) or not name:
        raise StripeLiveVerifierError("wrangler.toml D1 database entry has no database_name")
    return name


def _open_memory_env(env_vars: Mapping[str, str]) -> tuple[int, str]:
    """Return a sealed, inherited memfd path for Wrangler's ``--env-file``.

    The descriptor never has a directory entry. The Wrangler launcher starts a
    second Node process that closes non-stdio descriptors, so its child reads
    the still-live parent descriptor through a PID-pinned procfs path instead
    of relying on a plaintext temporary file.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        memfd_create = libc.memfd_create
    except AttributeError as exc:
        raise StripeLiveVerifierError("sealed in-memory env descriptors are not available") from exc
    memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    memfd_create.restype = ctypes.c_int
    fd: int | None = None
    try:
        # Wrangler's executable launcher invokes Node in a second exec step. Keep
        # this descriptor inheritable for that short, allowlisted child chain;
        # ``pass_fds`` still ensures no unrelated child receives it.
        fd = int(memfd_create(b"disco-stripe-live-env", _MFD_ALLOW_SEALING))
        if fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        payload = "".join(
            f"{key}={_dotenv_escape(value)}\n" for key, value in env_vars.items()
        ).encode("utf-8")
        os.write(fd, payload)
        seals = _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
        fcntl.fcntl(fd, _F_ADD_SEALS, seals)
    except OSError as exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise StripeLiveVerifierError(f"could not create sealed in-memory env: {exc}") from exc
    assert fd is not None
    return fd, f"/proc/{os.getpid()}/fd/{fd}"


def _dotenv_escape(value: str) -> str:
    if "\n" in value or "'" in value or '"' in value or " " in value:
        return json.dumps(value)
    return value


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
        time.sleep(0.05)
    raise StripeLiveVerifierError(f"port {port} was still accepting connections after kill")


def _port_is_down(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return False
    except OSError:
        return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def _set_cookie_headers(headers: Any) -> list[str]:
    values = headers.get_all("Set-Cookie")
    return list(values) if values is not None else []


def _stripe_metadata(
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
    user_id: int,
) -> dict[str, str]:
    message = f"{app_binding}\0{plan_selector}\0{user_id}".encode()
    return {
        "disco_app_binding": app_binding,
        "disco_plan_selector": plan_selector,
        "disco_correlation": hmac.new(binding_secret.encode(), message, hashlib.sha256).hexdigest(),
    }


def _signed_header(secret: str, body: bytes, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode(),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _checkout_event(
    event_id: str,
    timestamp: int,
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
    user_id: int,
) -> bytes:
    payload = {
        "id": event_id,
        "created": timestamp,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_live_session",
                "subscription": "sub_live_subscription",
                "client_reference_id": str(user_id),
                "payment_status": "paid",
                "metadata": _stripe_metadata(app_binding, plan_selector, binding_secret, user_id),
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _scan_for_secrets(
    paths: Sequence[Path],
    exact_secrets: Sequence[str],
    patterns: Sequence[str],
) -> list[str]:
    hits: list[str] = []
    compiled = [_re.compile(pattern) for pattern in patterns]
    for base in paths:
        if not base.exists():
            continue
        if base.is_file():
            _scan_file(base, base, exact_secrets, compiled, hits)
        elif base.is_dir():
            for root, _dirs, files in os.walk(base):
                for name in files:
                    path = Path(root) / name
                    _scan_file(path, path.relative_to(base), exact_secrets, compiled, hits)
    return sorted(set(hits))


def _scan_file(
    path: Path,
    display: Path,
    exact_secrets: Sequence[str],
    compiled_patterns: Sequence[Any],
    hits: list[str],
) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return
    for secret in exact_secrets:
        if secret and secret in text:
            hits.append(f"{display}: exact secret value")
    for pattern in compiled_patterns:
        if pattern.search(text):
            hits.append(f"{display}: pattern {pattern.pattern!r}")


# Import re late to keep the module import-time stdlib-only until we need it.
import re as _re  # noqa: E402


def _check_result(name: str, passed: bool, evidence: str) -> VerifyCheck:
    return VerifyCheck(name=name, passed=passed, evidence=evidence)


async def _check_restricted_key_only(secret_store: SecretStore) -> VerifyCheck:
    """The host must accept only restricted ``rk_`` keys, never full ``sk_``."""
    sk_attempt = "sk_test_liveverifier_rejected_" + _py_secrets.token_urlsafe(16)
    try:
        configure_stripe_restricted_key(secret_store, sk_attempt)
    except StripeConfigurationError:
        return _check_result(
            "restricted_key_only",
            True,
            "full-access sk_ credential refused; restricted rk_ credential accepted",
        )
    return _check_result(
        "restricted_key_only",
        False,
        "a full-access sk_ credential was accepted (blast radius too large)",
    )


async def _check_price_injection_refused(bus: _HostBusServer, token: str) -> VerifyCheck:
    """The host handler must reject client-chosed price/amount before egress."""
    status, _body = await bus._post(
        PAYMENTS_CHECKOUT_SERVICE_NAME,
        {
            "plan_selector": "pro",
            "user_id": 1,
            "success_path": "/",
            "cancel_path": "/",
            "binding_proof": "0" * 64,
            "webhook_proof": "0" * 64,
            "amount": 999,
            "currency": "usd",
            "price": "price_attacker",
        },
        token=token,
    )
    if status == 422:
        return _check_result(
            "price_injection_refused",
            True,
            "client-chosen price/amount rejected with 422 before Stripe egress",
        )
    return _check_result(
        "price_injection_refused",
        False,
        f"client-chosen price/amount was not refused (status {status})",
    )


async def _check_forged_signature_rejected(
    worker: _LiveWorker,
    webhook_secret: str,
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
) -> VerifyCheck:
    """Forged or stale signatures must return 400 with zero writes."""
    now = int(time.time())
    forged_body = _checkout_event("evt_forged", now, app_binding, plan_selector, binding_secret, 1)
    forged_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=forged_body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": f"t={now},v1={'0' * 64}",
        },
    )
    stale_ts = now - _SIGNATURE_TOLERANCE_S - 1
    stale_body = _checkout_event(
        "evt_stale", stale_ts, app_binding, plan_selector, binding_secret, 1
    )
    stale_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=stale_body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": _signed_header(webhook_secret, stale_body, stale_ts),
        },
    )
    events = await worker.d1_count_async("stripe_events")
    fulfillments = await worker.d1_count_async("stripe_fulfillments")
    grants = await worker.d1_count_async("user_role_grants")
    if (
        forged_status == 400
        and stale_status == 400
        and events == 0
        and fulfillments == 0
        and grants == 0
    ):
        return _check_result(
            "forged_signature_rejected",
            True,
            "forged and stale signatures returned 400 with zero fulfillment or grant rows",
        )
    return _check_result(
        "forged_signature_rejected",
        False,
        (
            f"forged={forged_status}, stale={stale_status}, "
            f"stripe_events={events}, stripe_fulfillments={fulfillments}, "
            f"user_role_grants={grants}"
        ),
    )


async def _check_replay_deduped(
    worker: _LiveWorker,
    webhook_secret: str,
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
    admin_token: str,
) -> VerifyCheck:
    """A valid signed event replayed once must produce exactly one grant."""
    status, _ = await worker.post_json_async(
        "/api/register",
        {
            "email": "payer@example.com",
            "password": "correct horse battery staple",
            "role": "member",
        },
        token=admin_token,
    )
    if status != 201:
        return _check_result(
            "replay_deduped",
            False,
            f"test user registration failed (status {status})",
        )
    login_status, _ = await worker.post_json_async(
        "/api/login",
        {"email": "payer@example.com", "password": "correct horse battery staple"},
    )
    if login_status != 200:
        return _check_result(
            "replay_deduped",
            False,
            f"test user login failed (status {login_status})",
        )
    now = int(time.time())
    body = _checkout_event("evt_replay", now, app_binding, plan_selector, binding_secret, 1)
    header = _signed_header(webhook_secret, body, now)
    first_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": header},
    )
    first_events = await worker.d1_count_async("stripe_events")
    first_fulfillments = await worker.d1_count_async("stripe_fulfillments")
    first_grants = await worker.d1_count_async("user_role_grants")
    second_status, _ = await worker.request_async(
        "POST",
        "/api/stripe/webhook",
        body=body,
        headers={"Content-Type": "application/json", "Stripe-Signature": header},
    )
    status, text = await worker.get_async("/api/stripe/status")
    entitled = False
    if status == 200:
        try:
            data = json.loads(text)
            entitled = bool(data.get("entitled"))
        except (json.JSONDecodeError, AttributeError):
            pass
    events = await worker.d1_count_async("stripe_events")
    fulfillments = await worker.d1_count_async("stripe_fulfillments")
    grants = await worker.d1_count_async("user_role_grants")
    if (
        first_status == 200
        and second_status == 200
        and entitled
        and (first_events, first_fulfillments, first_grants) == (1, 1, 1)
        and events == 1
        and fulfillments == 1
        and grants == 1
    ):
        return _check_result(
            "replay_deduped",
            True,
            (
                "valid signed event replayed once produced exactly one event, "
                "one fulfillment, and one grant"
            ),
        )
    return _check_result(
        "replay_deduped",
        False,
        f"first={first_status}, second={second_status}, entitled={entitled}, "
        f"first_counts={(first_events, first_fulfillments, first_grants)}, "
        f"stripe_events={events}, stripe_fulfillments={fulfillments}, user_role_grants={grants}",
    )


def _stripe_event_body(
    event_id: str,
    event_type: str,
    created: int,
    object_value: Mapping[str, object],
) -> bytes:
    """Encode one Stripe envelope without trusting a fixture or cassette."""
    return json.dumps(
        {
            "id": event_id,
            "created": created,
            "type": event_type,
            "data": {"object": dict(object_value)},
        },
        separators=(",", ":"),
    ).encode("utf-8")


async def _check_webhook_lifecycle(
    worker: _LiveWorker,
    webhook_secret: str,
    app_binding: str,
    plan_selector: str,
    binding_secret: str,
) -> VerifyCheck:
    """Prove the live bundle cannot resurrect a revoked entitlement.

    This deliberately retains the lifecycle proof that complements the five
    mandatory exploit checks: an authentic but unpaid event is a no-op, an
    unrelated correlation is a no-op, revocation wins a same-timestamp stale
    completion, and an async-payment success can re-grant afterwards.
    """
    initial = (
        await worker.d1_count_async("stripe_events"),
        await worker.d1_count_async("stripe_fulfillments"),
        await worker.d1_count_async("user_role_grants"),
    )
    now = int(time.time())
    metadata = _stripe_metadata(app_binding, plan_selector, binding_secret, 1)

    def checkout(
        event_id: str,
        created: int,
        *,
        event_type: str = "checkout.session.completed",
        payment_status: str = "paid",
        event_metadata: Mapping[str, str] = metadata,
    ) -> bytes:
        return _stripe_event_body(
            event_id,
            event_type,
            created,
            {
                "id": "cs_live_session",
                "subscription": "sub_live_subscription",
                "client_reference_id": "1",
                "payment_status": payment_status,
                "metadata": dict(event_metadata),
            },
        )

    async def deliver(body: bytes) -> int:
        status, _ = await worker.request_async(
            "POST",
            "/api/stripe/webhook",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": _signed_header(webhook_secret, body, now),
            },
        )
        return status

    async def entitled() -> bool:
        status, body = await worker.get_async("/api/stripe/status")
        if status != 200:
            return False
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("entitled") is True

    unpaid_status = await deliver(
        checkout("evt_lifecycleunpaid", now + 10, payment_status="unpaid")
    )
    mismatched_status = await deliver(
        checkout(
            "evt_lifecyclemismatch",
            now + 20,
            event_metadata={
                "disco_app_binding": app_binding,
                "disco_plan_selector": plan_selector,
                "disco_correlation": "0" * 64,
            },
        )
    )
    after_noops = (
        await worker.d1_count_async("stripe_events"),
        await worker.d1_count_async("stripe_fulfillments"),
        await worker.d1_count_async("user_role_grants"),
    )
    paid_status = await deliver(checkout("evt_lifecyclepaid", now + 100))
    after_paid = await entitled()

    revoked_body = _stripe_event_body(
        "evt_lifecyclerevoked",
        "customer.subscription.deleted",
        now + 200,
        {"id": "sub_live_subscription"},
    )
    revoked_status = await deliver(revoked_body)
    after_revoke = await entitled()

    stale_status = await deliver(checkout("evt_lifecyclestale", now + 200))
    after_stale = await entitled()
    async_status = await deliver(
        checkout(
            "evt_lifecycleasyncsuccess",
            now + 300,
            event_type="checkout.session.async_payment_succeeded",
        )
    )
    after_async = await entitled()
    final = (
        await worker.d1_count_async("stripe_events"),
        await worker.d1_count_async("stripe_fulfillments"),
        await worker.d1_count_async("user_role_grants"),
    )
    statuses = (
        unpaid_status,
        mismatched_status,
        paid_status,
        revoked_status,
        stale_status,
        async_status,
    )
    expected_final = (initial[0] + 4, initial[1], initial[2])
    if (
        unpaid_status == 200
        and mismatched_status == 200
        and after_noops == initial
        and paid_status == 200
        and after_paid
        and revoked_status == 200
        and not after_revoke
        and stale_status == 200
        and not after_stale
        and async_status == 200
        and after_async
        and final == expected_final
    ):
        return _check_result(
            "webhook_lifecycle",
            True,
            (
                "unpaid/unrelated events were no-ops; revoke resisted stale completion; "
                "async success re-granted"
            ),
        )
    return _check_result(
        "webhook_lifecycle",
        False,
        (
            f"statuses={statuses}, "
            f"entitled={(after_paid, after_revoke, after_stale, after_async)}, "
            f"counts={(initial, after_noops, final)}, expected_final={expected_final}"
        ),
    )


async def _check_secret_absence(
    tree: Mapping[str, str],
    worker: _WorkerdApp,
    exact_secrets: Sequence[str],
    private_root: Path,
) -> VerifyCheck:
    """No Stripe secret value or secret-shaped pattern may leak to tree/bundle/logs/state."""
    scan_paths: list[Path] = [worker.app_dir, private_root]
    if worker.bundle_dir is not None:
        scan_paths.append(worker.bundle_dir)
    if worker.dev_log_path is not None:
        scan_paths.append(worker.dev_log_path)
    hits = _scan_for_secrets(scan_paths, exact_secrets, _SECRET_PATTERNS)
    # Also scan the emitted tree directly (it is already in app_dir, but the caller's
    # tree mapping is the authoritative source before materialization).
    for contents in tree.values():
        for secret in exact_secrets:
            if secret and secret in contents:
                hits.append("tree: exact secret value")
                break
        for pattern in _SECRET_PATTERNS:
            if _re.search(pattern, contents):
                hits.append(f"tree: pattern {pattern!r}")
    if not hits:
        return _check_result(
            "secret_absence",
            True,
            (
                "no Stripe secret values or secret-shaped patterns in "
                "tree, bundle, logs, or local state"
            ),
        )
    return _check_result("secret_absence", False, "; ".join(sorted(set(hits))))


class _StripeVerifyRun:
    """One complete isolated live-verification run."""

    def __init__(self, app: AppSpec, design: DesignSpec, tree: Mapping[str, str]) -> None:
        self.app = app
        self.design = design
        self.tree = tree
        self.tmp_root: Path | None = None
        self.secret_store: SecretStore | None = None
        self.config_store: ConfigStore | None = None
        self.event_store: SqliteEventStore | None = None
        self.token_store: HostTokenStore | None = None
        self.stripe_config_store: StripeAppConfigStore | None = None
        self.bus: _HostBusServer | None = None
        self.bus_cert: Path | None = None
        self.bus_key: Path | None = None
        self.worker: _WorkerdApp | None = None
        self.token: str | None = None
        self.token_selector: str | None = None
        self.admin_token: str = ""
        self.d1_control_token: str = ""
        self.owner_id: str = "stripe-live-owner"
        self.conversation_id: str = "stripe-live-verify"
        self.audience: str = ""
        self.origin: str = ""
        self.worker_port: int | None = None
        self.webhook_secret: str = ""
        self.binding_secret: str = ""
        self.restricted_key: str = ""
        self.price_id: str = ""
        self.exact_secrets: list[str] = []

    async def run(self) -> PrimitiveVerifyResult:
        checks: list[VerifyCheck] = []
        try:
            self._ensure_prerequisites()
            stripe = self.app.stripe
            assert stripe is not None
            self._setup_temp_stores()
            restricted_check = await self._configure_host_plane()
            checks.append(restricted_check)

            # Price injection is exercised against the host bus directly.
            assert self.bus is not None
            assert self.token is not None
            price_check = await _check_price_injection_refused(self.bus, self.token)
            checks.append(price_check)

            # Worker-level checks require wrangler dev to be running.
            assert self.tmp_root is not None
            if self.worker_port is None:
                raise StripeLiveVerifierError("Worker port was not reserved")
            if self.bus_cert is None:
                raise StripeLiveVerifierError("loopback bus certificate was not initialized")
            self.worker = _WorkerdApp(
                self.tree,
                self._env_bindings(),
                self.tmp_root,
                self.bus_cert,
            )
            self.worker.__enter__()
            try:
                self.worker.boot(self.worker_port)
                with _MiniflareRuntimeProbe(
                    self.worker, self.tmp_root, self.d1_control_token
                ) as runtime_probe:
                    runtime_ready = await asyncio.to_thread(
                        runtime_probe.payments_ready, self.admin_token
                    )
                    checks.append(
                        await _check_forged_signature_rejected(
                            runtime_probe,
                            self.webhook_secret,
                            stripe.app_binding,
                            stripe.plan_selector,
                            self.binding_secret,
                        )
                    )
                    replay = await _check_replay_deduped(
                        runtime_probe,
                        self.webhook_secret,
                        stripe.app_binding,
                        stripe.plan_selector,
                        self.binding_secret,
                        self.admin_token,
                    )
                    if not runtime_ready:
                        replay = _check_result(
                            "replay_deduped",
                            False,
                            "the deployed Worker bundle could not prove payments.ready through "
                            "the authenticated loopback host bus",
                        )
                    checks.append(replay)
                    if runtime_ready:
                        lifecycle = await _check_webhook_lifecycle(
                            runtime_probe,
                            self.webhook_secret,
                            stripe.app_binding,
                            stripe.plan_selector,
                            self.binding_secret,
                        )
                        if not lifecycle.passed:
                            raise StripeLiveVerifierError(lifecycle.evidence)
                    checks.append(
                        await _check_secret_absence(
                            self.tree, self.worker, self.exact_secrets, self.tmp_root
                        )
                    )
            finally:
                try:
                    self.worker.__exit__(None, None, None)
                except Exception:
                    # Keep the handle for _cleanup() to make a second process
                    # group termination attempt before revoking credentials.
                    raise
                else:
                    self.worker = None
        except StripeLiveVerifierError as exc:
            _LOG.warning("Stripe live verifier failed: %s", exc)
            checks.extend(_check_result(name, False, str(exc)) for name in _REQUIRED_CHECKS)
        finally:
            await self._cleanup()

        return _result(checks)

    def _ensure_prerequisites(self) -> None:
        if self.app is None or self.app.stripe is None:
            raise StripeLiveVerifierError("AppSpec.stripe metadata is missing")
        if self.app.app_kind != "records":
            raise StripeLiveVerifierError("Stripe live verifier requires a records app")
        if not _find_wrangler(Path.cwd()):
            raise StripeLiveVerifierError("wrangler executable is not available")

    def _setup_temp_stores(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="disco-stripe-live-"))
        self.bus_cert, self.bus_key = _make_loopback_tls_pair(self.tmp_root)
        app_secret = base64.b64encode(_py_secrets.token_bytes(32)).decode("ascii")
        self.secret_store = SecretStore(
            path=self.tmp_root / "secrets.json",
            box=SecretBox(app_secret),
        )
        self.config_store = ConfigStore(path=self.tmp_root / "disco-config.json")
        self.event_store = SqliteEventStore(self.tmp_root / "events.db")
        self.event_store.create_conversation(self.conversation_id, owner_id=self.owner_id)
        self.token_store = HostTokenStore(self.tmp_root / "tokens.db")
        self.stripe_config_store = StripeAppConfigStore(self.tmp_root / "stripe.db")
        stripe = self.app.stripe
        if stripe is None:
            raise StripeLiveVerifierError("AppSpec.stripe metadata is missing")
        self.audience = stripe.app_binding
        self.worker_port = _free_port()
        self.origin = f"http://127.0.0.1:{self.worker_port}"
        self.admin_token = "adm_" + _py_secrets.token_urlsafe(24)
        self.d1_control_token = "d1ctl_" + _py_secrets.token_urlsafe(24)

    async def _configure_host_plane(self) -> VerifyCheck:
        assert self.secret_store is not None
        assert self.config_store is not None
        assert self.event_store is not None
        assert self.token_store is not None
        assert self.stripe_config_store is not None
        assert self.bus_cert is not None and self.bus_key is not None
        stripe = self.app.stripe
        assert stripe is not None

        # Restricted-key-only gate: set the real key after proving sk_ is refused.
        restricted_check = await _check_restricted_key_only(self.secret_store)
        if not restricted_check.passed:
            raise StripeLiveVerifierError(restricted_check.evidence)
        self.restricted_key = "rk_test_liveverifier_" + _py_secrets.token_urlsafe(16)
        configure_stripe_restricted_key(self.secret_store, self.restricted_key)

        self.webhook_secret = "whsec_" + _py_secrets.token_urlsafe(32)
        configure_stripe_webhook_secret(
            self.secret_store, self.owner_id, self.audience, self.webhook_secret
        )
        self.binding_secret = ensure_stripe_binding_secret(
            self.secret_store, self.owner_id, self.audience
        )

        self.price_id = (
            "price_" + _py_secrets.token_urlsafe(16).replace("_", "").replace("-", "")[:24]
        )

        # Origin approval must exist before the checkout handler will egress.
        approval_store = self.config_store.approval_store(secret_store=self.secret_store)
        approval_store.approve(
            STRIPE_API_URL,
            PAYMENTS_CHECKOUT_SERVICE_NAME,
            STRIPE_SECRET_REF,
        )

        bus_port = _free_port()

        self.token = self.token_store.mint(
            self.conversation_id,
            self.owner_id,
            self.audience,
            allowed_services=frozenset(
                {PAYMENTS_CHECKOUT_SERVICE_NAME, PAYMENTS_READY_SERVICE_NAME}
            ),
            allowed_origins=frozenset({self.origin}),
            kind="probe",
        )
        parsed = self.token_store._parse(self.token)
        self.token_selector = parsed[0] if parsed else None

        self.bus = _HostBusServer(
            self.event_store,
            self.secret_store,
            self.config_store,
            self.token_store,
            self.stripe_config_store,
            bus_port,
            self.bus_cert,
            self.bus_key,
        )
        await self.bus.__aenter__()

        self.stripe_config_store.configure(
            owner_id=self.owner_id,
            audience=self.audience,
            plan_selector=stripe.plan_selector,
            stripe_price_id=self.price_id,
            allowed_return_origins=frozenset({self.origin}),
            enabled=True,
            secret_store=self.secret_store,
        )

        self.exact_secrets = [
            self.restricted_key,
            self.webhook_secret,
            self.binding_secret,
            self.token,
            self.admin_token,
            self.d1_control_token,
            self.secret_store.signing_secret or "",
        ]
        return restricted_check

    def _env_bindings(self) -> dict[str, str]:
        assert self.token is not None
        assert self.bus is not None
        return {
            "STRIPE_WEBHOOK_SECRET": self.webhook_secret,
            "STRIPE_APP_BINDING_SECRET": self.binding_secret,
            "STRIPE_RUNTIME_READY": "1",
            "DISCO_SVC_BUS": self.bus.origin,
            "DISCO_SVC_TOKEN": self.token,
            "ADMIN_TOKEN": self.admin_token,
            "DISCO_LIVE_D1_CONTROL_TOKEN": self.d1_control_token,
        }

    async def _cleanup(self) -> None:
        failures: list[str] = []
        if self.bus is not None:
            try:
                await self.bus.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - keep revocation and deletion running
                failures.append(type(exc).__name__)
            self.bus = None
        if self.worker is not None:
            try:
                self.worker.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - continue the security cleanup
                failures.append(type(exc).__name__)
            self.worker = None
        if self.token_store is not None and self.token_selector is not None:
            try:
                self.token_store.revoke(self.token_selector)
            except Exception as exc:  # noqa: BLE001 - report after remaining cleanup
                failures.append(type(exc).__name__)
        for store in (self.token_store, self.stripe_config_store, self.event_store):
            if store is not None:
                try:
                    store.close()
                except Exception as exc:  # noqa: BLE001 - report after remaining cleanup
                    failures.append(type(exc).__name__)
        if self.tmp_root is not None:
            try:
                shutil.rmtree(self.tmp_root)
            except OSError as exc:
                failures.append(type(exc).__name__)
            self.tmp_root = None
        if failures:
            raise StripeLiveVerifierError(
                "live verifier cleanup was incomplete (" + ", ".join(sorted(set(failures))) + ")"
            )


_REQUIRED_CHECKS = (
    "forged_signature_rejected",
    "replay_deduped",
    "secret_absence",
    "restricted_key_only",
    "price_injection_refused",
)


def _result(checks: list[VerifyCheck]) -> PrimitiveVerifyResult:
    by_name = {check.name: check for check in checks}
    ordered = [
        by_name.get(name, _check_result(name, False, "check missing")) for name in _REQUIRED_CHECKS
    ]
    passed = all(check.passed for check in ordered)
    return PrimitiveVerifyResult(
        ok=passed,
        detail=f"{sum(c.passed for c in ordered)}/{len(ordered)} checks passed",
        checks=tuple(ordered),
    )


async def _stripe_live_verify(
    app: AppSpec,
    design: DesignSpec,
    tree: Mapping[str, str],
) -> PrimitiveVerifyResult:
    run = _StripeVerifyRun(app, design, tree)
    try:
        return await run.run()
    except Exception as exc:
        _LOG.exception("Stripe live verifier raised unexpectedly")
        return _result(
            [
                _check_result(
                    name, False, f"host live verifier raised {type(exc).__name__} (fail-closed)"
                )
                for name in _REQUIRED_CHECKS
            ]
        )


def make_stripe_live_verifier() -> Any:
    """Factory for the ``stripe.security.v1`` host-owned live verifier callback.

    Returns a ``PrimitiveLiveVerifier`` callable that the tools layer dispatches
    through ``ToolContext.primitive_live_verifier``.
    """

    async def verifier(
        live_id: str,
        app: AppSpec,
        design: DesignSpec,
        tree: Mapping[str, str],
    ) -> PrimitiveVerifyResult:
        if live_id != _STRIPE_LIVE_VERIFY_ID:
            return _result(
                [
                    _check_result(name, False, f"unknown live_verify_id {live_id!r}")
                    for name in _REQUIRED_CHECKS
                ]
            )
        return await _stripe_live_verify(app, design, tree)

    return verifier


# Reusable, host-owned exploit-harness pieces. They remain implementation
# details of the agent-server (never part of core or generated apps), but F3.3
# shares the exact materialize/build/workerd and secret-scan machinery so the
# two security primitives cannot drift onto subtly different proof paths.
LiveWorkerdApp = _WorkerdApp
find_wrangler = _find_wrangler
free_loopback_port = _free_port
make_loopback_tls_pair = _make_loopback_tls_pair
scan_live_paths_for_secrets = _scan_for_secrets

__all__ = [
    "LiveWorkerdApp",
    "find_wrangler",
    "free_loopback_port",
    "make_loopback_tls_pair",
    "make_stripe_live_verifier",
    "scan_live_paths_for_secrets",
]

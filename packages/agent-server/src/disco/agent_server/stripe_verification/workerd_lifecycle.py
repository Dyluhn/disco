"""Workerd lifecycle owner — materialize and drive a generated AppKit Worker.

This module owns the ``_WorkerdApp`` class that stands up a real generated
Worker under local wrangler/workerd, manages its lifecycle (enter/exit/boot/
kill), and exposes the HTTP and D1 operations needed by the exploit checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import TextIO

from .evidence import (
    _BOOT_TIMEOUT_S,
    _D1_INIT_TIMEOUT_S,
    _DEPLOY_TIMEOUT_S,
    _HTTP_TIMEOUT_S,
    _KILL_TIMEOUT_S,
    _NPM_CI_TIMEOUT_S,
    StripeLiveVerifierError,
    _command_env,
    _CommandResult,
    _CookieJar,
    _d1_database_name,
    _find_wrangler,
    _materialize_tree,
    _open_memory_env,
    _parse_d1_count_json,
    _port_is_down,
    _read_capped_file,
    _run_checked,
    _set_cookie_headers,
    _stub_dist,
    _terminate_process,
    _wait_port_down,
)


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
            self._kill()
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
            self._kill()
            raise

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

    async def request_async(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, str]:
        """Avoid blocking the loop that serves the local authenticated bus."""
        return await asyncio.to_thread(self._request, method, path, body, headers, token)

    async def post_json_async(
        self,
        path: str,
        obj: Mapping[str, object],
        *,
        token: str | None = None,
    ) -> tuple[int, str]:
        return await asyncio.to_thread(self._post_json, path, obj, token=token)

    async def get_async(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return await asyncio.to_thread(self._get, path, token=token)

    async def d1_count_async(self, table: str) -> int:
        return await asyncio.to_thread(self._d1_count, table)

    # ------------------------------------------------------------------
    # Private helpers — not counted as public methods
    # ------------------------------------------------------------------

    def _kill(self) -> None:
        proc = self._proc
        port = self._port
        if proc is not None:
            _terminate_process(proc)
            self._proc = None
        self._close_dev_log()
        if port is not None:
            _wait_port_down(port, timeout_s=_KILL_TIMEOUT_S)

    def _is_down(self) -> bool:
        return self._port is None or _port_is_down(self._port)

    def _request(
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

    def _post_json(
        self,
        path: str,
        obj: Mapping[str, object],
        *,
        token: str | None = None,
    ) -> tuple[int, str]:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return self._request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
            token=token,
        )

    def _get(self, path: str, *, token: str | None = None) -> tuple[int, str]:
        return self._request("GET", path, token=token)

    def _dev_log_tail(self) -> str:
        if self._dev_log_handle is not None:
            self._dev_log_handle.flush()
        if self._dev_log_path is None or not self._dev_log_path.exists():
            return ""
        return _read_capped_file(self._dev_log_path)

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
                    f"wrangler dev exited early with code {proc.returncode}\n{self._dev_log_tail()}"
                )
            try:
                self._get("/api/leads")
                return
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(0.05)
        raise StripeLiveVerifierError(
            f"wrangler dev was not ready within {_BOOT_TIMEOUT_S:g}s ({last_error})\n"
            f"{self._dev_log_tail()}"
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

    def _d1_count(self, table: str) -> int:
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

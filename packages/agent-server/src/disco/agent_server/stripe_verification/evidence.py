"""Immutable evidence and shared harness helpers for the Stripe live verifier.

This module owns the cohesive immutable evidence pieces: the error type, cookie
jar, command result, live-worker protocol, host-bus server, secret scanning,
event encoding, check-result construction, and the low-level process/port/
command/TLS/materialization helpers.  These are shared by the workerd lifecycle,
probe-script, and webhook-lifecycle owners.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import fcntl
import hashlib
import hmac
import json
import os
import re as _re
import shutil
import signal
import socket
import ssl
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, cast

import uvicorn
from disco.core.appkit.primitives import VerifyCheck
from disco.core.llm.config_store import ConfigStore
from disco.core.llm.secrets import SecretStore
from disco.core.quota import SqliteQuotaStore
from disco.core.store.sqlite import SqliteEventStore
from disco.core.stripe_host_service import StripeAppConfigStore
from fastapi import FastAPI

from ..host_service_bus import make_host_service_bus_router
from ..host_token_store import HostTokenStore

# ---------------------------------------------------------------------------
# Constants — shared by all stripe_verification submodules
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class StripeLiveVerifierError(RuntimeError):
    """The live verifier could not complete one or more checks."""


# ---------------------------------------------------------------------------
# Cookie jar — minimal session round-trip support
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Live-worker protocol — the real local Worker operations needed by checks
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


# ---------------------------------------------------------------------------
# Silent uvicorn server — does not capture process signal handlers
# ---------------------------------------------------------------------------


class _SilentServer(uvicorn.Server):
    """Uvicorn server that does not capture process signal handlers."""

    def install_signal_handlers(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fake runtime — minimum runtime duck type for the host-service bus
# ---------------------------------------------------------------------------


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


def _fake_runtime_factory(
    secret_store: SecretStore,
    config_store: ConfigStore,
    store: SqliteEventStore,
) -> _FakeRuntime:
    """Construct a ``_FakeRuntime`` for the host-service bus."""
    return _FakeRuntime(secret_store, config_store, store)


# ---------------------------------------------------------------------------
# Host bus server — minimal loopback host-service bus
# ---------------------------------------------------------------------------


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
        self._task: Any | None = None

    @property
    def origin(self) -> str:
        return f"https://127.0.0.1:{self._port}"

    async def __aenter__(self) -> _HostBusServer:
        app = FastAPI()
        app.include_router(
            make_host_service_bus_router(
                self._store,
                cast(
                    Any,
                    _fake_runtime_factory(self._secret_store, self._config_store, self._store),
                ),
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


# ---------------------------------------------------------------------------
# Check-result and event-encoding helpers
# ---------------------------------------------------------------------------


def _check_result(name: str, passed: bool, evidence: str) -> VerifyCheck:
    return VerifyCheck(name=name, passed=passed, evidence=evidence)


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


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Output capping and file reading
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Process, port, and command helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Wrangler/bundle/miniflare discovery
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    # current/packages/agent-server/src/disco/agent_server/stripe_verification/evidence.py
    return Path(__file__).resolve().parents[7]


def _bin_name(name: str) -> str:
    return f"{name}.cmd" if os.name == "nt" else name


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


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

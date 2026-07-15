"""AppKit EPIC O — the injectable command runner + token verifier.

The deploy executor never shells out directly; it drives a :class:`CommandRunner`
(npm build + ``npx wrangler`` calls) and an optional :class:`TokenVerifier` (the
O1 connection test). Both are Protocols so tests inject a FAKE that records the
calls and returns canned results — no real network, no real ``wrangler``, no real
Cloudflare account in CI. The production implementations live here too but are
exercised only by the owner's real, documented deploy (Epic M-style live run),
never by the test suite.

SECRET HANDLING (load-bearing): the API token is injected ONLY through the
subprocess ENV (``CLOUDFLARE_API_TOKEN``), never as an argv element and never
logged. ``SubprocessCommandRunner`` returns stdout/stderr to the caller, which
redacts them before they touch any transcript/record (see ``deploy.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

#: Cloudflare's documented token-status endpoint (read-only; verifies the token
#: is valid/active without granting or mutating anything).
_TOKEN_VERIFY_URL = "https://api.cloudflare.com/client/v4/user/tokens/verify"

#: Per-step wall-clock cap for a host wrangler/npm subprocess. A hung, prompting,
#: or fake/wedged ``wrangler`` is KILLED+reaped at this bound so it can never
#: wedge the deploy request indefinitely.
_DEFAULT_STEP_TIMEOUT_S = 300.0
#: Per-stream cap on captured stdout/stderr (1 MiB). A build/wrangler that emits
#: an unbounded torrent of output cannot exhaust memory or bloat the
#: transcript/record — output past the cap is dropped (with a truncation marker).
_DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
#: Chunk size for the capped incremental stream reads.
_READ_CHUNK_BYTES = 65_536
#: returncode reported when the subprocess could not be spawned at all (missing
#: binary / bad cwd / OS spawn failure) — a structured failure, NOT a raised 500.
_SPAWN_FAILED_RC = 127
#: returncode reported when a step is killed at the timeout.
_TIMEOUT_RC = -1


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill a (timed-out) subprocess and REAP it so no zombie/orphan is left
    holding the deploy. Tolerant of an already-exited process."""
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one runner invocation. ``stdout``/``stderr`` are RAW (the
    caller redacts before persisting)."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class BuildResult:
    """The outcome of the UNTRUSTED ``npm ci && npm run build`` step (P0-1/P0-2).
    Shaped like :class:`CommandResult` (``returncode``/``stdout``/``stderr``/``ok``)
    so the executor records + aborts on it uniformly. ``isolated`` is True only
    when the build actually ran in an isolating sandbox (no host-secret/filesystem
    access) — a build that could not be isolated reports ``isolated=False`` and a
    real deploy is refused before any mutation."""

    returncode: int
    stdout: str
    stderr: str
    isolated: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.isolated


@runtime_checkable
class BuildBackend(Protocol):
    """Runs the workspace-controlled ``npm ci && npm run build`` for a deploy in an
    ISOLATING sandbox (gVisor/container) — NEVER as an unsandboxed same-user
    subprocess (P0-1). The build script lives in the agent/workspace-controlled
    ``package.json``, so it must execute with NO access to host secrets (the
    SecretStore, ``~/.config/disco``) or the host filesystem. The install is
    ``npm ci`` (deterministic, exactly from the hash-covered lockfile — P0-2), not
    a bare build on un-pinned ``node_modules``. The built output (``./dist``) is
    synced back into *workspace* so the trusted ``wrangler deploy`` (run with the
    CF token but NOT the untrusted build) publishes a hash-bound artifact.

    Injectable so tests provide a fake that records the dispatch + asserts the
    install/build commands without spawning a real sandbox."""

    #: True iff this backend runs the build in a real isolating sandbox. A backend
    #: that cannot isolate (no sandbox infra, or the same-user ``process`` backend)
    #: returns False, and the executor refuses a real deploy
    #: (``RefusalReason.BUILD_NOT_SANDBOXED``).
    @property
    def isolates(self) -> bool: ...

    async def build(
        self, workspace: Path, *, install_cmd: str, build_cmd: str, asset_dir: str = "dist"
    ) -> BuildResult: ...


@runtime_checkable
class CommandRunner(Protocol):
    """Runs one subprocess (npm / npx wrangler). Injectable so tests provide a
    fake that asserts the argv + env (token presence) and returns a canned
    result without spawning anything."""

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin: str | None = None,
    ) -> CommandResult: ...


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a Cloudflare API token is active. Returns (ok, detail)."""

    async def verify(self, token: str) -> tuple[bool, str]: ...


class SubprocessCommandRunner:
    """PRODUCTION runner — spawns the real command. The token is passed via
    ``env`` only (the executor sets ``CLOUDFLARE_API_TOKEN`` there); argv carries
    no secret. Used ONLY in the owner's real deploy, never in tests/CI.

    HARDENED (SEC-31/CORR-23): every host subprocess is bounded so an untrusted /
    misbehaving ``wrangler`` (or ``npm``) cannot wedge or exhaust the deploy
    request — (1) a per-step ``timeout_s`` after which the process is KILLED+reaped
    (no indefinite hang on a prompting/hung binary), (2) spawn failures (missing
    binary, bad cwd) are caught and returned as a STRUCTURED non-zero result rather
    than raising (so a deploy request fails closed, never 500s), and (3) captured
    stdout/stderr are CAPPED at ``max_output_bytes`` per stream (an unbounded output
    torrent cannot exhaust memory or bloat the transcript/record)."""

    def __init__(
        self,
        *,
        timeout_s: float = _DEFAULT_STEP_TIMEOUT_S,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin: str | None = None,
    ) -> CommandResult:
        return self._run_sync(argv, cwd=cwd, env=env, stdin=stdin)

    def _run_sync(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin: str | None = None,
    ) -> CommandResult:
        binary = argv[0] if argv else "<empty argv>"
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                input=stdin.encode() if stdin is not None else None,
                capture_output=True,
                timeout=self._timeout_s,
                check=False,
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            # Missing wrangler/npm binary, bad cwd, or any OS spawn failure → a
            # structured failure the executor aborts on, NEVER an unhandled 500.
            return CommandResult(
                returncode=_SPAWN_FAILED_RC,
                stdout="",
                stderr=f"failed to launch {binary!r}: {type(exc).__name__}: {exc}",
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                returncode=_TIMEOUT_RC,
                stdout="",
                stderr=(
                    f"command {binary!r} exceeded the {self._timeout_s:g}s deploy-step "
                    "timeout and was killed"
                ),
            )
        return CommandResult(
            returncode=completed.returncode,
            stdout=self._decode_capped(completed.stdout),
            stderr=self._decode_capped(completed.stderr),
        )

    def _decode_capped(self, data: bytes) -> str:
        truncated = len(data) > self._max_output_bytes
        text = data[: self._max_output_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n…[output truncated at {self._max_output_bytes} bytes]"
        return text

    async def _drain(self, proc: asyncio.subprocess.Process, stdin: str | None) -> tuple[str, str]:
        """Feed stdin (if any), read stdout+stderr CONCURRENTLY under a per-stream
        byte cap, and reap. Returns the decoded, capped ``(stdout, stderr)``."""

        async def _feed() -> None:
            if proc.stdin is None:
                return
            try:
                if stdin is not None:
                    proc.stdin.write(stdin.encode())
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                    if proc.stdin.can_write_eof():
                        proc.stdin.write_eof()
                try:
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        _, out, err = await asyncio.gather(
            _feed(),
            self._read_capped(proc.stdout),
            self._read_capped(proc.stderr),
        )
        await proc.wait()
        return out, err

    async def _read_capped(self, reader: asyncio.StreamReader | None) -> str:
        """Read a stream incrementally, retaining at most ``max_output_bytes`` so an
        unbounded output torrent cannot exhaust memory. Excess is drained+discarded
        (bounded overall by the run timeout) and flagged with a truncation marker."""
        if reader is None:
            return ""
        buf = bytearray()
        truncated = False
        while True:
            chunk = await reader.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            room = self._max_output_bytes - len(buf)
            if room > 0:
                if len(chunk) > room:
                    truncated = True
                buf.extend(chunk[:room])
            else:
                truncated = True
        text = buf.decode("utf-8", errors="replace")
        if truncated:
            text += f"\n…[output truncated at {self._max_output_bytes} bytes]"
        return text


class HttpTokenVerifier:
    """PRODUCTION token verifier — a single read-only GET to Cloudflare's
    ``/user/tokens/verify``. Used by the O1 connection test."""

    async def verify(self, token: str) -> tuple[bool, str]:
        headers = {"Authorization": f"Bearer {token}"}
        try:
            # SEC-33: trust_env=False so a hostile ``HTTP(S)_PROXY``/``ALL_PROXY``
            # in the environment can NOT intercept the ad-hoc CF token verify call
            # (no env proxy, no env CA/netrc). A direct TLS connection to Cloudflare.
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                res = await client.get(_TOKEN_VERIFY_URL, headers=headers)
        except (httpx.HTTPError, ssl.SSLError) as exc:
            # CORR-24: catch the FULL transport surface (connect/timeout/proxy/TLS/
            # protocol errors all derive from httpx.HTTPError) + raw ssl errors →
            # a structured verify failure, never a raised 500.
            return False, f"could not reach Cloudflare to verify the token: {type(exc).__name__}"
        if res.status_code in (401, 403):
            return False, "Cloudflare rejected the API token (unauthorized)."
        if res.status_code != 200:
            return False, f"Cloudflare token verify returned HTTP {res.status_code}."
        try:
            body = res.json()
        except ValueError:
            return False, "Cloudflare token verify returned a non-JSON body."
        # CORR-24: a non-object top-level body (array / null / string / number) must
        # NOT crash the verifier with an AttributeError (→ 500) — handle it as a
        # structured failure.
        if not isinstance(body, dict):
            return False, "Cloudflare token verify returned an unexpected (non-object) body."
        result = body.get("result")
        status = result.get("status") if isinstance(result, dict) else None
        if status == "active":
            return True, "token is active"
        return False, f"token status is {status!r} (expected 'active')."


__all__ = [
    "BuildBackend",
    "BuildResult",
    "CommandResult",
    "CommandRunner",
    "HttpTokenVerifier",
    "SubprocessCommandRunner",
    "TokenVerifier",
]

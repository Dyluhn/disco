"""`PiProcess` — the Python manager for the Node Pi-kernel sidecar (PR B2).

Disco Pi Build Kernel Campaign, EPIC B / PR B2. This module spawns the already
built Node sidecar (`packages/pi-kernel/dist/index.js`) and speaks its FROZEN,
newline-delimited JSON stdio control protocol (`packages/pi-kernel/src/protocol.ts`,
`index.ts`). It owns ONLY the transport: spawn, write commands, decode outbound
frames, and tear the process *tree* down. The behavioral kernel (`PiKernel`,
later PRs) consumes `events()` and drives `prompt`/`followup`/`cancel`/`aclose`.

Design mirrors the sidecar's two-plane model:
  * DATA commands (`init`/`prompt`/`followup`) are written as one JSON line each.
  * `cancel` is a CONTROL command — it aborts the in-flight turn WITHOUT killing
    the process (the sidecar stays alive and resumable). Only stdin EOF (via
    `aclose`) asks for a graceful exit.

Hardening invariants this manager upholds (so a hostile/slow/runaway sidecar
cannot harm the host):
  * The child runs in its OWN session/process group (`start_new_session=True`),
    so `aclose` can `killpg` the WHOLE tree — a grandchild (e.g. a preview
    server the agent spawned) can never outlive the kernel.
  * Graceful first, forceful second: EOF → wait → `SIGTERM` the group → wait →
    `SIGKILL` the group. A wedged sidecar still terminates.
  * Provider-credential env is scrubbed from the child env (defense in depth —
    the sidecar self-scrubs too, but the spawner must not rely on that).
  * An unparseable stdout line is a DIAGNOSTIC (ring-buffered), never a crash;
    stderr is ring-buffered too so a chatty child cannot grow memory without
    bound.

The protocol shapes are mirrored here as `TypedDict`s — this module imports
NOTHING from the TS package (the wire format is the only contract).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections import deque
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Final, Literal, TypedDict, cast

__all__ = [
    "PROTOCOL_VERSION",
    "ErrorEvent",
    "ExitEvent",
    "HeartbeatEvent",
    "KernelGatewayConfig",
    "KernelInitConfig",
    "PiProcess",
    "PiProcessError",
    "ReadyEvent",
    "default_pi_kernel_entry",
    "scrub_provider_credential_env",
]

#: Wire protocol version the sidecar advertises in its `ready` frame. Mirrors
#: `PROTOCOL_VERSION` in `protocol.ts`; a mismatch is surfaced (not silently
#: tolerated) by `start`.
PROTOCOL_VERSION: Final = 1


# ---------------------------------------------------------------------------
# Protocol shapes (mirror packages/pi-kernel/src/protocol.ts — do NOT import TS)
# ---------------------------------------------------------------------------


class KernelGatewayConfig(TypedDict, total=False):
    """The loopback Disco inference gateway — the ONLY provider the kernel may
    reach. `baseUrl`/`model`/`apiKey` are required when present; `api` is the
    optional wire dialect (defaults to `openai-completions` sidecar-side)."""

    baseUrl: str
    model: str
    apiKey: str
    api: str


class KernelInitConfig(TypedDict, total=False):
    """The `config` payload of the `init` command. All fields optional: with no
    `gateway` the session has no model (a `prompt` then fails loudly, never
    falling back to a real provider)."""

    conversationId: str
    heartbeatMs: int
    gateway: KernelGatewayConfig


class ToolSurface(TypedDict):
    """Summary of the session's tool surface (proves the no-tools posture)."""

    activeToolNames: list[str]
    customToolCount: int
    noTools: Literal["all", "builtin"] | None


class ReadyEvent(TypedDict):
    """Emitted once after `init` succeeds; the session is live and idle."""

    type: Literal["ready"]
    protocolVersion: int
    piVersion: str
    model: str | None
    tools: ToolSurface


class HeartbeatEvent(TypedDict):
    """Periodic liveness signal while the sidecar runs."""

    type: Literal["heartbeat"]
    ts: int


class AgentEventMessage(TypedDict):
    """A mapped Pi `AgentSessionEvent` envelope (see events.ts)."""

    type: Literal["agent_event"]
    event: Mapping[str, Any]


class ExitEvent(TypedDict):
    """Final frame before the process exits."""

    type: Literal["exit"]
    code: int


class ErrorEvent(TypedDict, total=False):
    """A recoverable (`fatal` absent/False) or fatal (`fatal` True) error."""

    type: Literal["error"]
    message: str
    fatal: bool


# A decoded outbound frame. Kept as a plain Mapping at the consumer boundary so
# unknown/future `kind`s on `agent_event` (EPIC I) survive losslessly.
OutboundFrame = Mapping[str, Any]


class PiProcessError(RuntimeError):
    """Raised when the sidecar fails to come up (timeout, fatal init error, or
    an early exit before `ready`)."""


# ---------------------------------------------------------------------------
# Provider-credential env scrub (mirror of runner.ts scrubProviderCredentialEnv)
# ---------------------------------------------------------------------------

# Explicit deny list — mirrors PROVIDER_CREDENTIAL_ENV_VARS in runner.ts. The
# sidecar self-scrubs these at startup; we strip them here too so the spawner
# never even hands them to the child (defense in depth for the §5.1 network
# invariant).
_PROVIDER_CREDENTIAL_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        # anthropic (OAuth token takes precedence over the API key in Pi)
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        # github copilot
        "COPILOT_GITHUB_TOKEN",
        # direct provider API keys
        "ANT_LING_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "NVIDIA_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_CLOUD_API_KEY",
        "GROQ_API_KEY",
        "CEREBRAS_API_KEY",
        "XAI_API_KEY",
        "OPENROUTER_API_KEY",
        "AI_GATEWAY_API_KEY",
        "ZAI_API_KEY",
        "ZAI_CODING_CN_API_KEY",
        "MISTRAL_API_KEY",
        "MINIMAX_API_KEY",
        "MINIMAX_CN_API_KEY",
        "MOONSHOT_API_KEY",
        "HF_TOKEN",
        "FIREWORKS_API_KEY",
        "TOGETHER_API_KEY",
        "OPENCODE_API_KEY",
        "KIMI_API_KEY",
        "CLOUDFLARE_API_KEY",
        "XIAOMI_API_KEY",
        "XIAOMI_TOKEN_PLAN_CN_API_KEY",
        "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
        "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
        # Google Vertex AI — Application Default Credentials discovery
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        # Amazon Bedrock — every credential source Pi recognizes
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    }
)

# Catch-all patterns — mirror PROVIDER_CREDENTIAL_ENV_PATTERNS in runner.ts.
# Anything looking like a provider credential is scrubbed; the child needs no
# ambient secret (the gateway token is injected via init config).
_PROVIDER_CREDENTIAL_ENV_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"_(API_)?KEY$"),
    re.compile(r"_API_TOKEN$"),
    re.compile(r"_OAUTH_TOKEN$"),
)


def scrub_provider_credential_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of `env` with every known/likely provider credential
    removed. Defense in depth: even though the sidecar scrubs its own ambient
    env, the spawner must never hand a real provider key to the child."""
    cleaned: dict[str, str] = {}
    for name, value in env.items():
        if name in _PROVIDER_CREDENTIAL_ENV_VARS:
            continue
        if any(pat.search(name) for pat in _PROVIDER_CREDENTIAL_ENV_PATTERNS):
            continue
        cleaned[name] = value
    return cleaned


# ---------------------------------------------------------------------------
# Entry resolution
# ---------------------------------------------------------------------------

# This file: packages/agent-server/src/disco/agent_server/build_kernel/pi_process.py
# Repo root is six parents up from build_kernel/ → src/ → agent-server/ → packages/.
_REPO_ROOT: Final = Path(__file__).resolve().parents[6]
_PI_KERNEL_DIR: Final = _REPO_ROOT / "packages" / "pi-kernel"
_DIST_ENTRY: Final = _PI_KERNEL_DIR / "dist" / "index.js"
_DEV_TS_ENTRY: Final = _PI_KERNEL_DIR / "src" / "index.ts"


def default_pi_kernel_entry() -> Path:
    """Resolve the sidecar entry, repo-relative to this file.

    Prefers the built `dist/index.js`. Falls back to the TypeScript source
    `src/index.ts` (Node >= 22.18 strips types natively, so `node src/index.ts`
    runs without a build) only when no dist build is present — a dev
    convenience. Build the package (`npm run build` in `packages/pi-kernel`) to
    get the production entry."""
    if _DIST_ENTRY.exists():
        return _DIST_ENTRY
    return _DEV_TS_ENTRY


# Sentinel pushed onto the event queue once stdout reaches EOF (the stream is
# over). Distinct object identity so it can never collide with a real frame.
_STREAM_END: Final = object()

# Bytes discarded per drain step when resyncing past an over-cap stdout line.
_RESYNC_CHUNK: Final = 1024 * 1024


class PiProcess:
    """Spawns and drives one Node Pi-kernel sidecar over its stdio protocol.

    Lifecycle: construct → `await start(init)` (spawn + handshake) → consume
    `events()` while issuing `prompt`/`followup`/`cancel` → `await aclose()`
    (graceful EOF, then `killpg` the tree on timeout). Single-consumer: one
    drain of `events()` at a time (the `PiKernel` consumer loop).
    """

    def __init__(
        self,
        *,
        node_bin: str = "node",
        entry: str,
        env: Mapping[str, str],
        cwd: str,
    ) -> None:
        self._node_bin = node_bin
        self._entry = entry
        self._cwd = cwd
        # Scrub credentials at construction so they never reach the child even if
        # `start` is delayed or the caller inspects `self._child_env`.
        self._child_env: dict[str, str] = scrub_provider_credential_env(env)

        self._proc: asyncio.subprocess.Process | None = None
        self._pgid: int | None = None

        # Decoded outbound frames (incl. `ready`) flow here; `events()` drains it.
        self._frames: asyncio.Queue[OutboundFrame | object] = asyncio.Queue()
        # Resolved with the `ready` frame, or errored on fatal/early-exit. Created
        # in `start()` (where a running loop is guaranteed) so construction never
        # touches the event loop.
        self._ready: asyncio.Future[ReadyEvent] | None = None
        self._started = False
        self._exited = False
        self._stream_ended = False

        # Bounded diagnostics — a chatty/hostile child cannot grow memory.
        self._stderr_ring: deque[str] = deque(maxlen=256)
        self._malformed_ring: deque[str] = deque(maxlen=64)

        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------

    async def start(
        self,
        init: KernelInitConfig | None = None,
        *,
        timeout: float = 20.0,
    ) -> ReadyEvent:
        """Spawn the sidecar, send `init`, and await the `ready` frame.

        Raises `PiProcessError` if the sidecar emits a fatal error, exits before
        `ready`, or does not become ready within `timeout`. On any such failure
        the process tree is torn down before the exception propagates."""
        if self._started:
            raise PiProcessError("PiProcess.start called twice")
        self._started = True
        ready_fut = asyncio.get_running_loop().create_future()
        self._ready = ready_fut

        # Own session/group so the whole tree can be signalled on teardown.
        self._proc = await asyncio.create_subprocess_exec(
            self._node_bin,
            self._entry,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._child_env,
            start_new_session=True,
            # Generous line cap: outbound `agent_event` frames (EPIC I) can be
            # large; match the sidecar's 4 MiB inbound cap.
            limit=4 * 1024 * 1024,
        )
        try:
            self._pgid = os.getpgid(self._proc.pid)
        except ProcessLookupError:
            self._pgid = self._proc.pid

        self._stdout_task = asyncio.ensure_future(self._read_stdout())
        self._stderr_task = asyncio.ensure_future(self._read_stderr())

        # `init` must be the first line on the wire.
        await self._send({"type": "init", "config": dict(init or {})})

        try:
            ready = await asyncio.wait_for(asyncio.shield(ready_fut), timeout)
        except asyncio.TimeoutError as exc:
            await self.aclose()
            raise PiProcessError(
                f"pi-kernel did not become ready within {timeout:.1f}s"
                f"{self._stderr_suffix()}"
            ) from exc
        except PiProcessError:
            await self.aclose()
            raise

        proto = ready.get("protocolVersion")
        if proto != PROTOCOL_VERSION:
            await self.aclose()
            raise PiProcessError(
                f"pi-kernel protocol version mismatch: expected {PROTOCOL_VERSION}, got {proto!r}"
            )
        return ready

    async def events(self) -> AsyncIterator[OutboundFrame]:
        """Yield every decoded outbound frame (including `ready` and the final
        `exit`) until the sidecar's stdout closes. Single-consumer."""
        while True:
            item = await self._frames.get()
            if item is _STREAM_END:
                return
            # `item` is a real frame here (the sentinel is the only bare object).
            yield cast(OutboundFrame, item)

    # -- data-plane commands --------------------------------------------------

    async def prompt(self, text: str) -> None:
        """Begin a new agent turn with `text`."""
        await self._send({"type": "prompt", "text": text})

    async def followup(self, text: str) -> None:
        """Queue a follow-up turn, delivered after the current one finishes."""
        await self._send({"type": "followup", "text": text})

    # -- control-plane command ------------------------------------------------

    async def cancel(self) -> None:
        """Abort any in-flight turn WITHOUT killing the process. The sidecar
        stays alive and remains drivable (matches the runner's cancel)."""
        await self._send({"type": "cancel"})

    # -- teardown -------------------------------------------------------------

    async def aclose(
        self,
        *,
        graceful_timeout: float = 5.0,
        kill_timeout: float = 2.0,
    ) -> int | None:
        """Tear the sidecar down. EOF stdin → await graceful exit; on timeout
        `SIGTERM` the process group, then `SIGKILL` it — so no child (e.g. a
        preview server) survives. Returns the process exit code (or `None`)."""
        proc = self._proc
        if proc is None:
            return None

        # EOF on stdin asks the sidecar for a clean shutdown.
        if proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except (OSError, RuntimeError):
                pass

        # Graceful: give the sidecar a chance to flush `exit` and leave on its own.
        # A clean leader exit is NOT enough on its own — a child/grandchild (e.g. a
        # preview server the agent spawned) can outlive the leader while staying in
        # the same process group (it is reparented to init but keeps the pgid). So
        # after the leader leaves we still probe the group and, if anything
        # survives, run the SAME SIGTERM→(grace)→SIGKILL teardown.
        if await self._wait_exit(graceful_timeout) and not self._group_alive():
            await self._reap()
            return proc.returncode

        # Forceful escalation 1: SIGTERM the WHOLE group (leader still wedged, or a
        # child/grandchild lingering after a clean leader exit).
        self._signal_group(signal.SIGTERM)
        if await self._wait_group_gone(kill_timeout):
            await self._reap()
            return proc.returncode

        # Forceful escalation 2: SIGKILL the WHOLE group — nothing survives.
        self._signal_group(signal.SIGKILL)
        await self._wait_group_gone(kill_timeout)
        await self._reap()
        return proc.returncode

    # -- diagnostics ----------------------------------------------------------

    def stderr_tail(self) -> list[str]:
        """A snapshot of the bounded stderr ring buffer (newest last)."""
        return list(self._stderr_ring)

    @property
    def returncode(self) -> int | None:
        """The child's exit code, or `None` while it is still running."""
        return self._proc.returncode if self._proc is not None else None

    @property
    def pgid(self) -> int | None:
        """The child's process-group id (== pid; it is its own group leader)."""
        return self._pgid

    # -- internals ------------------------------------------------------------

    async def _send(self, command: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise PiProcessError("pi-kernel is not running")
        if proc.stdin.is_closing() or self._exited:
            raise PiProcessError("pi-kernel stdin is closed")
        line = json.dumps(command, separators=(",", ":")) + "\n"
        async with self._write_lock:
            proc.stdin.write(line.encode("utf-8"))
            try:
                await proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:
                raise PiProcessError("pi-kernel stdin write failed") from exc

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stream = proc.stdout
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # An over-cap line with no in-limit newline: record + resync by
                # draining a bounded chunk, never crash.
                self._malformed_ring.append("<oversized stdout line discarded>")
                try:
                    await stream.read(_RESYNC_CHUNK)
                except (asyncio.LimitOverrunError, ValueError, OSError):
                    pass
                continue
            if not raw:
                break  # EOF — the sidecar's stdout is closed
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                frame = json.loads(text)
            except json.JSONDecodeError:
                # Unparseable line is a DIAGNOSTIC, never a crash.
                self._malformed_ring.append(text[:512])
                continue
            if not isinstance(frame, dict):
                self._malformed_ring.append(text[:512])
                continue
            self._dispatch_frame(frame)
        self._on_stream_end()

    def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        ready_fut = self._ready
        if kind == "ready" and ready_fut is not None and not ready_fut.done():
            ready_fut.set_result(cast(ReadyEvent, frame))
        elif (
            kind == "error"
            and frame.get("fatal") is True
            and ready_fut is not None
            and not ready_fut.done()
        ):
            # A fatal error during startup means the session never came up.
            ready_fut.set_exception(
                PiProcessError(
                    f"pi-kernel fatal error: {frame.get('message', '<no message>')}"
                )
            )
        elif kind == "exit":
            self._exited = True
        self._frames.put_nowait(frame)

    def _on_stream_end(self) -> None:
        if self._stream_ended:
            return
        self._stream_ended = True
        self._exited = True
        ready_fut = self._ready
        if ready_fut is not None and not ready_fut.done():
            ready_fut.set_exception(
                PiProcessError(
                    f"pi-kernel exited before becoming ready{self._stderr_suffix()}"
                )
            )
        self._frames.put_nowait(_STREAM_END)

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        stream = proc.stderr
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                self._stderr_ring.append("<oversized stderr line discarded>")
                continue
            if not raw:
                break
            self._stderr_ring.append(raw.decode("utf-8", "replace").rstrip("\n"))

    async def _wait_exit(self, timeout: float) -> bool:
        proc = self._proc
        if proc is None:
            return True
        if proc.returncode is not None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _group_alive(self) -> bool:
        """True iff the child's process group still has at least one member.

        After a clean leader exit a child/grandchild in the same group can linger
        (it is reparented to init but keeps the pgid), so `os.killpg(pgid, 0)`
        still succeeds until every member is gone."""
        pgid = self._pgid
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but unsignalable by us — treat as alive
        return True

    async def _wait_group_gone(self, timeout: float) -> bool:
        """Poll until the process group has no members, or `timeout` elapses.

        Unlike `_wait_exit` (which waits on the LEADER only) this watches the
        whole group, so a child/grandchild outliving a clean leader exit is not
        mistaken for a fully-torn-down tree."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._group_alive():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    def _signal_group(self, sig: signal.Signals) -> None:
        pgid = self._pgid
        if pgid is None:
            return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass  # group already gone
        except PermissionError:
            # Fallback: signal just the leader if the group is unsignalable.
            proc = self._proc
            if proc is not None and proc.returncode is None:
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    pass

    async def _reap(self) -> None:
        # Ensure the reader tasks settle so no frame/diagnostic is lost mid-flight.
        for task in (self._stdout_task, self._stderr_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), 1.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:  # noqa: BLE001 - reader already logged via rings
                pass
        # Make sure the stream-end sentinel was published even if stdout was
        # killed mid-line (so a pending `events()` consumer always terminates).
        self._on_stream_end()

    def _stderr_suffix(self) -> str:
        tail = list(self._stderr_ring)
        if not tail:
            return ""
        return " | stderr: " + " / ".join(tail[-5:])

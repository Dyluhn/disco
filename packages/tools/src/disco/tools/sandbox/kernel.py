from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import shlex
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty
from typing import Any

from .base import SandboxError
from .shell_sessions import SessionBusy

_LOG = logging.getLogger(__name__)

# Dev fallback for the kernel-gateway auth token when no app secret is set —
# a process-local random key (stable for this process's lifetime).
_DEV_GATEWAY_SECRET = _secrets.token_bytes(32)

_GATEWAY_DIAGNOSTIC_MAX_CHARS = 1200
_GATEWAY_SESSION = "__kernel"
_GATEWAY_SESSION_CLEANUP_S = 5.0
_GATEWAY_SECRET_RE = re.compile(
    r"(?i)\b(?:kg_auth_token|authorization|api[_-]?key|token|secret)\b"
    r"(?:\s*[:=]\s*|\s+)(?:bearer\s+|token\s+)?[^\s;]+"
)


def _bounded_gateway_diagnostic(
    output: object,
    *,
    exit_code: object = None,
    auth_token: str = "",
) -> str:
    """Return bounded startup evidence without retaining gateway credentials.

    Shell-session output can include the echoed ``KG_AUTH_TOKEN=...`` launch
    assignment, arbitrary terminal controls, or a very large traceback.  Kernel
    readiness failures are durable AgentError evidence, so sanitize and bound the
    text before it crosses that boundary.  Raw exception strings are intentionally
    excluded by callers; transport failures are represented by their class only.
    """

    text = str(output or "")
    if auth_token:
        text = text.replace(auth_token, "<redacted>")
    text = _GATEWAY_SECRET_RE.sub("<redacted>", text)
    text = "".join(
        char for char in text if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127)
    ).strip()
    if len(text) > _GATEWAY_DIAGNOSTIC_MAX_CHARS:
        text = "…" + text[-(_GATEWAY_DIAGNOSTIC_MAX_CHARS - 1) :]
    prefix = f"exit={exit_code}; " if exit_code is not None else ""
    return (prefix + text).strip()[: _GATEWAY_DIAGNOSTIC_MAX_CHARS + len(prefix)]


def _gateway_transport_state(exc: BaseException) -> str:
    """Class-only transport evidence: actionable category without raw URL/detail."""

    name = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
        name = "RequestError"
    return f"transport={name}"


def _gateway_auth_token(sandbox_id: str) -> str:
    """The kernel gateway's auth token for one sandbox. SECURITY: the gateway
    (`jupyter kernelgateway`) is an arbitrary-code-execution endpoint; without a
    token any caller that can reach its port can drive a kernel. The token is
    DERIVED, not stored, so it is STABLE and re-derivable: `_ensure_gateway`
    lazily REUSES a surviving gateway across suspend/resume / agent-server
    restart, and a fresh `GatewayKernel` must reconnect with the SAME token (a
    per-object random token would lock the object out of its own kernel).

    HMAC keyed on `DISCO_SECRET_KEY` so the value is secret from other sandboxes
    (interiors never receive the master key — SEC-1) and from the network; a
    process-local random key is the dev fallback when no app secret is set. The
    token is observable only from inside this sandbox (the gateway runs in its
    own container), which is the same trust boundary it protects."""
    from disco.core.env import disco_env

    master = disco_env("SECRET_KEY")
    key = master.encode() if master else _DEV_GATEWAY_SECRET
    return hmac.new(key, f"kernel-gateway:{sandbox_id}".encode(), hashlib.sha256).hexdigest()


# C15: idle-cull knob. Default 300s (5 min) — long enough that an agent thinking
# between tool calls doesn't trigger churn, short enough to free a forgotten
# kernel in a quiet conversation. Set to 0 to disable culling entirely.
_DEFAULT_IDLE_TIMEOUT_S = 300.0
_IDLE_TIMEOUT_ENV = "DISCO_KERNEL_IDLE_TIMEOUT_S"


def _default_idle_timeout_s() -> float:
    """Read `DISCO_KERNEL_IDLE_TIMEOUT_S`; missing/garbage -> 300.0; clamped to >=0.
    Read at call-time so tests can monkeypatch the env var per-case without
    process-level state."""
    raw = os.environ.get(_IDLE_TIMEOUT_ENV)
    if raw is None or raw.strip() == "":
        return _DEFAULT_IDLE_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError:
        _LOG.warning(
            "%s=%r is not a float; using default %.0fs",
            _IDLE_TIMEOUT_ENV,
            raw,
            _DEFAULT_IDLE_TIMEOUT_S,
        )
        return _DEFAULT_IDLE_TIMEOUT_S
    return max(0.0, v)


# ANSI escape sequence regex for stripping colors from tracebacks
_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_KERNEL_STREAM_HEAD = 16 * 1024
_KERNEL_STREAM_TAIL = 48 * 1024
_KERNEL_STREAM_CAP = _KERNEL_STREAM_HEAD + _KERNEL_STREAM_TAIL
_KERNEL_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_KERNEL_WS_MAX_FRAME_BYTES = 1024 * 1024


def _rewrite_process_workspace_literals(code: str, workspace: Path) -> str:
    """Translate Python string literals rooted at the guest ``/workspace`` path.

    Container kernels have a real ``/workspace`` mount.  The dev-only process
    kernel instead runs directly in its per-conversation host directory, so a
    literal path that is valid in every sibling tool otherwise raises
    ``FileNotFoundError``.  Rewrite only Python string constants whose complete
    prefix is exactly ``/workspace``; relative paths and strings containing the
    word elsewhere are untouched.

    The resolved target is jailed before it is inserted.  This does not turn the
    process backend into an isolation boundary (model code already executes as a
    host process), but the compatibility layer must never manufacture an escape
    path such as ``/workspace/../../etc`` itself.

    Cells using IPython-only syntax are left byte-for-byte unchanged when Python's
    AST parser cannot parse them.  Normal Python cells -- including f-strings --
    take the strict translated path.
    """
    if "/workspace" not in code:
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    root = workspace.resolve()

    def rewrite(value: str) -> str:
        if value == "/workspace":
            suffix = ""
        elif value.startswith("/workspace/"):
            suffix = value[len("/workspace/") :]
        else:
            return value
        target = (root / suffix).resolve()
        if target != root and root not in target.parents:
            raise SandboxError(f"process-kernel /workspace path escapes workspace: {value!r}")
        rendered = str(target)
        # In an f-string the literal segment often ends at `/workspace/` and
        # the next segment is a formatted value. Preserve that separator;
        # pathlib resolution intentionally removes it from the root path.
        if value.endswith("/") and not rendered.endswith("/"):
            rendered += "/"
        return rendered

    class _WorkspaceLiteralTransformer(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
            if isinstance(node.value, str):
                translated = rewrite(node.value)
                if translated != node.value:
                    return ast.copy_location(ast.Constant(value=translated), node)
            return node

    rewritten = _WorkspaceLiteralTransformer().visit(tree)
    ast.fix_missing_locations(rewritten)
    return ast.unparse(rewritten)


class _BoundedTextCapture:
    """List-compatible bounded accumulator for streamed kernel text."""

    def __init__(self) -> None:
        self.total = 0
        self._small = ""
        self._head = ""
        self._tail = ""

    def append(self, value: object) -> None:
        text = str(value or "")
        self.total += len(text)
        if len(self._small) <= _KERNEL_STREAM_CAP:
            room = _KERNEL_STREAM_CAP + 1 - len(self._small)
            self._small += text[:room]
        if len(self._head) < _KERNEL_STREAM_HEAD:
            self._head += text[: _KERNEL_STREAM_HEAD - len(self._head)]
        if len(text) >= _KERNEL_STREAM_TAIL:
            self._tail = text[-_KERNEL_STREAM_TAIL:]
        else:
            self._tail = (self._tail + text)[-_KERNEL_STREAM_TAIL:]

    def render(self) -> str:
        if self.total <= _KERNEL_STREAM_CAP:
            return self._small[: self.total]
        dropped = max(0, self.total - len(self._head) - len(self._tail))
        return (
            self._head + f"\n[disco: kernel output truncated; {self.total} chars total, "
            f"{dropped} omitted]\n" + self._tail
        )

    def __iter__(self):
        yield self.render()


def _cap_kernel_scalar(value: object) -> str | None:
    if value is None:
        return None
    capture = _BoundedTextCapture()
    capture.append(value)
    return capture.render()


def _cap_kernel_traceback(values: object) -> str:
    capture = _BoundedTextCapture()
    if not isinstance(values, (list, tuple)):
        capture.append(_ANSI_ESCAPE.sub("", str(values or "")))
        return capture.render()
    for index, value in enumerate(values):
        if index:
            capture.append("\n")
        capture.append(_ANSI_ESCAPE.sub("", str(value or "")))
    return capture.render()


@dataclass
class KernelResult:
    ok: bool
    stdout: str
    stderr: str
    result_repr: str | None = None
    error_traceback: str | None = None  # ANSI-stripped
    images: list[str] = field(default_factory=list)  # workspace-relative paths
    timed_out: bool = False
    restarted: bool = False

    def __str__(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        if self.result_repr:
            parts.append(f"→ {self.result_repr}")
        if self.error_traceback:
            parts.append(self.error_traceback)
        for img in self.images:
            parts.append(f"plot saved: {img}")
        return "\n".join(parts)


class KernelSession:
    """One persistent IPython kernel per conversation. State lives in kernel RAM."""

    async def start(self) -> None:
        """Start the kernel and run initialization (e.g. memory limits)."""
        raise NotImplementedError

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        """Execute code in the kernel. Handles interrupts and restarts on timeout."""
        raise NotImplementedError

    async def interrupt(self) -> None:
        """Send a SIGINT to the kernel."""
        raise NotImplementedError

    async def restart(self) -> None:
        """Hard restart of the kernel. RAM state is lost."""
        raise NotImplementedError

    async def shutdown(self) -> None:
        """Cleanly shutdown the kernel."""
        raise NotImplementedError


class ProcessKernel(KernelSession):
    """Transport for the 'process' backend: runs a local kernel via jupyter_client."""

    def __init__(self, workspace_path: str) -> None:
        self._workspace = Path(workspace_path).absolute()
        self._km: Any | None = None
        self._kc: Any | None = None
        self._ipc_dir: Path | None = None
        self._seq = 0

    async def start(self) -> None:
        from jupyter_client.manager import AsyncKernelManager

        from .base import clean_sandbox_env

        if os.name != "posix":
            raise SandboxError(
                "process kernel requires POSIX IPC transport; refusing plaintext TCP"
            )
        self._ipc_dir = Path(tempfile.mkdtemp(prefix="disco-kernel-ipc-"))
        self._ipc_dir.chmod(0o700)
        ipc_base = self._ipc_dir / "kernel"
        self._km = AsyncKernelManager(
            kernel_name="python3",
            transport="ipc",
            ip=str(ipc_base),
        )
        # Ensure the kernel runs in the workspace directory
        self._km.extra_arguments = ["--ProjectManager.root_dir=" + str(self._workspace)]

        # W3 C-4: launch with a SCRUBBED env (PATH/HOME/TMPDIR only). Without this,
        # jupyter_client defaults the child env to os.environ, so untrusted model
        # `code_exec` on this backend could read `os.environ['DISCO_SECRET_KEY']`
        # and the OpenRouter key. The connection info is passed via the connection
        # file (argv), not the env, so a minimal env is sufficient. Same allowlist
        # as the shell path (base.clean_sandbox_env) so they cannot drift.
        try:
            await self._km.start_kernel(
                cwd=str(self._workspace),
                env=clean_sandbox_env(self._workspace),
            )
            self._kc = self._km.client()
            assert self._kc is not None  # client() always returns a KernelClient
            self._kc.start_channels()
            await self._kc.wait_for_ready(timeout=60)

            # Setup memory limit: 4GiB as required by BP-08
            setup_cell = (
                "import resource; resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
            )
            await self.execute(setup_cell, timeout_s=10)
        except BaseException:
            with contextlib.suppress(Exception):
                await self.shutdown()
            raise

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        if not self._kc:
            await self.start()
        assert self._kc is not None  # start() always populates both _km and _kc
        kc = self._kc

        try:
            code = _rewrite_process_workspace_literals(code, self._workspace)
        except SandboxError as exc:
            return KernelResult(
                ok=False,
                stdout="",
                stderr="",
                error_traceback=f"SandboxError: {exc}",
            )

        msg_id = kc.execute(code)

        stdout = _BoundedTextCapture()
        stderr = _BoundedTextCapture()
        result_repr = None
        error_traceback = None
        images = []

        try:
            while True:
                try:
                    # We use a smaller interval to check for timeout more frequently
                    msg = await kc.get_iopub_msg(timeout=timeout_s)
                except (TimeoutError, Empty):
                    # Timeout protocol (EXACT): interrupt() -> wait <=5s -> restart() if hangs
                    _LOG.warning("Kernel execution timed out, interrupting...")
                    await self.interrupt()

                    # Wait up to 5s for the kernel to return to idle
                    idle = False
                    start_wait = time.time()
                    while time.time() - start_wait < 5.0:
                        try:
                            msg = await kc.get_iopub_msg(timeout=0.1)
                        except TimeoutError:
                            continue

                        if msg.get("parent_header", {}).get("msg_id") != msg_id:
                            continue
                        if (
                            msg.get("header", {}).get("msg_type") == "status"
                            and msg.get("content", {}).get("execution_state") == "idle"
                        ):
                            idle = True
                            break

                    if not idle:
                        _LOG.warning("Kernel failed to idle after interrupt, restarting...")
                        await self.restart()
                        return KernelResult(
                            ok=False,
                            stdout="".join(stdout),
                            stderr="".join(stderr),
                            error_traceback="Kernel timed out and was restarted",
                            timed_out=True,
                            restarted=True,
                        )

                    return KernelResult(
                        ok=False,
                        stdout="".join(stdout),
                        stderr="".join(stderr),
                        error_traceback="KeyboardInterrupt: execution timed out",
                        timed_out=True,
                    )

                content = msg.get("content", {})
                msg_type = msg.get("header", {}).get("msg_type")

                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue

                if msg_type == "stream":
                    if content.get("name") == "stdout":
                        stdout.append(content.get("text", ""))
                    elif content.get("name") == "stderr":
                        stderr.append(content.get("text", ""))
                elif msg_type == "execute_result":
                    result_repr = _cap_kernel_scalar(content.get("data", {}).get("text/plain"))
                elif msg_type == "display_data":
                    data = content.get("data", {})
                    if "image/png" in data:
                        self._seq += 1
                        img_path = f".pmx/plots/{self._seq:04d}.png"
                        full_path = self._workspace / img_path
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        encoded = data["image/png"]
                        if len(encoded) <= (_KERNEL_IMAGE_MAX_BYTES * 4 // 3) + 8:
                            with open(full_path, "wb") as f:
                                f.write(base64.b64decode(encoded))
                            images.append(img_path)
                elif msg_type == "error":
                    traceback = content.get("traceback", [])
                    error_traceback = _cap_kernel_traceback(traceback)
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    # Check if we've received the execute_reply
                    # We might need to skip stale replies from previous interrupted executions
                    try:
                        while True:
                            reply = await kc.get_shell_msg(timeout=1)
                            if reply.get("parent_header", {}).get("msg_id") == msg_id:
                                ok = reply.get("content", {}).get("status") == "ok"
                                res = KernelResult(
                                    ok=ok,
                                    stdout="".join(stdout),
                                    stderr="".join(stderr),
                                    result_repr=result_repr,
                                    error_traceback=error_traceback,
                                    images=images,
                                )
                                return await self._apply_discipline(res)
                            else:
                                stale = reply.get("parent_header", {}).get("msg_id")
                                _LOG.debug(f"Skipping stale shell message for {stale}")
                                continue
                    except Exception:
                        # If no reply yet, we might still be waiting for it or it might not come
                        # For now, we return what we have if we got an idle status
                        res = KernelResult(
                            ok=True if not error_traceback else False,
                            stdout="".join(stdout),
                            stderr="".join(stderr),
                            result_repr=result_repr,
                            error_traceback=error_traceback,
                            images=images,
                        )
                        return await self._apply_discipline(res)

        except Exception as e:
            _LOG.exception("Kernel execution failed")
            return KernelResult(
                ok=False, stdout="".join(stdout), stderr="".join(stderr), error_traceback=str(e)
            )

    async def _apply_discipline(self, res: KernelResult) -> KernelResult:
        """B2 kernel output discipline: truncate stdout/repr if > 2000 chars."""
        # Check stdout and result_repr
        for field_name in ("stdout", "result_repr"):
            val = getattr(res, field_name)
            if val and len(val) > 2000:
                self._seq += 1
                ts = int(time.time())
                filename = f"{ts}-{self._seq}-out.txt"
                rel_path = f".outputs/{filename}"
                full_path = self._workspace / rel_path
                full_path.parent.mkdir(parents=True, exist_ok=True)

                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(val)

                head = val[:500]
                stored_kind = (
                    "bounded output projection"
                    if "[disco: kernel output truncated;" in val
                    else "full output"
                )
                truncated = (
                    f"{head}\n[{stored_kind}: /workspace/{rel_path}, {len(val)} chars stored]"
                )
                setattr(res, field_name, truncated)
        return res

    async def interrupt(self) -> None:
        if self._km:
            await self._km.interrupt_kernel()

    async def restart(self) -> None:
        if self._km:
            old_client = self._kc
            if old_client is not None:
                old_client.stop_channels()
            await self._km.restart_kernel()
            self._kc = self._km.client()
            assert self._kc is not None  # client() always returns a KernelClient
            self._kc.start_channels()
            await self._kc.wait_for_ready(timeout=60)
            # Re-setup memory limit
            setup_cell = (
                "import resource; resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
            )
            await self.execute(setup_cell, timeout_s=10)

    async def shutdown(self) -> None:
        km, kc = self._km, self._kc
        ipc_dir = self._ipc_dir
        self._km = None
        self._kc = None
        self._ipc_dir = None
        try:
            try:
                if kc is not None:
                    # jupyter_client does not close DEALER/SUB channel sockets when the
                    # Python reference is discarded.  The client owns those channels,
                    # so close them explicitly before asking the manager to terminate
                    # the kernel process.
                    kc.stop_channels()
            finally:
                # A broken channel close must not strand the independently-owned
                # kernel process. Preserve the channel exception after this attempt.
                if km is not None:
                    await km.shutdown_kernel()
        finally:
            if ipc_dir is not None:
                shutil.rmtree(ipc_dir, ignore_errors=True)


class GatewayKernel(KernelSession):
    """Transport for container backends: reaches jupyter_kernel_gateway via WebSockets."""

    def __init__(self, sandbox: Any, sessions: Any) -> None:
        self._sandbox = sandbox
        self._sessions = sessions
        self._kernel_id: str | None = None
        self._ws: Any | None = None
        self._seq = 0
        self._url: str | None = None
        self._session_id = str(uuid.uuid4())
        # Per-sandbox gateway auth token (stable + re-derivable — see
        # _gateway_auth_token). Sent on every gateway HTTP request and on the WS
        # connect, and handed to the gateway at launch via --auth_token.
        self._token = _gateway_auth_token(str(getattr(sandbox, "id", "default")))
        self._auth_headers = {"Authorization": f"token {self._token}"}

    def _ws_with_token(self, ws_url: str) -> str:
        """Append the gateway token as a query param (version-agnostic across
        websockets releases vs threading per-connect header kwargs)."""
        sep = "&" if "?" in ws_url else "?"
        return f"{ws_url}{sep}token={self._token}"

    async def _normalize_gateway_session(self) -> None:
        """Boundedly return the internal gateway tmux session to an idle state.

        A startup process can exit after the shell manager's observation window,
        or remain alive after the readiness deadline.  In both cases a later
        ``code_exec`` must not inherit a stale ``SessionBusy``.  The cleanup
        result and exception text are deliberately ignored: either can contain
        the echoed token-bearing launch line.  Class-only logging keeps the
        original startup failure authoritative and token-free.
        """
        kill_foreground = getattr(self._sessions, "kill_foreground", None)
        if not callable(kill_foreground):
            _LOG.warning("kernel gateway session cleanup unavailable")
            return
        try:
            await asyncio.wait_for(
                self._sessions.kill_foreground(_GATEWAY_SESSION),
                timeout=_GATEWAY_SESSION_CLEANUP_S,
            )
        except TimeoutError:
            _LOG.warning("kernel gateway session cleanup timed out")
        except Exception as exc:  # noqa: BLE001 — preserve the original startup failure
            _LOG.warning(
                "kernel gateway session cleanup failed (%s)",
                _gateway_transport_state(exc),
            )

    async def _launch_gateway(self, command: str) -> Any:
        """Launch once, recovering a stale internal-session busy state once."""
        try:
            return await self._sessions.exec(_GATEWAY_SESSION, command, None)
        except SessionBusy:
            # A prior failed startup may have left the reserved session occupied.
            # Normalize and retry exactly once; never route code execution through
            # an ordinary shell session or expose the token-bearing busy detail.
            await self._normalize_gateway_session()
            try:
                return await self._sessions.exec(_GATEWAY_SESSION, command, None)
            except SessionBusy:
                raise SandboxError(
                    "jupyter kernel gateway internal session remained busy after bounded "
                    "cleanup; code_exec is unavailable for this sandbox session"
                ) from None

    async def _ensure_gateway(self) -> str:
        """Start the gateway lazily in tmux and wait for ready."""
        # Check if already running (port 8899)
        from ._container import INTERNAL_PORTS

        port = next(iter(INTERNAL_PORTS))  # 8899

        # Resolve host mapping
        mapping = None
        if hasattr(self._sandbox, "internal_port_mapping"):
            mapping = self._sandbox.internal_port_mapping(port)

        if not mapping:
            # Not a container or not published? For process backend it shouldn't reach here.
            # But just in case, if it's local, we might want localhost:8899
            mapping = ("localhost", port)

        self._url = f"http://{mapping[0]}:{mapping[1]}"

        # Try to reach it
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(f"{self._url}/api", timeout=1.0, headers=self._auth_headers)
                if res.status_code == 200:
                    return self._url
        except Exception:
            pass

        # Not running -> start in tmux session '__kernel'. exec_dir=None means the
        # container tmux default dir (WORKDIR /workspace), so kernels spawned by the
        # gateway inherit the workspace as cwd — user code's relative paths resolve
        # against the same tree the file API serves.
        #
        # The gateway requires every caller (REST + WS) to present the token;
        # without it the gateway is an unauthenticated RCE endpoint on whatever
        # interface the port is published to. W3 C-5: pass the token via the
        # KG_AUTH_TOKEN *environment variable* (Kernel Gateway reads it natively)
        # as an inline assignment, NOT as `--auth_token=<hex>` in argv — argv is
        # world-readable via `ps`/`/proc/<pid>/cmdline` to any process sharing the
        # sandbox's PID namespace, so an argv token is a self-exfiltrating secret.
        # The token is hex, but quote defensively regardless.
        cmd = (
            f"KG_AUTH_TOKEN={shlex.quote(self._token)} "
            f"jupyter kernelgateway --KernelGatewayApp.api=kernel_gateway.jupyter_websocket "
            f"--ip 0.0.0.0 --port {port}"
        )
        budget_s = int(
            os.environ.get("DISCO_KERNEL_GATEWAY_START_S")
            or os.environ.get("PMX_KERNEL_GATEWAY_START_S")
            or "120"
        )
        budget_s = max(1, budget_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        started = await self._launch_gateway(cmd)
        started_running = getattr(started, "running", None)
        started_exit = getattr(started, "exit_code", None)
        started_diagnostic = _bounded_gateway_diagnostic(
            getattr(started, "output", ""),
            exit_code=started_exit,
            auth_token=self._token,
        )
        if started_running is False:
            diagnostic = started_diagnostic or "gateway process exited without startup output"
            await self._normalize_gateway_session()
            raise SandboxError(
                "jupyter kernel gateway exited during startup; "
                f"diagnostic={diagnostic}. code_exec is unavailable for this sandbox session; "
                "continuing with shell is a degraded fallback and does not prove kernel state"
            )

        # Poll /api until ready. The gateway start is CPU-bound; on a saturated
        # box (local-LLM inference + the build agent competing for cores) it can
        # take well over 30s, so an autonomous build would forfeit on a
        # slow-but-fine start. The env-tunable budget is a REAL wall-clock bound,
        # inclusive of the tmux launch wait: poll count × request timeout × sleep
        # must never silently stretch a claimed 120 seconds toward four minutes.
        # (DISCO_KERNEL_GATEWAY_START_S, default 120s; PMX_ legacy honored).
        last_readiness = "not_reachable"
        async with httpx.AsyncClient() as client:
            while (remaining := deadline - loop.time()) > 0:
                try:
                    res = await client.get(
                        f"{self._url}/api",
                        timeout=max(0.05, min(1.0, remaining)),
                        headers=self._auth_headers,
                    )
                    if res.status_code == 200:
                        return self._url
                    last_readiness = f"http_status={res.status_code}"
                except Exception as exc:  # noqa: BLE001 — class-only readiness evidence below
                    last_readiness = _gateway_transport_state(exc)
                remaining = deadline - loop.time()
                if remaining > 0:
                    await asyncio.sleep(min(1.0, remaining))

        # The process may have exited after ShellSessionManager's initial 15-second
        # observation. Capture one final, separately bounded pane view without
        # letting diagnostics materially extend the advertised startup budget.
        final_diagnostic = started_diagnostic
        try:
            view = await asyncio.wait_for(self._sessions.view(_GATEWAY_SESSION), timeout=1.0)
            viewed = _bounded_gateway_diagnostic(
                getattr(view, "output", ""), auth_token=self._token
            )
            if viewed:
                final_diagnostic = viewed
        except Exception:  # noqa: BLE001 — keep the launch-time bounded fallback
            pass
        diagnostic_suffix = f"; diagnostic={final_diagnostic}" if final_diagnostic else ""

        await self._normalize_gateway_session()
        raise SandboxError(
            f"jupyter kernel gateway failed to start within {budget_s}s; "
            f"final_readiness={last_readiness}{diagnostic_suffix}. "
            "code_exec is unavailable for this sandbox session; continuing with shell is a "
            "degraded fallback and does not prove kernel state"
        )

    async def start(self) -> None:
        url = await self._ensure_gateway()
        import httpx

        async with httpx.AsyncClient() as client:
            res = await client.post(f"{url}/api/kernels", headers=self._auth_headers)
            res.raise_for_status()
            self._kernel_id = res.json()["id"]

        # Connect WS
        import websockets

        ws_url = url.replace("http://", "ws://") + f"/api/kernels/{self._kernel_id}/channels"
        self._ws = await websockets.connect(
            self._ws_with_token(ws_url),
            max_size=_KERNEL_WS_MAX_FRAME_BYTES,
            max_queue=16,
        )

        # Setup memory limit
        setup_cell = "import resource; resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
        await self.execute(setup_cell, timeout_s=10)

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        if not self._ws:
            await self.start()
        assert self._ws is not None  # start() always populates _ws
        ws = self._ws

        msg_id = str(uuid.uuid4())
        msg = {
            "header": {
                "msg_id": msg_id,
                "msg_type": "execute_request",
                "session": self._session_id,
                "version": "5.3",
                "date": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                "username": "agent",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
            "channel": "shell",
        }

        await ws.send(json.dumps(msg))

        stdout = _BoundedTextCapture()
        stderr = _BoundedTextCapture()
        result_repr = None
        error_traceback = None
        images = []
        # `ok` is only assigned when an `execute_reply` arrives; initialize so
        # the idle-status branch is well-defined even if we go straight from
        # busy to idle (no explicit reply).
        ok = False

        try:
            while True:
                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
                    msg = json.loads(raw_msg)
                except TimeoutError:
                    _LOG.warning("Kernel execution timed out, interrupting...")
                    await self.interrupt()

                    # Wait up to 5s for idle
                    try:
                        while True:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            msg = json.loads(raw_msg)
                            if msg.get("parent_header", {}).get("msg_id") == msg_id:
                                is_status = msg.get("header", {}).get("msg_type") == "status"
                                if is_status and msg["content"]["execution_state"] == "idle":
                                    return KernelResult(
                                        ok=False,
                                        stdout="".join(stdout),
                                        stderr="".join(stderr),
                                        error_traceback="KeyboardInterrupt: execution timed out",
                                        timed_out=True,
                                    )
                    except TimeoutError:
                        _LOG.warning("Interrupt timed out, restarting...")
                        await self.restart()
                        return KernelResult(
                            ok=False,
                            stdout="".join(stdout),
                            stderr="".join(stderr),
                            error_traceback="KeyboardInterrupt: execution timed out, state lost",
                            timed_out=True,
                            restarted=True,
                        )

                msg_type = msg.get("header", {}).get("msg_type")
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue

                content = msg.get("content", {})
                if msg_type == "stream":
                    if content.get("name") == "stdout":
                        stdout.append(content.get("text", ""))
                    elif content.get("name") == "stderr":
                        stderr.append(content.get("text", ""))
                elif msg_type == "execute_result":
                    result_repr = _cap_kernel_scalar(content.get("data", {}).get("text/plain"))
                elif msg_type == "display_data":
                    data = content.get("data", {})
                    if "image/png" in data:
                        self._seq += 1
                        img_path = f".pmx/plots/{self._seq:04d}.png"
                        # Since this is a container, we use the sandbox file API to write
                        encoded = data["image/png"]
                        if len(encoded) <= (_KERNEL_IMAGE_MAX_BYTES * 4 // 3) + 8:
                            png = base64.b64decode(encoded)
                            await self._sandbox.write_file(img_path, png)
                            images.append(img_path)
                elif msg_type == "error":
                    traceback = content.get("traceback", [])
                    error_traceback = _cap_kernel_traceback(traceback)
                elif msg_type == "execute_reply":
                    ok = content.get("status") == "ok"
                    # Wait for idle status after reply
                    continue
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    res = KernelResult(
                        ok=ok,
                        stdout="".join(stdout),
                        stderr="".join(stderr),
                        result_repr=result_repr,
                        error_traceback=error_traceback,
                        images=images,
                    )
                    return await self._apply_discipline(res)

        except Exception as e:
            _LOG.exception("Kernel execution failed")
            return KernelResult(
                ok=False, stdout="".join(stdout), stderr="".join(stderr), error_traceback=str(e)
            )

    async def _apply_discipline(self, res: KernelResult) -> KernelResult:
        """B2 kernel output discipline: truncate stdout/repr if > 2000 chars."""
        for field_name in ("stdout", "result_repr"):
            val = getattr(res, field_name)
            if val and len(val) > 2000:
                self._seq += 1
                ts = int(time.time())
                filename = f"{ts}-{self._seq}-out.txt"
                rel_path = f".outputs/{filename}"

                # Write full content via sandbox API
                await self._sandbox.write_file(rel_path, val.encode("utf-8"))

                head = val[:500]
                stored_kind = (
                    "bounded output projection"
                    if "[disco: kernel output truncated;" in val
                    else "full output"
                )
                truncated = (
                    f"{head}\n[{stored_kind}: /workspace/{rel_path}, {len(val)} chars stored]"
                )
                setattr(res, field_name, truncated)
        return res

    async def interrupt(self) -> None:
        if self._url and self._kernel_id:
            import httpx

            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self._url}/api/kernels/{self._kernel_id}/interrupt",
                    headers=self._auth_headers,
                )

    async def restart(self) -> None:
        if self._url and self._kernel_id:
            import httpx

            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self._url}/api/kernels/{self._kernel_id}/restart",
                    headers=self._auth_headers,
                )

            if self._ws:
                await self._ws.close()

            # Reconnect WS
            import websockets

            ws_base = self._url.replace("http://", "ws://")
            ws_url = f"{ws_base}/api/kernels/{self._kernel_id}/channels"
            self._ws = await websockets.connect(
                self._ws_with_token(ws_url),
                max_size=_KERNEL_WS_MAX_FRAME_BYTES,
                max_queue=16,
            )

            # Re-setup memory limit
            setup_cell = (
                "import resource; resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
            )
            await self.execute(setup_cell, timeout_s=10)

    async def shutdown(self) -> None:
        if self._url and self._kernel_id:
            import httpx

            async with httpx.AsyncClient() as client:
                await client.delete(
                    f"{self._url}/api/kernels/{self._kernel_id}", headers=self._auth_headers
                )
            if self._ws:
                await self._ws.close()
            self._kernel_id = None
            self._ws = None


class ManagedKernel(KernelSession):
    """C15: a culling wrapper around any `KernelSession` transport.

    The persistent CodeAct kernel is a long-lived subprocess (or container
    kernel) that holds RAM, file handles, and a tmux session forever — even
    when the agent goes quiet. `ManagedKernel` shuts the inner kernel down
    after `idle_timeout_s` of inactivity (no `execute()` calls) and re-spawns
    a fresh one on the next `execute()`. Active execs are never culled: the
    cull check is one comparison at the START of `execute()` only, so the
    hot path adds one monotonic-time read and one float subtract.

    Design notes:
      * `time_source` is injectable — tests pass a fake clock instead of
        sleeping.
      * `kernel_factory` is injectable — tests pass a fake `KernelSession`
        to avoid spawning a real jupyter kernel.
      * The threshold is read from `DISCO_KERNEL_IDLE_TIMEOUT_S` (env) by the
        helper `_default_idle_timeout_s()`; 0 disables culling.
      * State the agent still needs WITHIN the window is preserved (the
        cull only fires when the gap since the LAST exec exceeds N).
      * `execute()` updates `last_exec_end_at` in `finally:` so a failed
        exec still counts as "recently used" — no spurious cull on the
        next attempt.
      * `shutdown()` is best-effort: a failing inner shutdown is logged
        and we still spawn a fresh kernel on the next exec.
    """

    def __init__(
        self,
        kernel_factory: Callable[[], KernelSession],
        *,
        idle_timeout_s: float,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = kernel_factory
        self._idle_timeout_s = float(idle_timeout_s)
        self._time = time_source
        self._inner: KernelSession | None = None
        # Wall-time of the END of the last `execute()`. `None` means the
        # inner kernel has never been used yet (or has been explicitly shut
        # down). The cull check is `now - last_exec_end_at > threshold`.
        self._last_exec_end_at: float | None = None
        # Diagnostic counters — useful for tests and for ops dashboards.
        self.cull_count: int = 0
        self.spawn_count: int = 0
        self.exec_count: int = 0

    @property
    def inner(self) -> KernelSession | None:
        """The currently-spawned inner kernel, or None. Exposed for tests +
        observability. Do not use as a long-lived reference — it can be
        replaced after an idle cull."""
        return self._inner

    async def _ensure_inner(self) -> KernelSession:
        """Return a fresh, ready inner kernel. Spawns one if none exists OR
        if the previous one has been idle past the threshold. Captures
        `last_exec_end_at` AFTER `start()` returns, so a slow spawn
        (e.g. jupyter's `wait_for_ready`) doesn't burn the kernel's first
        useful life into the cull timer."""
        if self._inner is None:
            self._inner = self._factory()
            await self._inner.start()
            self.spawn_count += 1
            _LOG.debug("kernel: spawned inner kernel (total spawns=%d)", self.spawn_count)
            # Capture time AFTER start completes — a slow start must not let
            # the cull timer eat into the kernel's first useful life.
            self._last_exec_end_at = self._time()
            return self._inner

        if self._idle_timeout_s > 0 and self._last_exec_end_at is not None:
            idle_for = self._time() - self._last_exec_end_at
            if idle_for > self._idle_timeout_s:
                # Idle past threshold — cull the stale inner and spawn a
                # fresh one. Never raise from here: a broken inner shouldn't
                # block the agent from getting a working kernel.
                try:
                    await self._inner.shutdown()
                except Exception:  # noqa: BLE001 — best-effort; old kernel is dying anyway
                    _LOG.warning(
                        "kernel: idle cull: shutdown of stale inner failed",
                        exc_info=True,
                    )
                self.cull_count += 1
                _LOG.info(
                    "kernel: idle cull (idle_for=%.1fs > threshold=%.1fs); spawning fresh",
                    idle_for,
                    self._idle_timeout_s,
                )
                self._inner = self._factory()
                await self._inner.start()
                self.spawn_count += 1
                self._last_exec_end_at = self._time()
        return self._inner

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        inner = await self._ensure_inner()
        try:
            return await inner.execute(code, timeout_s=timeout_s)
        finally:
            # Update AFTER the exec returns (success or failure) — the
            # inner is now "recently used" from the cull's perspective.
            self._last_exec_end_at = self._time()
            self.exec_count += 1

    async def interrupt(self) -> None:
        if self._inner is not None:
            await self._inner.interrupt()

    async def restart(self) -> None:
        if self._inner is not None:
            await self._inner.restart()
            # A restart is semantically "the kernel is back and ready" —
            # treat the freshly-restarted inner as recently-used.
            self._last_exec_end_at = self._time()

    async def shutdown(self) -> None:
        if self._inner is not None:
            try:
                await self._inner.shutdown()
            finally:
                self._inner = None
                self._last_exec_end_at = None

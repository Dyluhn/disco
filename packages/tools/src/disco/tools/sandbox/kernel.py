from __future__ import annotations

# MODULE SURFACE, PRESERVED (Epic 10-B) ------------------------------------------
#
# `kernel_parts` resolves this module's names through the parent at call time (the
# standing §13 rule, which keeps test monkeypatches effective), so ruff cannot see
# those uses. Every name this module bound before the extraction is restored here
# in the redundant-alias re-export form (`X as X`) — ruff's sanctioned re-export
# marker, which needs no per-line suppression. `_secrets` is re-exported from
# `kernel_parts.text` rather than re-imported so both modules keep the identical
# module object (`_DEV_GATEWAY_SECRET` is derived from it exactly once).
import ast as ast
import asyncio
import base64 as base64
import contextlib
import hashlib as hashlib
import hmac as hmac
import json as json
import logging
import os
import re as re
import shlex as shlex
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty as Empty
from typing import Any

from .base import SandboxError
from .kernel_parts import gateway_execute as _gateway_execute_part
from .kernel_parts import gateway_startup as _gateway_startup_part
from .kernel_parts import process_execute as _process_execute_part
from .kernel_parts import process_recovery as _process_recovery_part
from .kernel_parts.text import _ANSI_ESCAPE as _ANSI_ESCAPE
from .kernel_parts.text import _DEFAULT_IDLE_TIMEOUT_S as _DEFAULT_IDLE_TIMEOUT_S
from .kernel_parts.text import _DEV_GATEWAY_SECRET as _DEV_GATEWAY_SECRET
from .kernel_parts.text import _GATEWAY_DIAGNOSTIC_MAX_CHARS as _GATEWAY_DIAGNOSTIC_MAX_CHARS
from .kernel_parts.text import _GATEWAY_SECRET_RE as _GATEWAY_SECRET_RE
from .kernel_parts.text import _IDLE_TIMEOUT_ENV as _IDLE_TIMEOUT_ENV
from .kernel_parts.text import _KERNEL_IMAGE_MAX_BYTES as _KERNEL_IMAGE_MAX_BYTES
from .kernel_parts.text import _KERNEL_STREAM_CAP as _KERNEL_STREAM_CAP
from .kernel_parts.text import _KERNEL_STREAM_HEAD as _KERNEL_STREAM_HEAD
from .kernel_parts.text import _KERNEL_STREAM_TAIL as _KERNEL_STREAM_TAIL
from .kernel_parts.text import _bounded_gateway_diagnostic as _bounded_gateway_diagnostic
from .kernel_parts.text import (
    _BoundedTextCapture,
    _gateway_auth_token,
    _gateway_transport_state,
)
from .kernel_parts.text import _cap_kernel_scalar as _cap_kernel_scalar
from .kernel_parts.text import _cap_kernel_traceback as _cap_kernel_traceback
from .kernel_parts.text import _default_idle_timeout_s as _default_idle_timeout_s
from .kernel_parts.text import (
    _rewrite_process_workspace_literals as _rewrite_process_workspace_literals,
)
from .kernel_parts.text import _secrets as _secrets
from .shell_sessions import SessionBusy

_LOG = logging.getLogger(__name__)

_GATEWAY_SESSION = "__kernel"
_GATEWAY_SESSION_CLEANUP_S = 5.0

_KERNEL_WS_MAX_FRAME_BYTES = 1024 * 1024
_KERNEL_INTERRUPT_GRACE_S = 5.0
_KERNEL_INTERRUPT_CALL_TIMEOUT_S = 1.0
_KERNEL_RESTART_CALL_TIMEOUT_S = 65.0
_KERNEL_SHELL_REPLY_GRACE_S = 5.0
_KERNEL_SHELL_REPLY_MAX_POLLS = 256


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
    interrupt_attempted: bool = False
    interrupt_failed: bool = False
    restart_attempted: bool = False
    restart_failed: bool = False
    protocol_failed: bool = False

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
        self._restart_failed_closed = False
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
            self._restart_failed_closed = False

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
        """Read IOPub messages until the matching execute_reply + idle pair
        proves completion; see `kernel_parts.process_execute` for the full
        contract."""
        return await _process_execute_part.execute(self, code, timeout_s=timeout_s)

    async def _wait_for_execute_reply(
        self,
        kc: Any,
        msg_id: str,
        *,
        deadline: float,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Return a typed shell reply or a sanitized protocol-failure reason;
        see `kernel_parts.process_recovery` for the full contract."""
        return await _process_recovery_part.wait_for_execute_reply(
            self, kc, msg_id, deadline=deadline
        )

    async def _recover_from_protocol_failure(
        self,
        *,
        stdout: _BoundedTextCapture,
        stderr: _BoundedTextCapture,
        result_repr: str | None,
        error_traceback: str | None,
        images: list[str],
        detail: str,
    ) -> KernelResult:
        """Quarantine a desynchronized shell channel without fabricating success;
        see `kernel_parts.process_recovery` for the full contract."""
        return await _process_recovery_part.recover_from_protocol_failure(
            self,
            stdout=stdout,
            stderr=stderr,
            result_repr=result_repr,
            error_traceback=error_traceback,
            images=images,
            detail=detail,
        )

    async def _recover_from_timeout(
        self,
        kc: Any,
        msg_id: str,
        stdout: _BoundedTextCapture,
        stderr: _BoundedTextCapture,
    ) -> KernelResult:
        """Bound interrupt/settle/restart while preserving timeout truth; see
        `kernel_parts.process_recovery` for the full contract."""
        return await _process_recovery_part.recover_from_timeout(self, kc, msg_id, stdout, stderr)

    async def _wait_for_interrupt_settle(
        self,
        kc: Any,
        msg_id: str,
        *,
        deadline: float | None = None,
    ) -> bool:
        """Wait boundedly for the interrupted request's IOPub idle + shell reply;
        see `kernel_parts.process_recovery` for the full contract."""
        return await _process_recovery_part.wait_for_interrupt_settle(
            self, kc, msg_id, deadline=deadline
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
        if self._km is None:
            raise SandboxError("process kernel cannot restart before it has started")
        old_client = self._kc
        # Detach before any operation that can raise or hang. A failed restart is
        # fail-closed; execute() must never reuse the stopped/desynchronized client.
        self._kc = None
        if old_client is not None:
            old_client.stop_channels()
        await self._km.restart_kernel()
        self._kc = self._km.client()
        assert self._kc is not None  # client() always returns a KernelClient
        self._kc.start_channels()
        await self._kc.wait_for_ready(timeout=60)
        self._restart_failed_closed = False
        # Re-setup memory limit
        setup_cell = "import resource; resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
        setup = await self.execute(setup_cell, timeout_s=10)
        if not setup.ok:
            raise SandboxError("process kernel restart initialization failed")

    async def shutdown(self) -> None:
        km, kc = self._km, self._kc
        ipc_dir = self._ipc_dir
        self._km = None
        self._kc = None
        self._ipc_dir = None
        self._restart_failed_closed = False
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
        """Start the gateway lazily in tmux and wait for ready; see
        `kernel_parts.gateway_startup` for the full contract."""
        return await _gateway_startup_part.ensure_gateway(self)

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
        """Dispatch WS messages until execute_reply + idle both prove
        completion; see `kernel_parts.gateway_execute` for the full contract."""
        return await _gateway_execute_part.execute(self, code, timeout_s=timeout_s)

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

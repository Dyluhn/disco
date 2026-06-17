from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty
from typing import Any

from .base import SandboxError

_LOG = logging.getLogger(__name__)

# Dev fallback for the kernel-gateway auth token when no app secret is set —
# a process-local random key (stable for this process's lifetime).
_DEV_GATEWAY_SECRET = _secrets.token_bytes(32)


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
            _IDLE_TIMEOUT_ENV, raw, _DEFAULT_IDLE_TIMEOUT_S,
        )
        return _DEFAULT_IDLE_TIMEOUT_S
    return max(0.0, v)

# ANSI escape sequence regex for stripping colors from tracebacks
_ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

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
        self._seq = 0

    async def start(self) -> None:
        from jupyter_client.manager import AsyncKernelManager
        self._km = AsyncKernelManager(kernel_name="python3")
        # Ensure the kernel runs in the workspace directory
        self._km.extra_arguments = ["--ProjectManager.root_dir=" + str(self._workspace)]

        await self._km.start_kernel(cwd=str(self._workspace))
        self._kc = self._km.client()
        assert self._kc is not None  # client() always returns a KernelClient
        self._kc.start_channels()
        await self._kc.wait_for_ready(timeout=60)

        # Setup memory limit: 4GiB as required by BP-08
        setup_cell = (
            "import resource; "
            "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
        )
        await self.execute(setup_cell, timeout_s=10)

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        if not self._kc:
            await self.start()
        assert self._kc is not None  # start() always populates both _km and _kc
        kc = self._kc

        msg_id = kc.execute(code)
        
        stdout = []
        stderr = []
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
                        if msg.get("header", {}).get("msg_type") == "status" and \
                           msg.get("content", {}).get("execution_state") == "idle":
                            idle = True
                            break
                    
                    if not idle:
                        _LOG.warning("Kernel failed to idle after interrupt, restarting...")
                        await self.restart()
                        return KernelResult(
                            ok=False, stdout="".join(stdout), stderr="".join(stderr),
                            error_traceback="Kernel timed out and was restarted",
                            timed_out=True, restarted=True)
                    
                    return KernelResult(ok=False, stdout="".join(stdout), stderr="".join(stderr),
                                       error_traceback="KeyboardInterrupt: execution timed out",
                                       timed_out=True)

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
                    result_repr = content.get("data", {}).get("text/plain")
                elif msg_type == "display_data":
                    data = content.get("data", {})
                    if "image/png" in data:
                        self._seq += 1
                        img_path = f".pmx/plots/{self._seq:04d}.png"
                        full_path = self._workspace / img_path
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(full_path, "wb") as f:
                            f.write(base64.b64decode(data["image/png"]))
                        images.append(img_path)
                elif msg_type == "error":
                    traceback = content.get("traceback", [])
                    error_traceback = _ANSI_ESCAPE.sub('', "\n".join(traceback))
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
                                    images=images
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
                            images=images
                        )
                        return await self._apply_discipline(res)

        except Exception as e:
            _LOG.exception("Kernel execution failed")
            return KernelResult(ok=False, stdout="".join(stdout), stderr="".join(stderr),
                               error_traceback=str(e))

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
                truncated = f"{head}\n[full output: /workspace/{rel_path}, {len(val)} bytes]"
                setattr(res, field_name, truncated)
        return res

    async def interrupt(self) -> None:
        if self._km:
            await self._km.interrupt_kernel()

    async def restart(self) -> None:
        if self._km:
            await self._km.restart_kernel()
            self._kc = self._km.client()
            assert self._kc is not None  # client() always returns a KernelClient
            self._kc.start_channels()
            await self._kc.wait_for_ready(timeout=60)
            # Re-setup memory limit
            setup_cell = (
                "import resource; "
                "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
            )
            await self.execute(setup_cell, timeout_s=10)

    async def shutdown(self) -> None:
        if self._km:
            await self._km.shutdown_kernel()
            self._km = None
            self._kc = None

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

    async def _ensure_gateway(self) -> str:
        """Start the gateway lazily in tmux and wait for ready."""
        # Check if already running (port 8899)
        from ._container import INTERNAL_PORTS
        port = next(iter(INTERNAL_PORTS)) # 8899
        
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
        # --auth_token requires every caller (REST + WS) to present the token;
        # without it the gateway is an unauthenticated RCE endpoint on whatever
        # interface the port is published to. The token is hex (shell-safe).
        cmd = (
            f"jupyter kernelgateway --KernelGatewayApp.api=kernel_gateway.jupyter_websocket "
            f"--KernelGatewayApp.auth_token={self._token} "
            f"--ip 0.0.0.0 --port {port}"
        )
        await self._sessions.exec("__kernel", cmd, None)
        
        # Poll /api until ready. The gateway start is CPU-bound; on a saturated
        # box (local-LLM inference + the build agent competing for cores) it can
        # take well over 30s, so an autonomous build would forfeit on a
        # slow-but-fine start. Budget generously + env-tunable
        # (DISCO_KERNEL_GATEWAY_START_S, default 120s; PMX_ legacy honored).
        budget_s = int(
            os.environ.get("DISCO_KERNEL_GATEWAY_START_S")
            or os.environ.get("PMX_KERNEL_GATEWAY_START_S")
            or "120"
        )
        async with httpx.AsyncClient() as client:
            for _ in range(max(1, budget_s)):
                try:
                    res = await client.get(f"{self._url}/api", timeout=1.0, headers=self._auth_headers)
                    if res.status_code == 200:
                        return self._url
                    await asyncio.sleep(1.0)  # up but not 200 yet — wait, don't tight-loop
                except Exception:
                    await asyncio.sleep(1.0)  # not up yet (connection refused) — wait

        raise SandboxError(
            f"jupyter kernel gateway failed to start within {budget_s}s "
            f"(set DISCO_KERNEL_GATEWAY_START_S higher if the box is heavily loaded)"
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
        self._ws = await websockets.connect(self._ws_with_token(ws_url))
        
        # Setup memory limit
        setup_cell = (
            "import resource; "
            "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
        )
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

        stdout = []
        stderr = []
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
                                        stdout="".join(stdout), stderr="".join(stderr),
                                        error_traceback=
                                        "KeyboardInterrupt: execution timed out",
                                        timed_out=True)
                    except TimeoutError:
                        _LOG.warning("Interrupt timed out, restarting...")
                        await self.restart()
                        return KernelResult(
                            ok=False, stdout="".join(stdout), stderr="".join(stderr),
                            error_traceback=
                            "KeyboardInterrupt: execution timed out, state lost",
                            timed_out=True, restarted=True)

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
                    result_repr = content.get("data", {}).get("text/plain")
                elif msg_type == "display_data":
                    data = content.get("data", {})
                    if "image/png" in data:
                        self._seq += 1
                        img_path = f".pmx/plots/{self._seq:04d}.png"
                        # Since this is a container, we use the sandbox file API to write
                        png = base64.b64decode(data["image/png"])
                        await self._sandbox.write_file(img_path, png)
                        images.append(img_path)
                elif msg_type == "error":
                    traceback = content.get("traceback", [])
                    error_traceback = _ANSI_ESCAPE.sub('', "\n".join(traceback))
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
                        images=images
                    )
                    return await self._apply_discipline(res)

        except Exception as e:
            _LOG.exception("Kernel execution failed")
            return KernelResult(ok=False, stdout="".join(stdout), stderr="".join(stderr),
                               error_traceback=str(e))

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
                truncated = f"{head}\n[full output: /workspace/{rel_path}, {len(val)} bytes]"
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
            self._ws = await websockets.connect(self._ws_with_token(ws_url))
            
            # Re-setup memory limit
            setup_cell = (
                "import resource; "
                "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
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

        if self._last_exec_end_at is not None:
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
                    idle_for, self._idle_timeout_s,
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

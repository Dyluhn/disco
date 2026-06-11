from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty
from typing import Any

from .base import SandboxError

_LOG = logging.getLogger(__name__)

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
        from jupyter_client import AsyncKernelManager
        self._km = AsyncKernelManager(kernel_name="python3")
        # Ensure the kernel runs in the workspace directory
        self._km.extra_arguments = ["--ProjectManager.root_dir=" + str(self._workspace)]
        
        await self._km.start_kernel(cwd=str(self._workspace))
        self._kc = self._km.client()
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
        
        msg_id = self._kc.execute(code)
        
        stdout = []
        stderr = []
        result_repr = None
        error_traceback = None
        images = []
        
        try:
            while True:
                try:
                    # We use a smaller interval to check for timeout more frequently
                    msg = await self._kc.get_iopub_msg(timeout=timeout_s)
                except (TimeoutError, Empty):
                    # Timeout protocol (EXACT): interrupt() -> wait <=5s -> restart() if hangs
                    _LOG.warning("Kernel execution timed out, interrupting...")
                    await self.interrupt()
                    
                    # Wait up to 5s for the kernel to return to idle
                    idle = False
                    start_wait = time.time()
                    while time.time() - start_wait < 5.0:
                        try:
                            msg = await self._kc.get_iopub_msg(timeout=0.1)
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
                            reply = await self._kc.get_shell_msg(timeout=1)
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
                res = await client.get(f"{self._url}/api", timeout=1.0)
                if res.status_code == 200:
                    return self._url
        except Exception:
            pass

        # Not running -> start in tmux session '__kernel'. exec_dir=None means the
        # container tmux default dir (WORKDIR /workspace), so kernels spawned by the
        # gateway inherit the workspace as cwd — user code's relative paths resolve
        # against the same tree the file API serves.
        cmd = (
            f"jupyter kernelgateway --KernelGatewayApp.api=kernel_gateway.jupyter_websocket "
            f"--ip 0.0.0.0 --port {port}"
        )
        await self._sessions.exec("__kernel", cmd, None)
        
        # Poll /api until ready (up to 30s)
        async with httpx.AsyncClient() as client:
            for _ in range(30):
                try:
                    # Re-resolve mapping as it might have changed on restart? 
                    # Usually it's stable once published.
                    res = await client.get(f"{self._url}/api", timeout=1.0)
                    if res.status_code == 200:
                        return self._url
                except Exception:
                    await asyncio.sleep(1.0)
        
        raise SandboxError("jupyter kernel gateway failed to start")

    async def start(self) -> None:
        url = await self._ensure_gateway()
        import httpx
        async with httpx.AsyncClient() as client:
            res = await client.post(f"{url}/api/kernels")
            res.raise_for_status()
            self._kernel_id = res.json()["id"]
        
        # Connect WS
        import websockets
        ws_url = url.replace("http://", "ws://") + f"/api/kernels/{self._kernel_id}/channels"
        self._ws = await websockets.connect(ws_url)
        
        # Setup memory limit
        setup_cell = (
            "import resource; "
            "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30))"
        )
        await self.execute(setup_cell, timeout_s=10)

    async def execute(self, code: str, *, timeout_s: int) -> KernelResult:
        if not self._ws:
            await self.start()

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
        
        await self._ws.send(json.dumps(msg))
        
        stdout = []
        stderr = []
        result_repr = None
        error_traceback = None
        images = []
        
        try:
            while True:
                try:
                    raw_msg = await asyncio.wait_for(self._ws.recv(), timeout=timeout_s)
                    msg = json.loads(raw_msg)
                except TimeoutError:
                    _LOG.warning("Kernel execution timed out, interrupting...")
                    await self.interrupt()
                    
                    # Wait up to 5s for idle
                    try:
                        while True:
                            raw_msg = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
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
                        ok=ok if 'ok' in locals() else False,
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
                await client.post(f"{self._url}/api/kernels/{self._kernel_id}/interrupt")

    async def restart(self) -> None:
        if self._url and self._kernel_id:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(f"{self._url}/api/kernels/{self._kernel_id}/restart")
            
            if self._ws:
                await self._ws.close()
            
            # Reconnect WS
            import websockets
            ws_base = self._url.replace("http://", "ws://")
            ws_url = f"{ws_base}/api/kernels/{self._kernel_id}/channels"
            self._ws = await websockets.connect(ws_url)
            
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
                await client.delete(f"{self._url}/api/kernels/{self._kernel_id}")
            if self._ws:
                await self._ws.close()
            self._kernel_id = None
            self._ws = None

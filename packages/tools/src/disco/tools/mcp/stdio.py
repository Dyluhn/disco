"""MCP stdio transport — RP-05 rung A.

Spawns an MCP server subprocess via asyncio.create_subprocess_exec and wraps the
mcp SDK's stdio_client for JSON-RPC framing. Non-JSON stdout lines (the spec
says servers MAY emit logging on stdout) are tolerated by the SDK's own
stdout_reader — it drops frames that fail JSON-RPC validation rather than
crashing. We always capture stderr into a buffer surfaced through the per-server
status.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_LOG = logging.getLogger(__name__)

# Only these keys are passed through from the host environment to an MCP
# subprocess. Everything else — crucially DISCO_SECRET_KEY (the Fernet master
# key) and every *_API_KEY provider credential read from os.environ — is
# withheld, because the MCP server is third-party code (an `npx some-server`)
# and the subprocess boundary is NOT a trust boundary. A compromised server
# that inherited os.environ could decrypt ~/.config/disco/secrets.json and
# harvest every provider key. The server still gets PATH (to locate its
# interpreter/node), locale/tz, and a temp dir — plus the per-server secret
# refs the user explicitly attached via `srv.env` (overlaid below). Mirrors
# the sandbox process backend's `_clean_env` (sandbox/process.py).
_SAFE_PASSTHROUGH = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SystemRoot",  # Windows: many runtimes fail to start without it
)


def _clean_base_env() -> dict[str, str]:
    """A minimal host env for an MCP subprocess — no secrets ever leak in."""
    import os as _os

    return {k: _os.environ[k] for k in _SAFE_PASSTHROUGH if k in _os.environ}


class StdioConnectionClosed(Exception):
    """The stdio subprocess exited prematurely (before explicit close)."""


class McpStdioClient:
    """One MCP server over stdio — owns the subprocess lifecycle.

    Uses the mcp SDK's stdio_client context manager for JSON-RPC framing.
    NOTE: the subprocess runs on the HOST and is NOT a trust boundary — the
    server is third-party code. It receives only `_clean_base_env()` plus the
    per-server secret refs the user attached; the host's DISCO_SECRET_KEY and
    provider keys are deliberately withheld (see `_SAFE_PASSTHROUGH`).
    """

    def __init__(
        self,
        command: list[str],
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        init_timeout_s: float = 15.0,
    ) -> None:
        self._command = list(command)
        self._args = list(args or [])
        self._env = dict(env or {})
        self._init_timeout_s = init_timeout_s
        self._session: ClientSession | None = None
        self._stderr_lines: list[str] = []
        self._owner_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # ---- lifecycle -----------------------------------------------------------

    async def connect(self) -> ClientSession:
        """Spawn the subprocess, initialize the MCP session.

        Times out after `init_timeout_s` on the initialize() call (SDK #1452
        hang defense). Stderr is captured into a buffer.
        """
        if self._session is not None:
            return self._session
        if self._owner_task is not None and not self._owner_task.done():
            raise RuntimeError("MCP stdio connection is already starting")

        loop = asyncio.get_running_loop()
        ready: asyncio.Future[ClientSession] = loop.create_future()
        self._stop_event = asyncio.Event()
        self._stderr_lines.clear()
        self._owner_task = asyncio.create_task(
            self._run_connection(ready, self._stop_event),
            name=f"mcp-stdio:{self._command[0]}",
        )
        try:
            return await ready
        except BaseException:
            owner = self._owner_task
            if owner is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await owner
            self._owner_task = None
            self._stop_event = None
            raise

    async def _run_connection(
        self,
        ready: asyncio.Future[ClientSession],
        stop: asyncio.Event,
    ) -> None:
        """Own every SDK cancel scope and subprocess context in one task."""
        full_cmd = self._command + self._args
        # Start from a CLEAN base (PATH/locale/tmp only — never the host's
        # secrets) and overlay only the SecretsStore-resolved refs the user
        # explicitly attached to THIS server via `srv.env`. The subprocess is
        # third-party code, so os.environ must not flow into it (SEC-1).
        resolved_env = _clean_base_env()
        resolved_env.update(self._env)

        params = StdioServerParameters(
            command=full_cmd[0],
            args=full_cmd[1:] if len(full_cmd) > 1 else [],
            env=resolved_env,
        )

        # Capture stderr into a temp file — the SDK's stdio_client
        # accepts an errlog TextIO. We use a temp file (has fileno())
        # and read it back after init for the per-server status (D4).
        errlog = tempfile.TemporaryFile(mode="w+", suffix=".stderr")
        init_failure: TimeoutError | None = None

        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    try:
                        await asyncio.wait_for(session.initialize(), timeout=self._init_timeout_s)
                    except TimeoutError as err:
                        init_failure = TimeoutError(
                            f"MCP server {self._command[0]!r} did not respond to "
                            f"initialize() within {self._init_timeout_s}s"
                        )
                        init_failure.__cause__ = err
                    if init_failure is None:
                        errlog.flush()
                        errlog.seek(0)
                        captured = errlog.read()
                        if captured:
                            self._stderr_lines.append(captured)
                        self._session = session
                        if not ready.done():
                            ready.set_result(session)
                        await stop.wait()
            if init_failure is not None:
                raise init_failure
        except BaseException as exc:
            if not ready.done():
                # AnyIO may wrap a timeout raised inside its task groups. Keep
                # the documented timeout type after those groups have drained,
                # without flattening unrelated exception groups.
                ready.set_exception(init_failure or exc)
            elif not isinstance(exc, asyncio.CancelledError):
                _LOG.warning(
                    "McpStdioClient owner stopped for %r: %s",
                    self._command[0],
                    type(exc).__name__,
                )
        finally:
            self._session = None
            errlog.close()

    @property
    def session(self) -> ClientSession:
        """The live ClientSession — None before connect(), raises after close()."""
        if self._session is None:
            raise RuntimeError("McpStdioClient not connected")
        return self._session

    @property
    def stderr_output(self) -> str:
        """Aggregated stderr lines from the subprocess."""
        return "".join(self._stderr_lines)

    async def close(self) -> None:
        """Drain the session + transport, kill the subprocess.

        SIGTERM, then SIGKILL after a 2s grace (the brief is explicit).
        """
        owner = self._owner_task
        stop = self._stop_event
        if owner is None:
            self._session = None
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout=5.0)
        except TimeoutError:
            owner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await owner
        finally:
            self._session = None
            self._owner_task = None
            self._stop_event = None

    async def list_tools(self) -> list:
        """Fetch the server's tool list via the MCP session."""
        result = await self.session.list_tools()
        return list(result.tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on the MCP server and return the raw result dict.

        The result dict has shape {"content": [...], "isError": bool}
        matching the MCP tools/call response.
        """
        result = await self.session.call_tool(tool_name, arguments)
        return {
            "content": list(result.content),
            "isError": getattr(result, "isError", False),
        }

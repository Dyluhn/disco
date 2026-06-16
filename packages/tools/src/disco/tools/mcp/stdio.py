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
        self._transport = None

    # ---- lifecycle -----------------------------------------------------------

    async def connect(self) -> ClientSession:
        """Spawn the subprocess, initialize the MCP session.

        Times out after `init_timeout_s` on the initialize() call (SDK #1452
        hang defense). Stderr is captured into a buffer.
        """
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

        self._stderr_lines.clear()

        # Capture stderr into a temp file — the SDK's stdio_client
        # accepts an errlog TextIO. We use a temp file (has fileno())
        # and read it back after init for the per-server status (D4).
        errlog = tempfile.TemporaryFile(mode="w+", suffix=".stderr")

        # The SDK's stdio_client yields (read_stream, write_stream).
        # We wrap them in a ClientSession and call initialize().
        streams = stdio_client(params, errlog=errlog)
        read, write = await streams.__aenter__()
        self._transport = streams

        session = ClientSession(read, write)
        self._session = session
        await session.__aenter__()

        try:
            await asyncio.wait_for(
                session.initialize(), timeout=self._init_timeout_s
            )
        except TimeoutError as err:
            raise TimeoutError(
                f"MCP server {self._command[0]!r} did not respond to initialize() "
                f"within {self._init_timeout_s}s"
            ) from err

        # Read captured stderr into our buffer (D4: real stderr capture)
        try:
            errlog.flush()
            errlog.seek(0)
            captured = errlog.read()
            if captured:
                self._stderr_lines.append(captured)
        finally:
            errlog.close()

        return session

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
        if self._session is not None:
            try:
                await asyncio.wait_for(self._session.__aexit__(None, None, None), timeout=2.0)
            except Exception:
                pass
            self._session = None

        if self._transport is not None:
            try:
                await asyncio.wait_for(self._transport.__aexit__(None, None, None), timeout=2.0)
            except Exception:
                pass
            self._transport = None

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


"""MCP stdio transport tests — RP-05 rung A.

Covers: subprocess spawn, JSON framing, timeout,
stderr capture, error surfacing. Uses FakeStdioServer via subprocess.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from perpleximanus.tools.mcp.stdio import McpStdioClient


@pytest.fixture
def fake_server_command() -> list[str]:
    return [
        sys.executable,
        "-c",
        "from packages.tools.tests.mcp_fakes import FakeStdioServer; "
        "import asyncio; asyncio.run(FakeStdioServer().run())",
    ]


@pytest.mark.asyncio
async def test_stdio_client_connect_and_list_tools(fake_server_command):
    """A real subprocess FakeStdioServer is spawned and tools are listed."""
    client = McpStdioClient(
        command=[fake_server_command[0]],
        args=fake_server_command[1:],
        init_timeout_s=10.0,
    )
    try:
        session = await client.connect()
        assert session is not None
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "echo" in tool_names
        assert "add" in tool_names
        assert "read_file" in tool_names
        assert "list_files" in tool_names
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_client_init_timeout():
    """A non-responsive command times out within init_timeout_s."""
    client = McpStdioClient(
        command=[sys.executable, "-c", "import time; time.sleep(60)"],
        init_timeout_s=1.0,
    )
    with pytest.raises((asyncio.TimeoutError, TimeoutError, OSError, EOFError)):
        await client.connect()


@pytest.mark.asyncio
async def test_stdio_client_close_is_idempotent(fake_server_command):
    """Multiple close() calls do not raise."""
    client = McpStdioClient(
        command=[fake_server_command[0]],
        args=fake_server_command[1:],
        init_timeout_s=10.0,
    )
    await client.connect()
    await client.close()
    # Second close should NOT raise
    await client.close()


@pytest.mark.asyncio
async def test_stdio_client_session_raises_before_connect():
    """Accessing session before connect() raises RuntimeError."""
    client = McpStdioClient(
        command=[sys.executable, "-c", "pass"],
        init_timeout_s=1.0,
    )
    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.session


@pytest.mark.asyncio
async def test_stdio_client_stderr_capture(fake_server_command):
    """Real stderr content is captured from a fake server that writes to stderr."""
    client = McpStdioClient(
        command=[fake_server_command[0]],
        args=fake_server_command[1:],
        init_timeout_s=10.0,
    )
    try:
        await client.connect()
        # D4: assert REAL captured stderr content, not just isinstance check
        stderr = client.stderr_output
        assert isinstance(stderr, str)
        # The FakeStdioServer writes a diagnostic to stderr; assert we captured it
        assert "MCP fake server diagnostic" in stderr, (
            f"Expected real stderr content, got: {stderr!r}"
        )
    finally:
        await client.close()

"""MCP stdio transport tests — RP-05 rung A.

Covers: subprocess spawn, JSON framing, timeout,
stderr capture, error surfacing. Uses FakeStdioServer via subprocess.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
from disco.tools.mcp.stdio import McpStdioClient, _clean_base_env


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
    try:
        with pytest.raises((asyncio.TimeoutError, TimeoutError, OSError, EOFError)):
            await client.connect()
    finally:
        # A failed connect owns and drains every partially-entered resource;
        # explicit close remains harmless for callers that always clean up.
        await client.close()


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
async def test_stdio_client_close_from_different_task_uses_lifecycle_owner(
    fake_server_command,
):
    client = McpStdioClient(
        command=[fake_server_command[0]],
        args=fake_server_command[1:],
        init_timeout_s=10.0,
    )
    await client.connect()

    await asyncio.create_task(client.close())

    with pytest.raises(RuntimeError, match="not connected"):
        _ = client.session


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
async def test_stdio_client_does_not_leak_host_secrets(fake_server_command, monkeypatch):
    """SEC-1 regression — END-TO-END.

    Set the Fernet master key + a provider key in the PARENT env, spawn a real
    MCP subprocess, and have the subprocess report (via the get_env tool) what
    IT can see. The secrets must NOT have crossed the process boundary, while
    PATH (needed to locate runtimes) and an explicitly-attached per-server
    secret ref MUST pass through.
    """
    # Poison the parent environment exactly as production would have it.
    monkeypatch.setenv("DISCO_SECRET_KEY", "MASTER-KEY-must-not-leak")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-must-not-leak")

    client = McpStdioClient(
        command=[fake_server_command[0]],
        args=fake_server_command[1:],
        # The user explicitly attaches THIS server's own secret ref — it SHOULD
        # reach the child (that's the legitimate channel).
        env={"MY_SERVER_TOKEN": "attached-on-purpose"},
        init_timeout_s=10.0,
    )

    async def _seen(var: str) -> str:
        res = await client.call_tool("get_env", {"name": var})
        # call_tool returns {"content": [TextContent...], "isError": bool}.
        return res["content"][0].text

    try:
        await client.connect()
        # The two host secrets must be ABSENT from the child's view.
        assert await _seen("DISCO_SECRET_KEY") == "<absent>"
        assert await _seen("OPENROUTER_API_KEY") == "<absent>"
        # PATH must pass through (runtimes won't start otherwise).
        assert await _seen("PATH") != "<absent>"
        # The explicitly-attached per-server secret ref MUST reach the child.
        assert await _seen("MY_SERVER_TOKEN") == "attached-on-purpose"
    finally:
        await client.close()


def test_clean_base_env_excludes_secrets(monkeypatch):
    """Unit guard on the allowlist itself — no secret-shaped key survives."""
    monkeypatch.setenv("DISCO_SECRET_KEY", "x")
    monkeypatch.setenv("PMX_SECRET_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _clean_base_env()
    assert "PATH" in env
    leaked = [k for k in env if "KEY" in k or "TOKEN" in k or "SECRET" in k]
    assert leaked == [], f"clean base env leaked {leaked}"


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

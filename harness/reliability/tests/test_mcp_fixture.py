from __future__ import annotations

import asyncio
import logging

import httpx
from disco.core import SecurityRisk
from disco.tools.mcp.config import McpServerConfig
from disco.tools.mcp.http import McpHttpClient

from harness.reliability.mcp_fixture import (
    EXPECTED_TOKEN,
    FETCH_TOOL_NAME,
    RESULT_MARKER,
    SEARCH_MARKER,
    SEARCH_TOOL_NAME,
    SOURCE_CONTENT,
    SOURCE_URL,
    TOOL_NAME,
    ReliabilityMcpServer,
)


async def test_fixture_speaks_to_production_streamable_http_client() -> None:
    server = ReliabilityMcpServer()
    server.start()
    client = McpHttpClient(
        McpServerConfig(
            name="reliability_mcp",
            transport="streamable_http",
            url=server.mcp_url,
            risk_tier=SecurityRisk.LOW,
            enabled=True,
        )
    )
    try:
        await client.connect()
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == [
            TOOL_NAME,
            SEARCH_TOOL_NAME,
            FETCH_TOOL_NAME,
        ]
        assert tools[0].inputSchema["required"] == ["token"]
        result = await client.call_tool(TOOL_NAME, {"token": EXPECTED_TOKEN})
        assert result["isError"] is False
        assert RESULT_MARKER in result["content"][0].text
        search = await client.call_tool(SEARCH_TOOL_NAME, {"query": SEARCH_MARKER})
        assert search["isError"] is False
        assert SOURCE_URL in search["content"][0].text
        fetched = await client.call_tool(FETCH_TOOL_NAME, {"id": SOURCE_URL})
        assert fetched["isError"] is False
        assert SOURCE_CONTENT in fetched["content"][0].text
    finally:
        await client.close()
        server.close()


async def test_fixture_keeps_optional_get_stream_open_without_reconnect_thrash(
    caplog,
) -> None:
    fixture = ReliabilityMcpServer()
    fixture.start()
    client = McpHttpClient(
        McpServerConfig(
            name="reliability",
            transport="streamable_http",
            url=fixture.mcp_url,
            risk_tier="low",
        ),
        init_timeout_s=2.0,
        call_timeout_s=2.0,
    )
    try:
        with caplog.at_level(logging.INFO, logger="mcp.client.streamable_http"):
            await client.connect()
            tools = await client.list_tools()
            await asyncio.sleep(1.2)
            await client.close()
        assert {tool.name for tool in tools} >= {"reliability_echo", "search", "fetch"}
        assert "GET stream disconnected" not in caplog.text
    finally:
        await client.close()
        fixture.close()


async def test_session_delete_does_not_race_optional_get_stream_closed() -> None:
    fixture = ReliabilityMcpServer()
    fixture.start()
    timeout = httpx.Timeout(2.0, read=2.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", fixture.mcp_url) as response:
                assert response.status_code == 200
                chunks = response.aiter_bytes()
                assert b"reliability keepalive" in await anext(chunks)

                deleted = await client.delete(fixture.mcp_url)
                assert deleted.status_code == 204
                assert b"reliability keepalive" in await anext(chunks)
    finally:
        fixture.close()

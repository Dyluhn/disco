from __future__ import annotations

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

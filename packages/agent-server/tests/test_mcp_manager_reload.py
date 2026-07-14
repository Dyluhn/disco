from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from disco.agent_server.mcp_manager import McpManager


async def test_reload_replaces_stale_runtime_registry_and_reports_truth() -> None:
    config_store = MagicMock()
    config_store.load.return_value = SimpleNamespace(mcp=SimpleNamespace(servers={"fresh": {}}))
    runtime = SimpleNamespace(
        _config_store=config_store,
        _mcp_approval_pending={"stale": {"kind": "tools"}},
        _mcp_http_clients={"stale": object()},
        _mcp_http_tools={"mcp__stale__old": object()},
        _mcp_http_status={"stale": {"status": "connected"}},
        _mcp_pool=None,
    )
    manager = McpManager(runtime)

    async def close() -> None:
        runtime._mcp_http_clients.clear()
        runtime._mcp_http_tools.clear()
        runtime._mcp_http_status.clear()

    async def start() -> None:
        runtime._mcp_http_clients["fresh"] = object()
        runtime._mcp_http_tools["mcp__fresh__lookup"] = object()
        runtime._mcp_http_status["fresh"] = {"status": "connected"}

    manager._close_mcp_pool = AsyncMock(side_effect=close)
    manager._start_mcp_pool = AsyncMock(side_effect=start)

    result = await manager.reload()

    assert result == {
        "ok": True,
        "configured_servers": ["fresh"],
        "connected_servers": ["fresh"],
        "registered_tools": ["mcp__fresh__lookup"],
        "approval_required": [],
        "server_statuses": {"fresh": {"status": "connected"}},
    }
    manager._close_mcp_pool.assert_awaited_once_with()
    manager._start_mcp_pool.assert_awaited_once_with()

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from disco.agent_server.mcp_manager import McpManager


async def test_reload_replaces_stale_runtime_registry_and_reports_truth() -> None:
    config_store = MagicMock()
    config_store.load.return_value = SimpleNamespace(mcp=SimpleNamespace(servers={"fresh": {}}))
    manager = McpManager(config_store, MagicMock(), MagicMock())
    manager._approval_pending = {"stale": {"kind": "tools"}}
    manager._http_clients = {"stale": object()}  # type: ignore[dict-item]
    manager._http_tools = {"mcp__stale__old": object()}  # type: ignore[dict-item]
    manager._http_status = {"stale": {"status": "connected"}}

    async def close() -> None:
        manager._http_clients.clear()
        manager._http_tools.clear()
        manager._http_status.clear()

    async def start() -> None:
        manager._http_clients["fresh"] = object()  # type: ignore[assignment]
        manager._http_tools["mcp__fresh__lookup"] = object()  # type: ignore[assignment]
        manager._http_status["fresh"] = {"status": "connected"}

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

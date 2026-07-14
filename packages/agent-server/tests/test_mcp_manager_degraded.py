from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

from disco.agent_server.mcp_manager import McpManager
from disco.tools.mcp.approval import compute_config_hash
from disco.tools.mcp.config import McpServerConfig


def _server(name: str) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport="streamable_http",
        url=f"https://{name}.example.test/mcp",
        risk_tier="low",
    )


def _manager() -> tuple[McpManager, SimpleNamespace]:
    runtime = SimpleNamespace(_mcp_http_status={}, _mcp_http_clients={})
    return McpManager(runtime), runtime


async def test_mixed_reachable_and_unreachable_http_servers_degrade_independently(
    monkeypatch,
) -> None:
    manager, runtime = _manager()
    reachable = _server("reachable")
    unreachable = _server("unreachable")
    attempts: Counter[str] = Counter()

    async def connect(name, _server, _approvals) -> None:
        attempts[name] += 1
        if name == "unreachable":
            raise ConnectionError("credential-shaped detail must never cross status")

    manager._connect_http = AsyncMock(side_effect=connect)
    sleep = AsyncMock()
    monkeypatch.setattr("disco.agent_server.mcp_manager.asyncio.sleep", sleep)
    config_approvals = {
        "reachable": compute_config_hash(reachable),
        "unreachable": compute_config_hash(unreachable),
    }

    await asyncio.gather(
        manager._start_http_server("unreachable", unreachable, {}, config_approvals),
        manager._start_http_server("reachable", reachable, {}, config_approvals),
    )

    assert attempts == Counter(reachable=1, unreachable=3)
    assert runtime._mcp_http_status["reachable"] == {
        "status": "connected",
    }
    assert runtime._mcp_http_status["unreachable"] == {
        "status": "degraded",
        "diagnostic": {
            "code": "mcp_connection_failed",
            "attempts": 3,
            "exception_type": "ConnectionError",
        },
    }
    assert "credential-shaped" not in repr(runtime._mcp_http_status)
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 1.0]


async def test_http_server_recovers_within_bounded_retry_ladder(monkeypatch) -> None:
    manager, runtime = _manager()
    recovering = _server("recovering")
    attempts = 0

    async def connect(_name, _server, _approvals) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary outage")

    manager._connect_http = AsyncMock(side_effect=connect)
    sleep = AsyncMock()
    monkeypatch.setattr("disco.agent_server.mcp_manager.asyncio.sleep", sleep)

    await manager._start_http_server(
        "recovering",
        recovering,
        {},
        {"recovering": compute_config_hash(recovering)},
    )

    assert attempts == 2
    assert runtime._mcp_http_status["recovering"] == {"status": "connected"}
    sleep.assert_awaited_once_with(0.25)


def test_server_normalization_accepts_typed_and_raw_without_echoing_secrets() -> None:
    manager, _runtime = _manager()
    typed = _server("typed")
    raw = {
        "transport": "streamable_http",
        "url": "https://raw.example.test/mcp",
        "headers": {"Authorization": "stored-secret-reference"},
        "risk_tier": "medium",
    }

    assert manager._typed_server("typed", typed) == typed
    normalized = manager._typed_server("raw", raw)
    assert normalized.name == "raw"
    assert normalized.transport == "streamable_http"

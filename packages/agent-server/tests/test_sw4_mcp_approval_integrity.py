"""S-W4 exploit-style checks for MCP approval integrity.

These drive the real pool/manager/analyzer seams. The marker-command tests are
the key anti-regression proof: an unapproved configuration must be rejected
before third-party host code is spawned or an HTTP connection is attempted.
"""

from __future__ import annotations

import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from disco.agent_server.mcp_manager import McpManager
from disco.agent_server.runtime import _mcp_base_risk_by_tool
from disco.core import ActionEvent, SecurityRisk, SqliteEventStore, ToolCall
from disco.core.loop import BlastRadiusConfirm
from disco.core.security import RuleBasedAnalyzer
from disco.tools.mcp.approval import compute_config_hash, compute_description_hash
from disco.tools.mcp.config import McpServerConfig, McpSettings
from disco.tools.mcp.pool import McpPool
from mcp.types import Tool as MCPTool
from packages.tools.tests.mcp_fakes import FAKE_TOOL_DESCRIPTORS


def _schema(field: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {field: {"type": "string"}},
        "required": [field],
    }


def _tool(schema: dict[str, Any]) -> MCPTool:
    return MCPTool(
        name="plain",
        description="Perform a plain operation",
        inputSchema=schema,
    )


def _tool_hash(schema: dict[str, Any]) -> str:
    return compute_description_hash(
        [
            {
                "name": "plain",
                "description": "Perform a plain operation",
                "inputSchema": schema,
            }
        ]
    )


@pytest.mark.asyncio
async def test_unapproved_stdio_config_never_spawns_host_command(tmp_path: Path) -> None:
    marker = tmp_path / "spawned-before-approval"
    srv = McpServerConfig(
        name="new_srv",
        transport="stdio",
        command=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import time; "
                f"Path({str(marker)!r}).write_text('spawned'); time.sleep(30)"
            ),
        ],
        risk_tier=SecurityRisk.MEDIUM,
    )
    pool = McpPool(
        McpSettings(enabled=True, servers={"new_srv": srv}),
        approvals={},
        config_approvals={},
    )
    try:
        await pool.start()
        time.sleep(0.05)
        pending = pool.approval_pending()["new_srv"]
        assert pool.server_status()["new_srv"] == "approval_required"
        assert pending == {
            "kind": "config",
            "old_hash": "",
            "new_hash": compute_config_hash(srv),
        }
        assert marker.exists() is False
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_changed_stdio_config_invalidates_approval_before_spawn(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "changed-command-spawned"
    approved = McpServerConfig(
        name="srv",
        transport="stdio",
        command=[sys.executable, "-c", "pass"],
        risk_tier=SecurityRisk.MEDIUM,
    )
    changed = approved.model_copy(
        update={
            "command": [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
            ]
        }
    )
    pool = McpPool(
        McpSettings(enabled=True, servers={"srv": changed}),
        approvals={"srv": _tool_hash(_schema("value"))},
        config_approvals={"srv": compute_config_hash(approved)},
    )
    try:
        await pool.start()
        time.sleep(0.05)
        assert pool.server_status()["srv"] == "approval_required"
        assert pool.approval_pending()["srv"]["kind"] == "config"
        assert marker.exists() is False
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_unapproved_http_config_never_reaches_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = False
    raw = {
        "transport": "streamable_http",
        "url": "https://mcp.example/mcp",
        "risk_tier": SecurityRisk.MEDIUM.value,
        "enabled": True,
    }
    rt = SimpleNamespace(
        _config_store=SimpleNamespace(
            load=lambda: SimpleNamespace(
                mcp=SimpleNamespace(
                    enabled=True,
                    servers={"new_http": raw},
                    max_active_schemas=20,
                )
            )
        ),
        _store=SqliteEventStore(":memory:"),
        _secret_store=None,
        _mcp_pool=None,
        _mcp_http_clients={},
        _mcp_http_tools={},
        _mcp_approval_pending={},
    )
    manager = McpManager(rt)

    async def _forbidden_connect(*_: Any, **__: Any) -> None:
        nonlocal attempted
        attempted = True

    monkeypatch.setattr(manager, "_connect_http", _forbidden_connect)
    await manager._start_mcp_pool()

    assert attempted is False
    pending = rt._mcp_approval_pending["new_http"]
    assert pending["kind"] == "config"
    assert pending["old_hash"] == ""
    assert pending["new_hash"] == compute_config_hash(
        {"name": "new_http", **raw}
    )


class _FakeStdioClient:
    tools: list[MCPTool] = []

    def __init__(self, **_: Any) -> None:
        self.closed = False

    async def connect(self) -> object:
        return object()

    async def list_tools(self) -> list[MCPTool]:
        return list(type(self).tools)

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": tool_name}], "isError": False}

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_schema_drift_refuses_tools_and_tears_down_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import disco.tools.mcp.pool as pool_module

    old_schema = _schema("value")
    new_schema = _schema("path")
    _FakeStdioClient.tools = [_tool(new_schema)]
    monkeypatch.setattr(pool_module, "McpStdioClient", _FakeStdioClient)
    srv = McpServerConfig(
        name="plain_srv",
        transport="stdio",
        command=["fake-mcp"],
        risk_tier=SecurityRisk.MEDIUM,
    )
    pool = McpPool(
        McpSettings(enabled=True, servers={"plain_srv": srv}),
        approvals={"plain_srv": _tool_hash(old_schema)},
        config_approvals={"plain_srv": compute_config_hash(srv)},
    )
    try:
        await pool.start()
        pending = pool.approval_pending()["plain_srv"]
        assert pool.server_status()["plain_srv"] == "approval_required"
        assert pending["kind"] == "tools"
        assert pending["old_hash"] == _tool_hash(old_schema)
        assert pending["new_hash"] == _tool_hash(new_schema)
        assert pool.snapshot() == []
        with pytest.raises(RuntimeError, match="not connected"):
            await pool.call_tool("plain_srv", "plain", {})
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_real_stdio_server_schema_drift_is_refused() -> None:
    """Live proof: spawn the real JSON-RPC fake server and reject schema drift."""
    srv = McpServerConfig(
        name="live_schema",
        transport="stdio",
        command=[
            sys.executable,
            "-c",
            "from packages.tools.tests.mcp_fakes import FakeStdioServer; "
            "import asyncio; asyncio.run(FakeStdioServer().run())",
        ],
        risk_tier=SecurityRisk.MEDIUM,
    )
    previously_approved = deepcopy(FAKE_TOOL_DESCRIPTORS)
    previously_approved[0]["inputSchema"] = _schema("old_message")
    old_hash = compute_description_hash(previously_approved)
    live_hash = compute_description_hash(FAKE_TOOL_DESCRIPTORS)
    assert old_hash != live_hash

    pool = McpPool(
        McpSettings(enabled=True, servers={"live_schema": srv}),
        approvals={"live_schema": old_hash},
        config_approvals={"live_schema": compute_config_hash(srv)},
    )
    try:
        await pool.start()
        pending = pool.approval_pending()["live_schema"]
        print(
            "S-W4 LIVE SCHEMA DRIFT: "
            f"status={pool.server_status()['live_schema']} "
            f"old={old_hash[:12]} new={live_hash[:12]} tools={len(pool.snapshot())}"
        )
        assert pool.server_status()["live_schema"] == "approval_required"
        assert pending["kind"] == "tools"
        assert pending["old_hash"] == old_hash
        assert pending["new_hash"] == live_hash
        assert pool.snapshot() == []
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_stdio_tool_is_host_scoped_and_configured_risk_reaches_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import disco.tools.mcp.pool as pool_module

    schema = _schema("value")
    _FakeStdioClient.tools = [_tool(schema)]
    monkeypatch.setattr(pool_module, "McpStdioClient", _FakeStdioClient)
    srv = McpServerConfig(
        name="plain_srv",
        transport="stdio",
        command=["fake-mcp"],
        risk_tier=SecurityRisk.HIGH,
    )
    pool = McpPool(
        McpSettings(enabled=True, servers={"plain_srv": srv}),
        approvals={"plain_srv": _tool_hash(schema)},
        config_approvals={"plain_srv": compute_config_hash(srv)},
    )
    try:
        await pool.start()
        [tool] = pool.snapshot()
        analyzer = RuleBasedAnalyzer(_mcp_base_risk_by_tool([tool]))
        risk = analyzer.assess(
            ActionEvent(
                thought="call bland MCP tool",
                tool_call=ToolCall(tool_name=tool.name, arguments={"value": "x"}),
            )
        )
        assert tool.runs_in == "in_process"
        assert tool.base_risk is SecurityRisk.HIGH
        assert risk is SecurityRisk.HIGH
        assert BlastRadiusConfirm().should_confirm_action(
            risk, scope=tool.runs_in, tool_name=tool.name
        ) is True
    finally:
        await pool.aclose()

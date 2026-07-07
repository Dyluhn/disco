"""S-W4 MCP approval-integrity exploit harness.

No cassettes: the tests drive the real MCP pool/approval/analyzer paths with a
marker stdio command or an in-process fake stdio client.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from disco.agent_server.runtime import _mcp_base_risk_by_tool
from disco.core import ActionEvent, SecurityRisk, ToolCall
from disco.core.loop import BlastRadiusConfirm
from disco.core.security import RuleBasedAnalyzer
from disco.tools.anatomy import ToolDef
from disco.tools.mcp.approval import (
    ApprovalRequired,
    compute_description_hash,
    compute_server_config_hash,
)
from disco.tools.mcp.config import McpServerConfig, McpSettings
from disco.tools.mcp.pool import McpPool
from mcp.types import Tool as MCPTool
from pydantic import BaseModel


class _EmptyArgs(BaseModel):
    pass


def _schema_a() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }


def _schema_b() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }


def _tool(schema: dict[str, Any]) -> MCPTool:
    return MCPTool(
        name="do",
        description="Perform a plain operation",
        inputSchema=schema,
    )


def _tool_hash(schema: dict[str, Any]) -> str:
    return compute_description_hash([
        {
            "name": "do",
            "description": "Perform a plain operation",
            "inputSchema": schema,
        }
    ])


class _FakeStdioClient:
    connects = 0
    closes = 0
    tools: list[MCPTool] = []

    def __init__(self, **_: Any) -> None:
        pass

    async def connect(self) -> object:
        type(self).connects += 1
        return object()

    async def list_tools(self) -> list[MCPTool]:
        return list(type(self).tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": tool_name}], "isError": False}

    async def close(self) -> None:
        type(self).closes += 1


def _fake_srv(name: str = "plain", *, risk: SecurityRisk = SecurityRisk.MEDIUM) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport="stdio",
        command=["fake-mcp"],
        risk_tier=risk,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_sw4_new_stdio_server_requires_approval_without_spawning(tmp_path: Path) -> None:
    marker = tmp_path / "spawned-before-approval"
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import time; "
            f"Path({str(marker)!r}).write_text('spawned'); time.sleep(30)"
        ),
    ]
    srv = McpServerConfig(
        name="new_srv",
        transport="stdio",
        command=command,
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )
    pool = McpPool(
        McpSettings(enabled=True, servers={"new_srv": srv}),
        approvals={},
        server_config_approvals={},
    )

    try:
        await pool.start()
        time.sleep(0.05)
        pending = pool.approval_pending()["new_srv"]
        print(
            "SW4-H3-FIRST-USE status="
            f"{pool.server_status()['new_srv']} kind={pending['kind']} "
            f"spawned={marker.exists()}"
        )
        assert pool.server_status()["new_srv"] == "approval_required"
        assert pending["kind"] == "server_config"
        assert pending["old_hash"] == ""
        assert pending["new_hash"] == compute_server_config_hash("new_srv", srv)
        assert not marker.exists()
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_sw4_new_http_server_requires_approval_without_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from disco.agent_server.mcp_manager import McpManager

    connected = False

    class _FakeHttpClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def connect(self, **_: Any) -> None:
            nonlocal connected
            connected = True

    monkeypatch.setattr("disco.tools.mcp.http.McpHttpClient", _FakeHttpClient)
    rt = type(
        "_Rt",
        (),
        {
            "_secret_store": None,
            "_mcp_http_clients": {},
            "_mcp_http_tools": {},
        },
    )()
    srv = McpServerConfig(
        name="new_http",
        transport="streamable_http",
        url="https://mcp.example/mcp",
        risk_tier=SecurityRisk.MEDIUM,
        enabled=True,
    )

    with pytest.raises(ApprovalRequired) as exc:
        await McpManager(rt)._connect_http("new_http", srv, {}, {})

    print(
        "SW4-H3-HTTP-FIRST-USE "
        f"kind={exc.value.kind} connected={connected}"
    )
    assert exc.value.kind == "server_config"
    assert connected is False


@pytest.mark.asyncio
async def test_sw4_stdio_tool_is_host_scope_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    import disco.tools.mcp.pool as pool_module

    _FakeStdioClient.connects = 0
    _FakeStdioClient.closes = 0
    _FakeStdioClient.tools = [_tool(_schema_a())]
    monkeypatch.setattr(pool_module, "McpStdioClient", _FakeStdioClient)

    srv = _fake_srv(risk=SecurityRisk.HIGH)
    pool = McpPool(
        McpSettings(enabled=True, servers={"plain": srv}),
        approvals={"plain": _tool_hash(_schema_a())},
        server_config_approvals={
            "plain": compute_server_config_hash("plain", srv),
        },
    )
    try:
        await pool.start()
        tdef = pool.snapshot()[0]
        gate = BlastRadiusConfirm()
        should_confirm = gate.should_confirm_action(
            SecurityRisk.HIGH,
            scope=tdef.runs_in,
            tool_name=tdef.name,
        )
        print(
            "SW4-H4-HOST-SCOPE "
            f"tool={tdef.name} runs_in={tdef.runs_in} confirm={should_confirm}"
        )
        assert _FakeStdioClient.connects == 1
        assert tdef.runs_in == "in_process"
        assert should_confirm is True
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_sw4_schema_flip_changes_hash_and_requires_reapproval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import disco.tools.mcp.pool as pool_module

    _FakeStdioClient.connects = 0
    _FakeStdioClient.closes = 0
    _FakeStdioClient.tools = [_tool(_schema_b())]
    monkeypatch.setattr(pool_module, "McpStdioClient", _FakeStdioClient)

    srv = _fake_srv()
    old_hash = _tool_hash(_schema_a())
    new_hash = _tool_hash(_schema_b())
    pool = McpPool(
        McpSettings(enabled=True, servers={"plain": srv}),
        approvals={"plain": old_hash},
        server_config_approvals={
            "plain": compute_server_config_hash("plain", srv),
        },
    )
    try:
        await pool.start()
        pending = pool.approval_pending()["plain"]
        print(
            "SW4-H7-SCHEMA-DRIFT "
            f"status={pool.server_status()['plain']} "
            f"old={pending['old_hash'][:12]} new={pending['new_hash'][:12]}"
        )
        assert old_hash != new_hash
        assert pool.server_status()["plain"] == "approval_required"
        assert pending["kind"] == "tool_schema"
        assert pending["old_hash"] == old_hash
        assert pending["new_hash"] == new_hash
        assert pool.snapshot() == []
    finally:
        await pool.aclose()


def test_sw4_high_risk_tier_bland_mcp_name_scores_high() -> None:
    qname = "mcp__plain__do"
    tdef = ToolDef(
        name=qname,
        description="Plain operation",
        args_model=_EmptyArgs,
        base_risk=SecurityRisk.HIGH,
        runs_in="in_process",
        read_only=False,
    )
    analyzer = RuleBasedAnalyzer(_mcp_base_risk_by_tool([tdef]))
    risk = analyzer.assess(
        ActionEvent(
            thought="call bland MCP tool",
            tool_call=ToolCall(tool_name=qname, arguments={}),
        )
    )
    should_confirm = BlastRadiusConfirm().should_confirm_action(
        risk,
        scope=tdef.runs_in,
        tool_name=qname,
    )
    print(
        "SW4-H5-RISK-TIER "
        f"tool={qname} base={tdef.base_risk.value} scored={risk.value} "
        f"confirm={should_confirm}"
    )
    assert risk is SecurityRisk.HIGH
    assert should_confirm is True

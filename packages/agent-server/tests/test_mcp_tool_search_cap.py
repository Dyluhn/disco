"""RP-05c — §6 active-schema cap via _apply_mcp_scope.

Drives the PRODUCTION registration path (_apply_mcp_scope from runtime.py) with
a fake pool of N fake MCP ToolDefs. Asserts executor state after registration —
never by constructing _MCPToolWrapper or _MetaToolSearchWrapper directly (that
was the round-3 tautology that let a false affordance slip through).
"""

from __future__ import annotations

import pytest
from disco.agent_server.runtime import _apply_mcp_scope
from disco.core import SecurityRisk, ToolCall
from disco.core.llm import ModelExecutionPolicy
from disco.tools import DefaultToolExecutor, agent_scope, build_default_registry
from disco.tools.anatomy import ToolDef
from pydantic import BaseModel

# ---- fakes ------------------------------------------------------------------


class _FakeMCPArgs(BaseModel):
    """Accepts any kwargs (MCP tools use dynamic schemas; empty call is valid)."""
    model_config = {"frozen": True, "extra": "ignore"}


class _FakeCallTarget:
    """Stand-in for the merged call target (_MergedCallTarget). Returns success."""

    async def call_tool(self, server: str, tool: str, arguments: dict) -> dict:
        return {"content": [{"text": f"ok:{server}/{tool}"}], "isError": False}


def _fake_mcp_tools(n: int, server: str = "srv") -> list[ToolDef]:
    """Build n fake MCP ToolDefs with qualified names mcp__<server>__tool_<i>."""
    return [
        ToolDef(
            name=f"mcp__{server}__tool_{i}",
            description=f"Fake MCP tool {i} on server {server}",
            args_model=_FakeMCPArgs,
            runs_in="sandbox",
            base_risk=SecurityRisk.LOW,
        )
        for i in range(n)
    ]


def _executor() -> DefaultToolExecutor:
    _policy = ModelExecutionPolicy.standard()
    return DefaultToolExecutor(
        build_default_registry(),
        agent_scope(model_policy=_policy),
        model_policy=_policy,
    )


# ---- over-cap path ----------------------------------------------------------


def test_over_cap_advertises_tool_search_not_mcp_schemas():
    """21 MCP tools > cap(20) → tool_search visible, mcp schemas hidden."""
    ex = _executor()
    _apply_mcp_scope(ex, _fake_mcp_tools(21), _FakeCallTarget(), max_active_schemas=20)

    advertised = {t.name for t in ex.available_tools()}
    assert "tool_search" in advertised
    assert not any(n.startswith("mcp__") for n in advertised), (
        f"MCP schemas must not be advertised over cap; got: {advertised}"
    )


def test_over_cap_non_mcp_tools_still_advertised():
    """Non-MCP tools (the built-in agent scope) remain visible over the cap."""
    ex = _executor()
    pre_cap_tools = {t.name for t in ex.available_tools()}
    _apply_mcp_scope(ex, _fake_mcp_tools(21), _FakeCallTarget(), max_active_schemas=20)

    advertised = {t.name for t in ex.available_tools()}
    for name in pre_cap_tools:
        assert name in advertised, f"non-MCP tool {name!r} must remain advertised over cap"


@pytest.mark.asyncio
async def test_over_cap_mcp_tool_callable_by_qualified_name():
    """Over cap: a non-advertised mcp__* call still resolves and succeeds."""
    ex = _executor()
    _apply_mcp_scope(ex, _fake_mcp_tools(21), _FakeCallTarget(), max_active_schemas=20)

    # mcp__srv__tool_0 is NOT advertised but IS in allowed_tools → must succeed
    result = await ex.execute(ToolCall(tool_name="mcp__srv__tool_0", arguments={}))
    assert result.success, f"expected success, got error: {result.error}"


@pytest.mark.asyncio
async def test_over_cap_tool_search_callable():
    """Over cap: tool_search is both advertised and callable."""
    ex = _executor()
    _apply_mcp_scope(ex, _fake_mcp_tools(21), _FakeCallTarget(), max_active_schemas=20)

    result = await ex.execute(ToolCall(
        tool_name="tool_search",
        arguments={"query": "fake tool", "limit": 3},
    ))
    assert result.success, f"tool_search call failed: {result.error}"


# ---- under-cap path ---------------------------------------------------------


def test_under_cap_all_mcp_tools_advertised():
    """20 MCP tools == cap → all 20 advertised, no tool_search."""
    ex = _executor()
    fake = _fake_mcp_tools(20)
    _apply_mcp_scope(ex, fake, _FakeCallTarget(), max_active_schemas=20)

    advertised = {t.name for t in ex.available_tools()}
    for tdef in fake:
        assert tdef.name in advertised, f"{tdef.name} must be advertised at cap boundary"
    assert "tool_search" not in advertised


def test_under_cap_no_tool_search():
    """5 MCP tools << cap(20) → no tool_search in advertised set."""
    ex = _executor()
    _apply_mcp_scope(ex, _fake_mcp_tools(5), _FakeCallTarget(), max_active_schemas=20)

    advertised = {t.name for t in ex.available_tools()}
    assert "tool_search" not in advertised


def test_zero_mcp_tools_noop():
    """Empty MCP tool list → executor unchanged (no-op).

    For a standard-policy executor, agent_scope() returns advertised_tools=None
    (show all allowed tools). The no-op check is that allowed_tools and the
    advertised set are both unchanged after _apply_mcp_scope with an empty list.
    """
    ex = _executor()
    before_allowed = ex._scope.allowed_tools
    before_advertised = ex._scope.advertised_tools
    _apply_mcp_scope(ex, [], _FakeCallTarget(), max_active_schemas=20)
    assert ex._scope.allowed_tools == before_allowed
    assert ex._scope.advertised_tools == before_advertised
    # Standard policy: advertised_tools=None means all allowed tools are shown
    assert ex._scope.advertised_tools is None


# ---- readonly_tool_names planner-safety (allowed_tools, not advertised) ------


def test_over_cap_readonly_tool_names_from_allowed_not_advertised():
    """Over the cap, readonly_tool_names still derives from allowed_tools.

    MCP tools are not read_only (pool.py sets read_only=False), but the
    planner-safety gate must not be restricted to the advertised subset.
    """
    ex = _executor()
    pre_readonly = ex.readonly_tool_names()
    _apply_mcp_scope(ex, _fake_mcp_tools(21), _FakeCallTarget(), max_active_schemas=20)

    post_readonly = ex.readonly_tool_names()
    # All pre-existing read-only tools must remain in the set after cap wiring.
    assert pre_readonly <= post_readonly, (
        "readonly_tool_names shrank after cap wiring — planner backstop weakened"
    )

"""RP-05c — ToolScope advertised/callable split.

Verifies that `advertised_tools` restricts visibility (available_tools) without
touching callability (execute / registry.get) or the planner-safety backstop
(readonly_tool_names still keys off allowed_tools, not the advertised subset).
"""

from __future__ import annotations

import pytest
from conftest import call
from disco.tools.anatomy import ToolContext, ToolDef, ToolOutcome
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import ToolRegistry, ToolScope
from pydantic import BaseModel


class _NoArgs(BaseModel):
    model_config = {"frozen": True}


class _SimpleTool:
    def __init__(self, name: str, *, read_only: bool = False, runs_in: str = "in_process") -> None:
        self.definition = ToolDef(
            name=name,
            description=f"Tool {name}",
            args_model=_NoArgs,
            runs_in=runs_in,
            read_only=read_only,
        )

    async def run(self, args: _NoArgs, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome(success=True, content=f"ran:{self.definition.name}")


def _make_executor(
    tool_names: list[str],
    allowed: frozenset[str],
    advertised: frozenset[str] | None = None,
    read_only_names: frozenset[str] = frozenset(),
) -> DefaultToolExecutor:
    reg = ToolRegistry()
    for name in tool_names:
        reg.register(_SimpleTool(name, read_only=name in read_only_names))
    scope = ToolScope(allowed_tools=allowed, advertised_tools=advertised)
    return DefaultToolExecutor(reg, scope)


# ---- visibility (available_tools) -------------------------------------------


def test_advertised_subset_restricts_visible_tools():
    """Scope with allowed={a,b,c}, advertised={a} → available_tools returns only a."""
    ex = _make_executor(["a", "b", "c"], allowed=frozenset({"a", "b", "c"}), advertised=frozenset({"a"}))
    names = [t.name for t in ex.available_tools()]
    assert names == ["a"]


def test_advertised_none_shows_all_allowed():
    """advertised_tools=None (default) → all allowed tools are visible."""
    ex = _make_executor(["a", "b", "c"], allowed=frozenset({"a", "b", "c"}))
    names = {t.name for t in ex.available_tools()}
    assert names == {"a", "b", "c"}


def test_advertised_must_be_subset_of_allowed_in_practice():
    """An advertised tool that is not allowed still doesn't appear — the registry
    only contains allowed tools in in_scope(), so the intersection is automatic."""
    ex = _make_executor(
        ["a", "b"],
        allowed=frozenset({"a"}),       # b not allowed
        advertised=frozenset({"a", "b"}),  # b advertised but not allowed
    )
    names = {t.name for t in ex.available_tools()}
    assert names == {"a"}  # b excluded because it's not in allowed_tools


# ---- callability (execute) --------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_not_advertised_tool_is_callable():
    """Tool b is allowed but not advertised → execute() succeeds."""
    ex = _make_executor(["a", "b", "c"], allowed=frozenset({"a", "b", "c"}), advertised=frozenset({"a"}))
    result = await ex.execute(call("b"))
    assert result.success, result.error
    assert "ran:b" in result.content


@pytest.mark.asyncio
async def test_not_allowed_tool_yields_unknown_tool():
    """Tool d not in allowed_tools → execute() returns unknown_tool, regardless of advertised."""
    ex = _make_executor(
        ["a", "b", "c", "d"],
        allowed=frozenset({"a", "b", "c"}),  # d registered but not allowed
        advertised=frozenset({"a", "d"}),    # d advertised — should still be uncallable
    )
    result = await ex.execute(call("d"))
    assert not result.success
    assert "unknown" in (result.error or "").lower() or "out-of-scope" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_allowed_and_advertised_tool_is_callable():
    """Tool a is both allowed and advertised → execute() succeeds normally."""
    ex = _make_executor(["a", "b"], allowed=frozenset({"a", "b"}), advertised=frozenset({"a"}))
    result = await ex.execute(call("a"))
    assert result.success


# ---- planner safety (readonly_tool_names from allowed_tools) ----------------


def test_readonly_tool_names_derived_from_allowed_not_advertised():
    """readonly_tool_names must include all allowed read-only tools, even non-advertised ones."""
    # b and c are read-only but not advertised
    ex = _make_executor(
        ["a", "b", "c"],
        allowed=frozenset({"a", "b", "c"}),
        advertised=frozenset({"a"}),
        read_only_names=frozenset({"b", "c"}),
    )
    rnames = ex.readonly_tool_names()
    assert "b" in rnames, "read-only allowed tool b must be in readonly_tool_names"
    assert "c" in rnames, "read-only allowed tool c must be in readonly_tool_names"
    assert "a" not in rnames, "a is not read_only"


def test_readonly_tool_names_not_restricted_to_advertised():
    """readonly_tool_names does NOT filter by advertised_tools — it is the security
    allowlist boundary, not the visibility boundary."""
    ex = _make_executor(
        ["a", "b"],
        allowed=frozenset({"a", "b"}),
        advertised=frozenset({"a"}),   # b not advertised
        read_only_names=frozenset({"b"}),
    )
    rnames = ex.readonly_tool_names()
    assert "b" in rnames

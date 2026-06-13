"""Wire-through: DefaultToolExecutor stamps its `assist` gate onto EVERY
ToolContext it builds.

The executor is the only thing that constructs ToolContext for live tool runs,
so a tool can only see the weak-model gate if the executor copies its `_assist`
into ctx.assist. The field-existence test (`test_assist_tier.py`) proves the
ToolContext *has* the field; this proves the executor *populates* it from the
constructor flag — the value-flow the field test can't see because it builds the
context by hand."""

from __future__ import annotations

import pytest
from disco.tools.anatomy import ToolDef
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import ToolRegistry, ToolScope
from pydantic import BaseModel

pytestmark = pytest.mark.asyncio


class _NoArgs(BaseModel):
    pass


_TOOL_DEF = ToolDef(name="probe", description="in-process probe", args_model=_NoArgs)


def _executor(*, assist):
    reg = ToolRegistry()
    return DefaultToolExecutor(
        reg, ToolScope(allowed_tools=frozenset({"probe"})), assist=assist
    )


async def test_build_context_stamps_assist_true():
    """assist=True at construction → ctx.assist True for an in-process tool."""
    ctx = await _executor(assist=True)._build_context(_TOOL_DEF)
    assert ctx.assist is True


async def test_build_context_stamps_assist_false():
    """assist=False → ctx.assist False (gate genuinely OFF, not just defaulted)."""
    ctx = await _executor(assist=False)._build_context(_TOOL_DEF)
    assert ctx.assist is False


async def test_build_context_assist_defaults_off():
    """Legacy construction with no `assist` kwarg → ctx.assist False, so the
    capable-model default carries through the executor unchanged."""
    reg = ToolRegistry()
    ex = DefaultToolExecutor(reg, ToolScope(allowed_tools=frozenset({"probe"})))
    ctx = await ex._build_context(_TOOL_DEF)
    assert ctx.assist is False

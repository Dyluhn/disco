"""DC-03 — executor.tool_scope() resolution tests.

sandbox tool → 'sandbox', in_process tool → 'in_process', unregistered → 'unknown'.
"""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy
from disco.tools import (
    DefaultToolExecutor,
    ToolDef,
    ToolOutcome,
    agent_scope,
    build_default_registry,
)
from disco.tools.anatomy import ToolContext
from disco.tools.registry import ToolRegistry, ToolScope
from pydantic import BaseModel
from tool_fakes import FakeSandboxInstance

_STANDARD = ModelExecutionPolicy.standard()


def _executor(scope=None, sandbox=None):
    return DefaultToolExecutor(
        build_default_registry(),
        scope or agent_scope(model_policy=_STANDARD),
        sandbox=sandbox or FakeSandboxInstance(),
    )


# ---- sandbox tools (default runs_in) ----------------------------------------


def test_sandbox_tool_reports_sandbox():
    """Built-in shell tool has runs_in='sandbox' (the default)."""
    ex = _executor()
    assert ex.tool_scope("shell") == "sandbox"


def test_file_write_reports_sandbox():
    ex = _executor()
    assert ex.tool_scope("file_write") == "sandbox"


def test_code_exec_reports_sandbox():
    ex = _executor()
    assert ex.tool_scope("code_exec") == "sandbox"


# ---- in_process tool --------------------------------------------------------


def test_in_process_tool_reports_in_process():
    """A tool registered with runs_in='in_process' is reported correctly."""

    class _IPArgs(BaseModel):
        pass

    class _IPTool:
        definition = ToolDef(
            name="ip_tool",
            description="runs in process",
            args_model=_IPArgs,
            runs_in="in_process",
        )

        async def run(self, args, ctx: ToolContext) -> ToolOutcome:
            return ToolOutcome(success=True, content="ok")

    scope = ToolScope(allowed_tools=frozenset({"ip_tool"}))
    reg = ToolRegistry()
    reg.register(_IPTool())
    ex = DefaultToolExecutor(reg, scope)
    assert ex.tool_scope("ip_tool") == "in_process"


# ---- unregistered / out-of-scope → unknown -----------------------------------


def test_unregistered_tool_reports_unknown():
    """A tool name not in the registry → 'unknown'."""
    ex = _executor()
    assert ex.tool_scope("nonexistent_tool_xyz") == "unknown"


def test_out_of_scope_tool_reports_unknown():
    """A tool that exists in the registry but is out of the active scope → 'unknown'.
    tool_scope uses in_scope() so it naturally returns 'unknown' for out-of-scope tools."""

    class _OtherArgs(BaseModel):
        pass

    class _OtherTool:
        definition = ToolDef(
            name="other_sandboxed",
            description="sandboxed but not in scope",
            args_model=_OtherArgs,
            runs_in="sandbox",
        )

        async def run(self, args, ctx: ToolContext) -> ToolOutcome:
            return ToolOutcome(success=True, content="ok")

    scope = ToolScope(allowed_tools=frozenset({"shell"}))  # other_sandboxed not in scope
    reg = ToolRegistry()
    reg.register(_OtherTool())
    ex = DefaultToolExecutor(reg, scope, sandbox=FakeSandboxInstance())
    assert ex.tool_scope("other_sandboxed") == "unknown"

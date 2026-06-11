"""Regression tests for the RP-05c requery gate fix.

Post-RP-05c, available_tools() returns only the *advertised* subset. The
unknown-tool requery gate in the engine must key off callability
(callable_tool_names()), NOT visibility (available_tools()), or it will
bounce hidden-but-callable MCP tools with a false "Unknown tool" error and
poison the model's context before the tool ever executes.
"""

import asyncio
import pytest
from perpleximanus.core import ToolResult
from perpleximanus.core.llm import ToolSpec
from loop_fakes import build_loop, action_step, finish_step, ScriptedAgent


class _SplitExecutor:
    """Models the RP-05c advertise/callable split.

    available_tools() returns only the advertised set (e.g. tool_search).
    callable_tool_names() returns the full callability set, including hidden
    MCP tools that are withheld from the model's tool listing.
    """

    def __init__(self, *, advertised, callable_names):
        self._advertised = list(advertised)
        self._callable = frozenset(callable_names)
        self.calls = []

    def available_tools(self):
        return self._advertised

    def callable_tool_names(self):
        return self._callable

    async def execute(self, call):
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id, tool_name=call.tool_name, success=True, content="ok"
        )


_TOOL_SEARCH = ToolSpec(name="tool_search", description="discover tools", parameters_schema={})
_HIDDEN_MCP = "mcp__srv__tool_5"


@pytest.mark.asyncio
async def test_hidden_callable_mcp_tool_not_requeried():
    """Positive: a tool hidden from available_tools() but present in
    callable_tool_names() must execute on the first attempt — no false
    'Unknown tool' requery.

    Bug scenario (pre-fix): available_tools() returns only [tool_search].
    The engine checked all_known_names against available_tools(), so
    mcp__srv__tool_5 was NOT in all_known_names. The requery fired, injecting
    an error; the ScriptedAgent advanced to finish_step() and the MCP call
    was silently discarded (executor.calls empty).

    Fix: the engine uses callable_tool_names() for the requery gate. The
    hidden MCP tool IS in callable_tool_names(), so no requery fires and
    executor.execute() is called normally.
    """
    executor = _SplitExecutor(
        advertised=[_TOOL_SEARCH],
        callable_names={"tool_search", _HIDDEN_MCP},
    )
    agent = ScriptedAgent([action_step(_HIDDEN_MCP), finish_step()])
    loop, _ = build_loop(agent, executor=executor)

    await loop.send_message("go")
    await asyncio.wait_for(loop.run(), timeout=5.0)

    executed_names = [c.tool_name for c in executor.calls]
    assert _HIDDEN_MCP in executed_names, (
        f"Hidden callable MCP tool was not executed; executor saw: {executed_names}. "
        "The requery gate is keying off visibility (available_tools) instead of "
        "callability (callable_tool_names)."
    )
    # No requery fired: agent was called exactly twice (tool call + finish).
    # With the bug, the requery advances the script to finish_step on the 2nd call,
    # agent.calls is still 2 — but executor.calls is empty (caught above).
    assert agent.calls == 2, f"Expected 2 agent steps (action + finish); got {agent.calls}"


@pytest.mark.asyncio
async def test_genuinely_unknown_tool_still_requeried():
    """Negative control: a name absent from callable_tool_names() still triggers
    the requery gate — the fix must not over-correct and pass all unknown names.

    With max_iterations=1, the ScriptedAgent repeats totally_not_a_tool for
    every call. The engine should requery twice (requery_count 0→1→2) before
    breaking, producing ≥3 agent.step() calls for one outer iteration.
    """
    executor = _SplitExecutor(
        advertised=[_TOOL_SEARCH],
        callable_names={"tool_search", _HIDDEN_MCP},
    )
    # Single step; ScriptedAgent repeats it on exhaustion.
    agent = ScriptedAgent([action_step("totally_not_a_tool")])
    loop, _ = build_loop(agent, executor=executor, max_iterations=1)

    await loop.send_message("go")
    await asyncio.wait_for(loop.run(), timeout=5.0)

    # Requery must have fired: 1 initial call + 2 requery bounces = 3 agent calls
    # before the gate breaks and executes (requery_count caps at 2).
    assert agent.calls >= 3, (
        f"Expected ≥3 agent calls (initial + 2 requery bounces); got {agent.calls}. "
        "The requery gate no longer catches genuinely unknown tool names."
    )

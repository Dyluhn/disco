"""Regression tests for the RP-05c requery gate fix.

Post-RP-05c, available_tools() returns only the *advertised* subset. The
unknown-tool requery gate in the engine must key off callability
(callable_tool_names()), NOT visibility (available_tools()), or it will
bounce hidden-but-callable MCP tools with a false "Unknown tool" error and
poison the model's context before the tool ever executes.
"""

import asyncio

import pytest
from disco.core import ToolResult
from disco.core.llm import ModelExecutionPolicy, ToolSpec
from loop_fakes import ScriptedAgent, action_step, build_loop, finish_step


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


class _KnownDeniedExecutor(_SplitExecutor):
    def __init__(self, *, advertised, callable_names, known_names):
        super().__init__(advertised=advertised, callable_names=callable_names)
        self._known = frozenset(known_names)

    def known_tool_names_for_requery(self):
        return self._known

    async def execute(self, call):
        self.calls.append(call)
        message = (
            "This conversation routes work through workflows. Call list_workflows, "
            "then enter_workflow(instance_id) to get a scope that includes "
            f"{call.tool_name!r}."
        )
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            content=message,
            error=message,
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


@pytest.mark.asyncio
async def test_known_but_scope_denied_tool_reaches_executor_without_unknown_requery():
    executor = _KnownDeniedExecutor(
        advertised=[ToolSpec(name="list_workflows", description="list", parameters_schema={})],
        callable_names={"list_workflows", "enter_workflow"},
        known_names={"list_workflows", "enter_workflow", "file_write"},
    )
    agent = ScriptedAgent([action_step("file_write"), finish_step()])
    loop, _ = build_loop(agent, executor=executor)

    await loop.send_message("go")
    await asyncio.wait_for(loop.run(), timeout=5.0)

    executed_names = [c.tool_name for c in executor.calls]
    assert executed_names == ["file_write"]
    assert agent.calls == 2
    for view in agent.seen_views:
        assert not _requery_hint_contents(view)


# ---------------------------------------------------------------------------
# T9 (F2): augment Rung-7 with an edit-distance "did you mean" suggestion.
# Gate: ONLY when self._assist is ON. With assist OFF the hint must be
# byte-identical to today (no new text, no new cap, no new reroute path).
# ---------------------------------------------------------------------------


def _requery_hint_contents(view):
    """Return the content of every 'Unknown tool' user message in view.messages.

    The Rung-7 reroute appends its hint as a transient user message; if the
    model keeps hallucinating the same tool name, subsequent views stack more
    such messages. Tests should grab the first one -- that's the one the model
    saw immediately after the first requery bounce.
    """
    return [
        m.content
        for m in view.messages
        if m.role == "user" and "Unknown tool" in (m.content or "")
    ]


def _tools_with_file_write():
    return [
        ToolSpec(name="file_read", description="read", parameters_schema={}),
        ToolSpec(name="file_write", description="write", parameters_schema={}),
        ToolSpec(name="shell", description="run a shell command", parameters_schema={}),
    ]


@pytest.mark.asyncio
async def test_unknown_tool_hint_suggests_nearest_with_assist_on():
    """Assist ON: the Rung-7 hint appends 'did you mean <name>?' naming the
    nearest real tool by edit distance (file_writ -> file_write).

    Same shape as the existing requery test, but asserts the *content* of the
    hint, not just that the requery fired. The ScriptedAgent repeats the bad
    action, so the requery bounces (requery_count 0 -> 1); the view on call #1
    (the second agent.step) is the first view that carries the hint.
    """
    executor = _SplitExecutor(
        advertised=_tools_with_file_write(),
        callable_names={"file_read", "file_write", "shell"},
    )
    agent = ScriptedAgent([action_step("file_writ")])
    loop, _ = build_loop(agent, executor=executor, max_iterations=1)
    loop._model_policy = ModelExecutionPolicy(tier="weak")

    await loop.send_message("go")
    await asyncio.wait_for(loop.run(), timeout=5.0)

    # Requery must have fired at least once (we only need to inspect its hint).
    assert agent.calls >= 2, (
        f"Expected the requery to fire at least once; got {agent.calls} agent calls."
    )
    hints = _requery_hint_contents(agent.seen_views[1])
    assert hints, (
        f"Expected an 'Unknown tool' hint in call #2's view; messages were: "
        f"{[m.content for m in agent.seen_views[1].messages]!r}"
    )
    hint = hints[0]
    # The original hint shape must still be present.
    assert "Unknown tool 'file_writ'" in hint, f"Hint should still name the unknown tool: {hint!r}"
    assert "Available:" in hint, f"Hint should still list available tools: {hint!r}"
    # The new assist-gated suggestion must be present.
    assert "did you mean" in hint, (
        f"Hint should contain 'did you mean' when assist is ON: {hint!r}"
    )
    assert "file_write" in hint, (
        f"Hint should suggest 'file_write' as the nearest real tool: {hint!r}"
    )


@pytest.mark.asyncio
async def test_unknown_tool_hint_unchanged_with_assist_off():
    """Assist OFF (the default): the Rung-7 hint is byte-identical to today --
    NO 'did you mean' suffix, NO new path. Regression guard for the gate.

    If a future change makes the suggestion always-on, this test fails.
    """
    executor = _SplitExecutor(
        advertised=_tools_with_file_write(),
        callable_names={"file_read", "file_write", "shell"},
    )
    agent = ScriptedAgent([action_step("file_writ")])
    loop, _ = build_loop(agent, executor=executor, max_iterations=1)
    # Default: loop._assist is False (never set it on this loop).
    assert loop._assist is False

    await loop.send_message("go")
    await asyncio.wait_for(loop.run(), timeout=5.0)

    assert agent.calls >= 2
    hints = _requery_hint_contents(agent.seen_views[1])
    assert hints, "Expected an 'Unknown tool' hint in call #2's view"
    hint = hints[0]
    # Original shape preserved.
    assert "Unknown tool 'file_writ'" in hint
    assert "Available:" in hint
    # The augment MUST NOT fire when the gate is off.
    assert "did you mean" not in hint, (
        f"Hint must be byte-identical to today when assist is OFF; got: {hint!r}"
    )

"""Structured debug tools are bounded by real artifact mutations, not retries."""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import ToolSpec
from disco.core.loop.no_progress_detector import (
    DEBUG_PROBE_TOOLS,
    debug_probe_blocked_tools,
    debug_probe_budget,
    debug_probe_budget_notice_active,
)
from event_fakes import with_seqs
from loop_fakes import FakeExecutor, ScriptedAgent, build_loop, finish_step


def _action(tool: str, *, host_probe: bool = False) -> ActionEvent:
    return ActionEvent(
        source=EventSource.AGENT,
        thought="check",
        tool_call=ToolCall(tool_name=tool, arguments={}),
        meta={"verify_probe": True} if host_probe else {},
    )


def _observation(action: ActionEvent, *, changed: bool) -> ObservationEvent:
    return ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=action.tool_call.tool_name,
            success=True,
            content="changed" if changed else "no-op",
            structured=(
                {"path": "index.html", "sha256": "a" * 64}
                if changed
                else {"kind": "no_op_write", "state_changed": False}
            ),
        ),
    )


def _failed_probe_observation(action: ActionEvent) -> ObservationEvent:
    return ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=action.tool_call.tool_name,
            success=True,
            content="failed check",
            structured={
                "passed": False,
                "failure_fingerprint": "same-output",
                "summary": "same failure",
            },
        ),
    )


def test_two_debug_probes_with_unchanged_files_withdraw_the_tool_set() -> None:
    events = with_seqs([_action("verify_web_app"), _action("design_lint")])

    budget = debug_probe_budget(events)

    assert budget.calls == 2
    assert budget.exhausted
    assert debug_probe_blocked_tools(events) == DEBUG_PROBE_TOOLS


def test_host_owned_finish_probes_do_not_spend_the_agent_debug_budget() -> None:
    events = with_seqs(
        [
            _action("verify_appkit_app", host_probe=True),
            _action("verify_appkit_app", host_probe=True),
        ]
    )

    budget = debug_probe_budget(events)

    assert budget.calls == 0
    assert debug_probe_blocked_tools(events) == frozenset()


def test_no_op_write_does_not_restore_debug_allowance() -> None:
    first = _action("verify_web_app")
    no_op = _action("file_write")
    second = _action("verify_web_app")
    events = with_seqs([first, no_op, _observation(no_op, changed=False), second])

    assert debug_probe_budget(events).calls == 2
    assert debug_probe_blocked_tools(events) == DEBUG_PROBE_TOOLS


def test_trusted_file_mutation_restores_two_debug_checks() -> None:
    old_first = _action("verify_web_app")
    old_second = _action("verify_web_app")
    mutation = _action("file_write")
    fresh_probe = _action("verify_web_app")
    events = with_seqs(
        [
            old_first,
            old_second,
            mutation,
            _observation(mutation, changed=True),
            fresh_probe,
        ]
    )

    budget = debug_probe_budget(events)

    assert budget.calls == 1
    assert not budget.exhausted
    assert debug_probe_blocked_tools(events) == frozenset()


def test_notice_is_scoped_to_the_current_trusted_mutation_floor() -> None:
    first = _action("verify_web_app")
    second = _action("design_lint")
    initial = with_seqs([first, second])
    budget = debug_probe_budget(initial)
    marker = StatusEvent(status=ConversationStatus.RUNNING, detail=budget.marker_detail)
    marked = with_seqs([first, second, marker])
    assert debug_probe_budget_notice_active(marked, debug_probe_budget(marked))

    mutation = _action("file_write")
    changed = with_seqs([first, second, marker, mutation, _observation(mutation, changed=True)])
    assert not debug_probe_budget_notice_active(changed, debug_probe_budget(changed))


@pytest.mark.asyncio
async def test_exhausted_debug_tools_are_absent_from_the_next_model_surface() -> None:
    agent = ScriptedAgent([finish_step()])
    executor = FakeExecutor(
        tools=[
            ToolSpec(name="file_write", description="write", parameters_schema={}),
            ToolSpec(name="verify_web_app", description="verify web", parameters_schema={}),
            ToolSpec(name="verify_appkit_app", description="verify appkit", parameters_schema={}),
            ToolSpec(name="design_lint", description="lint design", parameters_schema={}),
        ]
    )
    loop, _store = build_loop(agent, executor=executor)
    await loop.send_message("finish the app")
    for tool in ("verify_web_app", "design_lint"):
        probe = _action(tool)
        await loop._emit(probe)
        await loop._emit(_failed_probe_observation(probe))

    await loop.run()

    assert agent.seen_tools
    assert DEBUG_PROBE_TOOLS.isdisjoint(agent.seen_tools[0])

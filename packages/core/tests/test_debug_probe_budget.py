"""Structured debug tools are bounded by real artifact mutations, not retries."""

from __future__ import annotations

import pytest
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import ToolSpec
from disco.core.loop.control import Disp
from disco.core.loop.no_progress_detector import (
    BROWSER_INTERACTION_BUDGET_DETAIL,
    DEBUG_PROBE_TOOLS,
    browser_interaction_notice_active,
    debug_probe_blocked_tools,
    debug_probe_budget,
    debug_probe_budget_notice_active,
    repeated_browser_interaction_failure,
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


def _browser_action(action: str, **arguments) -> ActionEvent:
    return ActionEvent(
        source=EventSource.AGENT,
        thought="inspect",
        tool_call=ToolCall(
            tool_name="browser",
            arguments={"action": action, **arguments},
        ),
    )


def _browser_failure(action: ActionEvent, reason: str) -> AgentErrorEvent:
    return AgentErrorEvent(
        action_id=action.id,
        error="browser action failed",
        failure_class="browser_action_failed",
        failure_reason=reason,
    )


def _browser_success(action: ActionEvent) -> ObservationEvent:
    return ObservationEvent(
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name="browser",
            success=True,
            content="page",
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


def test_two_same_reason_browser_failures_ignore_selector_spelling() -> None:
    text_click = _browser_action("click", click_text="Play")
    css_click = _browser_action("click", selector="#play")
    events = with_seqs(
        [
            text_click,
            _browser_failure(text_click, "interaction_blocked"),
            css_click,
            _browser_failure(css_click, "interaction_blocked"),
        ]
    )

    failure = repeated_browser_interaction_failure(events)
    assert failure is not None
    assert failure.repeats == 2
    assert debug_probe_blocked_tools(events) == frozenset({"browser"})
    assert failure.marker_detail.startswith(f"{BROWSER_INTERACTION_BUDGET_DETAIL}:")

    mutation = _action("file_write")
    changed = with_seqs([*events, mutation, _observation(mutation, changed=True)])
    assert "browser" not in debug_probe_blocked_tools(changed)

    fill = _browser_action("fill", selector="#name", value="Ada")
    mixed = with_seqs(
        [
            text_click,
            _browser_failure(text_click, "interaction_blocked"),
            fill,
            _browser_failure(fill, "interaction_blocked"),
        ]
    )
    assert repeated_browser_interaction_failure(mixed) is None


def test_successful_browser_state_change_resets_interaction_failures() -> None:
    first = _browser_action("click", selector="#covered")
    navigate = _browser_action("navigate", url="http://127.0.0.1:8000/")
    second = _browser_action("click", click_text="Play")
    events = with_seqs(
        [
            first,
            _browser_failure(first, "interaction_blocked"),
            navigate,
            _browser_success(navigate),
            second,
            _browser_failure(second, "interaction_blocked"),
        ]
    )

    assert repeated_browser_interaction_failure(events) is None
    assert "browser" not in debug_probe_blocked_tools(events)


def test_browser_failure_notice_is_bound_to_the_failure_streak() -> None:
    first = _browser_action("click", selector="#one")
    second = _browser_action("click", selector="#two")
    initial = with_seqs(
        [
            first,
            _browser_failure(first, "selector_not_found"),
            second,
            _browser_failure(second, "selector_not_found"),
        ]
    )
    failure = repeated_browser_interaction_failure(initial)
    assert failure is not None
    marked = with_seqs(
        [
            *initial,
            StatusEvent(status=ConversationStatus.RUNNING, detail=failure.marker_detail),
        ]
    )

    current = repeated_browser_interaction_failure(marked)
    assert current is not None
    assert browser_interaction_notice_active(marked, current)


@pytest.mark.asyncio
async def test_browser_failure_budget_emits_one_wrap_up_notice() -> None:
    loop, store = build_loop(ScriptedAgent([]))
    await loop.send_message("finish the app")
    for selector in ("#one", "#two"):
        action = _browser_action("click", selector=selector)
        await loop._emit(action)
        await loop._emit(_browser_failure(action, "interaction_blocked"))

    assert await loop._valve.gate_no_progress(await store.get_events("conv")) is Disp.CONTINUE
    events = await store.get_events("conv")
    notices = [
        event
        for event in events
        if isinstance(event, MessageEvent)
        and event.meta.get("diagnostic") == BROWSER_INTERACTION_BUDGET_DETAIL
    ]
    assert len(notices) == 1
    assert await loop._valve.gate_no_progress(events) is Disp.FALLTHROUGH


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

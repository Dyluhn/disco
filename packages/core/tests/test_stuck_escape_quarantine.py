"""H415: a read-loop escape must enforce a genuinely different next action."""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import StuckDetector, StuckThresholds, signals
from disco.core.loop.control import Disp
from disco.core.loop.messages import STUCK_ESCAPE_PROGRESS_DIAGNOSTIC
from event_fakes import action, observation
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step


class _CoverageExecutor(FakeExecutor):
    """Return stable numbered bytes so varied read arguments hit coverage."""

    def __init__(self) -> None:
        super().__init__(
            tools=[
                ToolSpec(name="file_read", description="read", parameters_schema={}),
                ToolSpec(name="file_list", description="list", parameters_schema={}),
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="file_append", description="append", parameters_schema={}),
                ToolSpec(name="shell", description="shell", parameters_schema={}),
                ToolSpec(name="shell_exec", description="shell exec", parameters_schema={}),
                ToolSpec(name="code_exec", description="code exec", parameters_schema={}),
                ToolSpec(name="verify_web_app", description="verify app", parameters_schema={}),
                ToolSpec(name="think", description="think", parameters_schema={}),
            ]
        )

    async def execute(self, call):  # noqa: ANN001, ANN201 - fake protocol seam
        self.calls.append(call)
        if call.tool_name == "file_read":
            content = "[lines 1-2 of 2]\n1\tstable-a\n2\tstable-b"
            structured = None
        elif call.tool_name in {"file_write", "file_append"}:
            content = "wrote changed bytes"
            structured = {
                "path": str(call.arguments.get("path") or "styles.css"),
                "sha256": "a" * 64,
            }
        else:
            content = "wrote changed bytes"
            structured = None
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content=content,
            structured=structured,
        )


class _FailFirstWriteCoverageExecutor(_CoverageExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.failed_write = False

    async def execute(self, call):  # noqa: ANN001, ANN201 - fake protocol seam
        if call.tool_name == "file_write" and not self.failed_write:
            self.failed_write = True
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=False,
                content="simulated write failure",
                error="simulated write failure",
            )
        return await super().execute(call)


class _FailFirstVerificationReadExecutor(_CoverageExecutor):
    """Fail the first post-escape verification read, then allow the retry."""

    async def execute(self, call):  # noqa: ANN001, ANN201 - fake protocol seam
        if (
            call.tool_name == "file_read"
            and sum(previous.tool_name == "file_read" for previous in self.calls) == 5
        ):
            self.calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=False,
                content="simulated verification read failure",
                error="simulated verification read failure",
            )
        return await super().execute(call)


def _coverage_reads() -> list:
    # The whole read establishes knowledge; four varied, large-limit requests
    # return the same bytes. They avoid the small-read nudge and exact-action
    # detector while reproducing redundant_read_coverage at its default four.
    return [action_step("file_read", {"path": "index.html"})] + [
        action_step("file_read", {"path": "index.html", "limit": limit})
        for limit in (101, 102, 103, 104)
    ]


def _frozen_h415_read(
    seq: int,
    start: int,
    stop: int,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> list[Event]:
    """Rebuild one exact range from the H415 pre-fix read-loop trace."""
    args: dict[str, object] = {"path": "index.html"}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    read = action(
        thought=f"read {start}-{stop}",
        tool="file_read",
        args=args,
    ).model_copy(update={"seq": seq})
    numbered = "\n".join(f"{line:>3}\tline-{line}" for line in range(start, stop + 1))
    result = observation(
        action_id=read.id,
        tool="file_read",
        content=f"[lines {start}-{stop} of 246]\n{numbered}",
    ).model_copy(update={"seq": seq + 1})
    return [read, result]


def test_h415_frozen_seq_18_47_detector_replay():
    """The exact live range/escape shape still trips at seq 31 and seq 47."""
    detector = StuckDetector()
    events: list[Event] = [
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="build the site"),
        ).model_copy(update={"seq": 1}),
        *[
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail=f"h415_frozen_prefix:{seq}",
            ).model_copy(update={"seq": seq})
            for seq in range(2, 18)
        ],
    ]
    for seq, start, stop, offset, limit in (
        (18, 1, 246, None, None),
        (20, 88, 207, 88, 120),
        (22, 208, 246, 208, None),
        (24, 85, 244, 85, 160),
        (26, 1, 45, 1, 45),
        (28, 46, 246, 46, None),
    ):
        events += _frozen_h415_read(seq, start, stop, offset=offset, limit=limit)
    assert detector.evaluate(events[-detector.required_scan_window() :]).is_stuck is False

    events += _frozen_h415_read(30, 113, 192, offset=113, limit=80)
    first = detector.evaluate(events[-detector.required_scan_window() :])
    assert events[-1].seq == 31
    assert first.reason == "redundant_read_coverage"

    events.extend(
        [
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content="take a genuinely different action"),
            ).model_copy(update={"seq": 32}),
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="stuck_escape",
            ).model_copy(update={"seq": 33}),
        ]
    )
    assert detector.evaluate(events[-detector.required_scan_window() :]).is_stuck is False

    for seq, start, stop, offset, limit in (
        (34, 1, 246, None, None),
        (36, 88, 172, 88, 85),
        (38, 173, 246, 173, 80),
        (40, 1, 95, 1, 95),
        (42, 96, 246, 96, 155),
        (44, 1, 50, 1, 50),
    ):
        events += _frozen_h415_read(seq, start, stop, offset=offset, limit=limit)
        assert detector.evaluate(events[-detector.required_scan_window() :]).is_stuck is False

    events += _frozen_h415_read(46, 51, 150, offset=51, limit=100)
    second = detector.evaluate(events[-detector.required_scan_window() :])
    assert events[-1].seq == 47
    assert second.reason == "redundant_read_coverage"


def _block_details(events) -> list[str]:  # noqa: ANN001
    return [
        event.detail
        for event in events
        if isinstance(event, StatusEvent)
        and isinstance(event.detail, str)
        and event.detail.startswith(signals.STUCK_ESCAPE_BLOCK_DETAIL_PREFIX)
    ]


async def test_read_coverage_escape_quarantines_read_until_productive_action():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_list", {"path": "/workspace"}),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    details = [event.detail for event in events if isinstance(event, StatusEvent)]
    block_index = details.index("stuck_escape_block:file_read")
    assert details[block_index + 1] == "stuck_escape"
    assert signals.stuck_escape_blocked_tools(events) == frozenset({"file_read"})

    escape_tools = set(agent.seen_tools[5])
    assert "file_read" not in escape_tools
    assert {"file_write", "think", "finish"} <= escape_tools
    allowed = loop._driver.allowed_tool_names_for_mode(  # noqa: SLF001 - scope contract
        OperatingMode.LONG_HORIZON,
        available_tools=executor.available_tools(),
        blocked_tools=frozenset({"file_read"}),
    )
    assert "file_read" not in allowed
    assert {"file_write", "think", "finish"} <= allowed
    assert "file_read" not in set(agent.seen_tools[6])
    assert "file_read" in set(agent.seen_tools[7])


async def test_h553_recovery_receipt_allows_one_read_then_forces_deliverable_progress():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_append", {"path": "styles.css", "content": "main{}"}),
            action_step("file_read", {"path": "styles.css"}),
            action_step(
                "file_write",
                {"path": "index.html", "content": "<main>bakery</main>"},
            ),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the bakery landing page")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" not in set(agent.seen_tools[5])
    assert "file_read" in set(agent.seen_tools[6])
    assert "file_read" not in set(agent.seen_tools[7])
    assert "file_read" in set(agent.seen_tools[8])
    assert [call.tool_name for call in executor.calls][-3:] == [
        "file_append",
        "file_read",
        "file_write",
    ]
    assert executor.calls[-1].arguments == {
        "path": "index.html",
        "content": "<main>bakery</main>",
    }
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 6
    assert signals.stuck_escape_refusal_count(events, "file_read") == 0

    index_write = next(
        event
        for event in events
        if isinstance(event, ActionEvent)
        and event.tool_call is not None
        and event.tool_call.tool_name == "file_write"
        and event.tool_call.arguments.get("path") == "index.html"
    )
    reconstructed = [
        event.model_copy()
        for event in events
        if event.seq is not None and index_write.seq is not None and event.seq < index_write.seq
    ]
    assert loop._driver.active_stuck_escape_blocked_tools(reconstructed) == frozenset({"file_read"})


async def test_h553_second_post_receipt_read_is_refused_not_executed():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            action_step("file_read", {"path": "styles.css"}),
            action_step("file_read", {"path": "styles.css", "limit": 105}),
            action_step(
                "file_write",
                {"path": "index.html", "content": "<main>bakery</main>"},
            ),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the bakery landing page")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" not in set(agent.seen_tools[7])
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 6
    assert signals.stuck_escape_refusal_count(events, "file_read") == 1
    refusal = next(
        event
        for event in events
        if isinstance(event, AgentErrorEvent)
        and event.error == "stuck_escape_tool_quarantine:file_read"
    )
    refused_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusal.action_id
    )
    assert refusal.tool_call_id == refused_action.tool_call.call_id
    assert [call.tool_name for call in executor.calls][-1] == "file_write"


async def test_h553_failed_verification_read_does_not_consume_receipt_budget():
    executor = _FailFirstVerificationReadExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            action_step("file_read", {"path": "styles.css"}),
            action_step("file_read", {"path": "styles.css"}),
            action_step(
                "file_write",
                {"path": "index.html", "content": "<main>bakery</main>"},
            ),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the bakery landing page")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" in set(agent.seen_tools[6])
    assert "file_read" in set(agent.seen_tools[7])
    assert "file_read" not in set(agent.seen_tools[8])
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 7


async def test_h553_newer_mutation_replenishes_exactly_one_verification_read():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            action_step("file_read", {"path": "styles.css"}),
            action_step("file_append", {"path": "styles.css", "content": "main{}"}),
            action_step("file_read", {"path": "styles.css"}),
            action_step("file_read", {"path": "styles.css", "limit": 105}),
            action_step(
                "file_write",
                {"path": "index.html", "content": "<main>bakery</main>"},
            ),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the bakery landing page")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" in set(agent.seen_tools[6])
    assert "file_read" not in set(agent.seen_tools[7])
    assert "file_read" in set(agent.seen_tools[8])
    assert "file_read" not in set(agent.seen_tools[9])
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 7
    assert signals.stuck_escape_refusal_count(events, "file_read") == 1
    assert [call.tool_name for call in executor.calls][-4:] == [
        "file_read",
        "file_append",
        "file_read",
        "file_write",
    ]
    reminders = [
        event
        for event in events
        if isinstance(event, MessageEvent)
        and event.meta.get("diagnostic") == STUCK_ESCAPE_PROGRESS_DIAGNOSTIC
    ]
    assert len(reminders) == 2
    assert len({event.meta["verification_observation_seq"] for event in reminders}) == 2


async def test_h533_nonproductive_bridge_cannot_restore_quarantined_read():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_list", {"path": "/workspace"}),
            action_step("file_read", {"path": "index.html", "limit": 105}),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" not in set(agent.seen_tools[5])
    assert "file_read" not in set(agent.seen_tools[6])
    assert "file_read" not in set(agent.seen_tools[7])
    assert "file_read" in set(agent.seen_tools[8])
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert [call.tool_name for call in executor.calls][-2:] == ["file_list", "file_write"]
    assert (
        sum(
            isinstance(event, ActionEvent)
            and event.tool_call is not None
            and event.tool_call.tool_name == "file_read"
            for event in events
        )
        == 6
    )
    refusal = next(
        event
        for event in events
        if isinstance(event, AgentErrorEvent)
        and event.error == "stuck_escape_tool_quarantine:file_read"
    )
    refused_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusal.action_id
    )
    assert refusal.tool_call_id == refused_action.tool_call.call_id


async def test_h533_failed_mutation_does_not_release_quarantine():
    executor = _FailFirstWriteCoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_write", {"path": "styles.css", "content": "broken"}),
            action_step("file_read", {"path": "index.html", "limit": 105}),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" not in set(agent.seen_tools[5])
    assert "file_read" not in set(agent.seen_tools[6])
    assert "file_read" not in set(agent.seen_tools[7])
    assert "file_read" in set(agent.seen_tools[8])
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert sum(call.tool_name == "file_write" for call in executor.calls) == 2
    assert (
        sum(
            isinstance(event, ActionEvent)
            and event.tool_call is not None
            and event.tool_call.tool_name == "file_read"
            for event in events
        )
        == 6
    )


async def test_h536_receiptless_success_cannot_release_quarantine():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("shell", {"command": "pwd"}),
            action_step("file_read", {"path": "index.html", "limit": 105}),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" not in set(agent.seen_tools[5])
    assert "file_read" not in set(agent.seen_tools[6])
    assert "file_read" not in set(agent.seen_tools[7])
    assert "file_read" in set(agent.seen_tools[8])
    assert all("shell" not in set(tools) for tools in agent.seen_tools[5:])
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert [call.tool_name for call in executor.calls][-1] == "file_write"
    assert all(call.tool_name != "shell" for call in executor.calls)
    assert (
        sum(
            isinstance(event, ActionEvent)
            and event.tool_call is not None
            and event.tool_call.tool_name == "file_read"
            for event in events
        )
        == 6
    )
    assert signals.stuck_escape_refusal_count(events, "shell") == 1


async def test_h583_general_exec_bypass_is_refused_then_progress_reminder_drives_deliverable():
    executor = _CoverageExecutor()

    def write_missing_deliverable(view):  # noqa: ANN001, ANN202 - scripted agent seam
        reminders = [
            message
            for message in view.messages
            if "The one allowed `file_read` verification" in message.content
        ]
        assert len(reminders) == 1
        assert "Advance the actual deliverable now" in reminders[0].content
        return action_step(
            "file_write",
            {"path": "index.html", "content": "<main>bakery</main>"},
        )

    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_read", {"path": "index.html", "limit": 105}),
            action_step(
                "shell",
                {"command": "wc -l index.html && sed -n '1,200p' index.html"},
            ),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            action_step("file_read", {"path": "styles.css"}),
            write_missing_deliverable,
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    async def forbidden_fanout(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("delegate_explore bypass reached fan-out execution")

    loop._run_fanout = forbidden_fanout  # noqa: SLF001 - production-wired capability surface

    await loop.send_message("build the bakery landing page")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    for tools in agent.seen_tools[5:]:
        assert {"shell", "shell_exec", "code_exec", "delegate_explore"}.isdisjoint(tools)
    assert {"file_write", "verify_web_app", "serve", "finish"} <= set(agent.seen_tools[5])
    assert "file_read" not in set(agent.seen_tools[5])
    assert "file_read" not in set(agent.seen_tools[6])
    assert "file_read" not in set(agent.seen_tools[7])
    assert "file_read" in set(agent.seen_tools[8])
    assert "file_read" not in set(agent.seen_tools[9])
    assert [call.tool_name for call in executor.calls][-3:] == [
        "file_write",
        "file_read",
        "file_write",
    ]
    assert all(call.tool_name != "shell" for call in executor.calls)
    assert signals.stuck_escape_refusal_count(events, "file_read") == 1
    assert signals.stuck_escape_refusal_count(events, "shell") == 1
    assert not any(
        isinstance(event, AgentErrorEvent)
        and event.error == "stuck_escape_tool_quarantine:file_read"
        and event.seq is not None
        and event.seq
        > next(
            reminder.seq
            for reminder in events
            if isinstance(reminder, MessageEvent)
            and reminder.meta.get("diagnostic") == STUCK_ESCAPE_PROGRESS_DIAGNOSTIC
        )
        for event in events
    )

    reminders = [
        event
        for event in events
        if isinstance(event, MessageEvent)
        and event.source == EventSource.ENVIRONMENT
        and event.meta.get("diagnostic") == STUCK_ESCAPE_PROGRESS_DIAGNOSTIC
    ]
    assert len(reminders) == 1
    verification_seq = reminders[0].meta["verification_observation_seq"]
    verification = next(event for event in events if event.seq == verification_seq)
    assert isinstance(verification, ObservationEvent)
    assert verification.tool_result.tool_name == "file_read"
    assert verification.tool_result.success is True

    restarted, _ = build_loop(ScriptedAgent([finish_step()]), store=store, executor=executor)
    assert (  # noqa: SLF001 - direct restart reconstruction contract
        await restarted._gate_stuck_escape_progression(events)
    ) is Disp.FALLTHROUGH
    after_restart_check = await store.get_events("conv")
    assert len(after_restart_check) == len(events)


def test_h583_general_exec_boundary_is_authenticated_and_resets_on_new_user_turn():
    executor = _CoverageExecutor()
    loop, _store = build_loop(ScriptedAgent([]), executor=executor)
    block = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="stuck_escape_block:file_read",
    ).model_copy(update={"seq": 1})
    escape = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="stuck_escape",
    ).model_copy(update={"seq": 2})

    blocked = loop._driver.stuck_escape_blocked_tools_for_step(  # noqa: SLF001
        [block, escape]
    )
    assert blocked == frozenset(
        {"file_read", "shell", "shell_exec", "code_exec", "delegate_explore"}
    )
    allowed = loop._driver.allowed_tool_names_for_mode(  # noqa: SLF001
        OperatingMode.LONG_HORIZON,
        available_tools=executor.available_tools(),
        blocked_tools=blocked,
    )
    assert {
        "shell",
        "shell_exec",
        "code_exec",
        "delegate_explore",
        "file_read",
    }.isdisjoint(allowed)
    assert {"file_write", "verify_web_app", "serve", "finish"} <= allowed
    spoofed_block = block.model_copy(update={"source": EventSource.AGENT})
    assert (
        loop._driver.stuck_escape_blocked_tools_for_step(  # noqa: SLF001
            [spoofed_block, escape]
        )
        == frozenset()
    )
    new_turn = MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content="continue with a revised request"),
    ).model_copy(update={"seq": 3})
    assert (
        loop._driver.stuck_escape_blocked_tools_for_step(  # noqa: SLF001
            [block, escape, new_turn]
        )
        == frozenset()
    )


async def test_h587_delegate_explore_cannot_proxy_quarantined_file_read():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step(
                "delegate_explore",
                {
                    "question": "Read index.html and return the missing middle section",
                    "context": "Use file_read on index.html",
                },
            ),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)
    fanout_calls = 0

    async def fanout(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal fanout_calls
        fanout_calls += 1
        raise AssertionError("quarantined delegate_explore reached fan-out execution")

    loop._run_fanout = fanout  # noqa: SLF001 - production-wired capability surface

    await loop.send_message("build the bakery landing page")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "delegate_explore" not in set(agent.seen_tools[5])
    assert fanout_calls == 0
    assert signals.stuck_escape_refusal_count(events, "delegate_explore") == 1
    refusal = next(
        event
        for event in events
        if isinstance(event, AgentErrorEvent)
        and event.error == "stuck_escape_tool_quarantine:delegate_explore"
    )
    refused_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusal.action_id
    )
    assert refused_action.tool_call.tool_name == "delegate_explore"
    assert refusal.tool_call_id == refused_action.tool_call.call_id


async def test_hallucinated_quarantined_read_is_paired_not_executed():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_read", {"path": "index.html", "limit": 105}),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert "file_read" not in set(agent.seen_tools[5])
    assert "file_read" not in set(agent.seen_tools[6])
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert (
        sum(
            isinstance(event, ActionEvent)
            and event.tool_call is not None
            and event.tool_call.tool_name == "file_read"
            for event in events
        )
        == 6
    )
    assert [call.tool_name for call in executor.calls][-1] == "file_write"
    refusals = [
        event
        for event in events
        if isinstance(event, AgentErrorEvent)
        and event.error == "stuck_escape_tool_quarantine:file_read"
    ]
    assert len(refusals) == 1
    refused_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusals[0].action_id
    )
    assert refusals[0].tool_call_id == refused_action.tool_call.call_id
    assert signals.stuck_escape_refusal_count(events, "file_read") == 1


async def test_quarantined_read_requery_exhaustion_lands_explicitly():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_read", {"path": "index.html", "limit": 105}),
            action_step("shell", {"command": "mkdir -p /workspace/js"}),
            action_step("file_read", {"path": "index.html", "limit": 106}),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert all(call.tool_name != "shell" for call in executor.calls)
    assert signals.stuck_escape_refusal_count(events, "shell") == 1
    assert signals.stuck_escape_refusal_count(events, "file_read") == 2
    refused_actions = [
        event
        for event in events
        if isinstance(event, ActionEvent)
        and event.seq is not None
        and signals.stuck_escape_seq(events) is not None
        and event.seq > signals.stuck_escape_seq(events)
        and event.tool_call.tool_name == "file_read"
    ]
    assert len(refused_actions) == 2
    assert all(
        any(
            isinstance(error, AgentErrorEvent)
            and error.action_id == action.id
            and error.tool_call_id == action.tool_call.call_id
            for error in events
        )
        for action in refused_actions
    )
    assert any(
        isinstance(event, StatusEvent)
        and event.meta.get("blocked_reason") == "stuck_escape_tool_quarantine"
        for event in events
    )


def test_escape_refusal_count_requires_current_exact_paired_events():
    old_escape = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="stuck_escape",
    ).model_copy(update={"seq": 1})
    stale_action = action(tool="file_read", args={"path": "old"}).model_copy(update={"seq": 2})
    stale_error = AgentErrorEvent(
        error="stuck_escape_tool_quarantine:file_read",
        action_id=stale_action.id,
        tool_call_id=stale_action.tool_call.call_id,
    ).model_copy(update={"seq": 3})
    current_escape = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="stuck_escape",
    ).model_copy(update={"seq": 4})
    current_action = action(tool="file_read", args={"path": "current"}).model_copy(
        update={"seq": 5}
    )
    unpaired = AgentErrorEvent(
        error="stuck_escape_tool_quarantine:file_read",
        action_id=current_action.id,
        tool_call_id="wrong-call-id",
    ).model_copy(update={"seq": 6})
    different_code = AgentErrorEvent(
        error="stuck_escape_tool_quarantine:file_list",
        action_id=current_action.id,
        tool_call_id=current_action.tool_call.call_id,
    ).model_copy(update={"seq": 7})
    paired = AgentErrorEvent(
        error="stuck_escape_tool_quarantine:file_read",
        action_id=current_action.id,
        tool_call_id=current_action.tool_call.call_id,
    ).model_copy(update={"seq": 8})

    events = [
        old_escape,
        stale_action,
        stale_error,
        current_escape,
        current_action,
        unpaired,
        different_code,
        paired,
    ]

    assert signals.stuck_escape_refusal_count(events, "file_read") == 1
    assert signals.stuck_escape_refusal_count(events, "file_list") == 0


def test_escape_block_marker_requires_exact_system_adjacency():
    block = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="stuck_escape_block:file_read",
    )
    escape = StatusEvent(status=ConversationStatus.RUNNING, detail="stuck_escape")

    assert signals.stuck_escape_blocked_tools([block, escape]) == frozenset({"file_read"})
    assert (
        signals.stuck_escape_blocked_tools(
            [block.model_copy(update={"source": EventSource.AGENT}), escape]
        )
        == frozenset()
    )
    intervening = MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content="not an adjacent marker"),
    )
    assert signals.stuck_escape_blocked_tools([block, intervening, escape]) == frozenset()
    malformed = block.model_copy(update={"detail": "stuck_escape_block:file-read"})
    assert signals.stuck_escape_blocked_tools([malformed, escape]) == frozenset()
    unsupported = block.model_copy(update={"detail": "stuck_escape_block:finish"})
    assert signals.stuck_escape_blocked_tools([unsupported, escape]) == frozenset()
    non_system_escape = escape.model_copy(update={"source": EventSource.AGENT})
    assert signals.stuck_escape_blocked_tools([block, non_system_escape]) == frozenset()


def test_h553_delayed_pre_escape_mutation_result_cannot_open_verification_budget():
    executor = _CoverageExecutor()
    loop, _store = build_loop(ScriptedAgent([]), executor=executor)
    stale_write = action(
        tool="file_write",
        args={"path": "styles.css", "content": "body{}"},
    ).model_copy(update={"seq": 1})
    block = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="stuck_escape_block:file_read",
    ).model_copy(update={"seq": 2})
    escape = StatusEvent(
        status=ConversationStatus.RUNNING,
        detail="stuck_escape",
    ).model_copy(update={"seq": 3})
    delayed_result = ObservationEvent(
        source=EventSource.ENVIRONMENT,
        action_id=stale_write.id,
        tool_result=ToolResult(
            call_id=stale_write.tool_call.call_id,
            tool_name="file_write",
            success=True,
            content="wrote changed bytes",
            structured={"path": "styles.css", "sha256": "a" * 64},
        ),
    ).model_copy(update={"seq": 4})

    assert loop._driver.active_stuck_escape_blocked_tools(  # noqa: SLF001
        [stale_write, block, escape, delayed_result]
    ) == frozenset({"file_read"})


async def test_non_read_stuck_reason_preserves_existing_escape_surface():
    executor = FakeExecutor()
    agent = ScriptedAgent([action_step()] * 6 + [finish_step()])
    loop, store = build_loop(
        agent,
        executor=executor,
        stuck_thresholds=StuckThresholds(repeat_action_observation=3),
    )

    await loop.send_message("repeat please")
    await loop.run()
    events = await store.get_events("conv")

    assert _block_details(events) == []
    assert signals.stuck_escape_blocked_tools(events) == frozenset()
    assert "shell" in set(agent.seen_tools[3])

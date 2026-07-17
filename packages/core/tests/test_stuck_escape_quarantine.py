"""H415: a read-loop escape must enforce a genuinely different next action."""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ConversationStatus,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    ToolResult,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import StuckDetector, StuckThresholds, signals
from event_fakes import action, observation
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step


class _CoverageExecutor(FakeExecutor):
    """Return stable numbered bytes so varied read arguments hit coverage."""

    def __init__(self) -> None:
        super().__init__(
            tools=[
                ToolSpec(name="file_read", description="read", parameters_schema={}),
                ToolSpec(name="file_write", description="write", parameters_schema={}),
                ToolSpec(name="think", description="think", parameters_schema={}),
            ]
        )

    async def execute(self, call):  # noqa: ANN001, ANN201 - fake protocol seam
        self.calls.append(call)
        if call.tool_name == "file_read":
            content = "[lines 1-2 of 2]\n1\tstable-a\n2\tstable-b"
        else:
            content = "wrote changed bytes"
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content=content,
        )


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


async def test_read_coverage_escape_quarantines_read_for_exactly_one_action():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
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
    assert "file_read" in set(agent.seen_tools[6])


async def test_hallucinated_quarantined_read_is_requeried_not_executed():
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
        == 5
    )
    assert [call.tool_name for call in executor.calls][-1] == "file_write"


async def test_quarantined_read_requery_exhaustion_lands_explicitly():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_read", {"path": "index.html", "limit": limit})
            for limit in (105, 106, 107)
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert not any(
        isinstance(event, ActionEvent)
        and event.seq is not None
        and signals.stuck_escape_seq(events) is not None
        and event.seq > signals.stuck_escape_seq(events)
        for event in events
    )
    assert any(
        isinstance(event, StatusEvent)
        and event.meta.get("blocked_reason") == "stuck_escape_tool_quarantine"
        for event in events
    )


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

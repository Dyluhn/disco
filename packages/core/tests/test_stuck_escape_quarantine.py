"""H415/F1: a read-loop escape must enforce a genuinely different next action.

The k6g 128k canary (2026-07-19, finding F1) replaced the tool-name ration
("one allowed `file_read` per changed-state receipt") with a semantic
redundancy guard: during an active recovery episode, ``file_read`` STAYS
OFFERED, and a call is refused only when every requested line is provably
already held — same canonical resource, same content digest, receipt-proven
line coverage, still visible (not condensed away). Reads of different
resources, changed resources, unseen ranges, and post-mutation rereads
execute normally. The read-BYPASS aliases (general shell/code execution and
delegated exploration) stay schema-withheld only while the episode has zero
trusted changed-state receipts.
"""

from __future__ import annotations

import hashlib

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
from disco.core.effects import (
    CoverageSpan,
    CoverageUnit,
    MutationReceipt,
    ObservationReceipt,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
)
from disco.core.effects import (
    EffectCapability as _EC,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import StuckDetector, StuckThresholds, signals
from event_fakes import action, observation
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step

_BYPASS_TOOLS = {"shell", "shell_exec", "code_exec", "delegate_explore"}


def _digest(path: str, version: int) -> str:
    return hashlib.sha256(f"{path}:{version}".encode()).hexdigest()


def _read_receipt(path: str, version: int, *, total: int = 2) -> ObservationReceipt:
    return ObservationReceipt(
        capability=_EC.WORKSPACE_CONTENT_READ,
        revision=ResourceRevision(
            resource=ResourceKey(namespace="workspace.file", identifier=path),
            digest=_digest(path, version),
        ),
        coverage=ResourceCoverage(
            unit=CoverageUnit.LINES,
            spans=(CoverageSpan(start=0, end=total),),
            total=total,
        ),
        complete=True,
    )


def _mutation_receipt(path: str, version: int) -> MutationReceipt:
    return MutationReceipt(
        resource=ResourceKey(namespace="workspace.file", identifier=path),
        after=ResourceRevision(
            resource=ResourceKey(namespace="workspace.file", identifier=path),
            digest=_digest(path, version),
        ),
        after_size_bytes=32,
    )


class _CoverageExecutor(FakeExecutor):
    """Mirror the REAL file-tool receipt contract (verified against the k6g
    durable events): reads emit ObservationReceipts with the current content
    digest + exact line coverage; mutations emit MutationReceipts whose
    ``after`` digest advances the resource revision. Content bytes stay stable
    per version so varied read arguments hit redundancy exactly like a real
    unchanged file."""

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
        self.versions: dict[str, int] = {}

    async def execute(self, call):  # noqa: ANN001, ANN201 - fake protocol seam
        self.calls.append(call)
        path = str(call.arguments.get("path") or "styles.css")
        if call.tool_name == "file_read":
            version = self.versions.get(path, 1)
            content = "[lines 1-2 of 2]\n1\tstable-a\n2\tstable-b"
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content=content,
                effect_receipts=(_read_receipt(path, version),),
            )
        if call.tool_name in {"file_write", "file_append"}:
            version = self.versions.get(path, 1) + 1
            self.versions[path] = version
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                success=True,
                content="wrote changed bytes",
                structured={"path": path, "sha256": "a" * 64},
                effect_receipts=(_mutation_receipt(path, version),),
            )
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="ran",
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
    """Fail the first post-escape read so the retry contract is provable."""

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


def _read_refusals(events) -> list[AgentErrorEvent]:  # noqa: ANN001
    return [
        event
        for event in events
        if isinstance(event, AgentErrorEvent)
        and event.error == "stuck_escape_tool_quarantine:file_read"
    ]


async def test_read_escape_keeps_read_offered_and_refuses_only_redundant_targets():
    """F1 core: the escape arms, `file_read` STAYS OFFERED, a different-file
    read executes, and only the provably redundant reread is refused."""
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_read", {"path": "app.js"}),  # different resource → executes
            action_step("file_read", {"path": "index.html", "limit": 105}),  # redundant
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

    # The escape-turn schema keeps the observation tool and withholds only the
    # bypass aliases while the episode has zero trusted receipts.
    escape_tools = set(agent.seen_tools[5])
    assert "file_read" in escape_tools
    assert _BYPASS_TOOLS.isdisjoint(escape_tools)
    assert {"file_write", "think", "finish"} <= escape_tools

    # The different-resource read EXECUTED; the redundant one did not.
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 6
    assert executor.calls[5].tool_name == "file_read"
    assert executor.calls[5].arguments == {"path": "app.js"}
    refusals = _read_refusals(events)
    assert len(refusals) == 1
    refused_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusals[0].action_id
    )
    assert refused_action.tool_call.arguments.get("path") == "index.html"
    assert refusals[0].tool_call_id == refused_action.tool_call.call_id
    # The refusal names the scope, the reset condition, and real alternatives.
    assert "index.html" in (refusals[0].detail or "")
    assert "unchanged" in (refusals[0].detail or "")
    # After the trusted receipt lands (the write executes during step 7), the
    # bypass aliases return to the NEXT step's schema.
    assert "shell" not in set(agent.seen_tools[7])
    assert "shell" in set(agent.seen_tools[8])


async def test_mutation_then_reread_executes_and_identical_reread_is_refused():
    """F1 acceptance: mutation-then-reread is progress (new digest); repeating
    the identical, unchanged read afterwards is still detected and bounded."""
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_append", {"path": "styles.css", "content": "main{}"}),
            action_step("file_read", {"path": "styles.css"}),  # digest advanced → executes
            action_step("file_read", {"path": "styles.css", "limit": 105}),  # redundant now
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
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 6
    assert executor.calls[6].tool_name == "file_read"
    assert executor.calls[6].arguments == {"path": "styles.css"}
    refusals = _read_refusals(events)
    assert len(refusals) == 1
    assert signals.stuck_escape_refusal_count(events, "file_read", action_path="styles.css") == 1
    assert signals.stuck_escape_refusal_count(events, "file_read", action_path="index.html") == 0
    # The ration machinery is gone: no "one allowed read" reminders exist.
    assert not any(
        isinstance(event, MessageEvent) and "one allowed" in (event.message.content or "")
        for event in events
    )


async def test_failed_read_leaves_no_coverage_and_the_retry_executes():
    """A FAILED read proves nothing was delivered; the retry must execute."""
    executor = _FailFirstVerificationReadExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_append", {"path": "styles.css", "content": "main{}"}),
            action_step("file_read", {"path": "styles.css"}),  # fails (executor)
            action_step("file_read", {"path": "styles.css"}),  # retry → executes
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
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 7
    assert _read_refusals(events) == []


async def test_h533_nonproductive_bridge_cannot_restore_quarantined_read():
    """A read-only bridge (file_list) is not progress: the redundant reread is
    still refused and the bypass aliases stay withheld."""
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
    # file_list executed, but the identical index.html reread was refused.
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert _BYPASS_TOOLS.isdisjoint(set(agent.seen_tools[6]))
    refusals = _read_refusals(events)
    assert len(refusals) == 1
    refused_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusals[0].action_id
    )
    assert refusals[0].tool_call_id == refused_action.tool_call.call_id


async def test_h533_failed_mutation_does_not_release_quarantine():
    executor = _FailFirstWriteCoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("file_write", {"path": "styles.css", "content": "broken"}),  # fails
            action_step("file_read", {"path": "index.html", "limit": 105}),  # still redundant
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert sum(call.tool_name == "file_write" for call in executor.calls) == 2
    # The failed write is not a trusted receipt: bypass aliases stay withheld
    # for the step after it, and the redundant reread was refused.
    assert _BYPASS_TOOLS.isdisjoint(set(agent.seen_tools[6]))
    assert len(_read_refusals(events)) == 1


async def test_h536_receiptless_success_cannot_release_quarantine():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step("shell", {"command": "pwd"}),  # bypass alias → refused
            action_step("file_read", {"path": "index.html", "limit": 105}),  # redundant
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert all(call.tool_name != "shell" for call in executor.calls)
    assert signals.stuck_escape_refusal_count(events, "shell") == 1
    assert sum(call.tool_name == "file_read" for call in executor.calls) == 5
    assert len(_read_refusals(events)) == 1
    # After the trusted write receipt the ordinary surface returns.
    assert "shell" in set(agent.seen_tools[8])


async def test_h583_general_exec_bypass_is_refused_while_episode_has_no_progress():
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        _coverage_reads()
        + [
            action_step(
                "shell",
                {"command": "wc -l index.html && sed -n '1,200p' index.html"},
            ),
            action_step("file_write", {"path": "styles.css", "content": "body{}"}),
            action_step("file_read", {"path": "styles.css"}),  # digest advanced → executes
            action_step(
                "file_write",
                {"path": "index.html", "content": "<main>bakery</main>"},
            ),
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
    # Zero-progress window: bypass aliases are schema-withheld and the shell
    # attempt is refused without execution.
    assert _BYPASS_TOOLS.isdisjoint(set(agent.seen_tools[5]))
    assert all(call.tool_name != "shell" for call in executor.calls)
    assert signals.stuck_escape_refusal_count(events, "shell") == 1
    # After the styles.css receipt, general execution returns to the schema.
    assert "shell" in set(agent.seen_tools[7])
    # The post-mutation reread executed (no ration, digest changed).
    assert executor.calls[-2].tool_name == "file_read"
    assert executor.calls[-2].arguments == {"path": "styles.css"}
    assert signals.stuck_escape_refusal_count(events, "file_read") == 0


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
    # F1: the observation tool itself is never schema-withheld; only the
    # bypass aliases are, and only while the episode has zero receipts.
    assert blocked == frozenset({"shell", "shell_exec", "code_exec", "delegate_explore"})
    allowed = loop._driver.allowed_tool_names_for_mode(  # noqa: SLF001
        OperatingMode.LONG_HORIZON,
        available_tools=executor.available_tools(),
        blocked_tools=blocked,
    )
    assert _BYPASS_TOOLS.isdisjoint(allowed)
    assert {"file_read", "file_write", "verify_web_app", "serve", "finish"} <= allowed
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


async def test_redundant_read_is_paired_not_executed():
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
    refusals = _read_refusals(events)
    assert len(refusals) == 1
    refused_action = next(
        event
        for event in events
        if isinstance(event, ActionEvent) and event.id == refusals[0].action_id
    )
    assert refusals[0].tool_call_id == refused_action.tool_call.call_id
    assert signals.stuck_escape_refusal_count(events, "file_read") == 1


async def test_same_target_refusal_exhaustion_lands_explicitly():
    """Two refusals of the SAME redundant resource still stop spend."""
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
    assert signals.stuck_escape_refusal_count(events, "file_read", action_path="index.html") == 2
    assert any(
        isinstance(event, StatusEvent)
        and event.meta.get("blocked_reason") == "stuck_escape_tool_quarantine"
        for event in events
    )


async def test_different_target_refusals_do_not_stack_to_stuck():
    """F1: refusals of two DIFFERENT redundant resources are two recoverable
    errors, never one repeated behavior — the run continues and finishes."""
    executor = _CoverageExecutor()
    agent = ScriptedAgent(
        [action_step("file_read", {"path": "styles.css"})]
        + _coverage_reads()
        + [
            action_step("file_read", {"path": "styles.css", "limit": 105}),  # redundant #1
            action_step("file_read", {"path": "index.html", "limit": 105}),  # redundant #2
            action_step("file_write", {"path": "app.js", "content": "init()"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)

    await loop.send_message("build the site")
    state = await loop.run()
    events = await store.get_events("conv")

    assert state.execution_status == ConversationStatus.FINISHED
    assert signals.stuck_escape_refusal_count(events, "file_read") == 2
    assert signals.stuck_escape_refusal_count(events, "file_read", action_path="styles.css") == 1
    assert signals.stuck_escape_refusal_count(events, "file_read", action_path="index.html") == 1
    assert not any(
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
    # Path scoping: only the paired action's exact normalized resource counts.
    assert signals.stuck_escape_refusal_count(events, "file_read", action_path="current") == 1
    assert signals.stuck_escape_refusal_count(events, "file_read", action_path="old") == 0
    assert (
        signals.stuck_escape_refusal_count(events, "file_read", action_path="workspace/current")
        == 0
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


def test_delayed_pre_escape_mutation_result_cannot_restore_bypass_tools():
    """A pre-escape action's late result is not post-escape progress."""
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

    assert loop._driver.stuck_escape_blocked_tools_for_step(  # noqa: SLF001
        [stale_write, block, escape, delayed_result]
    ) == frozenset({"shell", "shell_exec", "code_exec", "delegate_explore"})


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

"""Cross-contract acceptance gate — agent-loop-contract.md §10.9.

End-to-end with fakes, composing THREE contracts: a user message → run() → the
real RouterAgent builds a CompletionRequest(AGENT_DRIVER) to the (faked) router →
the loop gates per policy → executes via the fake executor → appends an
observation → the agent declares finished → FINISHED. Asserts the event log
replays to the same final ConversationState and that request_id flowed into
ActionEvent.llm_response_id.
"""

from __future__ import annotations

from disco.core import (
    ActionEvent,
    ConversationState,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    StatusEvent,
)
from disco.core.llm import (
    DefaultLLMRouter,
    LLMContentFiltered,
    OperatingMode,
    ProposedToolCall,
    ToolSpec,
)
from disco.core.llm.types import EMPTY_REASONING_ONLY_METADATA_KEY
from disco.core.loop import BuildAgent, NeverConfirm, RouterAgent
from llm_fakes import FakeModelProvider, simple_config  # the router-contract test config
from loop_fakes import FakeExecutor, SequenceProvider, build_loop

CID = "conv"
_EMPTY_REASONING_REPAIR_REMINDER = (
    "Your previous response produced no visible text and no tool call — call exactly "
    "one tool now, or say in plain text what you need."
)
_PROSE_NOOP_REPAIR_DIAGNOSTIC = "prose_noop_repair"
_PROSE_NOOP_REPAIR_REMINDER = (
    "You described the next action instead of performing it — call the tool for it "
    "in THIS turn."
)
_READ_CHURN_NUDGE_DIAGNOSTIC = "read_churn_nudge"


def _empty_reasoning_response(reasoning_len: int = 17) -> dict:
    return {
        "text": "",
        "finish_reason": "stop",
        "response_metadata": {
            EMPTY_REASONING_ONLY_METADATA_KEY: {
                "finish_reason": "stop",
                "content_len": 0,
                "reasoning_len": reasoning_len,
                "tool_call_count": 0,
            }
        },
    }


def _diagnostic_events(events: list):
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == EMPTY_REASONING_ONLY_METADATA_KEY
    ]


def _prose_noop_diagnostic_events(events: list):
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == _PROSE_NOOP_REPAIR_DIAGNOSTIC
    ]


def _read_churn_diagnostic_events(events: list):
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == _READ_CHURN_NUDGE_DIAGNOSTIC
    ]


def _tool_specs(*names: str) -> list[ToolSpec]:
    return [
        ToolSpec(name=name, description=name, parameters_schema={})
        for name in names
    ]


def _tool_step(tool_name: str, arguments: dict) -> dict:
    return {
        "tool_calls": [
            ProposedToolCall(tool_name=tool_name, arguments=arguments),
        ]
    }


def _small_read_step(path: str = "index.html", *, offset: int = 1, limit: int = 6) -> dict:
    return _tool_step("file_read", {"path": path, "offset": offset, "limit": limit})


def _whole_read_step(path: str = "index.html") -> dict:
    return _tool_step("file_read", {"path": path})


def _finish_tool_step(summary: str = "done") -> dict:
    return _tool_step("finish", {"summary": summary})


async def _run_scripted_execution(
    steps: list[dict],
    *,
    tool_names: tuple[str, ...] = ("file_read",),
):
    provider = SequenceProvider(steps)
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor(tools=_tool_specs(*tool_names))
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())
    await loop.send_message("work")
    state = await loop.run()
    return state, await store.get_events(CID), provider, executor


async def _seed_approved_incomplete_plan(store):
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="build it"),
        ),
    )
    await store.append(CID, PlanEvent(summary="build it", steps=[{"title": "ship"}]))
    await store.append(
        CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )


async def test_loop_router_event_contracts_compose():
    # Faked router: call 1 proposes a shell tool call; call 2 calls the `finish`
    # tool — the new AFFIRMATIVE terminal move (GAP B). A tool-less prose turn no
    # longer ends the run; only `finish` does.
    provider = SequenceProvider(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls -la"})]},
            {
                "text": "all done",
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed the files"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = RouterAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("list the files")
    state = await loop.run()

    # Terminal: FINISHED.
    assert state.execution_status == ConversationStatus.FINISHED

    # The tool actually executed with the model's arguments.
    assert len(executor.calls) == 1
    assert executor.calls[0].tool_name == "shell"
    assert executor.calls[0].arguments == {"cmd": "ls -la"}

    events = await store.get_events(CID)

    # request_id flowed: router echoed it; the loop carried it into the event.
    action = next(e for e in events if isinstance(e, ActionEvent))
    sent_request_id = provider.seen[0].request_id
    assert sent_request_id is not None
    assert action.llm_response_id == sent_request_id

    # The agent declared AGENT_DRIVER role to the router (capability routing).
    from disco.core.llm import ModelRole

    assert provider.seen[0].profile.role == ModelRole.AGENT_DRIVER

    # Determinism: the full log replays to the same final ConversationState.
    replayed = ConversationState.reconstruct(CID, events)
    assert replayed.execution_status == ConversationStatus.FINISHED
    assert replayed == ConversationState.reconstruct(CID, events)  # pure


async def test_empty_reasoning_turn_retries_once_and_executes_tool():
    provider = SequenceProvider(
        [
            _empty_reasoning_response(reasoning_len=23),
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("list the files")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert [c.tool_name for c in executor.calls] == ["shell"]
    assert provider.calls == 3
    assert provider.seen[1].messages[-1].content == _EMPTY_REASONING_REPAIR_REMINDER

    events = await store.get_events(CID)
    diagnostics = _diagnostic_events(events)
    assert len(diagnostics) == 1
    assert diagnostics[0].meta["finish_reason"] == "stop"
    assert diagnostics[0].meta["content_len"] == 0
    assert diagnostics[0].meta["reasoning_len"] == 23
    assert diagnostics[0].meta["tool_call_count"] == 0
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.PAUSED
        and e.detail == "actionless"
        for e in events
    )


async def test_truly_blank_turn_retries_once_and_executes_tool():
    provider = SequenceProvider(
        [
            _empty_reasoning_response(reasoning_len=0),
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("list the files")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert [c.tool_name for c in executor.calls] == ["shell"]
    assert provider.calls == 3
    assert provider.seen[1].messages[-1].content == _EMPTY_REASONING_REPAIR_REMINDER

    diagnostics = _diagnostic_events(await store.get_events(CID))
    assert len(diagnostics) == 1
    assert diagnostics[0].meta["reasoning_len"] == 0


async def test_empty_reasoning_retry_empty_counts_into_actionless_breaker():
    provider = SequenceProvider(
        [
            _empty_reasoning_response(reasoning_len=11),
            _empty_reasoning_response(reasoning_len=13),
            {"text": ""},
            {"text": ""},
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    loop.mode = OperatingMode.LONG_HORIZON
    await _seed_approved_incomplete_plan(store)

    state = await loop.run()

    events = await store.get_events(CID)
    assert len(_diagnostic_events(events)) == 2
    assert any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.PAUSED
        and e.detail == "actionless"
        for e in events
    )
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert provider.calls == 4


async def test_prose_noop_in_execution_retries_once_and_executes_tool():
    provider = SequenceProvider(
        [
            {"text": "Let me list the files next."},
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("list the files")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert [c.tool_name for c in executor.calls] == ["shell"]
    assert provider.calls == 3
    assert provider.seen[1].messages[-1].content == _PROSE_NOOP_REPAIR_REMINDER

    events = await store.get_events(CID)
    diagnostics = _prose_noop_diagnostic_events(events)
    assert len(diagnostics) == 1
    assert diagnostics[0].meta["content_len"] == len("Let me list the files next.")
    assert not any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and e.message.content == "Let me list the files next."
        for e in events
    )
    assert not any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.PAUSED
        and e.detail == "actionless"
        for e in events
    )


async def test_prose_noop_retry_prose_again_counts_into_actionless_breaker():
    provider = SequenceProvider(
        [
            {"text": "I will inspect the files next."},
            {"text": "Still about to inspect."},
            {"text": "ordinary no-op 2"},
            {"text": "ordinary no-op 3"},
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    loop.mode = OperatingMode.LONG_HORIZON
    await _seed_approved_incomplete_plan(store)

    state = await loop.run()

    events = await store.get_events(CID)
    assert len(_prose_noop_diagnostic_events(events)) == 1
    assert [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and (
            e.message.content.startswith("Still")
            or e.message.content.startswith("ordinary")
        )
    ] == ["Still about to inspect.", "ordinary no-op 2", "ordinary no-op 3"]
    assert any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.PAUSED
        and e.detail == "actionless"
        for e in events
    )
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert provider.calls == 4


async def test_same_path_small_file_reads_warn_then_feed_actionless_ladder():
    provider = SequenceProvider(
        [_small_read_step(offset=i) for i in range(1, 24)]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor(tools=_tool_specs("file_read"))
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())
    loop.mode = OperatingMode.LONG_HORIZON
    await _seed_approved_incomplete_plan(store)

    state = await loop.run()

    events = await store.get_events(CID)
    diagnostics = _read_churn_diagnostic_events(events)
    assert [d.meta["count"] for d in diagnostics] == [5, 10, 15]
    assert all(d.meta["path"] == "index.html" for d in diagnostics)
    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert any(
        isinstance(e, StatusEvent)
        and e.status == ConversationStatus.PAUSED
        and e.detail == "actionless"
        for e in events
    )


async def test_read_churn_resets_after_edit_before_warning_threshold():
    steps = (
        [_small_read_step(offset=i) for i in range(1, 5)]
        + [
            _tool_step(
                "file_edit",
                {"path": "index.html", "old": "before", "new": "after"},
            )
        ]
        + [_small_read_step(offset=i) for i in range(5, 9)]
        + [_finish_tool_step()]
    )

    state, events, _provider, executor = await _run_scripted_execution(
        steps, tool_names=("file_read", "file_edit")
    )

    assert state.execution_status == ConversationStatus.FINISHED
    assert [call.tool_name for call in executor.calls] == [
        "file_read",
        "file_read",
        "file_read",
        "file_read",
        "file_edit",
        "file_read",
        "file_read",
        "file_read",
        "file_read",
    ]
    assert _read_churn_diagnostic_events(events) == []


async def test_read_churn_whole_file_read_resets_streak_after_first_warning():
    steps = (
        [_small_read_step(offset=i) for i in range(1, 7)]
        + [_whole_read_step()]
        + [_small_read_step(offset=i) for i in range(7, 11)]
        + [_finish_tool_step()]
    )

    state, events, _provider, _executor = await _run_scripted_execution(steps)

    assert state.execution_status == ConversationStatus.FINISHED
    diagnostics = _read_churn_diagnostic_events(events)
    assert [d.meta["count"] for d in diagnostics] == [5]


async def test_read_churn_alternating_paths_do_not_warn():
    steps = [
        _small_read_step("index.html" if i % 2 == 0 else "styles.css", offset=i)
        for i in range(1, 13)
    ] + [_finish_tool_step()]

    state, events, _provider, _executor = await _run_scripted_execution(steps)

    assert state.execution_status == ConversationStatus.FINISHED
    assert _read_churn_diagnostic_events(events) == []


async def test_read_churn_full_file_reads_do_not_warn():
    steps = [_whole_read_step() for _ in range(3)] + [_finish_tool_step()]

    state, events, _provider, _executor = await _run_scripted_execution(steps)

    assert state.execution_status == ConversationStatus.FINISHED
    assert _read_churn_diagnostic_events(events) == []


async def test_read_churn_planning_mode_reads_do_not_warn():
    provider = SequenceProvider(
        [_small_read_step(offset=i) for i in range(1, 7)]
        + [
            _tool_step(
                "submit_plan",
                {"summary": "p", "steps": [{"title": "do it"}]},
            )
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor(tools=_tool_specs("file_read"))
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())
    loop.mode = OperatingMode.PLANNING

    await loop.send_message("plan")
    state = await loop.run()

    events = await store.get_events(CID)
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert _read_churn_diagnostic_events(events) == []


async def test_read_churn_rearms_after_reset():
    steps = (
        [_small_read_step(offset=i) for i in range(1, 6)]
        + [
            _tool_step(
                "file_edit",
                {"path": "index.html", "old": "before", "new": "after"},
            )
        ]
        + [_small_read_step(offset=i) for i in range(6, 11)]
        + [_finish_tool_step()]
    )

    state, events, _provider, _executor = await _run_scripted_execution(
        steps, tool_names=("file_read", "file_edit")
    )

    assert state.execution_status == ConversationStatus.FINISHED
    diagnostics = _read_churn_diagnostic_events(events)
    assert [d.meta["count"] for d in diagnostics] == [5, 5]
    assert [d.meta["path"] for d in diagnostics] == ["index.html", "index.html"]


async def test_second_prose_noop_in_same_execution_segment_gets_no_second_nudge():
    provider = SequenceProvider(
        [
            {"text": "Let me list the files next."},
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls"})]},
            {"text": "Now I will check the current directory."},
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "pwd"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "checked"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("inspect the workspace")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert [c.tool_name for c in executor.calls] == ["shell", "shell"]
    events = await store.get_events(CID)
    assert len(_prose_noop_diagnostic_events(events)) == 1
    assert sum(
        1
        for req in provider.seen
        if req.messages and req.messages[-1].content == _PROSE_NOOP_REPAIR_REMINDER
    ) == 1
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and e.message.content == "Now I will check the current directory."
        for e in events
    )


async def test_planning_mode_prose_turn_gets_no_prose_noop_nudge():
    provider = SequenceProvider(
        [
            {"text": "I will gather context and then submit a plan."},
            {
                "tool_calls": [
                    ProposedToolCall(
                        tool_name="submit_plan",
                        arguments={"summary": "p", "steps": [{"title": "do it"}]},
                    )
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())
    loop.mode = OperatingMode.PLANNING

    await loop.send_message("build it")
    state = await loop.run()

    events = await store.get_events(CID)
    assert _prose_noop_diagnostic_events(events) == []
    assert not any(
        _PROSE_NOOP_REPAIR_REMINDER in m.content
        for req in provider.seen
        for m in req.messages
    )
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and e.message.content == "I will gather context and then submit a plan."
        for e in events
    )
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL


async def test_empty_reasoning_and_prose_noop_repairs_are_independent():
    provider = SequenceProvider(
        [
            _empty_reasoning_response(reasoning_len=19),
            {"text": "I will list the files next."},
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("list the files")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert [c.tool_name for c in executor.calls] == ["shell"]
    events = await store.get_events(CID)
    assert len(_diagnostic_events(events)) == 1
    assert len(_prose_noop_diagnostic_events(events)) == 1
    assert provider.seen[1].messages[-1].content == _EMPTY_REASONING_REPAIR_REMINDER
    assert provider.seen[2].messages[-1].content == _PROSE_NOOP_REPAIR_REMINDER


async def test_ordinary_prose_noop_does_not_empty_reasoning_retry():
    provider = SequenceProvider(
        [
            {"text": "plain no-op"},
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "pwd"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "checked"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("check cwd")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert provider.calls == 3
    assert [c.tool_name for c in executor.calls] == ["shell"]
    events = await store.get_events(CID)
    assert _diagnostic_events(events) == []
    assert not any(
        _EMPTY_REASONING_REPAIR_REMINDER in m.content
        for req in provider.seen
        for m in req.messages
    )


async def test_ordinary_tool_call_turn_is_untouched_by_empty_reasoning_repair():
    provider = SequenceProvider(
        [
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "pwd"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "checked"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("check cwd")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert provider.calls == 2
    assert [c.tool_name for c in executor.calls] == ["shell"]
    assert _diagnostic_events(await store.get_events(CID)) == []


async def test_finish_length_empty_turn_uses_existing_truncation_repair():
    provider = SequenceProvider(
        [
            {"text": "", "finish_reason": "length"},
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "pwd"})]},
            {
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "checked"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("check cwd")
    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert provider.calls == 3
    events = await store.get_events(CID)
    assert _diagnostic_events(events) == []
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "cut off mid-sentence" in e.message.content
        for e in events
    )
    assert not any(
        _EMPTY_REASONING_REPAIR_REMINDER in m.content
        for req in provider.seen
        for m in req.messages
    )


async def test_truncated_prose_does_not_end_run_and_injects_continue_reminder():
    """W-31 end-to-end — a prose turn the provider cut off mid-sentence
    (finish_reason=="length", no tool call) must NOT silently end the run.
    WITHOUT the fix a prose-finishing agent (RouterAgent) would FINISH on the
    truncated fragment; WITH it, the loop records the partial text, injects a
    'your message was cut off — continue it' reminder, and re-steps. The model
    then does real work and finishes on a later turn."""
    provider = SequenceProvider(
        [
            # turn 1: cut off mid-sentence with no tool call.
            {"text": "…Let me take a real", "finish_reason": "length"},
            # turn 2: a real action (proves the run continued past the fragment).
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls -la"})]},
            # turn 3: affirmative finish (work has happened → gates pass).
            {
                "text": "all done",
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "listed the files"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = RouterAgent(router, conversation_id=CID)  # prose_finishes=True — the hard case
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("build it")
    state = await loop.run()

    # The run did NOT end on the truncated fragment — it continued and finished.
    assert state.execution_status == ConversationStatus.FINISHED
    # The real action actually executed (proof the loop stepped past truncation).
    assert any(c.tool_name == "shell" for c in executor.calls)

    events = await store.get_events(CID)
    # The partial assistant text was recorded, not dropped.
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "Let me take a real" in (e.message.content or "")
        for e in events
    ), "the truncated fragment must be persisted as the assistant's partial turn"
    # A continue reminder was injected on the ENVIRONMENT channel.
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "cut off mid-sentence" in (e.message.content or "")
        for e in events
    ), "a 'continue where you left off' reminder must be injected after truncation"


async def test_unclosed_think_wedge_injects_continue_reminder_and_continues():
    """fix-slides-wedge end-to-end — reproduces Dylan's wedged agent slides build.

    A reasoning model (MiniMax) inlined its whole chain-of-thought as a literal
    `<think>` block in `content`, exhausted the output cap mid-thought — drafting
    an entire deck inside the unclosed block — and returned WITHOUT
    finish_reason=="length". Before the fix that never-finished, tool-less turn
    was mis-read as a clean no-op: the loop silently re-stepped another 2-minute
    reasoning dump that never reached a tool call, so the build looked WEDGED (the
    user had to kill it). WITH the fix the unclosed `<think>` is detected as a
    structural truncation from the raw response, strips the user-visible
    reasoning span, injects the 'cut off — take the action now with a tool call'
    reminder, and re-steps to real work — never a silent spin."""
    provider = SequenceProvider(
        [
            # turn 1: the wedge — an unclosed `<think>` dump, finish_reason "stop".
            {
                "text": "<think>\nLet me design the deck. Slide 1 title, slide 2 the",
                "finish_reason": "stop",
            },
            # turn 2: steered back to a real action (proves the run stepped past it).
            {"tool_calls": [ProposedToolCall(tool_name="shell", arguments={"cmd": "ls -la"})]},
            # turn 3: affirmative finish (work happened → gates pass).
            {
                "text": "all done",
                "tool_calls": [
                    ProposedToolCall(tool_name="finish", arguments={"summary": "built the deck"})
                ],
            },
        ]
    )
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = RouterAgent(router, conversation_id=CID)  # prose_finishes=True — the hard case
    executor = FakeExecutor()
    loop, store = build_loop(agent, executor=executor, policy=NeverConfirm())

    await loop.send_message("make slides for the report")
    state = await loop.run()

    # The run did NOT wedge/end on the unclosed-think fragment — it continued.
    assert state.execution_status == ConversationStatus.FINISHED
    assert any(c.tool_name == "shell" for c in executor.calls)

    events = await store.get_events(CID)
    # The raw response still drove truncation detection, but the user-visible
    # assistant message no longer leaks the inline chain-of-thought.
    assert not any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and "Let me design the deck" in (e.message.content or "")
        for e in events
    ), "the unclosed-think fragment must not be persisted as assistant-visible text"
    # The W-31 continue/take-action reminder was injected (NOT a silent no-op).
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and "cut off mid-sentence" in (e.message.content or "")
        for e in events
    ), "an unclosed `<think>` must trigger the 'cut off — take the action now' reminder"


async def test_provider_error_reaches_the_user_with_real_content():
    """Reactive error surfacing acceptance gate: when the driver model's provider
    rejects the call, the loop does NOT crash or flatten it — it emits a terminal
    ErrorEvent whose detail carries the PROVIDER's real reason, and the agent
    server streams that event to the UI. No capability check happened first; the
    request was sent and the provider's 'no' is what surfaced."""
    real_reason = "image input not supported by local-driver-q4"
    # The assigned driver provider rejects the call with a typed provider error.
    rejecting = FakeModelProvider("ollama", raises=LLMContentFiltered(real_reason))
    router = DefaultLLMRouter(
        simple_config(), {"ollama": rejecting, "openrouter": FakeModelProvider("openrouter")}
    )
    agent = RouterAgent(router, conversation_id=CID)
    loop, store = build_loop(agent, executor=FakeExecutor(), policy=NeverConfirm())

    await loop.send_message("describe the attached image")
    state = await loop.run()

    # The loop terminated as ERROR rather than raising out / hanging in RUNNING.
    assert state.execution_status == ConversationStatus.ERROR

    # Exactly one legible ErrorEvent reached the log (→ streamed to the UI).
    errors = [e for e in await store.get_events(CID) if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    err = errors[0]
    assert err.code == "model_error"
    # The REAL provider reason is intact — not flattened to "model call failed".
    assert real_reason in err.detail
    # The typed classification is preserved alongside the real reason.
    assert "LLMContentFiltered" in err.detail
    assert "model call failed" not in err.detail  # not a generic message
    # The provider WAS actually called (no pre-call capability gating).
    # rp-12 bounded requery: a provider rejection enters the requery ladder
    # (initial + 2 corrective attempts) before surfacing — 3 calls, one error.
    assert rejecting.calls == 3

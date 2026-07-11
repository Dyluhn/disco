from __future__ import annotations

from disco.core import ConversationStatus, EventSource, MessageEvent, PlanEvent, StatusEvent
from disco.core.llm import DefaultLLMRouter, OperatingMode, ProposedToolCall, ToolSpec
from disco.core.loop import BuildAgent, NeverConfirm
from disco.core.loop.turn_control import _PROPOSE_PLAN_UPDATE_REPEAT_CAP
from llm_fakes import simple_config
from loop_fakes import FakeExecutor, SequenceProvider, assert_blocked_question_landing, build_loop

CID = "conv"
_DIAGNOSTIC = "identical_plan_nudge"


def _tool_specs(*names: str) -> list[ToolSpec]:
    return [ToolSpec(name=name, description=name, parameters_schema={}) for name in names]


def _tool_call(tool_name: str, arguments: dict) -> ProposedToolCall:
    return ProposedToolCall(tool_name=tool_name, arguments=arguments)


def _step(tool_name: str, arguments: dict) -> dict:
    return {"tool_calls": [_tool_call(tool_name, arguments)]}


def _plan_args(*titles: str, summary: str = "plan") -> dict:
    return {"summary": summary, "steps": [{"title": title} for title in titles]}


def _submit(*titles: str) -> dict:
    return _step("submit_plan", _plan_args(*titles))


def _revise(*titles: str) -> dict:
    return _step("propose_plan_update", _plan_args(*titles, summary="revision"))


def _file_edit() -> dict:
    return _step(
        "file_edit",
        {
            "path": "index.html",
            "old": "before",
            "new": "after",
        },
    )


def _plan_step_done() -> dict:
    return _step("plan_step", {"index": 1, "state": "done"})


def _finish() -> dict:
    return _step("finish", {"summary": "done"})


async def _run_autonomous(script: list[dict]):
    provider = SequenceProvider(script)
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    executor = FakeExecutor(tools=_tool_specs("file_edit"))
    loop, store = build_loop(
        agent,
        executor=executor,
        policy=NeverConfirm(),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"submit_plan"}),
        autonomous=True,
    )
    await loop.send_message("build it")
    state = await loop.run()
    return state, await store.get_events(CID), provider, executor


async def test_identical_plan_update_warns_then_halts_on_workless_streak():
    script = [
        _submit("one"),
        _file_edit(),
        *[_revise("one") for _ in range(_PROPOSE_PLAN_UPDATE_REPEAT_CAP)],
    ]

    state, events, provider, _executor = await _run_autonomous(script)

    nudges = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == _DIAGNOSTIC
    ]
    assert len(nudges) == 1
    assert "One more identical proposal will halt the run" in nudges[0].message.content
    assert state.execution_status == ConversationStatus.STUCK
    assert_blocked_question_landing(
        events, legacy_detail="bookkeeping_only", flavor="terminal"
    )
    assert provider.calls >= 2 + _PROPOSE_PLAN_UPDATE_REPEAT_CAP


async def test_identical_plan_update_streak_restarts_after_real_work():
    script = [
        _submit("one"),
        _file_edit(),
        *[_revise("one") for _ in range(_PROPOSE_PLAN_UPDATE_REPEAT_CAP - 1)],
        _file_edit(),
        _revise("one"),
        _file_edit(),
        _plan_step_done(),
        _finish(),
    ]

    state, events, _provider, executor = await _run_autonomous(script)

    nudges = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == _DIAGNOSTIC
    ]
    assert len(nudges) == 1
    assert [call.tool_name for call in executor.calls if call.tool_name == "file_edit"] == [
        "file_edit",
        "file_edit",
        "file_edit",
    ]
    assert state.execution_status != ConversationStatus.STUCK


async def test_different_plan_update_resets_identical_streak():
    script = [
        _submit("one"),
        _file_edit(),
        *[_revise("one") for _ in range(_PROPOSE_PLAN_UPDATE_REPEAT_CAP - 1)],
        _revise("one", "two"),
        _revise("one", "two"),
        _file_edit(),
        _plan_step_done(),
        _finish(),
    ]

    state, events, _provider, _executor = await _run_autonomous(script)

    nudges = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == _DIAGNOSTIC
    ]
    assert len(nudges) == 1
    assert state.execution_status != ConversationStatus.STUCK


async def test_interactive_identical_plan_update_still_awaits_approval_without_nudge():
    provider = SequenceProvider([_revise("one")])
    router = DefaultLLMRouter(simple_config(), {"ollama": provider, "openrouter": provider})
    agent = BuildAgent(router, conversation_id=CID)
    loop, store = build_loop(
        agent,
        executor=FakeExecutor(tools=_tool_specs("file_edit")),
        policy=NeverConfirm(),
        mode=OperatingMode.LONG_HORIZON,
        planning_tools=frozenset({"submit_plan"}),
        autonomous=False,
    )
    await store.append(CID, PlanEvent(summary="plan", steps=[{"title": "one"}], revision=1))
    await store.append(
        CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved")
    )
    await loop.send_message("continue")

    state = await loop.run()
    events = await store.get_events(CID)

    nudges = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.meta.get("diagnostic") == _DIAGNOSTIC
    ]
    assert nudges == []
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

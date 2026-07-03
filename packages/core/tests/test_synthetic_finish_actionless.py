"""REL-RC-P — repeated actionless pauses synthesize a gated finish attempt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.llm import OperatingMode, ToolSpec
from disco.core.loop import AgentStep, signals
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop

CID = "conv-rel-rc-p"


def _noop(thought: str = "thinking") -> AgentStep:
    return AgentStep(thought=thought)


def _submit_plan() -> AgentStep:
    return action_step(
        "submit_plan",
        {
            "summary": "build it",
            "steps": [
                {
                    "title": "write the artifact",
                    "done_condition": {"kind": "file_exists", "path": "artifact.txt"},
                }
            ],
        },
    )


def _tool_specs(*names: str) -> list[ToolSpec]:
    return [
        ToolSpec(name=name, description=name, parameters_schema={})
        for name in names
    ]


def _env_messages(events: list[Any]) -> list[str]:
    return [
        e.message.content
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and e.message is not None
    ]


class _Sandbox:
    def __init__(self, root: Path) -> None:
        self.workspace_path = str(root)
        self._root = root

    async def file_exists(self, path: str) -> bool:
        return (self._root / path).is_file()

    async def read_file(self, path: str) -> bytes:
        return (self._root / path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_dir(self, path: str) -> list[str]:
        target = self._root / path
        return [p.name for p in target.iterdir()]


class _FSExecutor(FakeExecutor):
    def __init__(self, root: Path) -> None:
        super().__init__(
            tools=_tool_specs("submit_plan", "file_write", "file_read", "browser", "shell")
        )
        self.sandbox = _Sandbox(root)
        self.root = root

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        if call.tool_name == "file_write":
            path = self.root / str(call.arguments.get("path") or "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(call.arguments.get("content") or ""), encoding="utf-8")
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            content="ok",
        )


async def _approve(loop: Any, prompt: str = "Build the artifact.") -> None:
    await loop.send_message(prompt)
    await loop.run()
    await loop.approve_plan()


async def _append_success(store: Any, tool_name: str, args: dict[str, Any] | None = None) -> None:
    action = await store.append(
        CID,
        ActionEvent(
            thought=f"run {tool_name}",
            tool_call=ToolCall(tool_name=tool_name, arguments=args or {}),
        ),
    )
    await store.append(
        CID,
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(
                call_id=action.tool_call.call_id,
                tool_name=tool_name,
                success=True,
                content="ok",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_dawdle_after_two_actionless_pauses_synthesizes_finish() -> None:
    executor = FakeExecutor(tools=_tool_specs("submit_plan", "shell", "file_read", "browser"))
    agent = ScriptedAgent(
        [
            _submit_plan(),
            action_step("shell", {"command": "echo built"}),
            _noop("The work is complete."),
            _noop("Everything is ready."),
            _noop("All done."),
        ]
    )
    loop, store = build_loop(
        agent,
        conversation_id=CID,
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )
    await _approve(loop)

    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED

    loop.agent = ScriptedAgent(
        [
            action_step("browser", {"action": "navigate", "url": "http://example.test"}),
            action_step("file_read", {"path": "notes.txt"}),
            _noop("I checked it."),
            _noop("The deliverable is ready."),
            _noop("All done."),
        ]
    )
    state = await loop.resume()
    assert state.execution_status == ConversationStatus.PAUSED

    pre_finish_events = await store.get_events(CID)
    assert signals.actionless_pause_count_current_execution_segment(pre_finish_events) == 2
    assert signals.should_synthesize_finish_after_actionless_pauses(pre_finish_events)

    must_not_call = ScriptedAgent([action_step("shell", {"command": "echo should-not-run"})])
    loop.agent = must_not_call
    state = await loop.resume()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert must_not_call.calls == 0
    assert any("REL-RC-P SYNTHETIC FINISH" in m for m in _env_messages(events))
    assert any(
        isinstance(e, StatusEvent)
        and e.detail == signals.SYNTHETIC_FINISH_ATTEMPT_DETAIL
        for e in events
    )
    assert any(
        isinstance(e, MessageEvent)
        and e.source == EventSource.AGENT
        and e.message.content == "All done."
        for e in events
    )


@pytest.mark.asyncio
async def test_synthetic_finish_refusal_continues_with_dictated_content_blocker(
    tmp_path: Path,
) -> None:
    agent = ScriptedAgent(
        [
            _submit_plan(),
            action_step(
                "file_write",
                {"path": "artifact.txt", "content": "plain content"},
            ),
            _noop("Artifact is written."),
            _noop("Ready to wrap up."),
            _noop("All done."),
        ]
    )
    loop, store = build_loop(
        agent,
        conversation_id=CID,
        executor=_FSExecutor(tmp_path),
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )
    await _approve(loop, 'Build the artifact with button text "Get Started".')
    state = await loop.run()
    assert state.execution_status == ConversationStatus.PAUSED

    loop.agent = ScriptedAgent(
        [
            action_step("browser", {"action": "navigate", "url": "http://example.test"}),
            action_step("file_read", {"path": "artifact.txt"}),
            _noop("I checked the artifact."),
            _noop("It looks ready."),
            _noop("All done."),
        ]
    )
    state = await loop.resume()
    assert state.execution_status == ConversationStatus.PAUSED

    loop.agent = ScriptedAgent(
        [
            action_step("shell", {"command": "echo continue after refusal"}),
            action_step("ask_user", {"question": "The dictated literal is still missing."}),
        ]
    )
    state = await loop.resume()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    env = _env_messages(events)
    assert any("REL-RC-P SYNTHETIC FINISH" in m for m in env)
    assert any("quoted user literal is missing" in m and "Get Started" in m for m in env)
    assert not any(
        isinstance(e, StatusEvent) and e.status == ConversationStatus.FINISHED
        for e in events
    )


@pytest.mark.asyncio
async def test_planning_mode_never_synthesizes_finish() -> None:
    loop, store = build_loop(
        ScriptedAgent([_submit_plan()]),
        conversation_id=CID,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )
    await store.append(CID, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")))
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "one"}], revision=1))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))
    await _append_success(store, "shell", {"command": "echo built"})
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="planning"))

    loop.mode = OperatingMode.PLANNING
    state = await loop.run()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL
    assert not any("REL-RC-P SYNTHETIC FINISH" in m for m in _env_messages(events))


@pytest.mark.asyncio
async def test_no_productive_work_never_synthesizes_finish() -> None:
    executor = FakeExecutor(tools=_tool_specs("submit_plan", "file_read", "shell"))
    loop, store = build_loop(
        ScriptedAgent(
            [
                action_step("shell", {"command": "echo now do real work"}),
                action_step("ask_user", {"question": "No prior productive work was present."}),
            ]
        ),
        conversation_id=CID,
        executor=executor,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )
    await store.append(CID, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")))
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "one"}], revision=1))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))
    await _append_success(store, "file_read", {"path": "notes.txt"})
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    await _append_success(store, "file_read", {"path": "notes.txt"})
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))

    assert not signals.should_synthesize_finish_after_actionless_pauses(
        await store.get_events(CID)
    )

    state = await loop.resume()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.AWAITING_USER_QUESTION
    assert not any("REL-RC-P SYNTHETIC FINISH" in m for m in _env_messages(events))


@pytest.mark.asyncio
async def test_actionless_pause_counter_survives_simulated_resume() -> None:
    seed_loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id=CID,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )
    await store.append(CID, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")))
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "one"}], revision=1))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))
    await _append_success(store, "shell", {"command": "echo built"})
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"))
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content="resume context"),
        ),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    assert seed_loop is not None

    events = await store.get_events(CID)
    assert signals.actionless_pause_count_current_execution_segment(events) == 2
    assert signals.should_synthesize_finish_after_actionless_pauses(events)

    rebuilt_agent = ScriptedAgent([action_step("shell", {"command": "echo should-not-run"})])
    rebuilt, _ = build_loop(
        rebuilt_agent,
        store=store,
        conversation_id=CID,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )

    state = await rebuilt.resume()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert rebuilt_agent.calls == 0
    assert any("REL-RC-P SYNTHETIC FINISH" in m for m in _env_messages(events))


@pytest.mark.asyncio
async def test_synthetic_finish_is_replay_derived_after_rebuild() -> None:
    seed_loop, store = build_loop(
        ScriptedAgent([]),
        conversation_id=CID,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )
    await store.append(CID, MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="go")))
    await store.append(CID, PlanEvent(summary="p", steps=[{"title": "one"}], revision=1))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING, detail="plan_approved"))
    await _append_success(store, "shell", {"command": "echo built"})
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content="The work is complete."),
        ),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))
    await _append_success(store, "browser", {"action": "navigate"})
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.AGENT,
            message=LLMMessage(role="assistant", content="All done after checking."),
        ),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED, detail="actionless"))
    assert seed_loop is not None

    rebuilt_agent = ScriptedAgent([action_step("shell", {"command": "echo should-not-run"})])
    rebuilt, _ = build_loop(
        rebuilt_agent,
        store=store,
        conversation_id=CID,
        mode=OperatingMode.PLANNING,
        planning_tools=frozenset({"file_read"}),
    )

    state = await rebuilt.resume()
    events = await store.get_events(CID)

    assert state.execution_status == ConversationStatus.FINISHED
    assert rebuilt_agent.calls == 0
    assert any("REL-RC-P SYNTHETIC FINISH" in m for m in _env_messages(events))

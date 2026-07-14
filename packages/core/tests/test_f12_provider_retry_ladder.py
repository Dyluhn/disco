"""F12 — bounded driver retries preserve state and remain interruptible."""

from __future__ import annotations

import asyncio

import pytest
from disco.core import ActionEvent, ConversationStatus, ErrorEvent, MessageEvent, ObservationEvent
from disco.core.llm import LLMAuthError, LLMTransientError
from disco.core.loop import driver as driver_module
from loop_fakes import FakeExecutor, ScriptedAgent, action_step, build_loop, finish_step


class _WorkspaceExecutor(FakeExecutor):
    """One visible side effect over pre-existing workspace state."""

    def __init__(self) -> None:
        super().__init__()
        self.files = {"keep.txt": "original"}

    async def execute(self, call):
        result = await super().execute(call)
        self.files["result.txt"] = f"executions={len(self.calls)}"
        return result


async def _blocking_clock(started: asyncio.Event, cancelled: asyncio.Event, _delay: float) -> None:
    started.set()
    try:
        await asyncio.Future()
    finally:
        cancelled.set()


@pytest.mark.asyncio
async def test_temporary_outage_preserves_state_and_executes_side_effect_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def fake_clock(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(driver_module, "_sleep", fake_clock)
    executor = _WorkspaceExecutor()
    agent = ScriptedAgent(
        [
            LLMTransientError("offline"),
            LLMTransientError("offline"),
            action_step("shell", {"command": "produce result"}),
            finish_step(),
        ]
    )
    loop, store = build_loop(agent, executor=executor)
    await loop.send_message("keep the current workspace and produce the result")

    state = await loop.run()

    assert state.execution_status == ConversationStatus.FINISHED
    assert delays == [10.0, 30.0]
    assert executor.files == {"keep.txt": "original", "result.txt": "executions=1"}
    assert len(executor.calls) == 1
    events = await store.get_events("conv")
    assert len([event for event in events if isinstance(event, ActionEvent)]) == 1
    assert len([event for event in events if isinstance(event, ObservationEvent)]) == 1
    assert any(
        isinstance(event, MessageEvent)
        and "keep the current workspace" in event.message.content
        for event in events
    )


@pytest.mark.asyncio
async def test_permanent_auth_rejection_fails_once_with_actionable_error() -> None:
    agent = ScriptedAgent([LLMAuthError("API key credits exhausted", provider="provider")])
    loop, store = build_loop(agent)
    await loop.send_message("continue the task")

    state = await loop.run()

    assert state.execution_status == ConversationStatus.ERROR
    assert agent.calls == 1
    errors = [event for event in await store.get_events("conv") if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == "auth_error"
    assert "API key credits exhausted" in errors[0].detail


@pytest.mark.asyncio
async def test_cancellation_interrupts_backoff_without_another_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    sleep_cancelled = asyncio.Event()
    monkeypatch.setattr(
        driver_module,
        "_sleep",
        lambda delay: _blocking_clock(started, sleep_cancelled, delay),
    )
    agent = ScriptedAgent([LLMTransientError("offline")])
    loop, _store = build_loop(agent)
    await loop.send_message("start")

    run_task = asyncio.create_task(loop.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        cancel_state = await asyncio.wait_for(loop.cancel(), timeout=1)
        state = await asyncio.wait_for(run_task, timeout=1)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert cancel_state.execution_status == ConversationStatus.IDLE
    assert state.execution_status == ConversationStatus.IDLE
    assert sleep_cancelled.is_set()
    assert agent.calls == 1
    assert not loop._retry_interrupt.is_set()


@pytest.mark.asyncio
async def test_pause_interrupts_backoff_at_safe_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    sleep_cancelled = asyncio.Event()
    monkeypatch.setattr(
        driver_module,
        "_sleep",
        lambda delay: _blocking_clock(started, sleep_cancelled, delay),
    )
    agent = ScriptedAgent([LLMTransientError("offline")])
    loop, _store = build_loop(agent)
    await loop.send_message("start")

    run_task = asyncio.create_task(loop.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await loop.pause()
        state = await asyncio.wait_for(run_task, timeout=1)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert state.execution_status == ConversationStatus.PAUSED
    assert sleep_cancelled.is_set()
    assert agent.calls == 1
    assert not loop._retry_interrupt.is_set()


@pytest.mark.asyncio
async def test_user_steer_interrupts_backoff_and_same_task_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    sleep_cancelled = asyncio.Event()
    monkeypatch.setattr(
        driver_module,
        "_sleep",
        lambda delay: _blocking_clock(started, sleep_cancelled, delay),
    )
    executor = _WorkspaceExecutor()
    agent = ScriptedAgent(
        [
            LLMTransientError("offline"),
            action_step("shell", {"command": "continue recovered task"}),
            finish_step(),
        ]
    )
    loop, _store = build_loop(agent, executor=executor)
    await loop.send_message("start")

    run_task = asyncio.create_task(loop.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(loop.steer("also preserve the existing file"), timeout=1)
        state = await asyncio.wait_for(run_task, timeout=1)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert state.execution_status == ConversationStatus.FINISHED
    assert sleep_cancelled.is_set()
    assert agent.calls == 3
    assert len(executor.calls) == 1
    assert executor.files == {"keep.txt": "original", "result.txt": "executions=1"}
    assert any(
        "also preserve the existing file" in message.content
        for message in agent.seen_views[1].messages
    )
    assert not loop._retry_interrupt.is_set()

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from disco.agent_server.report_deck_handoff import build_report_deck_job
from disco.agent_server.report_deck_service import ReportDeckRunService
from disco.agent_server.run_completion import RunCompletionPort
from disco.agent_server.run_registry import (
    CancellationRegistry,
    RunAuthorityLedger,
    RunIngressLedger,
    RunRegistry,
    RunResourceRegistry,
    RunWorkspacePort,
)
from disco.agent_server.run_supervisor import RunSupervisor
from disco.agent_server.runtime import ConversationRuntime, execute_disco_tool
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    ConversationState,
    ConversationStatus,
    ReportEvent,
    ReportSection,
    StatusEvent,
    ToolCall,
    ToolResult,
)
from disco.core.store.sqlite import SqliteEventStore


def _report() -> ReportEvent:
    return ReportEvent(
        query="Build an ORBIT-731 briefing",
        summary="ORBIT-731 is the authoritative source fact.",
        sections=[ReportSection(id="s1", title="Finding", markdown="ORBIT-731")],
    )


class _Settings:
    def set_artifact_mode(self, conversation_id: str, enabled: bool) -> None:
        assert conversation_id == "deck-target"
        assert enabled is True


class _Contract:
    def set_build_kind(self, conversation_id: str, kind: str) -> None:
        assert conversation_id == "deck-target"
        assert kind == "deck"


class _LoopFactory:
    def __init__(self) -> None:
        self.source_reports: dict[str, str] = {}
        self.injected: list[tuple[str, str]] = []
        self.cleared: list[str] = []

    def set_source_report(self, conversation_id: str, source_report: str) -> None:
        self.source_reports[conversation_id] = source_report
        self.injected.append((conversation_id, source_report))

    def clear_source_report(self, conversation_id: str) -> None:
        self.source_reports.pop(conversation_id, None)
        self.cleared.append(conversation_id)


class _Supervisor:
    def __init__(self) -> None:
        self.operation: Callable[[], Awaitable[ConversationState]] | None = None

    def create_operation_task(
        self,
        conversation_id: str,
        operation: Callable[[], Awaitable[ConversationState]],
    ) -> tuple[object, int]:
        assert conversation_id == "deck-target"
        self.operation = operation
        return object(), 1


class _Lifecycle:
    def __init__(self, store: SqliteEventStore) -> None:
        self.store = store
        self.commits: list[StatusEvent] = []

    @staticmethod
    def build_status(
        status: ConversationStatus,
        *,
        detail: str | None = None,
    ) -> StatusEvent:
        return StatusEvent(status=status, detail=detail)

    async def append_status(
        self,
        conversation_id: str,
        status: ConversationStatus,
        *,
        detail: str | None = None,
    ) -> StatusEvent:
        event = self.build_status(status, detail=detail)
        stored = await self.store.append(conversation_id, event)
        assert isinstance(stored, StatusEvent)
        return stored

    async def commit_finished_workspace(
        self,
        conversation_id: str,
        event: StatusEvent,
    ) -> StatusEvent:
        self.commits.append(event)
        stored = await self.store.append(conversation_id, event)
        assert isinstance(stored, StatusEvent)
        return stored


class _Runtime:
    def __init__(self, store: SqliteEventStore, *, success: bool = True) -> None:
        self.settings = _Settings()
        self.contract = _Contract()
        self._loop_factory = _LoopFactory()
        self._run_supervisor = _Supervisor()
        self._lifecycle_commands = _Lifecycle(store)
        self._cancellations = CancellationRegistry()
        self.success = success
        self.calls: list[ToolCall] = []

    async def execute_disco_tool(self, conversation_id: str, call: ToolCall) -> ToolResult:
        assert conversation_id == "deck-target"
        self.calls.append(call)
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=self.success,
            content="deck complete" if self.success else "",
            error=None if self.success else "renderer unavailable",
        )


class _BlockingRuntime(_Runtime):
    def __init__(self, store: SqliteEventStore) -> None:
        super().__init__(store)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute_disco_tool(self, conversation_id: str, call: ToolCall) -> ToolResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("blocked deck tool unexpectedly completed")


class _BlockingExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, _call: ToolCall) -> ToolResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked tool unexpectedly completed")


class _ToolRuntime:
    def __init__(self, store: SqliteEventStore, executor: _BlockingExecutor) -> None:
        self._store = store
        self._run_resources = self
        self._executor = executor

    def executor(self, _conversation_id: str) -> _BlockingExecutor:
        return self._executor


class _Workspace:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear_run_claim(self, conversation_id: str) -> None:
        self.cleared.append(conversation_id)


class _Finalizer:
    def __init__(self) -> None:
        self.clean: list[tuple[str, int | None]] = []

    async def finalize_clean(
        self,
        conversation_id: str,
        generation: int | None = None,
        **_kwargs: object,
    ) -> None:
        self.clean.append((conversation_id, generation))

    async def terminalize_crash(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("operation should not crash")

    async def rekick_unadmitted(self, _conversation_id: str) -> None:
        raise AssertionError("operation should not be superseded")


@pytest.mark.asyncio
async def test_managed_report_deck_uses_one_exact_tool_and_terminal_commit() -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation("source", owner_id="owner-1", surface="deep_research")
    report = _report()
    await store.append("source", report)
    runtime = _Runtime(store)
    service = ReportDeckRunService(store, cast(ConversationRuntime, runtime))
    port = service.start_port()
    port._target_id_factory = lambda: "deck-target"

    target = await port.start_report_deck("source", build_report_deck_job(report))

    assert target == "deck-target"
    assert await store.conversation_owner_id(target) == "owner-1"
    copied = [event for event in await store.get_events(target) if isinstance(event, ReportEvent)]
    assert len(copied) == 1
    assert copied[0].id != report.id
    assert target not in runtime._loop_factory.source_reports
    operation = runtime._run_supervisor.operation
    assert operation is not None

    state = await operation()

    assert state.execution_status is ConversationStatus.FINISHED
    assert [call.tool_name for call in runtime.calls] == ["slides_generate"]
    assert runtime.calls[0].arguments == {
        "goal": "Build an ORBIT-731 briefing",
        "filename": "deck",
        "format": "pptx",
    }
    assert "source_report" not in runtime.calls[0].arguments
    assert len(runtime._loop_factory.injected) == 1
    assert runtime._loop_factory.injected[0][0] == target
    assert "ORBIT-731" in runtime._loop_factory.injected[0][1]
    assert len(runtime._lifecycle_commands.commits) == 1
    assert runtime._loop_factory.cleared == [target]


@pytest.mark.asyncio
async def test_managed_report_deck_failure_never_commits_finished() -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation("source", owner_id="owner-1", surface="deep_research")
    report = _report()
    await store.append("source", report)
    runtime = _Runtime(store, success=False)
    service = ReportDeckRunService(store, cast(ConversationRuntime, runtime))
    port = service.start_port()
    port._target_id_factory = lambda: "deck-target"
    await port.start_report_deck("source", build_report_deck_job(report))
    operation = runtime._run_supervisor.operation
    assert operation is not None

    with pytest.raises(RuntimeError, match="renderer unavailable"):
        await operation()

    assert runtime._lifecycle_commands.commits == []
    assert runtime._loop_factory.cleared == ["deck-target"]


@pytest.mark.asyncio
async def test_managed_report_deck_stop_cancels_tool_and_lands_idle() -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation("source", owner_id="owner-1", surface="deep_research")
    report = _report()
    await store.append("source", report)
    runtime = _BlockingRuntime(store)
    service = ReportDeckRunService(store, cast(ConversationRuntime, runtime))
    port = service.start_port()
    port._target_id_factory = lambda: "deck-target"
    await port.start_report_deck("source", build_report_deck_job(report))
    operation = runtime._run_supervisor.operation
    assert operation is not None

    task = asyncio.create_task(operation())
    await runtime.started.wait()
    runtime._cancellations.request("deck-target")
    state = await task

    assert state.execution_status is ConversationStatus.IDLE
    statuses = [
        event
        for event in await store.get_events("deck-target")
        if isinstance(event, StatusEvent)
    ]
    assert statuses[-1].status is ConversationStatus.IDLE
    assert statuses[-1].detail == "cancelled"
    assert runtime._lifecycle_commands.commits == []
    assert runtime.cancelled.is_set()
    assert runtime._loop_factory.cleared == ["deck-target"]
    assert "deck-target" not in runtime._cancellations._flags


@pytest.mark.asyncio
async def test_execute_disco_tool_pairs_cancelled_action_with_agent_error() -> None:
    store = SqliteEventStore(":memory:")
    store.create_conversation("deck-target", owner_id="owner-1", surface="agent")
    executor = _BlockingExecutor()
    runtime = _ToolRuntime(store, executor)
    call = ToolCall(tool_name="slides_generate", arguments={})
    task = asyncio.create_task(
        execute_disco_tool(cast(ConversationRuntime, runtime), "deck-target", call)
    )
    await executor.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    events = await store.get_events("deck-target")
    action = next(event for event in events if isinstance(event, ActionEvent))
    errors = [
        event
        for event in events
        if isinstance(event, AgentErrorEvent) and event.action_id == action.id
    ]
    assert len(errors) == 1
    assert errors[0].error == "cancelled"
    assert errors[0].tool_call_id == call.call_id


@pytest.mark.asyncio
async def test_host_operation_uses_normal_run_registry_and_completion_callback() -> None:
    registry = RunRegistry()
    workspace = _Workspace()
    finalizer = _Finalizer()
    supervisor = RunSupervisor(
        registry,
        RunAuthorityLedger(),
        RunIngressLedger(),
        RunResourceRegistry(),
        cast(RunWorkspacePort, workspace),
        cast(RunCompletionPort, finalizer),
    )

    async def operation() -> ConversationState:
        return cast(ConversationState, object())

    task, generation = supervisor.create_operation_task("deck-target", operation)
    assert registry.active_task("deck-target") is task

    await task
    for _ in range(3):
        await asyncio.sleep(0)

    assert registry.task("deck-target") is None
    assert workspace.cleared == ["deck-target"]
    assert finalizer.clean == [("deck-target", generation)]

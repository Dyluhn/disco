"""PKG-06 lifecycle boundary: ordered hard-stop and unauthorized append proof.

The broader lifecycle, resume, restart, and ship suites cover each compatible
entry point. These tests add the two missing cross-cutting assertions: one
ordered persist/cleanup trace for the real kill service, including a repeated
command, and a fail-closed direct status append.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
)
from disco.tools import ProcessSandboxService

CID = "conv-lifecycle-trace"


def test_lifecycle_service_is_the_only_agent_server_status_constructor() -> None:
    source_root = Path(__file__).parents[1] / "src" / "disco" / "agent_server"
    writers: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "StatusEvent"
            for node in ast.walk(tree)
        ):
            writers.add(path.relative_to(source_root).as_posix())
    assert writers == {"lifecycle_command_service.py"}


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    return ConversationRuntime(
        store,
        router=MagicMock(),
        sandbox_service=ProcessSandboxService(),
    )


async def _seed_running(store: SqliteEventStore) -> None:
    store.create_conversation(CID, owner_id="local", surface="build")
    await store.append(
        CID,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="build it"),
        ),
    )
    await store.append(CID, StatusEvent(status=ConversationStatus.RUNNING))


async def _admit_view(store: SqliteEventStore, view_id: str) -> None:
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    await store.append_many(
        CID,
        [
            intent,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id=view_id,
                run_protocol_version=1,
            ),
        ],
    )


class _LiveTask:
    def __init__(self, trace: list[str]) -> None:
        self._trace = trace

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self._trace.append("task.cancel")

    def __await__(self) -> Any:
        self._trace.append("task.await")
        return iter(())


async def test_kill_cleanup_publish_order_and_repeat_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteEventStore(":memory:")
    await _seed_running(store)
    await _admit_view(store, "view-trace")
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    rt._run_registry._generations[CID] = 1

    trace: list[str] = []
    rt._run_registry._tasks[CID] = _LiveTask(trace)  # type: ignore[arg-type]
    executor = MagicMock()
    executor.kill = AsyncMock(side_effect=lambda: trace.append("executor.kill"))
    pending = MagicMock()
    pending.destroy = AsyncMock(side_effect=lambda: trace.append("pending.destroy"))
    rt._run_resources.set_executor(CID, executor)
    rt._run_resources.set_pending_session(CID, pending)
    rt._loop_registry.bind(CID, object())  # type: ignore[arg-type]

    close_actions = rt._run_kills._close_dangling_actions_locked

    async def _close(conversation_id: str, authority):
        trace.append("actions.close")
        return await close_actions(conversation_id, authority)

    authority_is_current = rt._lifecycle_commands.authority_is_current_for_events

    def _authority(events, authority):
        trace.append("authority.check")
        return authority_is_current(events, authority)

    append_status = rt._lifecycle_commands.append_status_locked

    async def _append(conversation_id: str, event: StatusEvent):
        assert rt._workspace.lock(conversation_id).locked()
        assert rt._workspace._fence_owned_by_current_task(conversation_id)
        trace.append("status.append")
        return await append_status(conversation_id, event)

    monkeypatch.setattr(rt._run_kills, "_close_dangling_actions_locked", _close)
    monkeypatch.setattr(rt._lifecycle_commands, "authority_is_current_for_events", _authority)
    monkeypatch.setattr(rt._lifecycle_commands, "append_status_locked", _append)

    await rt.kill(CID)
    first_trace = list(trace)
    await rt.kill(CID)

    assert first_trace == [
        "task.cancel",
        "task.await",
        "authority.check",
        "actions.close",
        "executor.kill",
        "pending.destroy",
        "authority.check",
        "status.append",
    ]
    assert trace == [*first_trace, "authority.check"]
    killed = [
        event
        for event in await store.get_events(CID)
        if isinstance(event, StatusEvent)
        and event.status is ConversationStatus.IDLE
        and event.detail == "killed"
    ]
    assert len(killed) == 1
    assert killed[0].agent_view_id == "view-trace"
    assert (await store.get_state(CID)).execution_status is ConversationStatus.IDLE


async def test_direct_locked_status_append_without_fence_is_rejected() -> None:
    store = SqliteEventStore(":memory:")
    await _seed_running(store)
    rt = _runtime(store)
    rt.set_surface(CID, "build")

    with pytest.raises(
        RuntimeError,
        match="locked status append requires the workspace fence",
    ):
        await rt._lifecycle_commands.append_status_locked(
            CID,
            StatusEvent(status=ConversationStatus.IDLE, detail="unauthorized"),
        )

    assert not any(
        isinstance(event, StatusEvent) and event.detail == "unauthorized"
        for event in await store.get_events(CID)
    )

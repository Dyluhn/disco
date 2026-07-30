"""PKG-06 lifecycle boundary: ordered hard-stop and unauthorized append proof.

The broader lifecycle, resume, restart, and ship suites cover each compatible
entry point. These tests add the two missing cross-cutting assertions: one
ordered persist/cleanup trace for the real kill service, including a repeated
command, and a fail-closed direct status append.
"""

from __future__ import annotations

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
)
from disco.tools import ProcessSandboxService

CID = "conv-lifecycle-trace"


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


async def test_kill_cleanup_publish_order_and_repeat_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteEventStore(":memory:")
    await _seed_running(store)
    rt = _runtime(store)
    rt.set_surface(CID, "build")
    rt._run_generation[CID] = 1

    trace: list[str] = []
    executor = MagicMock()
    executor.kill = AsyncMock(side_effect=lambda: trace.append("executor.kill"))
    pending = MagicMock()
    pending.destroy = AsyncMock(side_effect=lambda: trace.append("pending.destroy"))
    rt._executors[CID] = executor
    rt._pending_sessions[CID] = pending
    rt._loops[CID] = object()

    close_actions = rt._close_dangling_actions_for_kill_locked

    async def _close(conversation_id: str, authority):
        trace.append("actions.close")
        return await close_actions(conversation_id, authority)

    append_status = rt._workspace.append_status_locked

    async def _append(conversation_id: str, event: StatusEvent):
        assert rt.workspace_lock(conversation_id).locked()
        assert rt._workspace.fence_owned_by_current_task(conversation_id)
        trace.append("status.append")
        return await append_status(conversation_id, event)

    monkeypatch.setattr(rt, "_close_dangling_actions_for_kill_locked", _close)
    monkeypatch.setattr(rt._workspace, "append_status_locked", _append)

    await rt.kill(CID)
    await rt.kill(CID)

    assert trace == [
        "actions.close",
        "executor.kill",
        "pending.destroy",
        "status.append",
    ]
    killed = [
        event
        for event in await store.get_events(CID)
        if isinstance(event, StatusEvent)
        and event.status is ConversationStatus.IDLE
        and event.detail == "killed"
    ]
    assert len(killed) == 1
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
        await rt._workspace.append_status_locked(
            CID,
            StatusEvent(status=ConversationStatus.IDLE, detail="unauthorized"),
        )

    assert not any(
        isinstance(event, StatusEvent) and event.detail == "unauthorized"
        for event in await store.get_events(CID)
    )

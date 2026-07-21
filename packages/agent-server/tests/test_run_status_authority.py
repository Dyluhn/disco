from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from disco.agent_server.workspace_service import WorkspaceCoordinator
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
)
from disco.tools.projects import ProjectStore

CID = "conv-run-status-authority"


class _Runtime:
    _BUILD_LIKE_SURFACES = frozenset({"build"})

    def __init__(self, store: SqliteEventStore, project_store: ProjectStore) -> None:
        self._store = store
        self._project_store = project_store

    def _surface_of(self, conversation_id: str) -> str:
        assert conversation_id == CID
        return "build"

    def _project_store_now(self) -> ProjectStore:
        return self._project_store


def _coordinator_pair(
    store: SqliteEventStore,
    tmp_path: Path,
) -> tuple[WorkspaceCoordinator, WorkspaceCoordinator]:
    project_store = ProjectStore(str(tmp_path / "projects"))
    return (
        WorkspaceCoordinator(_Runtime(store, project_store)),
        WorkspaceCoordinator(_Runtime(store, project_store)),
    )


async def _seed_view(store: SqliteEventStore, *, view_id: str) -> WorkspaceMutationEvent:
    store.create_conversation(CID, owner_id="local", surface="build")
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    stored = await store.append_many(
        CID,
        [
            MessageEvent(
                source=EventSource.USER,
                message={"role": "user", "content": "build it"},
            ),
            intent,
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id=view_id,
                run_protocol_version=1,
            ),
            StatusEvent(
                status=ConversationStatus.RUNNING,
                agent_view_id=view_id,
            ),
        ],
    )
    return next(event for event in stored if event.id == intent.id)  # type: ignore[return-value]


async def _append_peer_ingress(
    coordinator: WorkspaceCoordinator,
    store: SqliteEventStore,
    *,
    view_id: str,
) -> WorkspaceMutationEvent:
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    async with coordinator.lock(CID):
        async with coordinator.interprocess_mutation_fence(CID):
            stored = await coordinator.append_run_ingress_locked(
                CID,
                [
                    MessageEvent(
                        source=EventSource.USER,
                        message={"role": "user", "content": "newer revision"},
                    )
                ],
                "user-turn",
                intent=intent,
            )
            await store.append_many(
                CID,
                [
                    WorkspaceMutationEvent(
                        operation="agent.view-admitted",
                        run_intent_id=intent.id,
                        agent_view_id=view_id,
                        run_protocol_version=1,
                    ),
                    StatusEvent(
                        status=ConversationStatus.RUNNING,
                        agent_view_id=view_id,
                    ),
                ],
            )
    return next(event for event in stored if event.id == intent.id)  # type: ignore[return-value]


async def test_current_view_status_is_attributed_and_accepted(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    store = SqliteEventStore(":memory:")
    try:
        first, _second = _coordinator_pair(store, tmp_path)
        await _seed_view(store, view_id="view-current")

        stored = await first.append_run_status_if_current(
            CID,
            StatusEvent(status=ConversationStatus.ERROR, detail="worker crashed"),
            agent_view_id="view-current",
            run_intent_id=None,
        )

        assert stored is not None
        assert stored.agent_view_id == "view-current"
        assert stored.run_intent_id is None
        assert (await store.get_state(CID)).execution_status is ConversationStatus.ERROR
    finally:
        store.close()


async def test_peer_ingress_wins_before_stale_status_and_rejects_it(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    store = SqliteEventStore(":memory:")
    try:
        stale, peer = _coordinator_pair(store, tmp_path)
        await _seed_view(store, view_id="view-old")
        await _append_peer_ingress(peer, store, view_id="view-new")

        rejected = await stale.append_run_status_if_current(
            CID,
            StatusEvent(status=ConversationStatus.ERROR, detail="late old worker"),
            agent_view_id="view-old",
            run_intent_id=None,
        )

        assert rejected is None
        assert (await store.get_state(CID)).execution_status is ConversationStatus.RUNNING
        assert not any(
            isinstance(event, StatusEvent) and event.detail == "late old worker"
            for event in await store.get_events(CID)
        )
    finally:
        store.close()


async def test_status_and_peer_ingress_are_serialized_without_a_poisoned_head(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    store = SqliteEventStore(":memory:")
    try:
        old_worker, peer = _coordinator_pair(store, tmp_path)
        await _seed_view(store, view_id="view-old")
        checked = asyncio.Event()
        release = asyncio.Event()
        original = old_worker._run_authority_is_current_locked

        async def checked_then_blocked(*args: Any, **kwargs: Any) -> bool:
            result = await original(*args, **kwargs)
            checked.set()
            await release.wait()
            return result

        monkeypatch.setattr(
            old_worker,
            "_run_authority_is_current_locked",
            checked_then_blocked,
        )
        old_status = asyncio.create_task(
            old_worker.append_run_status_if_current(
                CID,
                StatusEvent(status=ConversationStatus.ERROR, detail="old worker won first"),
                agent_view_id="view-old",
                run_intent_id=None,
            )
        )
        await asyncio.wait_for(checked.wait(), timeout=1)

        peer_ingress = asyncio.create_task(_append_peer_ingress(peer, store, view_id="view-new"))
        await asyncio.sleep(0)
        assert not peer_ingress.done()
        release.set()

        accepted = await asyncio.wait_for(old_status, timeout=1)
        new_intent = await asyncio.wait_for(peer_ingress, timeout=1)
        assert accepted is not None and accepted.seq is not None
        assert new_intent.seq is not None and accepted.seq < new_intent.seq
        assert (await store.get_state(CID)).execution_status is ConversationStatus.RUNNING
    finally:
        store.close()


async def test_child_task_does_not_inherit_parent_local_fence_ownership(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Task context is copied at create_task(), but lock ownership is not.

    A run task is registered while its ingress task owns the workspace fence.
    Once that parent exits, the child must take the ordinary lock path rather
    than treating the copied ContextVar as proof that it owns a released lock.
    """

    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "locks"))
    store = SqliteEventStore(":memory:")
    try:
        coordinator, _peer = _coordinator_pair(store, tmp_path)
        store.create_conversation(CID, owner_id="local", surface="build")
        release_child = asyncio.Event()

        async with coordinator.lock(CID):
            async with coordinator.interprocess_mutation_fence(CID):
                assert coordinator.fence_owned_by_current_task(CID)

                async def append_after_parent_releases() -> StatusEvent:
                    await release_child.wait()
                    assert not coordinator.fence_owned_by_current_task(CID)
                    return await coordinator.append_status(
                        CID,
                        StatusEvent(status=ConversationStatus.RUNNING),
                    )

                child = asyncio.create_task(append_after_parent_releases())

        release_child.set()
        stored = await asyncio.wait_for(child, timeout=1)
        assert stored.status is ConversationStatus.RUNNING
    finally:
        store.close()

"""Shared fakes and construction helpers for final-workspace contract tests."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.workspace_commit import resolve_committed_workspace
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    Event,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
)
from disco.tools import ProcessSandboxService
from disco.tools.projects import ProjectStore, SnapshotResult


class _Workspace:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def list_dir(self, path: str) -> list[str]:
        if path in {"", "."}:
            return sorted(self.files)
        raise RuntimeError("not a directory")

    async def read_file(self, path: str) -> bytes:
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


class _AvailableStore:
    def status(self):
        from disco.tools.projects import StorageStatus

        return StorageStatus.OK


class _AvailableProjects:
    def current_project_store(self):
        return _AvailableStore()


class _Notifier:
    async def emit(self, *_args, **_kwargs):
        return None


def _resources():
    from disco.agent_server.run_registry import RunResourceRegistry

    return RunResourceRegistry()


def _persistence(resources):
    from disco.agent_server.workspace_persistence import WorkspacePersistence

    persistence = WorkspacePersistence.__new__(WorkspacePersistence)
    persistence._projects = _AvailableProjects()
    persistence._run_resources = resources
    persistence._persistence_notifier = _Notifier()
    return persistence


def _set_executor(rt, cid, workspace, **attrs):  # noqa: ANN001, ANN201
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=workspace, **attrs))


def _runtime(
    event_store: SqliteEventStore, tmp_path: Path
) -> tuple[ConversationRuntime, ProjectStore]:
    rt = ConversationRuntime(
        event_store, router=MagicMock(), sandbox_service=ProcessSandboxService()
    )
    projects = ProjectStore(str(tmp_path / "projects"))
    rt.projects.current_project_store = MagicMock(return_value=projects)  # type: ignore[method-assign]
    rt.lifecycle._sandbox.current_project_store = MagicMock(return_value=projects)  # type: ignore[method-assign]
    return rt, projects


async def _seed_running(store: SqliteEventStore, cid: str) -> None:
    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message={"role": "user", "content": "build it"},
        ),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))


async def _append_user_intent(
    store: SqliteEventStore,
    conversation_id: str,
    content: str,
    *,
    batched: bool,
    old_agent_content: str | None = None,
) -> WorkspaceMutationEvent:
    user = MessageEvent(
        source=EventSource.USER,
        message={"role": "user", "content": content},
    )
    intent = WorkspaceMutationEvent(
        operation="agent.run-intent.user-turn",
        run_protocol_version=1,
    )
    events: list[Event] = [user, intent]
    if old_agent_content is not None:
        events.append(
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": old_agent_content},
            )
        )
    if batched:
        await store.append_many(conversation_id, events)
    else:
        await store.append(conversation_id, user)
        await store.append(conversation_id, intent)
        if len(events) == 3:
            await store.append(conversation_id, events[-1])
    return intent


async def _append_view_progress(
    store: SqliteEventStore,
    conversation_id: str,
    intent: WorkspaceMutationEvent,
    view_id: str,
    assistant_content: str,
    *,
    running: bool,
    running_view_id: bool = True,
) -> None:
    events: list[Event] = [
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            agent_view_id=view_id,
            run_protocol_version=1,
        ),
        MessageEvent(
            source=EventSource.AGENT,
            message={"role": "assistant", "content": assistant_content},
            agent_view_id=view_id,
        ),
    ]
    if running:
        events.append(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                agent_view_id=view_id if running_view_id else None,
            )
        )
    await store.append_many(conversation_id, events)


async def _running_runtime(
    event_store: SqliteEventStore,
    tmp_path: Path,
    conversation_id: str,
    files: dict[str, bytes],
    **executor_attrs: object,
) -> tuple[ConversationRuntime, ProjectStore]:
    runtime, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, conversation_id)
    _set_executor(
        runtime,
        conversation_id,
        _Workspace(files),
        **executor_attrs,
    )
    return runtime, projects


async def _make_loop() -> MagicMock:
    return MagicMock()


def _scripted_snapshot(
    result: SnapshotResult,
    *,
    files: dict[str, bytes] | None = None,
    seen_destinations: list[Path] | None = None,
) -> Callable[[object, Path], Awaitable[SnapshotResult]]:
    async def snapshot(_session: object, destination: Path) -> SnapshotResult:
        target = Path(destination)
        if seen_destinations is not None:
            seen_destinations.append(target)
        target.mkdir(parents=True)
        for relative_path, data in (files or {}).items():
            output = target / relative_path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        return result

    return snapshot


def _snapshot_with_skips(
    skipped: list[str],
    *,
    written_files: dict[str, bytes] | None = None,
    reported_files: dict[str, int] | None = None,
    seen_destinations: list[Path] | None = None,
) -> Callable[[object, Path], Awaitable[SnapshotResult]]:
    reported = (
        reported_files
        if reported_files is not None
        else {path: len(data) for path, data in (written_files or {}).items()}
    )
    return _scripted_snapshot(
        SnapshotResult(
            file_count=len(reported),
            total_bytes=sum(reported.values()),
            paths=list(reported),
            skipped=skipped,
        ),
        files=written_files,
        seen_destinations=seen_destinations,
    )


def _interrupt_before_version(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("simulated crash before immutable version publication")


def _interrupt_final_seal(
    real_append: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    async def interrupt(conversation_id: str, event: Event) -> Any:
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None:
            raise RuntimeError("simulated crash before seal event append")
        return await real_append(conversation_id, event)

    return interrupt


def _gated_snapshot(
    real_snapshot: Callable[..., Awaitable[SnapshotResult]],
    entered: asyncio.Event,
    release: asyncio.Event,
    *,
    failure: Exception | None = None,
    cancelled: asyncio.Event | None = None,
) -> Callable[[object, Path], Awaitable[SnapshotResult]]:
    async def snapshot(session: object, destination: Path) -> SnapshotResult:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if cancelled is not None:
                cancelled.set()
            raise
        if failure is not None:
            raise failure
        return await real_snapshot(session, destination)

    return snapshot


def _recording_snapshot(
    real_snapshot: Callable[..., Awaitable[SnapshotResult]],
    order: list[str],
) -> Callable[[object, Path], Awaitable[SnapshotResult]]:
    async def snapshot(session: object, destination: Path) -> SnapshotResult:
        order.append("capture")
        return await real_snapshot(session, destination)

    return snapshot


class _ShadowFold:
    def __init__(
        self,
        store: SqliteEventStore,
        conversation_id: str,
        workspace: _Workspace,
        order: list[str],
    ) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._workspace = workspace
        self._order = order
        self.count = 0

    async def __call__(self, conversation_id: str, *, events: object = None) -> None:
        assert conversation_id == self._conversation_id and events is not None
        persisted = await self._store.get_events(conversation_id)
        marker = persisted[-1]
        assert isinstance(marker, WorkspaceMutationEvent)
        assert marker.operation == "agent.artifact-manifest-fold"
        assert marker.paths == (".disco/context/artifact_manifest.json",)
        assert marker.agent_view_id == "aview-shadow"
        assert not any(
            isinstance(event, StatusEvent) and event.status is ConversationStatus.FINISHED
            for event in persisted
        )
        self.count += 1
        self._order.append("fold")
        self._workspace.files["shadow-manifest.json"] = b'{"sealed":true}'


async def _record_host_mutation(
    runtime: ConversationRuntime,
    conversation_id: str,
    entered: asyncio.Event,
    *,
    attempting: asyncio.Event | None = None,
) -> None:
    if attempting is not None:
        attempting.set()
    async with runtime.workspace.mutation(
        conversation_id,
        "test.host-edit",
        paths=("index.html",),
    ):
        entered.set()


def _blocked_run(
    started: asyncio.Event,
    release: asyncio.Event,
) -> Callable[[str, object], Awaitable[str]]:
    async def run(_conversation_id: str, _loop: object) -> str:
        started.set()
        await release.wait()
        return "done"

    return run


async def _queued_committed_view(
    lock: asyncio.Lock,
    runtime: ConversationRuntime,
    conversation_id: str,
) -> None:
    async with lock:
        await runtime.workspace.require_committed_host_mirror_locked(conversation_id)


def _mutating_cut(
    real_cut: Callable[..., Any],
    path: Path,
    data: bytes,
    *,
    after: bool,
) -> Callable[..., Any]:
    def cut(*args: Any, **kwargs: Any) -> Any:
        if not after:
            path.write_bytes(data)
        version = real_cut(*args, **kwargs)
        if after:
            path.write_bytes(data)
        return version

    return cut


class _ConcurrentStoreWriter:
    def __init__(
        self,
        db_path: Path,
        conversation_id: str,
        events: list[Event],
    ) -> None:
        self._db_path = db_path
        self._conversation_id = conversation_id
        self._events = events
        self._started = threading.Event()
        self._done = threading.Event()
        self._errors: list[BaseException] = []
        self.plan = MagicMock(name="stale-plan")

    def append(self) -> None:
        secondary = SqliteEventStore(str(self._db_path))
        try:
            if not self._started.wait(timeout=2):
                raise TimeoutError("plan builder did not start")
            if len(self._events) == 1:
                asyncio.run(secondary.append(self._conversation_id, self._events[0]))
            else:
                asyncio.run(secondary.append_many(self._conversation_id, self._events))
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            self._errors.append(exc)
        finally:
            secondary.close()
            self._done.set()

    def build_plan(self, _workspace: object, _secrets: object) -> object:
        self._started.set()
        assert self._done.wait(timeout=2)
        return self.plan

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.append, daemon=True)
        thread.start()
        return thread

    def assert_finished(self, thread: threading.Thread) -> None:
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert self._errors == []


async def _finish(
    runtime: ConversationRuntime,
    conversation_id: str,
    *,
    agent_view_id: str | None = None,
) -> StatusEvent:
    return await runtime.lifecycle.commit_finished_workspace(
        conversation_id,
        StatusEvent(
            status=ConversationStatus.FINISHED,
            agent_view_id=agent_view_id,
        ),
    )


async def _final_seals(
    store: SqliteEventStore,
    conversation_id: str,
) -> list[WorkspaceVersionEvent]:
    return _sealed_versions(await store.get_events(conversation_id))


def _sealed_versions(events: list[Event]) -> list[WorkspaceVersionEvent]:
    return [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]


def _only_sealed_version(events: list[Event]) -> WorkspaceVersionEvent:
    seals = _sealed_versions(events)
    assert len(seals) == 1
    return seals[0]


async def _only_final_seal(
    store: SqliteEventStore,
    conversation_id: str,
) -> WorkspaceVersionEvent:
    return _only_sealed_version(await store.get_events(conversation_id))


def _assert_no_versions(events: list[Event]) -> None:
    assert not any(isinstance(event, WorkspaceVersionEvent) for event in events)


def _assert_no_final_seals(events: list[Event]) -> None:
    assert not _sealed_versions(events)


def _journal_path(projects: ProjectStore, conversation_id: str) -> Path:
    return projects.manifest_for(conversation_id).parent / "finalization-v1.json"


def _journal_phase(projects: ProjectStore, conversation_id: str) -> str:
    return str(json.loads(_journal_path(projects, conversation_id).read_text())["phase"])


async def _seed_shadow_fold_view(
    store: SqliteEventStore,
    conversation_id: str,
) -> None:
    await _seed_running(store, conversation_id)
    intent = await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append_many(
        conversation_id,
        [
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="aview-shadow",
                run_protocol_version=1,
            ),
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": "built the artifact"},
                agent_view_id="aview-shadow",
            ),
        ],
    )


async def _seed_winning_shadow_views(
    store: SqliteEventStore,
    conversation_id: str,
) -> None:
    await store.append(
        conversation_id,
        MessageEvent(
            source=EventSource.USER,
            message={"role": "user", "content": "build it"},
        ),
    )
    intent = await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append_many(
        conversation_id,
        [
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-losing",
                run_protocol_version=1,
            ),
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-winning",
                run_protocol_version=1,
            ),
            DeliverableEvent(
                title="late losing output",
                path="stale.txt",
                artifact_kind="files",
                agent_view_id="view-losing",
            ),
            DeliverableEvent(
                title="winning output",
                path="current.txt",
                artifact_kind="files",
                agent_view_id="view-winning",
            ),
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": "done"},
                agent_view_id="view-winning",
            ),
            StatusEvent(
                status=ConversationStatus.RUNNING,
                agent_view_id="view-winning",
            ),
        ],
    )


async def _seed_idempotent_view(
    store: SqliteEventStore,
    conversation_id: str,
) -> None:
    await store.append(
        conversation_id,
        MessageEvent(
            source=EventSource.USER,
            message={"role": "user", "content": "build it"},
        ),
    )
    intent = await store.append(
        conversation_id,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await store.append_many(
        conversation_id,
        [
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="view-idempotent",
                run_protocol_version=1,
            ),
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": "done"},
                agent_view_id="view-idempotent",
            ),
            StatusEvent(
                status=ConversationStatus.RUNNING,
                agent_view_id="view-idempotent",
            ),
        ],
    )


async def _seed_host_authority_view(
    store: SqliteEventStore,
    conversation_id: str,
) -> None:
    stored = await store.append_many(
        conversation_id,
        [
            MessageEvent(
                source=EventSource.USER,
                message={"role": "user", "content": "build it"},
            ),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
        ],
    )
    intent = next(
        event
        for event in stored
        if isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.run-intent.user-turn"
    )
    await store.append_many(
        conversation_id,
        [
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="aview_host_base",
                run_protocol_version=1,
            ),
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": "complete"},
                agent_view_id="aview_host_base",
            ),
        ],
    )


def _assert_idempotent_finalization(events: list[Event]) -> None:
    assert (
        sum(
            isinstance(event, WorkspaceMutationEvent)
            and event.operation == "agent.artifact-manifest-fold"
            for event in events
        )
        == 1
    )
    assert len(_sealed_versions(events)) == 1


def _assert_shadow_fold_seal(
    events: list[Event],
    projects: ProjectStore,
    conversation_id: str,
) -> None:
    marker = next(
        event
        for event in events
        if isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.artifact-manifest-fold"
    )
    terminal = next(
        event
        for event in events
        if isinstance(event, StatusEvent) and event.status is ConversationStatus.FINISHED
    )
    assert marker.seq is not None and terminal.seq is not None
    assert marker.seq < terminal.seq
    seal = _only_sealed_version(events)
    assert seal.final_seal is not None
    assert seal.final_seal.latest_effect_seq == marker.seq
    with projects.open_verified_version(conversation_id, seal.version_seq) as verified:
        assert verified.read_bytes("shadow-manifest.json") == b'{"sealed":true}'


async def _assert_host_mirror_authority(
    runtime: ConversationRuntime,
    store: SqliteEventStore,
    projects: ProjectStore,
    conversation_id: str,
) -> None:
    async with runtime.workspace.lock(conversation_id):
        mutation = await runtime.workspace.record_mutation_locked(
            conversation_id,
            "host-write",
            paths=("index.html",),
        )
        (projects.path_for(conversation_id) / "index.html").write_bytes(b"host edit")
        before = await store.get_events(conversation_id)
        for terminal in (
            StatusEvent(
                status=ConversationStatus.FINISHED,
                host_mutation_id="wrong-mutation-id",
            ),
            StatusEvent(status=ConversationStatus.FINISHED),
        ):
            with pytest.raises(RuntimeError, match="matching mutation authority"):
                await runtime.lifecycle.commit_finished_host_mirror_locked(
                    conversation_id,
                    terminal,
                )
            assert await store.get_events(conversation_id) == before
        await runtime.lifecycle.commit_finished_host_mirror_locked(
            conversation_id,
            StatusEvent(
                status=ConversationStatus.FINISHED,
                host_mutation_id=mutation.id,
            ),
        )
    committed = resolve_committed_workspace(
        await store.get_events(conversation_id),
        projects,
        conversation_id,
    )
    assert committed.record.tree_digest == projects.inspect_workspace(conversation_id).tree_digest

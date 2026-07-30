from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.agent_server.appkit_cloudflare import routes as cloudflare_routes
from disco.agent_server.workspace_commit import (
    WorkspaceCommitUnavailable,
    pending_workspace_run_intent,
    resolve_committed_workspace,
)
from disco.agent_server.workspace_process_fence import workspace_process_lock_path
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
    derive_final_workspace_fence,
)
from disco.tools import ProcessSandboxService
from disco.tools.projects import ProjectStore, SnapshotResult, snapshot_workspace
from fastapi import HTTPException


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


@pytest.fixture
def event_store():
    store = SqliteEventStore(":memory:")
    yield store
    store.close()


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


@pytest.fixture(autouse=True)
def _fresh_deploy_lock_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "deploy-locks"))


def _runtime(
    event_store: SqliteEventStore, tmp_path: Path
) -> tuple[ConversationRuntime, ProjectStore]:
    rt = ConversationRuntime(
        event_store, router=MagicMock(), sandbox_service=ProcessSandboxService()
    )
    projects = ProjectStore(str(tmp_path / "projects"))
    # The lifecycle/persistence owners resolve the project store through
    # ``ProjectRuntimeService.current_project_store()`` (no longer through a
    # runtime-level ``_project_store_now``).  Override the owner so every
    # collaborator that asks for the current store receives the test's
    # temporary ``ProjectStore``.
    rt._projects.current_project_store = MagicMock(return_value=projects)  # type: ignore[method-assign]
    rt._lifecycle._sandbox.current_project_store = MagicMock(return_value=projects)  # type: ignore[method-assign]
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


async def _make_loop() -> MagicMock:
    return MagicMock()


async def test_finished_commit_seals_fresh_immutable_bytes(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-final-seal"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    _set_executor(rt, cid, _Workspace({"index.html": b"<h1>sealed</h1>"}))

    terminal = await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    events = await event_store.get_events(cid)
    commits = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert terminal.seq is not None
    assert len(commits) == 1
    commit = commits[0]
    assert commit.final_seal is not None
    assert commit.final_seal.terminal_seq == terminal.seq
    assert commit.final_seal.latest_effect_seq is None
    assert commit.final_seal.scope.identifier == cid
    record = projects.verify_version(cid, commit.version_seq)
    assert record.pinned is True
    assert commit.final_seal.file_count == record.file_count == 1
    assert commit.final_seal.total_bytes == record.total_bytes == 15
    assert commit.final_seal.tree_digest == record.tree_digest
    with projects.open_verified_version(cid, record.seq) as verified:
        assert verified.read_bytes("index.html") == b"<h1>sealed</h1>"
    assert not (projects.manifest_for(cid).parent / "finalization-v1.json").exists()


async def test_incomplete_capture_leaves_finished_honestly_unsealed(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-incomplete-seal"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"lost.txt": b"bytes"})))

    async def incomplete_snapshot(session, dest):  # noqa: ANN001
        dest.mkdir(parents=True)
        (dest / "old.txt").write_bytes(b"stale")
        return SnapshotResult(
            file_count=0,
            total_bytes=0,
            paths=[],
            skipped=["lost.txt: could not read or descend: transport reset"],
        )

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", incomplete_snapshot)
    terminal = await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    events = await event_store.get_events(cid)
    assert terminal.status is ConversationStatus.FINISHED
    assert not any(isinstance(event, WorkspaceVersionEvent) for event in events)
    journal = projects.manifest_for(cid).parent / "finalization-v1.json"
    assert json.loads(journal.read_text())["phase"] == "capturing"


async def test_content_refused_seal_discloses_typed_evidence(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REL-27 (F-27) — a strict seal refused on deterministic CONTENT must say
    so in TYPED meta, on the durable log, with the exact blocking entries.

    This is the product half of the harness classifier split: counted trial
    590005 was laundered into rerunnable INVALID_RUN because the only durable
    disclosure was post-terminal English prose."""
    cid = "conv-content-refused-seal"
    rt, _projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    _set_executor(rt, cid, _Workspace({"steer-dashboard/index.html": b"<h1>built</h1>"}))
    live_skips = [
        "dist: symlink excluded",
        "index.html: symlink excluded",
        "package.json: symlink excluded",
        "src/App.jsx: symlink excluded",
    ]

    async def symlink_snapshot(session, dest):  # noqa: ANN001
        del session
        dest.mkdir(parents=True)
        (dest / "steer-dashboard").mkdir()
        (dest / "steer-dashboard" / "index.html").write_bytes(b"<h1>built</h1>")
        return SnapshotResult(
            file_count=1,
            total_bytes=14,
            paths=["steer-dashboard/index.html"],
            skipped=list(live_skips),
        )

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", symlink_snapshot)
    terminal = await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    events = await event_store.get_events(cid)
    assert terminal.status is ConversationStatus.FINISHED
    assert not any(isinstance(event, WorkspaceVersionEvent) for event in events)
    disclosures = [
        e for e in events if isinstance(e, MessageEvent) and "persistence_failure" in e.meta
    ]
    assert len(disclosures) == 1
    failure = disclosures[0].meta["persistence_failure"]
    assert failure["kind"] == "seal_incomplete_content"
    assert failure["blocking"] == live_skips
    assert "snapshot failed" in disclosures[0].message.content


async def test_transient_refused_seal_never_claims_content(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The split's other direction — a transient capture failure (read error)
    still refuses the seal but must NOT emit the typed content disclosure:
    branding an infra hiccup as a non-rerunnable product failure would be the
    same laundering as F-27, reversed."""
    cid = "conv-transient-refused-seal"
    rt, _projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"lost.txt": b"bytes"})))

    async def transient_snapshot(session, dest):  # noqa: ANN001
        del session
        dest.mkdir(parents=True)
        return SnapshotResult(
            file_count=0,
            total_bytes=0,
            paths=[],
            skipped=["lost.txt: read failed: [Errno 5] I/O error"],
        )

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", transient_snapshot)
    terminal = await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    events = await event_store.get_events(cid)
    assert terminal.status is ConversationStatus.FINISHED
    assert not any(isinstance(event, WorkspaceVersionEvent) for event in events)
    # The seal refusal is still disclosed — but never as a content judgment.
    assert not any(isinstance(e, MessageEvent) and "persistence_failure" in e.meta for e in events)
    assert any(
        isinstance(e, MessageEvent)
        and e.message is not None
        and "snapshot failed" in (e.message.content or "")
        for e in events
    )


async def test_finish_sealability_probe_judges_content_only(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REL-27 probe unit — the probe runs the real snapshot seam against a
    throwaway destination and reports ONLY deterministic content blockers:
    allowed skips and transient failures never refuse a finish, symlinks do.
    No live sandbox ⇒ sealable (nothing to judge)."""
    cid = "conv-seal-probe"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"ok"})))
    seen_dests: list[Path] = []

    async def scripted_snapshot(session, dest):  # noqa: ANN001
        del session
        seen_dests.append(Path(dest))
        dest.mkdir(parents=True)
        return SnapshotResult(
            file_count=1,
            total_bytes=2,
            paths=["index.html"],
            skipped=[
                "node_modules/x: dependency/cache path excluded",
                ".env: runtime secret path excluded",
                "flaky.txt: read failed: transport reset",
                "dist: symlink excluded",
            ],
        )

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", scripted_snapshot)
    result = await rt._lifecycle.probe_finish_sealability(cid)
    assert result.sealable is False
    assert result.blocking == ("dist: symlink excluded",)
    # Side-effect-free on durable storage: the probe never wrote into the
    # ProjectStore authority, and its throwaway destination is gone.
    assert seen_dests and not seen_dests[0].exists()
    assert not projects.path_for(cid).exists()

    async def clean_snapshot(session, dest):  # noqa: ANN001
        del session
        dest.mkdir(parents=True)
        return SnapshotResult(
            file_count=1,
            total_bytes=2,
            paths=["index.html"],
            skipped=["node_modules/x: dependency/cache path excluded"],
        )

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", clean_snapshot)
    result = await rt._lifecycle.probe_finish_sealability(cid)
    assert result.sealable is True
    assert result.blocking == ()

    # No live sandbox ⇒ sealable, "nothing to judge" (commit-time authority).
    rt._run_resources.pop_executor(cid)
    result = await rt._lifecycle.probe_finish_sealability(cid)
    assert result.sealable is True
    assert "no live workspace" in result.detail


async def test_finalizer_and_host_mutation_share_one_barrier(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-final-barrier"
    rt, _projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"v1"})))
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()
    real_snapshot = __import__(
        "disco.tools.projects", fromlist=["snapshot_workspace"]
    ).snapshot_workspace

    async def blocked_snapshot(session, dest):  # noqa: ANN001
        capture_entered.set()
        await release_capture.wait()
        return await real_snapshot(session, dest)

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", blocked_snapshot)
    finishing = asyncio.create_task(
        rt._lifecycle.commit_finished_workspace(
            cid,
            StatusEvent(status=ConversationStatus.FINISHED),
        )
    )
    await capture_entered.wait()
    mutation_entered = asyncio.Event()

    async def mutate() -> None:
        async with rt.workspace_mutation(cid, "test.host-edit", paths=("index.html",)):
            mutation_entered.set()

    mutation = asyncio.create_task(mutate())
    await asyncio.sleep(0)
    assert not mutation_entered.is_set()
    release_capture.set()
    await finishing
    await mutation

    events = await event_store.get_events(cid)
    seal = next(
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    )
    host_edit = next(event for event in events if isinstance(event, WorkspaceMutationEvent))
    assert seal.seq is not None and host_edit.seq is not None and host_edit.seq > seal.seq
    with pytest.raises(ValueError, match="effect event must precede"):
        derive_final_workspace_fence(events)


async def test_snapshot_journal_without_sqlite_checkpoint_remains_unsealed(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-snapshot-recovery"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    _set_executor(rt, cid, _Workspace({"index.html": b"recover me"}))
    real_cut = projects.cut_verified_version

    def interrupt_before_version(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args, kwargs
        raise RuntimeError("simulated crash before immutable version publication")

    monkeypatch.setattr(projects, "cut_verified_version", interrupt_before_version)
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    journal_path = projects.manifest_for(cid).parent / "finalization-v1.json"
    assert json.loads(journal_path.read_text())["phase"] == "snapshot"
    assert not any(
        isinstance(event, WorkspaceVersionEvent) for event in await event_store.get_events(cid)
    )

    monkeypatch.setattr(projects, "cut_verified_version", real_cut)
    assert await rt._lifecycle._recover_finalization_journals() == 0
    assert json.loads(journal_path.read_text())["phase"] == "snapshot"
    assert not any(
        isinstance(event, WorkspaceVersionEvent) for event in await event_store.get_events(cid)
    )


async def test_snapshot_journal_refuses_changed_mirror_bytes(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-snapshot-tamper"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"before"})))
    real_cut = projects.cut_verified_version

    def interrupt_before_version(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args, kwargs
        raise RuntimeError("simulated crash before immutable version publication")

    monkeypatch.setattr(projects, "cut_verified_version", interrupt_before_version)
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    journal_path = projects.manifest_for(cid).parent / "finalization-v1.json"
    assert json.loads(journal_path.read_text())["phase"] == "snapshot"

    projects.path_for(cid).joinpath("index.html").write_bytes(b"after")
    monkeypatch.setattr(projects, "cut_verified_version", real_cut)
    assert await rt._lifecycle._recover_finalization_journals() == 0

    assert json.loads(journal_path.read_text())["phase"] == "snapshot"
    assert not any(
        isinstance(event, WorkspaceVersionEvent) for event in await event_store.get_events(cid)
    )


async def test_version_journal_recovers_after_event_append_interruption(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-version-recovery"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    _set_executor(rt, cid, _Workspace({"index.html": b"versioned"}))
    real_append = event_store.append

    async def interrupt_seal_append(conversation_id, event):  # noqa: ANN001, ANN202
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None:
            raise RuntimeError("simulated crash before seal event append")
        return await real_append(conversation_id, event)

    monkeypatch.setattr(event_store, "append", interrupt_seal_append)
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    journal_path = projects.manifest_for(cid).parent / "finalization-v1.json"
    journal = json.loads(journal_path.read_text())
    assert journal["phase"] == "checkpoint"
    version = projects.verify_version(cid, journal["version_seq"])
    assert version.pinned is True

    monkeypatch.setattr(event_store, "append", real_append)
    assert await rt._lifecycle._recover_finalization_journals() == 1

    events = await event_store.get_events(cid)
    commits = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert len(commits) == 1
    assert commits[0].final_seal is not None
    assert commits[0].version_seq == version.seq
    assert not journal_path.exists()


async def test_version_journal_refuses_live_workspace_drift_after_checkpoint(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-version-recovery-drift"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    _set_executor(rt, cid, _Workspace({"index.html": b"versioned"}))
    real_append = event_store.append

    async def interrupt_seal_append(conversation_id, event):  # noqa: ANN001, ANN202
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None:
            raise RuntimeError("simulated crash before seal event append")
        return await real_append(conversation_id, event)

    monkeypatch.setattr(event_store, "append", interrupt_seal_append)
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    journal_path = projects.manifest_for(cid).parent / "finalization-v1.json"
    journal = json.loads(journal_path.read_text())
    assert journal["phase"] == "checkpoint"

    projects.path_for(cid).joinpath("index.html").write_bytes(b"changed after checkpoint")
    monkeypatch.setattr(event_store, "append", real_append)
    assert await rt._lifecycle._recover_finalization_journals() == 0

    assert journal_path.is_file()
    assert not any(
        isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
        for event in await event_store.get_events(cid)
    )


async def test_recovery_refuses_older_version_without_matching_sqlite_checkpoint(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-older-journal-version"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"first"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    first_events = await event_store.get_events(cid)
    first_seal = next(
        event
        for event in first_events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    )
    first_record = projects.verify_version(cid, first_seal.version_seq)

    await event_store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    later_terminal = await event_store.append(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    assert isinstance(later_terminal, StatusEvent) and later_terminal.seq is not None
    journal_path = projects.manifest_for(cid).parent / "finalization-v1.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "conversation_id": cid,
                "terminal_event_id": later_terminal.id,
                "phase": "checkpoint",
                "terminal_seq": later_terminal.seq,
                "latest_effect_seq": None,
                "version_seq": first_record.seq,
                "file_count": first_record.file_count,
                "total_bytes": first_record.total_bytes,
                "tree_digest": first_record.tree_digest,
                "checkpoint_event_id": "attacker-selected-checkpoint",
            }
        )
    )

    assert await rt._lifecycle._recover_finalization_journals() == 0
    assert journal_path.is_file()
    assert not any(
        isinstance(event, WorkspaceVersionEvent)
        and event.final_seal is not None
        and event.final_seal.terminal_seq == later_terminal.seq
        for event in await event_store.get_events(cid)
    )


async def test_cancelled_finish_drains_failed_capture_then_reraises_cancellation(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-cancelled-failed-capture"
    rt, _projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"bytes"})))
    capture_entered = asyncio.Event()
    release_failure = asyncio.Event()

    async def failed_snapshot(session, dest):  # noqa: ANN001
        del session, dest
        capture_entered.set()
        await release_failure.wait()
        raise RuntimeError("simulated capture failure")

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", failed_snapshot)
    finishing = asyncio.create_task(
        rt._lifecycle.commit_finished_workspace(
            cid,
            StatusEvent(status=ConversationStatus.FINISHED),
        )
    )
    await capture_entered.wait()
    finishing.cancel()
    release_failure.set()

    with pytest.raises(asyncio.CancelledError):
        await finishing
    events = await event_store.get_events(cid)
    assert any(
        isinstance(event, StatusEvent) and event.status is ConversationStatus.FINISHED
        for event in events
    )
    assert not any(
        isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
        for event in events
    )


async def test_repeated_cancellation_cannot_cancel_successful_finish_seal(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-double-cancelled-seal"
    rt, _projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"complete"})))
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()
    capture_cancelled = asyncio.Event()
    real_snapshot = snapshot_workspace

    async def blocked_snapshot(session, dest):  # noqa: ANN001
        capture_entered.set()
        try:
            await release_capture.wait()
        except asyncio.CancelledError:
            capture_cancelled.set()
            raise
        return await real_snapshot(session, dest)

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", blocked_snapshot)
    finishing = asyncio.create_task(
        rt._lifecycle.commit_finished_workspace(
            cid,
            StatusEvent(status=ConversationStatus.FINISHED),
        )
    )
    await capture_entered.wait()
    finishing.cancel()
    await asyncio.sleep(0)
    finishing.cancel()
    await asyncio.sleep(0)
    assert not finishing.done()
    assert not capture_cancelled.is_set()

    release_capture.set()
    with pytest.raises(asyncio.CancelledError):
        await finishing
    assert not capture_cancelled.is_set()
    seals = [
        event
        for event in await event_store.get_events(cid)
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert len(seals) == 1


async def test_shadow_fold_bytes_are_captured_before_the_final_seal(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-shadow-before-seal"
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "1")
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    intent = await event_store.append(
        cid,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await event_store.append_many(
        cid,
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
    sandbox = _Workspace({"index.html": b"app"})
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=sandbox))
    order: list[str] = []
    fold_count = 0

    async def fold(conversation_id, *, events=None):  # noqa: ANN001
        nonlocal fold_count
        assert conversation_id == cid and events is not None
        persisted = await event_store.get_events(cid)
        marker = persisted[-1]
        assert isinstance(marker, WorkspaceMutationEvent)
        assert marker.operation == "agent.artifact-manifest-fold"
        assert marker.paths == (".disco/context/artifact_manifest.json",)
        assert marker.agent_view_id == "aview-shadow"
        assert not any(
            isinstance(event, StatusEvent) and event.status is ConversationStatus.FINISHED
            for event in persisted
        )
        fold_count += 1
        order.append("fold")
        sandbox.files["shadow-manifest.json"] = b'{"sealed":true}'

    async def capture(session, dest):  # noqa: ANN001
        order.append("capture")
        return await snapshot_workspace(session, dest)

    monkeypatch.setattr(rt._artifact_manifest_shadow, "fold", fold)
    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", capture)
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(
            status=ConversationStatus.FINISHED,
            agent_view_id="aview-shadow",
        ),
    )
    await rt._run_finalizer.finalize_clean(cid)

    assert order == ["fold", "capture"]
    assert fold_count == 1
    events = await event_store.get_events(cid)
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
    seal = next(
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    )
    assert seal.final_seal is not None
    assert seal.final_seal.latest_effect_seq == marker.seq
    with projects.open_verified_version(cid, seal.version_seq) as verified:
        assert verified.read_bytes("shadow-manifest.json") == b'{"sealed":true}'


async def test_shadow_fold_seals_winning_artifact_without_losing_view_output(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-shadow-winning-view"
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "1")
    rt, projects = _runtime(event_store, tmp_path)
    rt.set_surface(cid, "build")
    await event_store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message={"role": "user", "content": "build it"},
        ),
    )
    intent = await event_store.append(
        cid,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await event_store.append_many(
        cid,
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
    workspace = _Workspace(
        {
            "index.html": b"winner",
            "current.txt": b"current",
            "stale.txt": b"stale",
        }
    )
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=workspace, sandbox=workspace))

    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(
            status=ConversationStatus.FINISHED,
            agent_view_id="view-winning",
        ),
    )

    seal = next(
        event
        for event in await event_store.get_events(cid)
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    )
    with projects.open_verified_version(cid, seal.version_seq) as verified:
        manifest = verified.read_bytes(".disco/context/artifact_manifest.json")
    assert b"current.txt" in manifest
    assert b"stale.txt" not in manifest


async def test_shadow_fold_flag_off_records_no_effect(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-shadow-disabled"
    monkeypatch.delenv("DISCO_ARTIFACT_MANIFEST_SHADOW", raising=False)
    monkeypatch.delenv("PMX_ARTIFACT_MANIFEST_SHADOW", raising=False)
    rt, _projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"app"})))
    fold = AsyncMock()
    monkeypatch.setattr(rt._artifact_manifest_shadow, "fold", fold)

    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    fold.assert_not_awaited()
    assert not any(
        isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.artifact-manifest-fold"
        for event in await event_store.get_events(cid)
    )


async def test_host_mirror_finalizer_never_folds_sandbox_manifest(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-host-mirror-no-shadow-fold"
    monkeypatch.delenv("DISCO_ARTIFACT_MANIFEST_SHADOW", raising=False)
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"initial"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "1")
    fold = AsyncMock()
    monkeypatch.setattr(rt._artifact_manifest_shadow, "fold", fold)
    async with rt._workspace.lock(cid):
        async with rt._workspace.interprocess_mutation_fence(cid):
            await rt.record_workspace_mutation_locked(
                cid,
                "host-write",
                paths=("index.html",),
            )
            (projects.path_for(cid) / "index.html").write_bytes(b"host edit")
        await rt.finalize_host_mirror_change_locked(cid, "host-write")

    fold.assert_not_awaited()
    assert not any(
        isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.artifact-manifest-fold"
        for event in await event_store.get_events(cid)
    )


async def test_nonfinished_suspend_snapshot_serializes_with_host_mutation(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-nonfinished-snapshot-lock"
    rt, _projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    await event_store.append(cid, StatusEvent(status=ConversationStatus.PAUSED))
    _set_executor(rt, cid, _Workspace({"index.html": b"paused"}), kill=AsyncMock())
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()
    mutation_attempting = asyncio.Event()
    mutation_entered = asyncio.Event()

    async def blocked_snapshot(session, dest):  # noqa: ANN001
        capture_entered.set()
        await release_capture.wait()
        return await snapshot_workspace(session, dest)

    monkeypatch.setattr("disco.agent_server.lifecycle.snapshot_workspace", blocked_snapshot)
    suspending = asyncio.create_task(rt._lifecycle._suspend(cid))
    await capture_entered.wait()

    async def mutate() -> None:
        mutation_attempting.set()
        async with rt.workspace_mutation(cid, "test.host-edit", paths=("index.html",)):
            mutation_entered.set()

    mutation = asyncio.create_task(mutate())
    await mutation_attempting.wait()
    assert not mutation_entered.is_set()
    release_capture.set()
    await suspending
    await mutation
    assert mutation_entered.is_set()


async def test_forget_and_recreate_preserve_workspace_lock_identity(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-workspace-lock-aba"
    rt, _projects = _runtime(event_store, tmp_path)
    original = rt._workspace.lock(cid)

    await rt.forget_conversation(cid)

    assert rt._workspace.lock(cid) is original


async def test_inactive_finished_host_edit_gets_a_fresh_committed_revision(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-host-revision"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    sandbox = _Workspace({"index.html": b"before"})
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=sandbox))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    async with rt.workspace_mutation(cid, "deck.patch", paths=("index.html",)):
        sandbox.files["index.html"] = b"after"
    committed = await rt.finalize_host_workspace_change(cid, "deck.patch")

    events = await event_store.get_events(cid)
    seals = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert len(seals) == 2
    assert seals[-1].version_seq == committed.seq
    first_final = seals[0].final_seal
    latest_final = seals[-1].final_seal
    assert first_final is not None and latest_final is not None
    assert latest_final.terminal_seq > first_final.terminal_seq
    with projects.open_verified_version(cid, committed.seq) as verified:
        assert verified.read_bytes("index.html") == b"after"


async def test_locked_host_mirror_finalizer_seals_mirror_not_stale_sandbox(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-host-mirror-revision"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    sandbox = _Workspace({"index.html": b"sandbox-before"})
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=sandbox))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    async with rt._workspace.lock(cid):
        await rt.record_workspace_mutation_locked(
            cid,
            "cloudflare.deploy-record",
            paths=(".disco/cloudflare/deployments/abc.json",),
        )
        audit = projects.path_for(cid) / ".disco/cloudflare/deployments/abc.json"
        audit.parent.mkdir(parents=True)
        audit.write_bytes(b'{"deployment":"host-owned"}')
        # A sandbox recapture would publish this value and erase the host-only
        # record. The dedicated finalizer must consume the mirror directly.
        sandbox.files["index.html"] = b"stale-sandbox-copy"
        committed = await rt.finalize_host_mirror_change_locked(
            cid,
            "cloudflare.deploy-record",
        )

    with projects.open_verified_version(cid, committed.seq) as verified:
        assert verified.read_bytes("index.html") == b"sandbox-before"
        assert (
            verified.read_bytes(".disco/cloudflare/deployments/abc.json")
            == b'{"deployment":"host-owned"}'
        )

    events = await event_store.get_events(cid)
    seals = [
        event
        for event in events
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert len(seals) == 2
    assert seals[-1].final_seal is not None
    mutation = next(
        event
        for event in events
        if isinstance(event, WorkspaceMutationEvent)
        and event.operation == "cloudflare.deploy-record"
    )
    assert mutation.seq is not None
    assert seals[-1].final_seal.terminal_seq > mutation.seq


async def test_committed_host_mirror_guard_rejects_unsealed_drift(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-host-mirror-guard"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"sealed"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    async with rt._workspace.lock(cid):
        committed = await rt.require_committed_host_mirror_locked(cid)
        assert committed.record.tree_digest == projects.inspect_workspace(cid).tree_digest
        (projects.path_for(cid) / "index.html").write_bytes(b"drifted")
        with pytest.raises(WorkspaceCommitUnavailable, match="drifted"):
            await rt.require_committed_host_mirror_locked(cid)


async def test_host_mirror_finalizer_requires_lock_and_inactive_finished_head(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-host-mirror-preconditions"
    rt, projects = _runtime(event_store, tmp_path)
    projects.path_for(cid).mkdir(parents=True)
    (projects.path_for(cid) / "index.html").write_bytes(b"host")

    with pytest.raises(RuntimeError, match="requires the conversation lock"):
        await rt.finalize_host_mirror_change_locked(cid, "host-write")

    await _seed_running(event_store, cid)
    rt.kick = MagicMock()
    async with rt._workspace.lock(cid):
        await rt.record_workspace_mutation_locked(cid, "host-write", paths=("index.html",))
        # record_workspace_mutation_locked on a RUNNING head publishes a run
        # claim; clear it so the finalizer's inactive-FINISHED-head guard is the
        # one that fires (not the run-claim guard).
        rt._workspace.clear_run_claim(cid)
        with pytest.raises(RuntimeError, match="inactive FINISHED head"):
            await rt.finalize_host_mirror_change_locked(cid, "host-write")

    await event_store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    release_active = asyncio.Event()
    active = asyncio.create_task(release_active.wait())
    rt._run_registry._tasks[cid] = active
    rt._workspace._fences._admitted_runs.add(cid)
    try:
        async with rt._workspace.lock(cid):
            await rt.record_workspace_mutation_locked(cid, "host-write", paths=("index.html",))
            (projects.path_for(cid) / "index.html").write_bytes(b"host-edit")
            with pytest.raises(RuntimeError, match="agent run is active"):
                await rt.finalize_host_mirror_change_locked(cid, "host-write")
    finally:
        rt._workspace._fences._admitted_runs.discard(cid)
        release_active.set()
        await active
        rt._run_registry._tasks.pop(cid, None)

    events = await event_store.get_events(cid)
    assert not any(
        isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
        for event in events
    )


async def test_retrying_exact_finished_event_never_writes_after_terminal(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-finished-idempotent"
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "1")
    rt, _projects = _runtime(event_store, tmp_path)
    rt.set_surface(cid, "build")
    await event_store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message={"role": "user", "content": "build it"},
        ),
    )
    intent = await event_store.append(
        cid,
        WorkspaceMutationEvent(
            operation="agent.run-intent.user-turn",
            run_protocol_version=1,
        ),
    )
    await event_store.append_many(
        cid,
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
    workspace = _Workspace({"index.html": b"complete"})
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=workspace, sandbox=workspace))
    terminal = StatusEvent(
        status=ConversationStatus.FINISHED,
        agent_view_id="view-idempotent",
    )

    first = await rt._lifecycle.commit_finished_workspace(cid, terminal)
    before = await event_store.get_events(cid)
    second = await rt._lifecycle.commit_finished_workspace(cid, terminal)
    after = await event_store.get_events(cid)

    assert second == first
    assert after == before
    assert (
        sum(
            isinstance(event, WorkspaceMutationEvent)
            and event.operation == "agent.artifact-manifest-fold"
            for event in after
        )
        == 1
    )
    assert (
        sum(
            isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
            for event in after
        )
        == 1
    )


async def test_host_mirror_finalizer_rejects_wrong_authority_before_append(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-host-mirror-authority"
    rt, projects = _runtime(event_store, tmp_path)
    stored = await event_store.append_many(
        cid,
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
    await event_store.append_many(
        cid,
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
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"initial"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(
            status=ConversationStatus.FINISHED,
            agent_view_id="aview_host_base",
        ),
    )

    async with rt._workspace.lock(cid):
        mutation = await rt.record_workspace_mutation_locked(
            cid,
            "host-write",
            paths=("index.html",),
        )
        (projects.path_for(cid) / "index.html").write_bytes(b"host edit")
        before = await event_store.get_events(cid)
        for terminal in (
            StatusEvent(
                status=ConversationStatus.FINISHED,
                host_mutation_id="wrong-mutation-id",
            ),
            StatusEvent(status=ConversationStatus.FINISHED),
        ):
            with pytest.raises(RuntimeError, match="matching mutation authority"):
                await rt._lifecycle.commit_finished_host_mirror_locked(cid, terminal)
            assert await event_store.get_events(cid) == before

        await rt._lifecycle.commit_finished_host_mirror_locked(
            cid,
            StatusEvent(
                status=ConversationStatus.FINISHED,
                host_mutation_id=mutation.id,
            ),
        )

    committed = resolve_committed_workspace(await event_store.get_events(cid), projects, cid)
    assert committed.record.tree_digest == projects.inspect_workspace(cid).tree_digest


async def test_registered_run_waiting_on_fence_cannot_race_host_reseal(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task registration is not workspace admission.

    A run queued while a host mutation owns the permanent fence executes no run
    code and does not make the FINISHED head falsely active. It is admitted only
    after the host record has a new immutable seal.
    """
    cid = "conv-run-admission-fence"
    rt, projects = _runtime(event_store, tmp_path)
    rt.set_surface(cid, "build")
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"initial"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    run_started = asyncio.Event()
    release_run = asyncio.Event()

    async def fake_run(_conversation_id: str, _loop) -> str:  # noqa: ANN001
        run_started.set()
        await release_run.wait()
        return "done"

    monkeypatch.setattr(rt._run_execution, "run", fake_run)
    lock = rt._workspace.lock(cid)
    async with lock:
        task, _generation = rt._run_supervisor.create_task(cid, _make_loop())
        await asyncio.sleep(0)
        assert rt._run_registry._tasks[cid] is task
        assert not rt._workspace.has_admitted_run(cid)
        assert not run_started.is_set()

        await rt.record_workspace_mutation_locked(
            cid,
            "cloudflare.deploy-record",
            paths=(".disco/cloudflare/deployments/queued.json",),
        )
        record = projects.path_for(cid) / ".disco/cloudflare/deployments/queued.json"
        record.parent.mkdir(parents=True)
        record.write_bytes(b'{"status":"succeeded"}')
        committed = await rt.finalize_host_mirror_change_locked(
            cid,
            "cloudflare.deploy-record",
        )

    await asyncio.wait_for(run_started.wait(), timeout=1)
    assert rt._workspace.has_admitted_run(cid)
    with projects.open_verified_version(cid, committed.seq) as verified:
        assert verified.read_bytes(".disco/cloudflare/deployments/queued.json") == (
            b'{"status":"succeeded"}'
        )
    release_run.set()
    assert await task == "done"
    assert not rt._workspace.has_admitted_run(cid)
    rt._run_registry._tasks.pop(cid, None)


async def test_ingress_first_pending_run_blocks_already_queued_deploy(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deploy queued behind ingress cannot beat that ingress's run admission."""

    cid = "conv-ingress-first-claim"
    rt, _projects = _runtime(event_store, tmp_path)
    rt.set_surface(cid, "build")
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"sealed"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    run_started = asyncio.Event()
    release_run = asyncio.Event()

    async def fake_run(_conversation_id: str, _loop) -> str:  # noqa: ANN001
        run_started.set()
        await release_run.wait()
        return "done"

    monkeypatch.setattr(rt._run_execution, "run", fake_run)
    lock = rt._workspace.lock(cid)
    await lock.acquire()

    async def queued_deploy_preflight() -> None:
        async with lock:
            await rt.require_committed_host_mirror_locked(cid)

    deploy = asyncio.create_task(queued_deploy_preflight())
    await asyncio.sleep(0)  # place deploy first in the lock's waiter queue
    run, _generation = rt._run_supervisor.create_task(cid, _make_loop())
    rt._workspace.claim_registered_run_locked(cid)
    assert rt._workspace.has_run_claim(cid)
    lock.release()

    with pytest.raises(WorkspaceCommitUnavailable, match="agent run is active"):
        await deploy
    await asyncio.wait_for(run_started.wait(), timeout=1)
    release_run.set()
    assert await run == "done"
    rt._run_registry._tasks.pop(cid, None)


async def test_forget_clears_claim_when_queued_run_is_cancelled_before_entry(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-cancel-before-run-entry"
    rt, _projects = _runtime(event_store, tmp_path)
    rt.set_surface(cid, "build")
    lock = rt._workspace.lock(cid)
    await lock.acquire()
    same_lock = rt._workspace.lock(cid)
    task, _generation = rt._run_supervisor.create_task(cid, _make_loop())
    rt._workspace.claim_registered_run_locked(cid)
    assert rt._workspace.has_run_claim(cid)

    forgetting = asyncio.create_task(rt._workspace.forget(cid))
    await asyncio.sleep(0)
    assert not forgetting.done()
    lock.release()
    await forgetting

    assert task.cancelled()
    assert not rt._workspace.has_run_claim(cid)
    assert rt._workspace.lock(cid) is same_lock


async def test_durable_run_intent_blocks_a_second_runtime_from_old_seal(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-cross-runtime-run-intent"
    rt_a, projects = _runtime(event_store, tmp_path)
    rt_b, _unused = _runtime(event_store, tmp_path / "other-runtime")
    rt_a.set_surface(cid, "build")
    rt_b.set_surface(cid, "build")
    rt_b._projects.current_project_store = MagicMock(return_value=projects)  # type: ignore[method-assign]
    rt_b._lifecycle._sandbox.current_project_store = MagicMock(return_value=projects)  # type: ignore[method-assign]
    await _seed_running(event_store, cid)
    rt_a._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"sealed"})))
    await rt_a._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    rt_a.kick = MagicMock()
    rt_b.kick = MagicMock()

    await rt_a._disco_kernel.send_user_turn(cid, "change the headline")

    events = await event_store.get_events(cid)
    assert any(
        isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.run-intent.user-turn"
        for event in events
    )
    async with rt_b._workspace.lock(cid):
        with pytest.raises(WorkspaceCommitUnavailable):
            await rt_b.require_committed_host_mirror_locked(cid)
        # Record a mutation first to establish authority, then confirm the
        # pending run intent blocks finalization.
        await rt_b.record_workspace_mutation_locked(
            cid, "stale-host-finalizer", paths=("index.html",)
        )
        # The run-intent event reopens the workspace to RUNNING, so
        # record_workspace_mutation_locked publishes a run claim.  Clear it
        # so the inactive-FINISHED-head guard is the one that fires (not the
        # run-claim guard).
        rt_b._workspace.clear_run_claim(cid)
        with pytest.raises(RuntimeError, match="inactive FINISHED head"):
            await rt_b.finalize_host_mirror_change_locked(cid, "stale-host-finalizer")

    seals = [
        event
        for event in await event_store.get_events(cid)
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert len(seals) == 1


async def test_cancelled_process_fence_wait_leaves_no_partial_user_ingress(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    cid = "conv-cancelled-process-fence-wait"
    rt, projects = _runtime(event_store, tmp_path)
    rt.set_surface(cid, "build")
    rt.kick = MagicMock()
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "process-locks"))
    lock_path = workspace_process_lock_path(projects.path_for(cid))
    holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        sending = asyncio.create_task(rt._disco_kernel.send_user_turn(cid, "build it"))
        await asyncio.sleep(0.06)
        assert await event_store.get_events(cid) == []
        sending.cancel("owner disconnected")
        with pytest.raises(asyncio.CancelledError, match="owner disconnected"):
            await sending
        assert await event_store.get_events(cid) == []
        rt.kick.assert_not_called()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    stored = await rt._disco_kernel.send_user_turn(cid, "build it")
    events = await event_store.get_events(cid)
    assert stored in events
    assert sum(isinstance(event, MessageEvent) for event in events) == 1
    assert (
        sum(
            isinstance(event, WorkspaceMutationEvent)
            and event.operation == "agent.run-intent.user-turn"
            for event in events
        )
        == 1
    )


async def test_locked_restore_helper_rejects_missing_local_or_process_fence(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-restore-fence-precondition"
    rt, _projects = _runtime(event_store, tmp_path)

    with pytest.raises(RuntimeError, match="conversation lock"):
        await rt._workspace.restore_version_locked(cid, 1)
    async with rt._workspace.lock(cid):
        with pytest.raises(RuntimeError, match="process fence"):
            await rt._workspace.restore_version_locked(cid, 1)


async def test_resolver_rejects_structurally_valid_seal_that_launders_run_intent(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-manual-run-intent-launder"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"sealed"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    await event_store.append_many(
        cid,
        [
            MessageEvent(
                source=EventSource.USER,
                message={"role": "user", "content": "revise it"},
            ),
            WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": "late output from the old run"},
            ),
        ],
    )
    await event_store.append(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )

    with pytest.raises(WorkspaceCommitUnavailable, match="unprocessed agent run intent"):
        resolve_committed_workspace(await event_store.get_events(cid), projects, cid)


async def test_run_intent_requires_post_admission_agent_progress(
    event_store: SqliteEventStore,
) -> None:
    cid = "conv-run-intent-admission-order"
    stored = await event_store.append_many(
        cid,
        [
            MessageEvent(
                source=EventSource.USER,
                message={"role": "user", "content": "change it"},
            ),
            WorkspaceMutationEvent(
                operation="agent.run-intent.user-turn",
                run_protocol_version=1,
            ),
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": "old run tail"},
            ),
        ],
    )
    assert pending_workspace_run_intent(await event_store.get_events(cid)) is not None
    intent = next(
        e
        for e in stored
        if isinstance(e, WorkspaceMutationEvent) and e.operation == "agent.run-intent.user-turn"
    )
    await event_store.append(
        cid,
        WorkspaceMutationEvent(
            operation="agent.view-admitted",
            run_intent_id=intent.id,
            agent_view_id="aview_1",
            run_protocol_version=1,
        ),
    )
    assert pending_workspace_run_intent(await event_store.get_events(cid)) is not None
    await event_store.append(
        cid,
        MessageEvent(
            source=EventSource.AGENT,
            message={"role": "assistant", "content": "new run progress"},
            agent_view_id="aview_1",
        ),
    )
    assert pending_workspace_run_intent(await event_store.get_events(cid)) is None


async def test_admitted_run_with_real_progress_can_publish_final_workspace(
    event_store: SqliteEventStore,
    tmp_path: Path,
) -> None:
    cid = "conv-admitted-run-final-seal"
    rt, projects = _runtime(event_store, tmp_path)
    stored = await event_store.append_many(
        cid,
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
        e
        for e in stored
        if isinstance(e, WorkspaceMutationEvent) and e.operation == "agent.run-intent.user-turn"
    )
    await event_store.append_many(
        cid,
        [
            WorkspaceMutationEvent(
                operation="agent.view-admitted",
                run_intent_id=intent.id,
                agent_view_id="aview_s",
                run_protocol_version=1,
            ),
            MessageEvent(
                source=EventSource.AGENT,
                message={"role": "assistant", "content": "completed the build"},
                agent_view_id="aview_s",
            ),
            StatusEvent(status=ConversationStatus.RUNNING),
        ],
    )
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"complete"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED, agent_view_id="aview_s"),
    )

    committed = resolve_committed_workspace(await event_store.get_events(cid), projects, cid)
    assert committed.record.tree_digest == projects.inspect_workspace(cid).tree_digest


async def test_host_mirror_finalizer_fails_closed_if_mirror_changes_during_cut(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-host-mirror-race"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"initial"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    real_cut = projects.cut_verified_version

    def raced_cut(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        (projects.path_for(cid) / "index.html").write_bytes(b"raced")
        return real_cut(*args, **kwargs)

    monkeypatch.setattr(projects, "cut_verified_version", raced_cut)
    async with rt._workspace.lock(cid):
        await rt.record_workspace_mutation_locked(cid, "host-write", paths=("index.html",))
        (projects.path_for(cid) / "index.html").write_bytes(b"intended")
        with pytest.raises(WorkspaceCommitUnavailable):
            await rt.finalize_host_mirror_change_locked(cid, "host-write")

    seals = [
        event
        for event in await event_store.get_events(cid)
        if isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
    ]
    assert len(seals) == 1


async def test_host_mirror_finalizer_rejects_drift_after_version_cut_returns(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-host-mirror-post-cut-race"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_running(event_store, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"initial"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    before = await event_store.get_events(cid)
    before_seals = sum(
        isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
        for event in before
    )
    before_checkpoints = sum(
        isinstance(event, WorkspaceVersionEvent) and event.final_seal is None for event in before
    )
    real_cut = projects.cut_verified_version

    def post_cut_drift(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        version = real_cut(*args, **kwargs)
        (projects.path_for(cid) / "index.html").write_bytes(b"drift-after-version-cut")
        return version

    monkeypatch.setattr(projects, "cut_verified_version", post_cut_drift)
    async with rt._workspace.lock(cid):
        async with rt._workspace.interprocess_mutation_fence(cid):
            await rt.record_workspace_mutation_locked(cid, "host-write", paths=("index.html",))
            (projects.path_for(cid) / "index.html").write_bytes(b"intended-host-bytes")
            with pytest.raises(WorkspaceCommitUnavailable):
                await rt.finalize_host_mirror_change_locked(cid, "host-write")

    after = await event_store.get_events(cid)
    assert (
        sum(
            isinstance(event, WorkspaceVersionEvent) and event.final_seal is not None
            for event in after
        )
        == before_seals
    )
    assert (
        sum(
            isinstance(event, WorkspaceVersionEvent) and event.final_seal is None for event in after
        )
        == before_checkpoints
    )
    async with rt._workspace.lock(cid):
        with pytest.raises(WorkspaceCommitUnavailable):
            await rt.require_committed_host_mirror_locked(cid)


async def test_dry_deploy_plan_revalidates_durable_head_after_synchronous_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-dry-plan-late-intent"
    db_path = tmp_path / "events.sqlite3"
    primary = SqliteEventStore(str(db_path))
    primary.create_conversation(cid, owner_id="local")
    rt, projects = _runtime(primary, tmp_path)
    await _seed_running(primary, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"sealed"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "process-locks"))
    writer_start = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[BaseException] = []

    def append_from_second_connection() -> None:
        secondary = SqliteEventStore(str(db_path))
        try:
            if not writer_start.wait(timeout=2):
                raise TimeoutError("plan builder did not start")
            asyncio.run(
                secondary.append_many(
                    cid,
                    [
                        MessageEvent(
                            source=EventSource.USER,
                            message={"role": "user", "content": "change it"},
                        ),
                        WorkspaceMutationEvent(operation="agent.run-intent.user-turn"),
                    ],
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            writer_errors.append(exc)
        finally:
            secondary.close()
            writer_done.set()

    sentinel_plan = MagicMock(name="stale-plan")

    def blocked_build_plan(_workspace, _secrets):  # noqa: ANN001, ANN202
        writer_start.set()
        assert writer_done.wait(timeout=2)
        return sentinel_plan

    writer = threading.Thread(target=append_from_second_connection, daemon=True)
    writer.start()
    monkeypatch.setattr(cloudflare_routes.cf, "build_plan", blocked_build_plan)
    gate = cloudflare_routes._CommittedWorkspaceGate(rt)
    try:
        with pytest.raises(HTTPException) as caught:
            await cloudflare_routes._build_committed_plan(gate, cid, MagicMock())
        assert caught.value.status_code == 409
        assert caught.value.detail["reason"] == "workspace_not_committed"
        writer.join(timeout=2)
        assert not writer.is_alive()
        assert writer_errors == []
        assert projects.path_for(cid).is_dir()
    finally:
        primary.close()


# --- [11] Cloudflare dry-plan: lone USER changes event_head_seq ---------------


async def test_dry_plan_lone_user_changes_event_head_seq(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "conv-dry-plan-lone-user"
    db_path = tmp_path / "events.sqlite3"
    primary = SqliteEventStore(str(db_path))
    primary.create_conversation(cid, owner_id="local")
    rt, projects = _runtime(primary, tmp_path)
    await _seed_running(primary, cid)
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=_Workspace({"index.html": b"sealed"})))
    await rt._lifecycle.commit_finished_workspace(
        cid,
        StatusEvent(status=ConversationStatus.FINISHED),
    )
    monkeypatch.setenv("DISCO_DEPLOY_LOCK_DIR", str(tmp_path / "process-locks"))
    writer_start = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[BaseException] = []

    def append_lone_user() -> None:
        secondary = SqliteEventStore(str(db_path))
        try:
            if not writer_start.wait(timeout=2):
                raise TimeoutError("plan builder did not start")
            asyncio.run(
                secondary.append(
                    cid,
                    MessageEvent(
                        source=EventSource.USER,
                        message={"role": "user", "content": "change it"},
                    ),
                )
            )
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            secondary.close()
            writer_done.set()

    sentinel_plan = MagicMock(name="stale-plan")

    def blocked_build_plan(_workspace, _secrets):
        writer_start.set()
        assert writer_done.wait(timeout=2)
        return sentinel_plan

    writer = threading.Thread(target=append_lone_user, daemon=True)
    writer.start()
    monkeypatch.setattr(cloudflare_routes.cf, "build_plan", blocked_build_plan)
    gate = cloudflare_routes._CommittedWorkspaceGate(rt)
    try:
        with pytest.raises(HTTPException) as caught:
            await cloudflare_routes._build_committed_plan(gate, cid, MagicMock())
        assert caught.value.status_code == 409
        writer.join(timeout=2)
        assert not writer.is_alive()
        assert writer_errors == []
        assert projects.path_for(cid).is_dir()
    finally:
        primary.close()


# ---- the seal must not race the executor's lifetime ---------------------------


@pytest.mark.asyncio
async def test_seal_uses_the_sandbox_pinned_at_the_terminal_not_a_later_lookup():
    """The seal used to re-resolve `runtime._executors[cid]` AFTER appending
    FINISHED. Anything dropping the executor in that window left the build
    unsealed — "no sandbox (executor=None) — workspace NOT persisted", then
    "FINISHED remains unsealed" — so no durable version existed and a later
    restore 503'd. A finished build was silently unsaved (pilot seed 406546,
    p4_ff_import_rollback). The reference is now pinned while the producing run is
    still live.
    """
    persistence = _persistence(_resources())  # no executor → pinned path
    pinned = object()

    resolved = await persistence._resolve_capture_session(
        "conv_pin", seal_fence=(7, None), pinned_session=pinned
    )

    assert resolved is not None, "a pinned sandbox must survive a dropped executor"
    session, _store = resolved
    assert session is pinned


@pytest.mark.asyncio
async def test_seal_still_fails_closed_when_nothing_was_pinned_and_nothing_is_live():
    """Regression guard: the pin is a window fix, not a way to seal without a
    sandbox. With no pin and no live executor the strict path still raises."""
    persistence = _persistence(_resources())  # no executor, no pin

    with pytest.raises(RuntimeError, match="no live sandbox"):
        await persistence._resolve_capture_session("conv_pin", seal_fence=(7, None))


@pytest.mark.asyncio
async def test_pinned_sandbox_yields_to_a_rotated_live_one():
    """A pin must never be WORSE than the lookup it replaces.

    If the box rotated between the terminal and the seal, the pinned reference is
    a dead generation — its workspace directory is already gone, and sealing
    against it fails on paths that no longer exist. The pin closes the
    dropped-executor window; it does not bind the seal to a corpse.
    """
    live = object()

    class _Executor:
        _sandbox = live

    run_resources = _resources()
    run_resources.set_executor("conv_rot", _Executor())  # type: ignore[arg-type]
    persistence = _persistence(run_resources)
    stale = object()

    resolved = await persistence._resolve_capture_session(
        "conv_rot", seal_fence=(9, None), pinned_session=stale
    )

    assert resolved is not None
    session, _store = resolved
    assert session is live, "a rotated generation must lose to the live sandbox"

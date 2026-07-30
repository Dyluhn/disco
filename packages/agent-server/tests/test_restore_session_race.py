"""F-28 (pilot seed 620108, p4_appkit_rollback) — a post-terminal workspace
restore raced the executor teardown: the session was acquired while the
container was dying, the restore's apply stage failed mid-flight, the product
logged NOTHING, and the client saw a bare 503. The identical restore replayed
moments later succeeded (HTTP 200) — the failure was moment-conditional.

Contract pinned here:
  1. an acquired-but-dying session must not kill the restore — the service
     reacquires ONCE through the existing wake/create chain and completes;
  2. the retry is bounded — if the fresh session also fails, the restore
     fails typed AND LOGGED, with no restore events and no version cut;
  3. every failed restore leaves a log line naming the conversation (the
     original incident was invisible outside the HTTP status line).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from disco.agent_server.runtime import ConversationRuntime
from disco.agent_server.workspace_service import WorkspaceRestoreStorageError
from disco.core import (
    ConversationStatus,
    SqliteEventStore,
    StatusEvent,
    WorkspaceRestoredEvent,
)
from disco.tools.projects import ProjectStore
from disco.tools.sandbox.base import ExecResult

CID = "conv_restore_race"


class _HealthySession:
    """Mirror of test_workspace_versions._MemorySession (healthy path)."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.clears = 0

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        assert "rm -rf" in cmd
        self.files.clear()
        self.clears += 1
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path.strip("/")] = data

    async def read_file(self, path: str) -> bytes:
        norm = path.strip("./")
        if norm in self.files:
            return self.files[norm]
        raise FileNotFoundError(norm)

    async def list_dir(self, path: str) -> list[str]:
        norm = "" if path in ("", ".") else path.strip("/")
        prefix = f"{norm}/" if norm else ""
        children: set[str] = set()
        for rel in self.files:
            if rel.startswith(prefix) and rel[len(prefix) :]:
                children.add(rel[len(prefix) :].split("/", 1)[0])
        if not children and norm and norm not in self.files:
            raise FileNotFoundError(norm)
        return sorted(children)


class _DyingSession(_HealthySession):
    """A session whose container is being torn down: every exec fails."""

    def __init__(self) -> None:
        super().__init__()
        self.exec_attempts = 0

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self.exec_attempts += 1
        raise ConnectionError("container is being removed")


def _runtime(store: SqliteEventStore, project_root: Path) -> ConversationRuntime:
    rt = ConversationRuntime(store, router=MagicMock())
    cfg = MagicMock()
    cfg.projects.projects_root = str(project_root)
    rt._config_store = MagicMock()
    rt._config_store.load.return_value = cfg
    return rt


def _write_workspace(ps: ProjectStore, cid: str, files: dict[str, bytes]) -> None:
    workspace = ps.path_for(cid)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, data in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        total += len(data)
    ps.write_manifest(
        cid,
        title="Race build",
        owner_id="local",
        created_at="2026-07-03T00:00:00+00:00",
        file_count=len(files),
        total_bytes=total,
    )


def _seed_versions(rt: ConversationRuntime) -> tuple[Any, ProjectStore]:
    ps = rt._projects.current_project_store()
    _write_workspace(ps, CID, {"index.html": b"old"})
    old = ps.cut_version(CID, trigger="turn")
    assert old is not None
    _write_workspace(ps, CID, {"index.html": b"new"})
    assert ps.cut_version(CID, trigger="turn") is not None
    return old, ps


async def test_restore_survives_a_dying_session_by_reacquiring_once(tmp_path: Path) -> None:
    """RED on pre-F-28 bytes (the 620108 signature): the registered session
    dies under the restore; the service must reacquire through the existing
    wake/create chain and COMPLETE the restore, not 503."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store, tmp_path)
    old, ps = _seed_versions(rt)

    dying = _DyingSession()
    rt._executors[CID] = cast(Any, SimpleNamespace(_sandbox=dying))
    fresh = _HealthySession({"index.html": b"new"})

    def _create_loop(cid: str) -> Any:
        # The documented creation seam (_restore_session -> _loop_for) —
        # production registers a fresh executor here; mirror that effect.
        rt._executors[cid] = cast(Any, SimpleNamespace(_sandbox=fresh))
        return SimpleNamespace()

    rt._loop_for = _create_loop  # type: ignore[method-assign]

    result = await rt.restore_workspace_version(CID, old.seq)

    assert dying.exec_attempts == 1, "the dying session was never even tried"
    assert fresh.clears == 1, "the reacquired session never applied the restore"
    assert fresh.files == {"index.html": b"old"}
    assert result["restored"] == old.seq
    assert result["new_version"] is not None
    events = await store.get_events(CID)
    assert [e for e in events if isinstance(e, WorkspaceRestoredEvent)], (
        "restore completed without durable lifecycle evidence"
    )
    versions = ps.list_versions(CID)
    assert versions[0].trigger == "restore"


async def test_restore_retry_is_bounded_and_failure_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """RED on pre-F-28 bytes (observability half): when the stale session AND
    the reacquired session both fail, the restore fails typed after exactly
    two apply attempts, LEAVES A LOG LINE naming the conversation (the
    original incident logged nothing), appends no restore events, and cuts
    no version."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store, tmp_path)
    old, ps = _seed_versions(rt)
    versions_before = [v.seq for v in ps.list_versions(CID)]

    first = _DyingSession()
    second = _DyingSession()
    rt._executors[CID] = cast(Any, SimpleNamespace(_sandbox=first))

    def _create_loop(cid: str) -> Any:
        rt._executors[cid] = cast(Any, SimpleNamespace(_sandbox=second))
        return SimpleNamespace()

    rt._loop_for = _create_loop  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        with pytest.raises(WorkspaceRestoreStorageError):
            await rt.restore_workspace_version(CID, old.seq)

    assert first.exec_attempts == 1
    assert second.exec_attempts == 1, "bounded retry must try the fresh session exactly once"
    assert any(CID in rec.message for rec in caplog.records), (
        "a failed restore must leave a product log line naming the conversation "
        "(F-28: the original 503 was invisible outside the HTTP status)"
    )
    events = await store.get_events(CID)
    assert not [e for e in events if isinstance(e, WorkspaceRestoredEvent)]
    assert [v.seq for v in ps.list_versions(CID)] == versions_before, (
        "a failed restore must not cut a version"
    )


async def test_restore_reacquire_never_reuses_the_failed_session(tmp_path: Path) -> None:
    """CONTROL — the reacquisition must not hand back the very session object
    that just failed (live_session still returns the stale registration until
    something replaces it). If creation yields nothing new, fail typed —
    never loop on the dead session."""
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")
    rt = _runtime(store, tmp_path)
    old, _ps = _seed_versions(rt)

    dying = _DyingSession()
    rt._executors[CID] = cast(Any, SimpleNamespace(_sandbox=dying))
    # Creation seam does NOT replace the executor (creation failed silently).
    rt._loop_for = lambda cid: SimpleNamespace()  # type: ignore[method-assign]

    with pytest.raises(WorkspaceRestoreStorageError):
        await rt.restore_workspace_version(CID, old.seq)

    assert dying.exec_attempts == 1, "the failed session must never be retried as if it were fresh"

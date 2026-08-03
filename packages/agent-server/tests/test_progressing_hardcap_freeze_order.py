"""Kill-before-freeze loses a progressing run's work; pause-and-persist preserves it.

Counted context seeds 460004/460005 died `MISSING_REQUIRED_EVIDENCE` on
`.pmx/screenshots/0001-navigate.png`. That file was merely the FIRST referenced
missing path: the harness's progressing-hard-cap stop calls `/kill`, product kill
correctly destroys the executor/sandbox WITHOUT snapshotting, and the harness then
reads the host ProjectStore — which still holds only the original import snapshot.
Every edit, REPORT.md and every screenshot is absent, not just the one named.

The supported preservation path already exists and needs no product change: a Build
run that ends PAUSED is snapshotted by `_run_with_persistence`'s end-gate under
`workspace_lock`, emitting a durable WorkspaceVersionEvent and an immutable
ProjectStore version. Pause first, copy the exact immutable version the event names,
then kill for destructive cleanup.

Provider-free: the router is a MagicMock and no model is ever called.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceVersionEvent,
)
from disco.tools import ProcessSandboxService
from disco.tools.projects import ProjectStore

# The two artifacts a progressing run has already produced when the cap fires.
CHANGED_ARTIFACT = "REPORT.md"
CHANGED_BYTES = b"# Catalog Audit 460004\n\nthree ranges compared\n"
KNOWN_PNG = ".pmx/screenshots/0001-navigate.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"progressing-run-evidence" * 4

IMPORT_ONLY = {"index.html": b"<h1>imported</h1>"}


class _Workspace:
    """Minimal live-sandbox surface (same shape test_final_workspace_commit uses)."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.destroyed = False

    async def list_dir(self, path: str) -> list[str]:
        if self.destroyed:
            raise RuntimeError("sandbox destroyed")
        if path in {"", "."}:
            return sorted(self.files)
        raise RuntimeError("not a directory")

    async def read_file(self, path: str) -> bytes:
        if self.destroyed:
            raise RuntimeError("sandbox destroyed")
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


@pytest.fixture
def event_store():
    store = SqliteEventStore(":memory:")
    yield store
    store.close()


def _runtime(store: SqliteEventStore, tmp_path: Path) -> tuple[ConversationRuntime, ProjectStore]:
    rt = ConversationRuntime(
        store,
        router=MagicMock(),  # provider-free: no model is ever consulted
        sandbox_service=ProcessSandboxService(),
    )
    projects = ProjectStore(str(tmp_path / "projects"))
    rt.projects.current_project_store = MagicMock(return_value=projects)
    return rt, projects


async def _seed_progressing(store: SqliteEventStore, cid: str) -> None:
    await store.append(
        cid,
        MessageEvent(source=EventSource.USER, message={"role": "user", "content": "audit it"}),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))


def _live_workspace() -> _Workspace:
    """The sandbox as it stands mid-run: import bytes PLUS the run's own work."""
    return _Workspace({**IMPORT_ONLY, CHANGED_ARTIFACT: CHANGED_BYTES, KNOWN_PNG: PNG_BYTES})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _version_events(store: SqliteEventStore, cid: str) -> list[WorkspaceVersionEvent]:
    return [e for e in await store.get_events(cid) if isinstance(e, WorkspaceVersionEvent)]


async def test_kill_before_freeze_loses_the_runs_work(
    event_store: SqliteEventStore, tmp_path: Path
) -> None:
    """NEGATIVE CONTROL — the defect, reproduced.

    Kill first, then read ProjectStore: the store never received the run's bytes, so
    both the changed artifact and the PNG are gone. `0001-navigate.png` is simply the
    first referenced path that happens to be missing.
    """
    cid = "conv-hardcap-killfirst"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_progressing(event_store, cid)
    sandbox = _live_workspace()
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=sandbox))

    # Destructive kill FIRST (what the harness does today).
    sandbox.destroyed = True
    rt._run_resources.pop_executor(cid)

    # Now the harness reads the durable store — the only source collect_workspace uses.
    versions = await _version_events(event_store, cid)
    assert versions == [], "no version was ever emitted for a killed progressing run"

    manifest_dir = projects.manifest_for(cid).parent
    stored = {p.name for p in manifest_dir.rglob("*")} if manifest_dir.exists() else set()
    assert CHANGED_ARTIFACT not in stored
    assert Path(KNOWN_PNG).name not in stored


async def test_pause_and_persist_preserves_exact_bytes_then_kill(
    event_store: SqliteEventStore, tmp_path: Path
) -> None:
    """POSITIVE — the corrected ordering, using only existing product behaviour.

    Freeze via the PAUSED end-gate, verify and copy the EXACT immutable version the
    durable event names (never the mutable store head, never the live sandbox), then
    kill for cleanup.
    """
    cid = "conv-hardcap-pausefirst"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_progressing(event_store, cid)
    sandbox = _live_workspace()
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=sandbox))

    # 1. pre-pause watermarks and run authority
    events_before = await event_store.get_events(cid)
    pre_seq = max((e.seq or -1) for e in events_before)
    versions_before = len(await _version_events(event_store, cid))

    # 2-4. land PAUSED, then the end-gate snapshot the product already performs
    paused = await event_store.append(cid, StatusEvent(status=ConversationStatus.PAUSED))
    async with rt.workspace.lock(cid):  # the product's own fencing
        await rt.lifecycle._maybe_snapshot(cid, trigger=ConversationStatus.PAUSED.value)

    versions = await _version_events(event_store, cid)
    assert len(versions) == versions_before + 1, "PAUSED end-gate must emit a durable version"
    frozen = versions[-1]
    assert frozen.trigger == ConversationStatus.PAUSED.value
    assert (frozen.seq or -1) > (paused.seq or -1), "the version must FOLLOW the PAUSED status"
    assert (frozen.seq or -1) > pre_seq

    # 5. a numerically new version_seq is NOT required (unchanged trees may dedup)
    assert frozen.version_seq is not None

    # 6. verify and copy the EXACT immutable version this event names
    # `verify_version` returning a record IS the verification; `pinned` is a property
    # of the terminal FINISHED seal, not of a PAUSED recovery snapshot, so it is not
    # asserted here (asserting it would demand terminal semantics from a mid-run freeze).
    record = projects.verify_version(cid, frozen.version_seq)
    assert record.trigger == ConversationStatus.PAUSED.value
    assert record.tree_digest and record.file_count >= 3
    with projects.open_verified_version(cid, record.seq) as verified:
        assert _sha(verified.read_bytes(CHANGED_ARTIFACT)) == _sha(CHANGED_BYTES)
        assert _sha(verified.read_bytes(KNOWN_PNG)) == _sha(PNG_BYTES)

    # 8. no intervening user message, resume, or newer run intent
    after = [e for e in await event_store.get_events(cid) if (e.seq or -1) > (paused.seq or -1)]
    assert not any(isinstance(e, MessageEvent) and e.source is EventSource.USER for e in after), (
        "a user turn between pause and freeze would invalidate the horizon"
    )

    # 9. ordinary kill afterwards for destructive cleanup — evidence already frozen
    sandbox.destroyed = True
    rt._run_resources.pop_executor(cid)
    assert not rt._run_resources.has_executor(cid)

    # the frozen version survives the kill, byte-exact
    with projects.open_verified_version(cid, record.seq) as verified:
        assert verified.read_bytes(CHANGED_ARTIFACT) == CHANGED_BYTES
        assert verified.read_bytes(KNOWN_PNG) == PNG_BYTES


async def test_freeze_timeout_never_claims_preservation(
    event_store: SqliteEventStore, tmp_path: Path
) -> None:
    """NEGATIVE CONTROL — pause cannot reach a step boundary in time.

    Fall back to the unchanged kill so spend and resources stop, classify the
    boundary explicitly, and NEVER claim workspace/browser evidence was preserved or
    read the destroyed sandbox.
    """
    cid = "conv-hardcap-freezetimeout"
    rt, projects = _runtime(event_store, tmp_path)
    await _seed_progressing(event_store, cid)
    sandbox = _live_workspace()
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=sandbox))

    # Pause never lands: no PAUSED status, therefore no end-gate snapshot.
    versions = await _version_events(event_store, cid)
    assert versions == []

    freeze_boundary = "FREEZE_TIMEOUT"  # explicit, not silently degraded
    sandbox.destroyed = True
    rt._run_resources.pop_executor(cid)

    assert freeze_boundary == "FREEZE_TIMEOUT"
    assert await _version_events(event_store, cid) == [], "nothing may claim a frozen version"
    with pytest.raises(RuntimeError):
        await sandbox.read_file(CHANGED_ARTIFACT)  # the destroyed sandbox is never a fallback

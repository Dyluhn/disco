"""[REL-2a step2b-2b] Tests for the finish-success artifact-manifest SHADOW fold.

`ConversationRuntime._shadow_fold_manifest` is a best-effort, default-OFF dual-write: on genuine
finish-success it projects the emitted artifact paths from the event log (the single-source truth
the download jail uses), logs any divergence vs the maintained manifest, and upserts the projected
paths so the manifest tracks reality. No reader is switched — this is telemetry + dual-write only,
so the campaign can prove the manifest faithfully mirrors the projection over many live runs before
any consumer migrates onto it. These tests pin: the fold folds, it's idempotent, it fails soft when
the sandbox is already evicted, and the finalize wire only calls it on FINISHED when the flag is ON.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.context.store import ArtifactMemoryStore
from disco.tools import ProcessSandboxService


class MemFS:
    """In-memory WorkspaceFS: dict-backed; read of a missing path raises."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _runtime(store: SqliteEventStore) -> ConversationRuntime:
    return ConversationRuntime(store, router=MagicMock(), sandbox_service=ProcessSandboxService())


async def _finished_conv_with_deliverable(store: SqliteEventStore, path: str) -> str:
    cid = f"conv_shadow_{abs(hash(path))}"
    await store.append(
        cid,
        MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content="build it")),
    )
    await store.append(
        cid, DeliverableEvent(title="Landing page", path=path, artifact_kind="files")
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    return cid


def _inject_executor(rt: ConversationRuntime, cid: str, fs: MemFS) -> None:
    fake_executor = MagicMock()
    fake_executor.sandbox = fs
    rt._executors[cid] = fake_executor


@pytest.mark.asyncio
async def test_shadow_fold_writes_projected_artifact_into_manifest():
    """The fold projects the emitted deliverable path and upserts it into the manifest."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    fs = MemFS()
    cid = await _finished_conv_with_deliverable(store, "out/index.html")
    _inject_executor(rt, cid, fs)

    # Manifest starts empty.
    assert await ArtifactMemoryStore(fs).read_artifacts() == ()

    await rt._shadow_fold_manifest(cid)

    paths = {r.path for r in await ArtifactMemoryStore(fs).read_artifacts()}
    assert paths == {"out/index.html"}


@pytest.mark.asyncio
async def test_shadow_fold_is_idempotent_no_duplicate():
    """Folding twice must not duplicate the record (upsert matches on path)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    fs = MemFS()
    cid = await _finished_conv_with_deliverable(store, "report.pdf")
    _inject_executor(rt, cid, fs)

    await rt._shadow_fold_manifest(cid)
    await rt._shadow_fold_manifest(cid)

    records = await ArtifactMemoryStore(fs).read_artifacts()
    assert [r.path for r in records] == ["report.pdf"]


@pytest.mark.asyncio
async def test_shadow_fold_no_sandbox_is_soft_noop():
    """Executor already evicted at finalize time → the sandbox guard no-ops, never raises."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _finished_conv_with_deliverable(store, "out/index.html")
    # No executor injected → getattr(..., "sandbox", None) is None.
    await rt._shadow_fold_manifest(cid)  # must not raise


@pytest.mark.asyncio
async def test_finalize_wire_folds_only_when_flag_on(monkeypatch):
    """The finalize hook calls the fold on FINISHED iff the shadow flag is ON (default OFF)."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _finished_conv_with_deliverable(store, "out/index.html")
    _inject_executor(rt, cid, MemFS())
    rt._shadow_fold_manifest = AsyncMock()  # observe whether the wire calls it

    # Flag OFF (both env prefixes cleared) → no fold.
    monkeypatch.delenv("DISCO_ARTIFACT_MANIFEST_SHADOW", raising=False)
    monkeypatch.delenv("PMX_ARTIFACT_MANIFEST_SHADOW", raising=False)
    await rt._finalize_clean_return(cid)
    assert rt._shadow_fold_manifest.call_count == 0

    # Flag ON → folds exactly once for this FINISHED conversation.
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "1")
    await rt._finalize_clean_return(cid)
    assert rt._shadow_fold_manifest.call_count == 1

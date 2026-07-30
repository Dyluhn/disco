"""[REL-2a step2b-2b] Tests for the finish-success artifact-manifest SHADOW fold.

`ArtifactManifestShadow.fold` is the low-level, best-effort dual-write. The terminal workspace
pipeline owns its production fence and mutation record; those ordering guarantees live in
`test_final_workspace_commit.py`. These tests pin only the writer's projection, idempotence,
logging, and soft failure behavior.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

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
    rt._run_resources.set_executor(cid, fake_executor)


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

    await rt._artifact_manifest_shadow.fold(cid)

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

    await rt._artifact_manifest_shadow.fold(cid)
    await rt._artifact_manifest_shadow.fold(cid)

    records = await ArtifactMemoryStore(fs).read_artifacts()
    assert [r.path for r in records] == ["report.pdf"]


@pytest.mark.asyncio
async def test_shadow_fold_no_sandbox_is_soft_noop():
    """Executor already evicted at finalize time → the sandbox guard no-ops, never raises."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _finished_conv_with_deliverable(store, "out/index.html")
    # No executor injected → getattr(..., "sandbox", None) is None.
    await rt._artifact_manifest_shadow.fold(cid)  # must not raise


@pytest.mark.asyncio
async def test_shadow_fold_no_sandbox_logs_skip(caplog):
    """Sandbox-released skip is observable, not silent."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = await _finished_conv_with_deliverable(store, "out/index.html")

    caplog.set_level(logging.INFO, logger="disco.agent_server.artifact_manifest_shadow")
    await rt._artifact_manifest_shadow.fold(cid)

    assert f"shadow fold skipped for {cid}: sandbox already released" in caplog.text


@pytest.mark.asyncio
async def test_shadow_fold_success_logs_counts(caplog):
    """A successful fold emits positive evidence with projection/manifest counts."""
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    fs = MemFS()
    cid = await _finished_conv_with_deliverable(store, "out/index.html")
    _inject_executor(rt, cid, fs)

    caplog.set_level(logging.INFO, logger="disco.agent_server.artifact_manifest_shadow")
    await rt._artifact_manifest_shadow.fold(cid)

    assert (
        f"artifact-manifest shadow divergence for {cid}: "
        f"missing=['out/index.html'] extra=[]" in caplog.text
    )

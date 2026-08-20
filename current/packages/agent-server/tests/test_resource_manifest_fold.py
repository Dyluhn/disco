"""CXT-2 resource-import hook — uploads become durable ResourceRefs.

The resource manifest (.disco/context/resource_manifest.json) has a fully live
READ path — ArtifactMemoryStore.read_resources → ContextLedger.resource_manifest
→ ContextPack.resource_refs → the rendered "Resources:" section — and this suite
pins the WRITE path introduced at the upload-attach site (files_support):

  • a successful upload upserts a ResourceRef into the manifest with the
    verbatim contract fields: rel_path=uploads/{name}, source=upload://{cid}/{name}
    (the scheme the retrieval index already uses), sha256 of the bytes, copied_at
  • a second upload MERGES (read-merge-write) — the first entry survives
  • a re-upload of the same rel_path UPSERTS (replaces in place, order stable)
  • end-to-end: the ContextPack renders the "Resources:" block with the rel_path
  • manifest bookkeeping failure never fails the upload (log-and-continue,
    matching ArtifactManifestShadow's posture)
  • the terminal fold marker declares the resource-manifest path alongside the
    artifact manifest, so the file survives the terminal fence
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from _workspace_commit_fakes import (
    _finish,
    _seed_shadow_fold_view,
    _Workspace,
)
from _workspace_commit_fakes import (
    _runtime as _commit_runtime,
)
from disco.agent_server import create_app
from disco.core import SqliteEventStore, WorkspaceMutationEvent
from disco.core.context import ContextLedger, ContextPack, ResourceRef
from disco.core.context.store import ArtifactMemoryStore
from disco.core.llm import ConfigStore, SecretStore
from disco.core.loop.context_builder import render_context_pack
from fastapi.testclient import TestClient

_MANIFEST_PATH = ".disco/context/resource_manifest.json"

# ── minimal sandbox stub (mirrors test_datasource_attach.py, but absent files
#    RAISE like the real sandbox so the store's missing-vs-corrupt contract holds)


class _FakeSession:
    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    async def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [k[len(prefix) :] for k in self._files if k.startswith(prefix)]

    async def read_file(self, path: str) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data

    async def destroy(self) -> None:
        pass


class _FakeExecutor:
    def __init__(self, session: _FakeSession) -> None:
        self._sandbox = session


class _LiveSessions:
    def live_session(self, cid: str) -> _FakeSession | None:
        return None

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        return None

    async def resolve_owned_cid_prefix(self, cid8: str, owner_id: str) -> str | None:
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self._executors: dict[str, _FakeExecutor] = {}
        self._pending_sessions: dict[str, _FakeSession] = {}
        self._sidecar: dict[str, dict[str, bytes]] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self.mutation_paths: list[tuple[str, ...]] = []
        self.sessions = SimpleNamespace(upload_session=self.upload_session)
        self.workspace = SimpleNamespace(
            lock=self._workspace_lock,
            fence=self._workspace_fence,
            record_mutation_locked=self._record_workspace_mutation_locked,
        )
        self.uploads = SimpleNamespace(
            store=self.store_upload,
            names=self.get_upload_names,
            size=self.get_upload_size,
        )
        self.deep_research = SimpleNamespace(add_upload_passages=self.add_upload_passages)
        self.live_sessions = _LiveSessions()
        self._config_store = ConfigStore()
        self._secret_store = SecretStore()

    def _workspace_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._workspace_locks.setdefault(conversation_id, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def _workspace_fence(self, conversation_id: str) -> AsyncIterator[None]:
        async with self._workspace_lock(conversation_id):
            yield

    async def _record_workspace_mutation_locked(
        self,
        conversation_id: str,
        operation: str,
        *,
        paths=(),  # noqa: ANN001
    ) -> None:
        assert self._workspace_lock(conversation_id).locked()
        self.mutation_paths.append(tuple(paths))

    def kick(self, cid: str) -> None:
        pass

    def upload_session(self, conversation_id: str) -> _FakeSession:
        executor = self._executors.get(conversation_id)
        if executor is not None:
            return executor._sandbox
        if conversation_id not in self._pending_sessions:
            self._pending_sessions[conversation_id] = _FakeSession()
        return self._pending_sessions[conversation_id]

    def store_upload(self, conversation_id: str, filename: str, data: bytes) -> None:
        if conversation_id not in self._sidecar:
            self._sidecar[conversation_id] = {}
        self._sidecar[conversation_id][filename] = data

    def get_upload_names(self, conversation_id: str) -> set[str]:
        return set(self._sidecar.get(conversation_id, {}).keys())

    def get_upload_size(self, conversation_id: str) -> int:
        return sum(len(v) for v in self._sidecar.get(conversation_id, {}).values())

    def add_upload_passages(self, conversation_id: str, passages: list) -> None:
        pass

    def get_upload_passages(self, conversation_id: str) -> list:
        return []


def _make_client() -> tuple[TestClient, str, SqliteEventStore, _FakeSession, _FakeRuntime]:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)
    return client, cid, store, sess, rt


def _upload(client: TestClient, cid: str, files: list[tuple[str, bytes]]) -> object:
    parts = [
        ("files", (fname, io.BytesIO(data), "application/octet-stream")) for fname, data in files
    ]
    return client.post(f"/conversations/{cid}/files", files=parts)


async def _manifest_refs(sess: _FakeSession) -> tuple[ResourceRef, ...]:
    return await ArtifactMemoryStore(sess).read_resources()


# ── acceptance ───────────────────────────────────────────────────────────────


async def test_upload_folds_resource_ref_with_contract_fields() -> None:
    """One successful upload → one manifest entry with the verbatim contract:
    rel_path, upload:// source (the retrieval-index scheme), sha256, copied_at."""
    import hashlib

    client, cid, _, sess, rt = _make_client()
    data = b"hello resource manifest"
    r = _upload(client, cid, [("data.csv", data)])
    assert r.status_code == 200

    refs = await _manifest_refs(sess)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.rel_path == "uploads/data.csv"
    assert ref.source == f"upload://{cid}/data.csv"
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.copied_at is not None

    # The manifest write is a declared workspace mutation alongside the upload.
    assert (_MANIFEST_PATH, "uploads/data.csv") in rt.mutation_paths

    # The durable file itself is well-formed JSON at the canonical path.
    raw = json.loads(sess._files[_MANIFEST_PATH])
    assert raw[0]["rel_path"] == "uploads/data.csv"


async def test_second_upload_merges_without_dropping_first() -> None:
    """record_resources is whole-list-replace; the hook must read-merge-write."""
    client, cid, _, sess, _ = _make_client()
    assert _upload(client, cid, [("a.csv", b"aaa")]).status_code == 200
    assert _upload(client, cid, [("b.csv", b"bbb")]).status_code == 200

    refs = await _manifest_refs(sess)
    assert [r.rel_path for r in refs] == ["uploads/a.csv", "uploads/b.csv"]
    assert {r.source for r in refs} == {
        f"upload://{cid}/a.csv",
        f"upload://{cid}/b.csv",
    }


async def test_multi_file_batch_folds_every_file() -> None:
    client, cid, _, sess, _ = _make_client()
    r = _upload(client, cid, [("a.csv", b"a" * 10), ("b.csv", b"b" * 20)])
    assert r.status_code == 200
    refs = await _manifest_refs(sess)
    assert {ref.rel_path for ref in refs} == {"uploads/a.csv", "uploads/b.csv"}


async def test_reupload_same_rel_path_upserts_in_place() -> None:
    """A pre-existing entry for the same rel_path is REPLACED (order stable),
    and unrelated entries survive untouched."""
    import hashlib

    client, cid, _, sess, _ = _make_client()
    stale = ResourceRef(rel_path="uploads/data.csv", source="upload://stale", sha256="old")
    keep = ResourceRef(rel_path="uploads/keep.txt", source=f"upload://{cid}/keep.txt")
    await ArtifactMemoryStore(sess).record_resources((stale, keep))

    data = b"fresh bytes"
    assert _upload(client, cid, [("data.csv", data)]).status_code == 200

    refs = await _manifest_refs(sess)
    assert [r.rel_path for r in refs] == ["uploads/data.csv", "uploads/keep.txt"]
    assert refs[0].source == f"upload://{cid}/data.csv"
    assert refs[0].sha256 == hashlib.sha256(data).hexdigest()
    assert refs[1] == keep


async def test_collision_suffix_gets_its_own_manifest_entry() -> None:
    """Name collisions save as data-2.csv — the manifest tracks the FINAL name."""
    client, cid, _, sess, _ = _make_client()
    assert _upload(client, cid, [("data.csv", b"first")]).status_code == 200
    assert _upload(client, cid, [("data.csv", b"second")]).status_code == 200

    refs = await _manifest_refs(sess)
    assert {r.rel_path for r in refs} == {"uploads/data.csv", "uploads/data-2.csv"}


async def test_context_pack_renders_resources_block_from_manifest() -> None:
    """End-to-end: upload → manifest → ContextLedger.resource_manifest →
    ContextPack.resource_refs → the rendered "Resources:" section."""
    client, cid, _, sess, _ = _make_client()
    assert _upload(client, cid, [("report.pdf", b"%PDF-1.7 fake")]).status_code == 200

    refs = await _manifest_refs(sess)
    ledger = ContextLedger.empty(cid).model_copy(update={"resource_manifest": refs})
    pack = ContextPack.from_ledger(ledger)
    assert pack.resource_refs == refs

    rendered = render_context_pack(pack)
    assert "Resources:" in rendered
    assert "- uploads/report.pdf" in rendered


async def test_manifest_bookkeeping_failure_never_fails_the_upload() -> None:
    """A corrupt manifest makes the fold raise internally; the upload must still
    succeed and store the file (log-and-continue posture)."""
    client, cid, _, sess, rt = _make_client()
    await sess.write_file(_MANIFEST_PATH, b"{not json")

    r = _upload(client, cid, [("data.csv", b"payload")])
    assert r.status_code == 200
    assert rt._sidecar[cid]["data.csv"] == b"payload"
    assert sess._files["uploads/data.csv"] == b"payload"
    # The corrupt manifest is left as-is (no destructive overwrite).
    assert sess._files[_MANIFEST_PATH] == b"{not json"


# ── terminal-fence persistence declaration ──────────────────────────────────


@pytest.fixture
def event_store():
    store = SqliteEventStore(":memory:")
    yield store
    store.close()


async def test_terminal_fold_marker_declares_resource_manifest_path(
    event_store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal pipeline's manifest-fold mutation event declares
    resource_manifest.json alongside artifact_manifest.json, so the resource
    manifest survives the terminal fence exactly like the artifact manifest."""
    cid = "conv-resource-manifest-fence"
    monkeypatch.setenv("DISCO_ARTIFACT_MANIFEST_SHADOW", "1")
    rt, _projects = _commit_runtime(event_store, tmp_path)
    await _seed_shadow_fold_view(event_store, cid)
    sandbox = _Workspace({"index.html": b"app"})
    rt._run_resources.set_executor(cid, MagicMock(_sandbox=sandbox, sandbox=sandbox))
    monkeypatch.setattr(rt._artifact_manifest_shadow, "fold", AsyncMock())

    await _finish(rt, cid, agent_view_id="aview-shadow")

    marker = next(
        event
        for event in await event_store.get_events(cid)
        if isinstance(event, WorkspaceMutationEvent)
        and event.operation == "agent.artifact-manifest-fold"
    )
    assert marker.paths == (
        ".disco/context/artifact_manifest.json",
        ".disco/context/resource_manifest.json",
    )

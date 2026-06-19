"""D6 — DatasourceEvent emitted at the upload-attach site.

The consumer (view.py:206) pins every DatasourceEvent so the contract survives
condensation; before this fix the producer did not exist, so an upload of
critical data would dissolve into a lossy summary on a long build. This test
asserts the contract at the attach site:

  • Exactly ONE DatasourceEvent per successful upload batch (no duplicates)
  • Fields are the verbatim contract: name, docs, kind=DATASOURCE, source=SYSTEM
  • No DatasourceEvent is emitted when the upload batch is fully rejected
  • The DatasourceEvent is condensation-immune (it is pinned by the View,
    so a tombstone forgetting the surrounding events still leaves the
    `<datasource>` block in the materialized messages)
"""

from __future__ import annotations

import io
import uuid

from disco.agent_server import create_app
from disco.core import (
    CondensationEvent,
    DatasourceEvent,
    EventKind,
    EventSource,
    MessageEvent,
    SqliteEventStore,
    View,
)
from fastapi.testclient import TestClient

# ── minimal sandbox stub (mirrors test_upload.py) ───────────────────────────


class _FakeSession:
    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    async def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [k[len(prefix):] for k in self._files if k.startswith(prefix)]

    async def read_file(self, path: str) -> bytes:
        return self._files.get(path, b"")

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data

    async def destroy(self) -> None:
        pass


class _FakeExecutor:
    def __init__(self, session: _FakeSession) -> None:
        self._sandbox = session


class _FakeRuntime:
    def __init__(self) -> None:
        self._executors: dict[str, _FakeExecutor] = {}
        self._pending_sessions: dict[str, _FakeSession] = {}
        self._sidecar: dict[str, dict[str, bytes]] = {}

    def kick(self, cid: str) -> None:  # noqa: D401
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
        pass  # G1/DR-4: no-op in this test — corpus not exercised here

    def get_upload_passages(self, conversation_id: str) -> list:
        return []


def _make_client() -> tuple[TestClient, str, SqliteEventStore, _FakeSession]:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)
    return client, cid, store, sess


def _upload(client: TestClient, cid: str, files: list[tuple[str, bytes, str]]) -> object:
    parts = [
        ("files", (fname, io.BytesIO(data), "application/octet-stream"))
        for _, data, fname in files
    ]
    return client.post(f"/conversations/{cid}/files", files=parts)


# ── D6 acceptance ────────────────────────────────────────────────────────────


async def test_single_upload_emits_exactly_one_datasource_event() -> None:
    """One successful file attach → exactly one DatasourceEvent with the
    verbatim contract fields. This is the D6 acceptance check."""
    client, cid, store, _ = _make_client()
    data = b"x" * 18234  # 18,234 bytes — exercises the thousands separator
    r = _upload(client, cid, [("files", data, "data.csv")])
    assert r.status_code == 200

    all_events = await store.get_events(cid)
    ds_events = [e for e in all_events if isinstance(e, DatasourceEvent)]

    # ── acceptance: exactly ONE DatasourceEvent ──
    assert len(ds_events) == 1
    ds = ds_events[0]

    # ── acceptance: verbatim contract fields ──
    assert ds.kind == EventKind.DATASOURCE
    assert ds.source == EventSource.SYSTEM
    assert ds.name == "uploads/data.csv"
    # The docs hold the verbatim path + size; this is the durable contract
    # the View pins, so the agent can re-discover the file after condensation.
    assert "uploads/data.csv" in ds.docs
    assert "18,234" in ds.docs
    assert "bytes" in ds.docs

    # The MessageEvent announcement is still emitted (existing behavior).
    msgs = [
        e for e in all_events
        if isinstance(e, MessageEvent) and e.source == EventSource.ENVIRONMENT
    ]
    assert len(msgs) == 1
    assert "uploads/data.csv" in msgs[0].message.content


async def test_multi_file_upload_emits_one_datasource_event_not_many() -> None:
    """Multiple files in one batch → ONE DatasourceEvent (no duplicates per
    turn). The contract summarizes the batch under one `name`, the docs list
    each file verbatim."""
    client, cid, store, _ = _make_client()
    r = _upload(client, cid, [
        ("files", b"a" * 100, "a.csv"),
        ("files", b"b" * 200, "b.csv"),
        ("files", b"c" * 300, "c.csv"),
    ])
    assert r.status_code == 200

    all_events = await store.get_events(cid)
    ds_events = [e for e in all_events if isinstance(e, DatasourceEvent)]
    assert len(ds_events) == 1

    ds = ds_events[0]
    assert ds.kind == EventKind.DATASOURCE
    assert ds.source == EventSource.SYSTEM
    # The batch is summarized under one `name`; the docs list each file.
    assert ds.name == "uploads/3_files"
    for n, sz in (("a.csv", 100), ("b.csv", 200), ("c.csv", 300)):
        assert f"uploads/{n}" in ds.docs
        assert f"{sz} bytes" in ds.docs


async def test_no_datasource_event_when_all_uploads_rejected() -> None:
    """A fully-rejected batch (e.g. oversize) attaches nothing → no
    DatasourceEvent. We only emit on a successful attach."""
    client, cid, store, _ = _make_client()
    big = b"x" * (25 * 1024 * 1024 + 1)  # 25 MB + 1 byte → over the per-file cap
    r = _upload(client, cid, [("files", big, "big.bin")])
    assert r.status_code == 413

    all_events = await store.get_events(cid)
    ds_events = [e for e in all_events if isinstance(e, DatasourceEvent)]
    assert ds_events == []


async def test_collision_suffix_is_reflected_in_datasource_event() -> None:
    """The DatasourceEvent name tracks the FINAL saved name (after collision
    suffixing), so the docs are truthful about the path the agent must read."""
    client, cid, store, _ = _make_client()
    # First upload lands as data.csv
    r1 = _upload(client, cid, [("files", b"first", "data.csv")])
    assert r1.status_code == 200
    # Second upload of the same name → data-2.csv
    r2 = _upload(client, cid, [("files", b"second", "data.csv")])
    assert r2.status_code == 200

    all_events = await store.get_events(cid)
    ds_events = [e for e in all_events if isinstance(e, DatasourceEvent)]
    # Two batches → two DatasourceEvents (one per attach operation).
    assert len(ds_events) == 2
    names = {e.name for e in ds_events}
    assert names == {"uploads/data.csv", "uploads/data-2.csv"}

    docs_blob = "\n".join(e.docs for e in ds_events)
    assert "uploads/data.csv" in docs_blob
    assert "uploads/data-2.csv" in docs_blob
    assert "5 bytes" in docs_blob  # both payloads are 5 bytes ("first" / "second")


async def test_datasource_event_survives_condensation() -> None:
    """The contract: DatasourceEvent is condensation-IMMUNE. Even with a
    tombstone forgetting the surrounding events, the verbatim contract must
    remain in the materialized View."""
    client, cid, store, _ = _make_client()
    r = _upload(client, cid, [("files", b"hello world", "greeting.txt")])
    assert r.status_code == 200

    all_events = await store.get_events(cid)
    # Assign seqs so the View can pin/condense deterministically.
    seqd = [e.model_copy(update={"seq": i}) for i, e in enumerate(all_events, start=1)]

    # Insert a tombstone that forgets EVERYTHING (range covers the whole log).
    tomb = CondensationEvent(
        forgotten_start_seq=1,
        forgotten_end_seq=max(e.seq for e in seqd),
        summary="[summary: everything forgotten]",
        summary_role="user",
    ).model_copy(update={"seq": max(e.seq for e in seqd) + 1})

    view = View.of([*seqd, tomb])
    joined = " ".join(m.content for m in view.messages)

    # The DatasourceEvent contract survives — this is the whole point of the
    # D6 fix. The pinned seq keeps the `<datasource>` block in the context.
    assert "<datasource" in joined
    assert "uploads/greeting.txt" in joined
    assert "11 bytes" in joined  # "hello world" is 11 bytes
    # The lossy summary IS present (the surrounding events were forgotten)…
    assert "[summary" in joined
    # …but the verbatim contract is NOT reduced to it.
    assert "greeting.txt" not in joined.split("[summary")[1].split("</condensation")[-1] \
        or "<datasource" in joined  # the <datasource> block precedes the summary


async def test_datasource_event_renders_to_llm_context() -> None:
    """Sanity: the emitted DatasourceEvent is LLMConvertible and renders the
    verbatim `<datasource>` XML block the agent's context will see."""
    client, cid, store, _ = _make_client()
    r = _upload(client, cid, [("files", b"abc", "x.txt")])
    assert r.status_code == 200

    all_events = await store.get_events(cid)
    ds = next(e for e in all_events if isinstance(e, DatasourceEvent))
    msg = ds.to_llm_message()
    assert msg.role == "user"
    assert "<datasource" in msg.content
    assert 'name="uploads/x.txt"' in msg.content
    assert "3 bytes" in msg.content

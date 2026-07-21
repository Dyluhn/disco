"""G1/DR-4: upload corpus integration tests.

Covers:
  • POST /conversations/{cid}/files with a .md file → passages stored in corpus
  • Non-text (.pdf) upload → corpus NOT populated, file still saved
  • research_stream receives seed_passages from pre-attached uploads
  • WS /ws/research accepts conversation_id in the frame body
  • DR run (DeepResearchRun) receives upload passages via extra_passages in each leg
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest import mock

import pytest
from disco.agent_server import create_app
from disco.core import SqliteEventStore
from fastapi.testclient import TestClient

# ── minimal fakes ─────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    async def list_dir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [k[len(prefix) :] for k in self._files if k.startswith(prefix)]

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
        self._upload_passages: dict[str, list[Any]] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}

    def workspace_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._workspace_locks.setdefault(conversation_id, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def workspace_fence(self, conversation_id: str) -> AsyncIterator[None]:
        async with self.workspace_lock(conversation_id):
            yield

    async def record_workspace_mutation_locked(
        self,
        conversation_id: str,
        operation: str,
        *,
        paths=(),  # noqa: ANN001
    ) -> None:
        assert self.workspace_lock(conversation_id).locked()

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
        self._sidecar.setdefault(conversation_id, {})[filename] = data

    def get_upload_names(self, conversation_id: str) -> set[str]:
        return set(self._sidecar.get(conversation_id, {}).keys())

    def get_upload_size(self, conversation_id: str) -> int:
        return sum(len(v) for v in self._sidecar.get(conversation_id, {}).values())

    def add_upload_passages(self, conversation_id: str, passages: list[Any]) -> None:
        self._upload_passages.setdefault(conversation_id, []).extend(passages)

    def get_upload_passages(self, conversation_id: str) -> list[Any]:
        return list(self._upload_passages.get(conversation_id, []))


def _make_client() -> tuple[TestClient, str, _FakeSession, _FakeRuntime]:
    store = SqliteEventStore(":memory:")
    rt = _FakeRuntime()
    client = TestClient(create_app(store, runtime=rt))
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    sess = _FakeSession()
    rt._executors[cid] = _FakeExecutor(sess)
    return client, cid, sess, rt


def _upload(client: TestClient, cid: str, files: list[tuple[str, bytes, str]]) -> Any:
    parts = [
        ("files", (fname, io.BytesIO(data), "application/octet-stream")) for _, data, fname in files
    ]
    return client.post(f"/conversations/{cid}/files", files=parts)


# ── corpus ingestion tests ─────────────────────────────────────────────────────


def test_md_upload_populates_corpus() -> None:
    """Uploading a .md file produces Passages in the upload corpus."""
    client, cid, _, rt = _make_client()
    md_text = b"# Research Notes\n\nThis is a key finding.\n\nAnother paragraph here."
    r = _upload(client, cid, [("files", md_text, "notes.md")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "notes.md"
    # Corpus should have passages
    passages = rt.get_upload_passages(cid)
    assert len(passages) >= 1
    # All passages have the right source_url
    for p in passages:
        assert f"upload://{cid}/notes.md" in p.source_url


def test_txt_upload_populates_corpus() -> None:
    """Uploading a .txt file populates the corpus."""
    client, cid, _, rt = _make_client()
    r = _upload(client, cid, [("files", b"Hello world.\n\nSecond para.", "data.txt")])
    assert r.status_code == 200
    passages = rt.get_upload_passages(cid)
    assert len(passages) >= 1


def test_csv_upload_populates_corpus() -> None:
    """Uploading a .csv file populates the corpus with one passage per row."""
    client, cid, _, rt = _make_client()
    csv_bytes = b"name,value\nalpha,1\nbeta,2\n"
    r = _upload(client, cid, [("files", csv_bytes, "data.csv")])
    assert r.status_code == 200
    passages = rt.get_upload_passages(cid)
    assert len(passages) == 2


def test_pdf_upload_does_not_populate_corpus() -> None:
    """Uploading a .pdf skips corpus ingestion (v1 text-only scope)."""
    client, cid, _, rt = _make_client()
    r = _upload(client, cid, [("files", b"%PDF-1.4 fake", "report.pdf")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "report.pdf"
    # Corpus should be empty for this cid
    passages = rt.get_upload_passages(cid)
    assert passages == []


def test_binary_upload_does_not_populate_corpus() -> None:
    """Binary files skip corpus ingestion."""
    client, cid, _, rt = _make_client()
    r = _upload(client, cid, [("files", b"\x00\x01\x02binary", "model.bin")])
    assert r.status_code == 200
    passages = rt.get_upload_passages(cid)
    assert passages == []


def test_corpus_accumulates_multiple_uploads() -> None:
    """Uploading multiple text files accumulates passages."""
    client, cid, _, rt = _make_client()
    _upload(client, cid, [("files", b"First file content.", "a.txt")])
    _upload(client, cid, [("files", b"Second file content.\n\nMore.", "b.md")])
    passages = rt.get_upload_passages(cid)
    # At least one passage from each file
    assert len(passages) >= 2


# ── WS frame cid threading test ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_research_passes_conversation_id_to_research_stream() -> None:
    """The /ws/research WS endpoint passes conversation_id from the frame body
    to runtime.research_stream so seed_passages can be loaded."""
    from disco.core import SqliteEventStore

    store = SqliteEventStore(":memory:")
    seen_kwargs: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        seen_kwargs.update(kwargs)
        yield {"type": "state", "status": "running"}
        yield {
            "type": "final",
            "answer": {
                "query": "q",
                "blocks": [],
                "claims": [],
                "passages": [],
                "all_hits": [],
                "unsupported_count": 0,
                "follow_ups": [],
            },
        }
        yield {"type": "state", "status": "finished"}

    fake_rt = mock.MagicMock()
    fake_rt.research_stream = mock.MagicMock(
        side_effect=lambda query, **kw: _fake_stream(**{"query": query, **kw})
    )
    fake_rt.get_last_selected_model = mock.MagicMock(return_value=None)
    # lifespan calls await / asyncio.create_task on several runtime methods —
    # they must be AsyncMocks or create_task will crash ("expected a coroutine").
    fake_rt._start_mcp_pool = mock.AsyncMock()
    fake_rt._idle_sweep_loop = mock.AsyncMock()
    fake_rt._schedule_manager_loop = mock.AsyncMock()
    fake_rt._close_mcp_pool = mock.AsyncMock()

    app = create_app(store, runtime=fake_rt)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/ws/research") as ws:
            ws.send_json(
                {
                    "query": "test question",
                    "conversation_id": "conv_abc123",
                    "sources": ["arxiv", "ddgs"],
                }
            )
            frames = []
            try:
                while True:
                    frames.append(ws.receive_json())
            except Exception:
                pass

    # conversation_id should have been passed to research_stream
    assert fake_rt.research_stream.called
    call_kwargs = fake_rt.research_stream.call_args
    assert call_kwargs is not None
    kwargs = call_kwargs.kwargs
    assert kwargs.get("conversation_id") == "conv_abc123"
    assert kwargs.get("sources") == ["arxiv", "ddgs"]


@pytest.mark.asyncio
async def test_ws_research_no_conversation_id_is_none() -> None:
    """When no conversation_id is in the frame, research_stream gets None."""
    from disco.core import SqliteEventStore

    store = SqliteEventStore(":memory:")
    seen_kwargs: dict[str, Any] = {}

    async def _fake_stream(**kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        seen_kwargs.update(kwargs)
        yield {"type": "state", "status": "running"}
        yield {
            "type": "final",
            "answer": {
                "query": "q",
                "blocks": [],
                "claims": [],
                "passages": [],
                "all_hits": [],
                "unsupported_count": 0,
                "follow_ups": [],
            },
        }
        yield {"type": "state", "status": "finished"}

    fake_rt = mock.MagicMock()
    fake_rt.research_stream = mock.MagicMock(
        side_effect=lambda query, **kw: _fake_stream(**{"query": query, **kw})
    )
    fake_rt.get_last_selected_model = mock.MagicMock(return_value=None)
    fake_rt._start_mcp_pool = mock.AsyncMock()
    fake_rt._idle_sweep_loop = mock.AsyncMock()
    fake_rt._schedule_manager_loop = mock.AsyncMock()
    fake_rt._close_mcp_pool = mock.AsyncMock()

    app = create_app(store, runtime=fake_rt)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        with client.websocket_connect("/ws/research") as ws:
            ws.send_json({"query": "test question"})
            frames = []
            try:
                while True:
                    frames.append(ws.receive_json())
            except Exception:
                pass

    call_kwargs = fake_rt.research_stream.call_args
    assert call_kwargs is not None
    kwargs = call_kwargs.kwargs
    assert kwargs.get("conversation_id") is None


# ── seed_passages in stream_research_answer ────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_passages_in_stream_research_answer() -> None:
    """seed_passages are merged into the candidate set before rerank so they
    can appear in the final answer alongside web passages.
    The OFF-path (empty seeds) is byte-identical — tested via the existing
    streaming tests; here we just verify seeds flow through."""
    from unittest.mock import AsyncMock, MagicMock

    from disco.retrieval.models import Passage
    from disco.retrieval.streaming import stream_research_answer

    seed = Passage(
        id="up_notes_0",
        source_url="upload://conv_x/notes.md",
        source_title="notes.md",
        text="Uploaded research note: the answer is 42.",
    )

    # Fake search returns one hit; extraction returns nothing useful.
    fake_search = AsyncMock()
    fake_search.search = AsyncMock(
        return_value=[
            MagicMock(
                url="https://example.com",
                title="Example",
                snippet="",
                source_engine="test",
                rank=0,
            ),
        ]
    )
    fake_extraction = AsyncMock()
    fake_extraction.extract_many = AsyncMock(return_value=[])
    fake_reranker = AsyncMock()

    # Reranker sees both web passages (none, since extraction failed) AND the
    # seed passage. Verify it receives the seed.
    captured_candidates: list[Passage] = []

    async def _rerank(query: str, passages: list[Passage], top_k: int) -> list[Passage]:
        captured_candidates.extend(passages)
        return passages[:top_k] if passages else []

    fake_reranker.rerank = _rerank
    fake_nli = MagicMock()
    fake_nli.predict = MagicMock(return_value=[])
    # NOTE: stream_complete is a plain `def` returning an AsyncIterator per the
    # LLMRouter Protocol (core/llm/routing.py). The caller does
    # `async for chunk in router.stream_complete(req)` — that iterates the
    # *return value* directly. An AsyncMock(return_value=<aiter>) wraps the
    # iterator in a coroutine and reproduces the "coroutine was never awaited"
    # RuntimeWarning. We assign a real async-generator function so calling
    # stream_complete yields an async iterator directly.
    fake_router = AsyncMock()
    follow_up_response = MagicMock()
    follow_up_response.text = (
        "What evidence supports the answer?\n"
        "Which exceptions matter most?\n"
        "What should be investigated next?"
    )
    fake_router.complete = AsyncMock(return_value=follow_up_response)

    async def _fake_token_stream(req: Any) -> AsyncIterator[Any]:
        chunk = MagicMock()
        chunk.delta_text = "The answer is 42."
        yield chunk

    fake_router.stream_complete = _fake_token_stream

    frames = []
    async for frame in stream_research_answer(
        "what is the answer?",
        router=fake_router,
        search=fake_search,
        extraction=fake_extraction,
        reranker=fake_reranker,
        nli=fake_nli,
        seed_passages=[seed],
    ):
        frames.append(frame)

    # The seed passage was visible to the reranker
    assert any(p.id == "up_notes_0" for p in captured_candidates)
    # Strengthen: stream_research_answer catches broad exceptions at
    # streaming.py:514 and emits an {"type":"error", ...} frame, so a green
    # test today can silently swallow real breakage. Verify the happy-path
    # frames actually flowed.
    frame_types = [f.get("type") for f in frames]
    assert "error" not in frame_types, f"unexpected error frame: {frames}"
    assert "token" in frame_types, f"no token frame in {frames}"
    assert "final" in frame_types, f"no final frame in {frames}"
    assert any(f.get("type") == "state" and f.get("status") == "finished" for f in frames), (
        f"no finished state frame in {frames}"
    )


# ── DR run receives upload passages ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_dr_run_extra_passages_seeded_into_legs() -> None:
    """DeepResearchRun passes upload_passages as extra_passages to each leg
    so they are in the working set from the start."""
    from unittest.mock import AsyncMock, MagicMock

    from disco.retrieval.deep_research.engine import DeepResearchRun
    from disco.retrieval.models import Passage

    upload_p = Passage(
        id="up_seed_0",
        source_url="upload://conv_y/data.csv",
        source_title="data.csv",
        text="Row 1: name=alpha value=1",
    )

    # Track what extra_passages each leg sees.
    seen_extra: list[Passage] = []

    async def _patched_gather(
        subq: Any,
        *,
        extra_passages: list[Any] | None = None,
        **kw: Any,
    ) -> Any:
        extra_passages = extra_passages or []
        seen_extra.extend(extra_passages)
        from disco.retrieval.deep_research.gather import SubQuestionResult

        return SubQuestionResult(subq=subq, passages=list(extra_passages))

    # Patch the name as imported into engine.py (not the original in gather.py)
    # because _gather_leg in engine.py closes over the local import.
    with mock.patch(
        "disco.retrieval.deep_research.engine.gather_for_subquestion",
        _patched_gather,
    ):
        fake_engine = AsyncMock()
        fake_router = AsyncMock()
        fake_embedder = None
        fake_vs = AsyncMock()
        fake_vs.upsert = AsyncMock()
        fake_vs.query = AsyncMock(return_value=[])
        fake_nli = MagicMock()
        fake_nli.predict = MagicMock(return_value=[])

        # Build a minimal synthesis mock so run() completes.
        with mock.patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            new_callable=lambda: lambda *a, **kw: _fake_synth(*a, **kw),
        ):
            run = DeepResearchRun(
                query="What is alpha?",
                router=fake_router,
                retrieval_engine=fake_engine,
                embedder=fake_embedder,
                vector_store=fake_vs,
                nli=fake_nli,
                depth="quick",
                conversation_id="conv_y",
                upload_passages=[upload_p],
            )

            try:
                await run.run(
                    ["Sub-question 1"],
                    emit=_noop_emit,
                    should_cancel=lambda: False,
                )
            except Exception:
                pass  # synthesis might fail with fake deps; that's OK

    # The upload passage should have been passed to each leg.
    assert any(p.id == "up_seed_0" for p in seen_extra)


async def _noop_emit(kind: str, payload: dict[str, Any]) -> None:
    pass


async def _fake_synth(*args: Any, **kwargs: Any) -> Any:
    from disco.retrieval.deep_research.synthesis import SectionResult

    return SectionResult(
        title="Sub-question 1",
        markdown="Synthesized.",
        cited_passage_ids=[],
        disputed_notes=[],
    )

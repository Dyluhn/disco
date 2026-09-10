"""G1/DR-4: upload corpus integration tests.

Covers:
  • POST /conversations/{cid}/files with a .md file → passages stored in corpus
  • Non-text (.pdf) upload → corpus NOT populated, file still saved
  • research_stream receives seed_passages from pre-attached uploads
  • WS /ws/research accepts conversation_id in the frame body
  • DR run receives upload passages as the engine's pool seed (upload_passages)
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from disco.agent_server import create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, SecretStore
from disco.tools.projects import StorageStatus
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

    @property
    def sandbox(self) -> _FakeSession:
        return self._sandbox


class _LiveSessions:
    def __init__(self, executors: dict[str, _FakeExecutor] | None = None) -> None:
        self._executors = executors or {}

    def live_session(self, cid: str) -> _FakeSession | None:
        executor = self._executors.get(cid)
        return executor.sandbox if executor is not None else None

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        return None

    async def resolve_owned_cid_prefix(self, cid8: str, owner_id: str) -> str | None:
        return None


class _EmptyProjectStore:
    def status(self) -> StorageStatus:
        return StorageStatus.NOT_FOUND

    @property
    def root(self) -> None:
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self._executors: dict[str, _FakeExecutor] = {}
        self._pending_sessions: dict[str, _FakeSession] = {}
        self._sidecar: dict[str, dict[str, bytes]] = {}
        self._upload_passages: dict[str, list[Any]] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}
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
        self.deep_research = SimpleNamespace(
            add_upload_passages=self.add_upload_passages,
            get_upload_passages=self.get_upload_passages,
        )
        self.live_sessions = _LiveSessions(self._executors)
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
    passages = rt.deep_research.get_upload_passages(cid)
    assert len(passages) >= 1
    # All passages have the right source_url
    for p in passages:
        assert f"upload://{cid}/notes.md" in p.source_url


def test_txt_upload_populates_corpus() -> None:
    """Uploading a .txt file populates the corpus."""
    client, cid, _, rt = _make_client()
    r = _upload(client, cid, [("files", b"Hello world.\n\nSecond para.", "data.txt")])
    assert r.status_code == 200
    passages = rt.deep_research.get_upload_passages(cid)
    assert len(passages) >= 1


def test_csv_upload_populates_corpus() -> None:
    """Uploading a .csv file populates the corpus with one passage per row."""
    client, cid, _, rt = _make_client()
    csv_bytes = b"name,value\nalpha,1\nbeta,2\n"
    r = _upload(client, cid, [("files", csv_bytes, "data.csv")])
    assert r.status_code == 200
    passages = rt.deep_research.get_upload_passages(cid)
    assert len(passages) == 2


def test_pdf_upload_does_not_populate_corpus() -> None:
    """Uploading a .pdf skips corpus ingestion (v1 text-only scope)."""
    client, cid, _, rt = _make_client()
    r = _upload(client, cid, [("files", b"%PDF-1.4 fake", "report.pdf")])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "report.pdf"
    # Corpus should be empty for this cid
    passages = rt.deep_research.get_upload_passages(cid)
    assert passages == []


def test_binary_upload_does_not_populate_corpus() -> None:
    """Binary files skip corpus ingestion."""
    client, cid, _, rt = _make_client()
    r = _upload(client, cid, [("files", b"\x00\x01\x02binary", "model.bin")])
    assert r.status_code == 200
    passages = rt.deep_research.get_upload_passages(cid)
    assert passages == []


def test_corpus_accumulates_multiple_uploads() -> None:
    """Uploading multiple text files accumulates passages."""
    client, cid, _, rt = _make_client()
    _upload(client, cid, [("files", b"First file content.", "a.txt")])
    _upload(client, cid, [("files", b"Second file content.\n\nMore.", "b.md")])
    passages = rt.deep_research.get_upload_passages(cid)
    # At least one passage from each file
    assert len(passages) >= 2


# ── WS frame cid threading test ───────────────────────────────────────────────


def _research_runtime(research_stream: Any) -> mock.MagicMock:
    runtime = mock.MagicMock()
    runtime.deep_research = SimpleNamespace(research_stream=research_stream)
    runtime.deep_research.research_stream = research_stream
    runtime.settings = SimpleNamespace(
        model_binding=SimpleNamespace(get_last_selected_model=mock.MagicMock(return_value=None))
    )
    runtime.mcp = SimpleNamespace(
        _start_mcp_pool=mock.AsyncMock(),
        _close_mcp_pool=mock.AsyncMock(),
    )
    runtime.lifecycle = SimpleNamespace(
        reconcile_orphaned_runs=mock.AsyncMock(),
        _idle_sweep_loop=mock.AsyncMock(),
    )
    runtime.drivers = SimpleNamespace(
        prewarm_model_probe=mock.AsyncMock(),
        prewarm_vision_probe=mock.AsyncMock(),
    )
    runtime.schedules = SimpleNamespace(_schedule_manager_loop=mock.AsyncMock())
    runtime._idle_sweeper = SimpleNamespace(run=mock.AsyncMock())
    runtime.live_sessions = _LiveSessions()
    runtime._config_store = ConfigStore()
    runtime._secret_store = SecretStore()
    runtime.lifecycle.reconcile_orphaned_runs = mock.AsyncMock(return_value=0)
    runtime.drivers.prewarm_model_probe = mock.AsyncMock()
    runtime.drivers.prewarm_vision_probe = mock.AsyncMock()
    runtime.aclose = mock.AsyncMock()
    runtime.projects.current_project_store = lambda: _EmptyProjectStore()
    return runtime


@pytest.mark.asyncio
async def test_ws_research_passes_conversation_id_to_research_stream() -> None:
    """The /ws/research WS endpoint passes conversation_id from the frame body
    to runtime.deep_research.research_stream so seed_passages can be loaded."""
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

    research_stream = mock.MagicMock(
        side_effect=lambda query, **kw: _fake_stream(**{"query": query, **kw})
    )
    fake_rt = _research_runtime(research_stream)

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
    assert research_stream.called
    call_kwargs = research_stream.call_args
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

    research_stream = mock.MagicMock(
        side_effect=lambda query, **kw: _fake_stream(**{"query": query, **kw})
    )
    fake_rt = _research_runtime(research_stream)

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

    call_kwargs = research_stream.call_args
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
    fake_nli.entail = MagicMock(return_value="entail")
    fake_nli.score = MagicMock(return_value=1.0)
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
        chunk.delta_text = "The answer is 42. [[up_notes_0]]"
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
async def test_dr_run_upload_passages_reach_the_engine() -> None:
    """The agent-server seam (v2): passages from a pre-attached upload are read
    off the conversation's Deep Research state and handed to the engine as
    ``upload_passages``, which seeds them into the evidence pool before the
    first search. The v1 per-leg ``extra_passages`` gather seam this used to
    patch no longer exists — the agentic loop has no gather legs."""
    from disco.agent_server import ConversationRuntime
    from disco.agent_server._deep_research_service_parts import execute as execute_parts
    from disco.core import (
        ConversationStatus,
        EventSource,
        LLMMessage,
        MessageEvent,
        ReportEvent,
        ReportSection,
        StatusEvent,
    )
    from disco.retrieval.models import Passage

    upload_p = Passage(
        id="up_seed_0",
        source_url="upload://conv_y/data.csv",
        source_title="data.csv",
        text="Row 1: name=alpha value=1",
    )

    class _FakeEncoder:
        def entail(self, premise: str, hypothesis: str) -> str:
            return "entail"

        def score(self, premise: str, hypothesis: str) -> float:
            return 0.9

    store = SqliteEventStore(":memory:")
    rt = ConversationRuntime(
        store,
        research_providers={
            "search": object(),
            "extraction": object(),
            "reranker": _FakeEncoder(),
            "embedder": None,
            "nli": _FakeEncoder(),
        },
    )
    cid = "conv_y"
    rt.settings._set_surface(cid, "deep_research")

    async def _preflight_ok(conversation_id: str, **kw: Any) -> None:
        return None

    rt._driver_preflight.check = _preflight_ok  # type: ignore[method-assign]
    rt.drivers.router = lambda **kw: object()  # type: ignore[assignment]

    await store.append(
        cid,
        MessageEvent(
            source=EventSource.USER,
            message=LLMMessage(role="user", content="What is alpha?"),
        ),
    )
    rt.deep_research.add_upload_passages(cid, [upload_p])

    seen: dict[str, Any] = {}

    class _RecordingRun:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        async def run(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                bounded_by=None,
                to_event=lambda: ReportEvent(
                    query=str(seen.get("query", "")),
                    summary="Done.",
                    sections=[ReportSection(id="s0", title="Findings", markdown="Done.")],
                    passages=[],
                    all_hits=[],
                ),
            )

    with mock.patch.object(execute_parts, "DeepResearchRun", _RecordingRun):
        await rt.deep_research._maybe_run_deep_research(cid)

    # The upload passage reached the engine's pool seed, and the run finished.
    assert [p.id for p in seen["upload_passages"]] == ["up_seed_0"]
    assert seen["query"] == "What is alpha?"
    statuses = [e for e in await store.get_events(cid) if isinstance(e, StatusEvent)]
    assert statuses[-1].status == ConversationStatus.FINISHED

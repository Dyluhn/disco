"""The research-answer stream (Stage 4) over the WebSocket, hermetic.

A fake-backed router + fake retrieval providers are injected so this runs in CI
with no network: a query over /ws/research drives search → extract → rerank →
streamed generation → NLI-verify, and the server emits the UI's grounded-answer
frames (state → token… → final → state) — the same contract the fixture replayed.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import (
    CompletionResponse,
    ConfigStore,
    DefaultLLMRouter,
    ModelEntry,
    RouterConfig,
    SecretBox,
    SecretStore,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.models import ExtractedDoc, Passage, SearchHit
from fastapi.testclient import TestClient

_ANSWER = "Paris is the capital of France. [[wiki_p0]]"


class _FakeProvider:
    name = "fake"

    async def complete(self, req, *, model):
        return CompletionResponse(
            text=_ANSWER,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        # Stream in two deltas so the token-frame path is exercised.
        yield StreamChunk(delta_text=_ANSWER[:18])
        yield StreamChunk(delta_text=_ANSWER[18:])
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


class _FakeSearch:
    def __init__(self):
        self.last_domains_deny = None
        self.calls = 0

    async def search(self, query, *, limit=10, domains_allow=None, domains_deny=None):
        self.calls += 1
        self.last_domains_deny = domains_deny  # record what the re-scope passed
        return [
            SearchHit(
                url="https://en.wikipedia.org/Paris",
                title="Paris",
                snippet="...",
                source_engine="fake",
                rank=1,
            )
        ]


class _FakeExtraction:
    async def extract(self, url):
        return (await self.extract_many([url]))[0]

    async def extract_many(self, urls):
        return [
            ExtractedDoc(
                url=urls[0],
                title="Paris",
                content="Paris is the capital and most populous city of France.",
                fetched_ok=True,
                status="ok",
                passages=[
                    Passage(
                        id="wiki_p0",
                        source_url=urls[0],
                        source_title="Paris",
                        text="Paris is the capital and most populous city of France.",
                    )
                ],
            )
        ]


class _FakeReranker:
    async def rerank(self, query, passages, *, top_k):
        return passages[:top_k]


class _FakeNLI:
    def entail(self, premise, hypothesis):
        return "entail"

    def score(self, premise, hypothesis):
        return 0.97


def _runtime(store: SqliteEventStore, search: _FakeSearch | None = None) -> ConversationRuntime:
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _FakeProvider()})
    return ConversationRuntime(
        store,
        router=router,
        research_providers={
            "search": search or _FakeSearch(),
            "extraction": _FakeExtraction(),
            "reranker": _FakeReranker(),
            "embedder": None,
            "nli": _FakeNLI(),
        },
    )


def test_research_stream_emits_grounded_frames():
    store = SqliteEventStore(":memory:")
    app = create_app(store, runtime=_runtime(store))
    client = TestClient(app)

    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "What is the capital of France?"})
        frames = []
        for _ in range(60):
            f = ws.receive_json()
            frames.append(f)
            if f["type"] == "state" and f["status"] == "finished":
                break

    kinds = [f["type"] for f in frames]
    assert kinds[0] == "state" and frames[0]["status"] == "running"
    assert "token" in kinds  # the typewriter streamed
    assert kinds[-1] == "state" and frames[-1]["status"] == "finished"

    final = next(f for f in frames if f["type"] == "final")["answer"]
    # real structure: a prose block carrying the citation marker
    assert any(b["kind"] == "prose" and "wiki_p0" in b["text"] for b in final["blocks"])
    # real grounding: the cited claim was NLI-verified as supported
    claim = final["claims"][0]
    assert claim["verdict"] == "supported"
    assert claim["best_passage_id"] == "wiki_p0"
    # provenance preserved end-to-end
    assert final["passages"][0]["id"] == "wiki_p0"
    assert final["all_hits"][0]["status"] == "ok"
    assert final["unsupported_count"] == 0


def test_research_passes_domain_deny_to_search():
    store = SqliteEventStore(":memory:")
    search = _FakeSearch()
    app = create_app(store, runtime=_runtime(store, search=search))
    client = TestClient(app)
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "capital of France?", "domains_deny": ["Reddit.com", " "]})
        for _ in range(60):
            f = ws.receive_json()
            if f["type"] == "state" and f["status"] == "finished":
                break
    # the re-scope's denied domains reached the search provider (normalized)
    assert search.last_domains_deny == frozenset({"reddit.com"})


async def test_research_stream_sources_builds_override_and_bypasses_cached_global(
    tmp_path, monkeypatch
):
    store = SqliteEventStore(":memory:")
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": _FakeProvider()})
    rt = ConversationRuntime(
        store,
        router=router,
        config_store=ConfigStore(tmp_path / "config.json", base_factory=lambda: cfg),
    )
    cached_search = _FakeSearch()
    override_search = _FakeSearch()
    rt.deep_research._research_providers = {
        "search": cached_search,
        "extraction": _FakeExtraction(),
        "reranker": _FakeReranker(),
        "embedder": None,
        "nli": _FakeNLI(),
    }

    seen: dict[str, object] = {}

    def fake_build_multi_search(sources, **kwargs):
        seen["sources"] = tuple(sources)
        seen["multi_kwargs"] = kwargs
        return override_search

    def fake_build_live_retrieval(**kwargs):
        seen["search_override"] = kwargs.get("search_override")
        return {
            "search": kwargs["search_override"] or cached_search,
            "extraction": _FakeExtraction(),
            "reranker": _FakeReranker(),
            "embedder": None,
            "nli": _FakeNLI(),
        }

    monkeypatch.setattr("disco.retrieval.live.build_multi_search", fake_build_multi_search)
    monkeypatch.setattr("disco.retrieval.live.build_live_retrieval", fake_build_live_retrieval)

    frames = [
        frame
        async for frame in rt.deep_research.research_stream(
            "capital?",
            sources=["arxiv", "ddgs"],
        )
    ]

    assert seen["sources"] == ("arxiv", "ddgs")
    assert seen["search_override"] is override_search
    assert override_search.calls == 1
    assert cached_search.calls == 0
    assert any(frame["type"] == "final" for frame in frames)


def test_think_toggles_reasoning_on_the_answerer_provider(tmp_path):
    # The Think toggle threads through to the per-request provider build: the
    # answerer's endpoint provider runs in reasoning mode (or not) accordingly.
    store = SqliteEventStore(":memory:")
    config_store = ConfigStore(tmp_path / "config.json")
    secret_store = SecretStore(
        tmp_path / "secrets.json", box=SecretBox("research-test-secret-32-bytes")
    )
    cfg = config_store.load()
    qwen = cfg.models["driver-local"]
    assert qwen.base_url is not None
    config_store.approvals.approve_origin(
        qwen.base_url,
        f"model:{qwen.provider}",
        qwen.api_key_env or "",
        secret_store=secret_store,
    )
    rt = ConversationRuntime(
        store,
        config_store=config_store,
        secret_store=secret_store,
    )
    on = rt._router_now(enable_thinking=True)._providers["qwen"]
    off = rt._router_now(enable_thinking=False)._providers["qwen"]
    assert on._enable_thinking is True
    assert off._enable_thinking is False


def test_research_rejects_empty_query():
    store = SqliteEventStore(":memory:")
    app = create_app(store, runtime=_runtime(store))
    client = TestClient(app)
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "   "})
        f = ws.receive_json()
    assert f["type"] == "error" and "empty" in f["message"]


def test_research_unavailable_without_runtime():
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store))  # no runtime
    with client.websocket_connect("/ws/research") as ws:
        ws.send_json({"query": "anything"})
        f = ws.receive_json()
    assert f["type"] == "error" and "not available" in f["message"]


def test_autonomous_deep_research_auto_approves_plan(tmp_path, monkeypatch):
    """Regression: an autonomous/headless deep_research run must NOT stall at
    AWAITING_PLAN_APPROVAL — it auto-approves the synthetic plan inline and runs
    the engine. (Both halves of the fix: surface eligibility + inline approve.)"""
    import asyncio
    from unittest.mock import AsyncMock

    import disco.agent_server.deep_research_service as drsvc
    from disco.core.events import (
        ConversationStatus,
        EventSource,
        LLMMessage,
        MessageEvent,
        StatusEvent,
    )

    class _SubQ:
        def __init__(self, title):
            self.title = title

    async def _fake_decompose(router, query, max_subq=5, recency_window=None):
        return [_SubQ("sub one"), _SubQ("sub two")]

    monkeypatch.setattr(drsvc, "decompose_query", _fake_decompose)

    def _details(events):
        return [e.detail for e in events if isinstance(e, StatusEvent)]

    # --- autonomous: auto-approves + runs the engine ---
    store = SqliteEventStore(":memory:")
    rt = _runtime(store)
    cid = "conv-dr-auto"
    asyncio.run(
        store.append(
            cid,
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(role="user", content="What is X?"),
            ),
        )
    )
    rt.settings._set_surface(cid, "deep_research")
    rt.settings.set_autonomous(cid, True)
    exec_mock = AsyncMock()
    monkeypatch.setattr(rt.deep_research, "_execute_deep_research", exec_mock)
    asyncio.run(
        rt.deep_research._propose_deep_research_plan(cid, asyncio.run(store.get_events(cid)))
    )
    details = _details(asyncio.run(store.get_events(cid)))
    assert "plan_approved" in details, details
    assert "AWAITING_PLAN_APPROVAL" not in [str(d) for d in details]
    assert exec_mock.await_count == 1

    # --- interactive control: stalls at approval, engine NOT run ---
    store2 = SqliteEventStore(":memory:")
    rt2 = _runtime(store2)
    cid2 = "conv-dr-manual"
    asyncio.run(
        store2.append(
            cid2,
            MessageEvent(
                source=EventSource.USER,
                message=LLMMessage(role="user", content="What is Y?"),
            ),
        )
    )
    rt2.settings._set_surface(cid2, "deep_research")  # autonomous NOT set
    exec_mock2 = AsyncMock()
    monkeypatch.setattr(rt2.deep_research, "_execute_deep_research", exec_mock2)
    asyncio.run(
        rt2.deep_research._propose_deep_research_plan(
            cid2, asyncio.run(store2.get_events(cid2))
        )
    )
    statuses2 = [e for e in asyncio.run(store2.get_events(cid2)) if isinstance(e, StatusEvent)]
    assert any(e.status == ConversationStatus.AWAITING_PLAN_APPROVAL for e in statuses2)
    assert exec_mock2.await_count == 0

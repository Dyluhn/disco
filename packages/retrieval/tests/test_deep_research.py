"""Deep Research engine — hermetic test.

Drives the full lifecycle with fake providers (search, extract, rerank, NLI,
embedder) and a scripted router. Asserts the four load-bearing properties:

  1. The decompose step produces sub-questions from the query.
  2. The retrieve-reason-refine loop accumulates a corpus across rounds (not
     single-pass) and stops when the gap-reasoner says sufficient.
  3. Map-reduce synthesis produces one ReportSection per sub-question, each
     grounded in its retrieved subset, with per-claim NLI verification.
  4. Depth bound terminates: setting a tiny cap → bounded_by set on the
     resulting transport object, not a hang.

The progress callback's events are also captured so the agent-server's
event-emission shape can be validated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from perpleximanus.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from perpleximanus.retrieval.deep_research import (
    DeepResearchRun,
    DepthBound,
    DepthTier,
    bounds_for,
    decompose_query,
)
from perpleximanus.retrieval.deep_research.decompose import SubQuestion
from perpleximanus.retrieval.deep_research.gather import (
    SubQuestionResult,
    gather_for_subquestion,
)
from perpleximanus.retrieval.engine import DefaultRetrievalEngine
from perpleximanus.retrieval.models import (
    ExtractedDoc,
    Passage,
    RetrievalRequest,
    RetrievalResult,
    SearchHit,
)
from perpleximanus.retrieval.vectorstore import InMemoryVectorStore

# ---- fakes ------------------------------------------------------------------


class _FakeSearch:
    """Returns deterministic ranked hits per query. Tracks calls so tests can
    assert iteration shape (multi-round not single-pass)."""

    name = "fake_search"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: Any = None,
        domains_deny: Any = None,
    ) -> list[SearchHit]:
        self.calls.append(query)
        return [
            SearchHit(
                url=f"https://example.com/{query}/{i}",
                title=f"{query} — source {i}",
                snippet=f"snippet for {query} #{i}",
                source_engine="fake",
                rank=i,
            )
            for i in range(min(limit, 3))
        ]


class _FakeExtraction:
    """Maps each URL to a 1-passage extracted doc with sequential, predictable
    passage ids (p0, p1, p2, ...) so scripted synthesis responses can cite
    deterministically."""

    name = "fake_extract"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._counter = 0
        self._by_url: dict[str, int] = {}

    async def extract(self, url: str) -> ExtractedDoc:
        return (await self.extract_many([url]))[0]

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        self.calls.extend(urls)
        out = []
        for url in urls:
            if url not in self._by_url:
                self._by_url[url] = self._counter
                self._counter += 1
            idx = self._by_url[url]
            content = (
                f"Body for {url} — evidence relevant to the research question. "
                f"Data was collected in 2024."
            )
            out.append(
                ExtractedDoc(
                    url=url,
                    title=f"Title for {url}",
                    content=content,
                    passages=[
                        Passage(
                            id=f"p{idx}",
                            source_url=url,
                            source_title=f"Title for {url}",
                            text=content,
                        )
                    ],
                    fetched_ok=True,
                    status="ok",
                )
            )
        return out


class _FakeReranker:
    async def rerank(
        self, query: str, passages: list[Passage], *, top_k: int
    ) -> list[Passage]:
        # rerank is a no-op for the fake — order by id for determinism.
        return sorted(passages, key=lambda p: p.id)[:top_k]


class _FakeEmbedder:
    """Deterministic 8-dim hash embeddings; sufficient for cosine retrieval to
    sort but not meaningful semantically."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            # tiny pseudo-embedding: byte-position sums modulo 1.0
            vec = [(sum(ord(c) for c in t[i::8]) % 97) / 97.0 for i in range(8)]
            out.append(vec)
        return out


class _FakeNLI:
    """Always returns 'entail' for non-empty premises that share a word with
    the claim; otherwise 'neutral'. Deterministic + verifies per-claim flow."""

    def entail(self, premise: str, hypothesis: str) -> str:
        if not premise or not hypothesis:
            return "neutral"
        prem_words = set(premise.lower().split())
        hyp_words = set(hypothesis.lower().split())
        return "entail" if prem_words & hyp_words else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


class _ScriptedRouter(LLMRouter):
    """Returns scripted responses based on a per-role queue. Tests script the
    decompose, gap_reason, section synthesis, and coherence calls. Round-robin
    across the queue per role; falls back to a benign default if exhausted."""

    def __init__(self, scripts: dict[str, list[str]] | None = None) -> None:
        self._scripts: dict[str, list[str]] = {
            "query_rewriter": [],
            "rag_answerer": [],
        }
        if scripts:
            for k, v in scripts.items():
                self._scripts[k] = list(v)
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        role = (
            request.profile.role.value if hasattr(request.profile.role, "value")
            else str(request.profile.role)
        )
        # snapshot what the LAST user message was for assertions
        last_msg = request.messages[-1].content if request.messages else ""
        self.calls.append((role, last_msg))
        queue = self._scripts.get(role, [])
        if queue:
            text = queue.pop(0)
        elif role == "query_rewriter":
            text = "SUFFICIENT\nnone"  # default: stop the loop
        else:
            text = "(no scripted response)"
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="fake",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))
        return gen()


# ---- helpers ----------------------------------------------------------------


def _collect_events() -> tuple[list[tuple[str, dict]], Any]:
    """Build a capture list + an async emit callback the engine can await."""
    captured: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        captured.append((kind, payload))

    return captured, emit


# ---- the decompose step -----------------------------------------------------


async def test_decompose_returns_sub_questions() -> None:
    router = _ScriptedRouter({
        "query_rewriter": [
            "What is X?\nHow does X work today?\nWhere is X going?"
        ],
    })
    subqs = await decompose_query(router, "the state of X", max_subq=6)
    assert [s.title for s in subqs] == [
        "What is X?",
        "How does X work today?",
        "Where is X going?",
    ]


async def test_decompose_falls_back_when_empty() -> None:
    router = _ScriptedRouter({"query_rewriter": [""]})
    subqs = await decompose_query(router, "the state of X", max_subq=6)
    # never returns an empty list — falls back to the original query as 1 step
    assert len(subqs) == 1
    assert subqs[0].title == "the state of X"


# ---- gather: retrieve-reason-refine ----------------------------------------


async def test_gather_iterates_multiple_rounds_until_sufficient() -> None:
    """Gap reasoner says GAP on round 1, SUFFICIENT on round 2 → 2 rounds run,
    with a follow-up query issued between them."""
    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    router = _ScriptedRouter({
        "query_rewriter": [
            "GAP: missing recent data\nlatest 2024 numbers",  # round 1 → refine
            "SUFFICIENT\nnone",  # round 2 → stop
        ],
    })
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    bound = DepthBound(
        max_sources=20, max_rounds_per_subq=3, max_wall_clock_s=60,
        max_subquestions=6, discover_limit=8, extract_cap=4, rerank_top_k=4,
    )
    captured, emit = _collect_events()
    result: SubQuestionResult = await gather_for_subquestion(
        SubQuestion(title="What is X?"),
        engine=engine, router=router, embedder=embedder,
        vector_store=vector_store, namespace="conv_test",
        bound=bound, emit=emit, remaining_source_budget=20,
    )
    assert result.rounds_run == 2  # multi-round, not single-pass
    assert result.issued_queries == ["What is X?", "latest 2024 numbers"]
    assert len(result.passages) > 0
    # emit callback received search + observation + gap_reason events
    kinds = [k for k, _ in captured]
    assert "search" in kinds and "observation" in kinds and "gap_reason" in kinds


async def test_gather_stops_at_round_cap_with_bounded_by_rounds() -> None:
    """When the gap reasoner keeps saying GAP, we still terminate at the round
    cap and mark `bounded_by_rounds` so the report surface can call it out."""
    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    router = _ScriptedRouter({
        # always says GAP — would loop forever without the cap
        "query_rewriter": [
            "GAP: still incomplete\nmore data please",
            "GAP: still incomplete\nyet more data",
        ],
    })
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    bound = DepthBound(
        max_sources=20, max_rounds_per_subq=2,  # tiny: 2 rounds max
        max_wall_clock_s=60, max_subquestions=6,
        discover_limit=8, extract_cap=4, rerank_top_k=4,
    )
    captured, emit = _collect_events()
    result = await gather_for_subquestion(
        SubQuestion(title="What is X?"),
        engine=engine, router=router, embedder=embedder,
        vector_store=vector_store, namespace="conv_test",
        bound=bound, emit=emit, remaining_source_budget=20,
    )
    assert result.rounds_run == 2
    assert result.bounded_by_rounds is True


# ---- the orchestrator: end-to-end ------------------------------------------


async def test_full_run_produces_multi_section_report() -> None:
    """End-to-end: 3 sub-questions × 2 rounds each, corpus accumulates,
    map-reduce produces a 3-section ReportFromRun whose .to_event() returns
    a valid ReportEvent. The headline contract."""
    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    router = _ScriptedRouter({
        "query_rewriter": [
            # round-1 gap reasoner for each subq says SUFFICIENT (1 round each)
            "SUFFICIENT\nnone",
            "SUFFICIENT\nnone",
            "SUFFICIENT\nnone",
        ],
        # synthesis: one body per section + coherence summary (4 total)
        # Each section retrieves up to top_k passages from the in-memory vector
        # store. Section 1 sees passages from the first sub-q's search (p0,p1,
        # p2), section 2 from the second (p3-onwards). We cite at least one
        # known id in each so cited_passage_ids is populated.
        "rag_answerer": [
            "The basics of X are well-established [[p0]]. Sources cite consistent data [[p1]].",
            "Today, X works via mechanism Y [[p3]]. Systems demonstrate this [[p4]].",
            "Future directions point to Z [[p6]]. The field expects progress [[p7]].",
            "This report surveys X — its basics, present state, and outlook.",
        ],
    })
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    # Pass embedder=None so the section-retrieval falls back to the sub-q's
    # gathered passages (deterministic id ordering: section i's by_id = the
    # passages from sub-q i's gather, which are sequentially `p{i*3..}`).
    run = DeepResearchRun(
        query="the state of X",
        router=router, retrieval_engine=engine,
        embedder=None, vector_store=vector_store, nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP, conversation_id="conv_e2e",
    )
    plan_steps = ["What is X?", "How does X work today?", "Where is X going?"]
    captured, emit = _collect_events()
    result = await run.run(plan_steps, emit=emit)

    # the headline: 3 sections + an executive summary
    assert len(result.sections) == 3
    assert [s.title for s in result.sections] == plan_steps
    assert result.summary.startswith("This report surveys X")
    # bounded_by is None when nothing hit the caps
    assert result.bounded_by is None
    # each section grounds in the corpus + has at least one cited passage id
    # whose id matches a real passage from the sub-q's gather (real grounding,
    # not unverified citation markup).
    for s in result.sections:
        assert s.markdown and "[[" in s.markdown
        assert s.cited_passage_ids, f"section {s.id} had no valid cited ids: {s.markdown!r}"
        assert s.confidence in ("high", "mixed", "low")
    # the assembled report event is constructible + has the expected shape
    ev = result.to_event()
    assert ev.kind.value == "report"
    assert ev.query == "the state of X"
    assert len(ev.sections) == 3
    assert ev.passages  # cited subset is populated
    # progress callback fired the right shape (phases + searches + synthesize)
    phases = [p for k, p in captured if k == "phase"]
    assert any(p.get("phase") == "gather" for p in phases)
    assert any(p.get("phase") == "synthesize" for p in phases)
    assert any(p.get("phase") == "coherence" for p in phases)


async def test_should_cancel_halts_at_checkpoint_with_partial_report() -> None:
    """Stop is REAL: `should_cancel` is polled at each sub-question boundary; when
    it trips, the run halts there, returns the partial sections gathered so far, and
    flags bounded_by='stopped'. (Regression for "Stop is useless" — the engine used
    to run to completion regardless.)"""
    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    router = _ScriptedRouter({
        "query_rewriter": ["SUFFICIENT\nnone"] * 5,
        "rag_answerer": ["body [[p0]]"] * 5 + ["summary"],
    })
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    run = DeepResearchRun(
        query="the state of X",
        router=router, retrieval_engine=engine,
        embedder=None, vector_store=vector_store, nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP, conversation_id="conv_stop",
    )
    plan_steps = ["What is X?", "How does X work?", "Where is X going?", "Risks?"]
    _, emit = _collect_events()

    # Cancel after the first sub-question is gathered: returns False once (the first
    # gather proceeds), then True (the second checkpoint halts).
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    result = await run.run(plan_steps, emit=emit, should_cancel=should_cancel)

    assert result.bounded_by == "stopped"  # halted by Stop, not by a cap
    assert len(result.sections) < len(plan_steps)  # partial — it did NOT finish all 4
    assert result.to_event().bounded_by == "stopped"  # propagates to the event


async def test_no_should_cancel_runs_to_completion() -> None:
    """Without a cancel hook the run is uninterruptible (baseline) — every section."""
    search, extraction, reranker, embedder = (
        _FakeSearch(), _FakeExtraction(), _FakeReranker(), _FakeEmbedder()
    )
    router = _ScriptedRouter({
        "query_rewriter": ["SUFFICIENT\nnone"] * 4,
        "rag_answerer": ["body [[p0]]"] * 4 + ["summary"],
    })
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    run = DeepResearchRun(
        query="X", router=router, retrieval_engine=engine, embedder=None,
        vector_store=InMemoryVectorStore(), nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP, conversation_id="conv_nostop",
    )
    _, emit = _collect_events()
    result = await run.run(["a", "b"], emit=emit)  # no should_cancel
    assert result.bounded_by is None and len(result.sections) == 2


async def test_cap_hit_produces_bounded_by_subquestions() -> None:
    """Plan width over the tier cap → truncate + bounded_by='subquestions'."""
    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    router = _ScriptedRouter({
        "query_rewriter": ["SUFFICIENT\nnone"] * 10,
        "rag_answerer": ["body [[p0]]"] * 10 + ["summary"],
    })
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    run = DeepResearchRun(
        query="overview", router=router, retrieval_engine=engine,
        embedder=embedder, vector_store=vector_store, nli=_FakeNLI(),
        depth=DepthTier.QUICK,  # cap = 3 sub-questions
        conversation_id="conv_cap",
    )
    # propose 5 — should be truncated to 3
    plan_steps = [f"sub {i}" for i in range(5)]
    _, emit = _collect_events()
    result = await run.run(plan_steps, emit=emit)
    assert len(result.sections) == 3
    assert result.bounded_by == "subquestions"


# ---- depth-tier bound smoke ----


def test_bounds_for_three_tiers_are_distinct_and_ordered() -> None:
    quick = bounds_for(DepthTier.QUICK)
    std = bounds_for(DepthTier.STANDARD_DEEP)
    deep = bounds_for(DepthTier.EXHAUSTIVE)
    assert quick.max_sources < std.max_sources < deep.max_sources
    assert quick.max_subquestions < std.max_subquestions < deep.max_subquestions
    assert quick.max_wall_clock_s < std.max_wall_clock_s < deep.max_wall_clock_s


def test_bounds_for_accepts_string_value() -> None:
    a = bounds_for("standard_deep")
    b = bounds_for(DepthTier.STANDARD_DEEP)
    assert a == b


# unused imports placeholder to keep import-sort tooling clean
_ = pytest, RetrievalRequest, RetrievalResult

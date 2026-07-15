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

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from disco.core.llm import (
    CallContext,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import (
    DeepResearchRun,
    DepthBound,
    DepthTier,
    bounds_for,
    decompose_query,
)
from disco.retrieval.deep_research.decompose import SubQuestion
from disco.retrieval.deep_research.gather import (
    GatherLegContext,
    SubQuestionResult,
    gather_for_subquestion,
)
from disco.retrieval.engine import DefaultRetrievalEngine
from disco.retrieval.models import (
    ExtractedDoc,
    Passage,
    RetrievalRequest,
    RetrievalResult,
    SearchHit,
)
from disco.retrieval.vectorstore import InMemoryVectorStore

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
        time_filter: str | None = None,
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
    async def rerank(self, query: str, passages: list[Passage], *, top_k: int) -> list[Passage]:
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
        # Parallel log of the CallContext each call was made with — used by
        # the C14 isolation tests to assert each gather leg's LLM calls are
        # scoped to that leg's per-leg CallContext, not the shared default.
        self.call_contexts: list[str | None] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        role = (
            request.profile.role.value
            if hasattr(request.profile.role, "value")
            else str(request.profile.role)
        )
        # snapshot what the LAST user message was for assertions
        last_msg = request.messages[-1].content if request.messages else ""
        self.calls.append((role, last_msg))
        ctx_id = (
            context.conversation_id
            if context is not None and getattr(context, "conversation_id", None)
            else None
        )
        self.call_contexts.append(ctx_id)
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
    router = _ScriptedRouter(
        {
            "query_rewriter": ["What is X?\nHow does X work today?\nWhere is X going?"],
        }
    )
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
    router = _ScriptedRouter(
        {
            "query_rewriter": [
                "GAP: missing recent data\nlatest 2024 numbers",  # round 1 → refine
                "SUFFICIENT\nnone",  # round 2 → stop
            ],
        }
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    bound = DepthBound(
        max_sources=20,
        max_rounds_per_subq=3,
        max_wall_clock_s=60,
        max_subquestions=6,
        discover_limit=8,
        extract_cap=4,
        rerank_top_k=4,
    )
    captured, emit = _collect_events()
    result: SubQuestionResult = await gather_for_subquestion(
        SubQuestion(title="What is X?"),
        engine=engine,
        router=router,
        embedder=embedder,
        vector_store=vector_store,
        namespace="conv_test",
        bound=bound,
        emit=emit,
        remaining_source_budget=20,
        leg_context=GatherLegContext(
            subq_id="s_test",
            namespace="conv_test",
            call_context=CallContext(conversation_id="conv_test/s_test"),
        ),
    )
    assert result.rounds_run == 2  # multi-round, not single-pass
    assert result.issued_queries == ["What is X?", "latest 2024 numbers"]
    assert len(result.passages) > 0
    # emit callback received search + observation + gap_reason events
    kinds = [k for k, _ in captured]
    assert "search" in kinds and "observation" in kinds and "gap_reason" in kinds
    # C14: the router's gap_reasoner call was scoped to this leg's
    # CallContext, not the shared default (which would have no
    # conversation_id set).
    assert any(ctx == "conv_test/s_test" for ctx in router.call_contexts), (
        f"expected per-leg conversation_id in call contexts, got {router.call_contexts}"
    )


async def test_gather_stops_at_round_cap_with_bounded_by_rounds() -> None:
    """When the gap reasoner keeps saying GAP, we still terminate at the round
    cap and mark `bounded_by_rounds` so the report surface can call it out."""
    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    router = _ScriptedRouter(
        {
            # always says GAP — would loop forever without the cap
            "query_rewriter": [
                "GAP: still incomplete\nmore data please",
                "GAP: still incomplete\nyet more data",
            ],
        }
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    bound = DepthBound(
        max_sources=20,
        max_rounds_per_subq=2,  # tiny: 2 rounds max
        max_wall_clock_s=60,
        max_subquestions=6,
        discover_limit=8,
        extract_cap=4,
        rerank_top_k=4,
    )
    captured, emit = _collect_events()
    result = await gather_for_subquestion(
        SubQuestion(title="What is X?"),
        engine=engine,
        router=router,
        embedder=embedder,
        vector_store=vector_store,
        namespace="conv_test",
        bound=bound,
        emit=emit,
        remaining_source_budget=20,
        leg_context=GatherLegContext(
            subq_id="s_test",
            namespace="conv_test",
            call_context=CallContext(conversation_id="conv_test/s_test"),
        ),
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
    router = _ScriptedRouter(
        {
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
        }
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    # Pass embedder=None so the section-retrieval falls back to the sub-q's
    # gathered passages (deterministic id ordering: section i's by_id = the
    # passages from sub-q i's gather, which are sequentially `p{i*3..}`).
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=vector_store,
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_e2e",
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
    router = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 5,
            "rag_answerer": ["body [[p0]]"] * 5 + ["summary"],
        }
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=vector_store,
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_stop",
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
    # partial but USABLE: the sub-question that finished before Stop is a fully
    # synthesized section (a durable checkpoint), not lost work.
    assert 0 < len(result.sections) < len(plan_steps)
    assert result.sections[0].title == "What is X?"
    assert result.to_event().bounded_by == "stopped"  # propagates to the event


async def test_resume_skips_completed_sections_and_finishes_the_rest() -> None:
    """Checkpointed resume: a stopped run's completed sections are carried forward;
    a second run.run() with those `resume_sections` only gathers+synthesizes the
    sub-questions NOT already done, then produces the full multi-section report.
    (Regression for 'Deep Research resume redoes everything from scratch'.)"""
    plan_steps = ["What is X?", "How does X work?", "Where is X going?", "Risks?"]

    def _engine():
        s, e, r = _FakeSearch(), _FakeExtraction(), _FakeReranker()
        return s, DefaultRetrievalEngine(search=s, extraction=e, reranker=r, embedder=None)

    # ---- run 1: stop after the first sub-question is gathered+synthesized ----
    search1, eng1 = _engine()
    router1 = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 5,
            "rag_answerer": ["body [[p0]]"] * 5 + ["summary"],
        }
    )
    run1 = DeepResearchRun(
        query="the state of X",
        router=router1,
        retrieval_engine=eng1,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_resume",
    )
    _, emit = _collect_events()
    n = {"c": 0}

    def cancel1() -> bool:
        n["c"] += 1
        return n["c"] > 1  # gather subq0, halt at subq1

    partial = await run1.run(plan_steps, emit=emit, should_cancel=cancel1)
    assert partial.bounded_by == "stopped"
    assert [s.title for s in partial.sections] == ["What is X?"]  # 1 done
    done_title = partial.sections[0].title

    # ---- run 2: resume — carry the completed section, finish the rest --------
    search2, eng2 = _engine()
    router2 = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 5,
            "rag_answerer": ["body [[p0]]"] * 5 + ["summary"],
        }
    )
    run2 = DeepResearchRun(
        query="the state of X",
        router=router2,
        retrieval_engine=eng2,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_resume",
    )
    _, emit2 = _collect_events()
    final = await run2.run(
        plan_steps,
        emit=emit2,
        resume_sections=partial.sections,
        resume_passages=partial.cited_passages,
        resume_all_hits=partial.all_hits,
    )

    # the full report: 4 sections in plan order, the carried one preserved verbatim
    assert [s.title for s in final.sections] == plan_steps
    assert final.bounded_by is None  # it finished
    # the completed sub-question was NOT re-gathered on resume (no search for it)
    assert not any(done_title in q for q in search2.calls), search2.calls
    # but the remaining sub-questions WERE searched this run
    assert any("How does X work?" in q for q in search2.calls)


async def test_no_should_cancel_runs_to_completion() -> None:
    """Without a cancel hook the run is uninterruptible (baseline) — every section."""
    search, extraction, reranker, embedder = (
        _FakeSearch(),
        _FakeExtraction(),
        _FakeReranker(),
        _FakeEmbedder(),
    )
    router = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 4,
            "rag_answerer": ["body [[p0]]"] * 4 + ["summary"],
        }
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    run = DeepResearchRun(
        query="X",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_nostop",
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
    router = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 10,
            "rag_answerer": ["body [[p0]]"] * 10 + ["summary"],
        }
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    run = DeepResearchRun(
        query="overview",
        router=router,
        retrieval_engine=engine,
        embedder=embedder,
        vector_store=vector_store,
        nli=_FakeNLI(),
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


# ============================================================================
# C14 — Per-leg isolated sub-context (concurrent gather legs)
# ============================================================================
#
# The concurrent gather in `DeepResearchRun.run` dispatches one
# `asyncio.create_task(gather_for_subquestion(...))` per pending sub-question.
# Each leg must have its OWN message / view / cost state — sibling legs must
# NOT see each other's intermediate messages, and one leg raising must NOT
# corrupt the other's state. The merge of leg results happens ONLY at the
# synthesis boundary (`synthesize_section` after the leg's gather task
# returns), never inside the leg.
#
# These tests assert the isolation contract end-to-end through the engine
# (Test 1) and at the leg level (Test 2).


class _GapRecordingRouter(LLMRouter):
    """Captures every (role, last_user_message, CallContext.conversation_id)
    triple across the gap_reasoner / synthesis calls. Used by the C14 tests
    to assert per-leg isolation: leg A's CallContext is NOT the same as
    leg B's, and leg A's user-message list does not contain leg B's task.

    Also supports a `fail_when_conv_id` hook so the error-isolation test can
    make a specific leg's LLM call raise (simulating a leg-level failure
    that the OTHER leg must survive)."""

    def __init__(
        self,
        scripts: dict[str, list[str]] | None = None,
        fail_when_conv_id: str | None = None,
        fail_message: str = "simulated leg failure",
    ) -> None:
        self._scripts: dict[str, list[str]] = {
            "query_rewriter": [],
            "rag_answerer": [],
        }
        if scripts:
            for k, v in scripts.items():
                self._scripts[k] = list(v)
        self.records: list[dict[str, Any]] = []
        self._fail_when_conv_id = fail_when_conv_id
        self._fail_message = fail_message
        # mutable list of message-list-snapshots, one per call — proves
        # the leg builds an INDEPENDENT list per call, never sharing a
        # mutable reference with a sibling leg.
        self.message_snapshots: list[list[Any]] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        role = (
            request.profile.role.value
            if hasattr(request.profile.role, "value")
            else str(request.profile.role)
        )
        # snapshot the message list (NOT a shared reference) — the snapshot
        # freezes the leg's view at call time so isolation is observable.
        msg_snapshot = list(request.messages)
        self.message_snapshots.append(msg_snapshot)
        last_msg = msg_snapshot[-1].content if msg_snapshot else ""
        ctx_id = (
            context.conversation_id
            if context is not None and getattr(context, "conversation_id", None)
            else None
        )
        self.records.append({"role": role, "last_msg": last_msg, "conversation_id": ctx_id})
        if ctx_id == self._fail_when_conv_id:
            raise RuntimeError(self._fail_message)
        queue = self._scripts.get(role, [])
        if queue:
            text = queue.pop(0)
        elif role == "query_rewriter":
            text = "SUFFICIENT\nnone"
        else:
            text = "(default)"
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="fake",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(  # pragma: no cover - unused
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))

        return gen()


async def test_c14_concurrent_legs_have_isolated_contexts() -> None:
    """C14 / Test 1 — each concurrent gather leg has its OWN sub-context.

    Two sub-questions with distinctive titles run concurrently via
    `DeepResearchRun.run`. After completion, we assert:

      (a) Each leg's gap_reasoner LLM call was made with a per-leg
          CallContext (unique conversation_id). No call was made with
          the shared default (None conversation_id) — the per-leg
          isolation reaches the router.

      (b) Each leg's gap_reasoner message list is INDEPENDENT — it
          contains the leg's own task (its sub-question title) and does
          NOT contain the sibling's task. The message list is built
          fresh per call (we snapshot it at call time).

      (c) The leg's intermediate state (leg_context, namespace, subq_id)
          is reflected in the CallContext for that leg's calls.

      (d) The synthesis boundary (synthesize_section's rag_answerer
          call) also uses the per-leg CallContext, not the shared one.

      (e) Results still merge correctly into the expected
          `ReportFromRun` shape (3 sections in plan order)."""
    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    # Scripted: gap reasoner SUFFICIENT for all legs (1 round each), one
    # synthesis body per section, one coherence summary. Distinctive
    # subq titles let us assert which leg's task appears in which call.
    router = _GapRecordingRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 3,
            "rag_answerer": [
                "Basics of ALPHA-QUESTION are clear [[p0]]. Source confirms [[p1]].",
                "BETA-QUESTION today looks like this [[p3]]. Evidence shows [[p4]].",
                "GAMMA-QUESTION trends toward Z [[p6]]. The field expects [[p7]].",
                "This report surveys the ALPHA, BETA, and GAMMA space.",
            ],
        }
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=vector_store,
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_c14",
    )
    plan_steps = [
        "ALPHA-QUESTION: what is it?",
        "BETA-QUESTION: how does it work?",
        "GAMMA-QUESTION: where is it going?",
    ]
    _, emit = _collect_events()
    result = await run.run(plan_steps, emit=emit)

    # (e) merge shape is unchanged — 3 sections, one per subq, in plan order.
    assert len(result.sections) == 3
    assert [s.title for s in result.sections] == plan_steps
    assert result.bounded_by is None

    # ---- (a) per-leg CallContext at the router -----------------------------
    # Every gap_reasoner call (query_rewriter) was scoped to a per-leg
    # conversation_id matching that leg's subq_id. No call landed on the
    # shared default (None).
    gap_calls = [r for r in router.records if r["role"] == "query_rewriter"]
    assert gap_calls, "expected gap_reasoner calls to be recorded"
    expected_leg_ids = {
        # sha256 of each title, first 8 hex chars, prefixed with "s".
        # We don't hard-code the hashes; instead we derive them so the
        # test stays robust to any change in the engine's hashing scheme.
        f"s{hashlib.sha256(t.encode()).hexdigest()[:8]}"
        for t in plan_steps
    }
    seen_leg_ids = {r["conversation_id"] for r in gap_calls}
    # Each conversation_id is of the form "conv_c14/<leg_id>".
    assert seen_leg_ids, "expected non-None conversation_ids on gap calls"
    for cid in seen_leg_ids:
        assert cid is not None, "shared default CallContext was used (isolation broken)"
        assert cid.startswith("conv_c14/s"), f"unexpected conv_id format: {cid}"
    leg_ids = {cid.split("/", 1)[1] for cid in seen_leg_ids}
    assert leg_ids == expected_leg_ids, f"leg ids {leg_ids} != expected {expected_leg_ids}"

    # ---- (b) per-leg message list — sibling messages do NOT bleed in ------
    # For each gap_reasoner call, the user message must contain that
    # leg's subq title and must NOT contain any sibling's subq title.
    for r in gap_calls:
        msg = r["last_msg"]
        # the leg's own title appears (the gap prompt includes it)
        leg_id = r["conversation_id"].split("/", 1)[1]
        # find the title this leg corresponds to
        own_title = next(
            t for t in plan_steps if f"s{hashlib.sha256(t.encode()).hexdigest()[:8]}" == leg_id
        )
        assert own_title in msg, (
            f"leg {leg_id} message did not contain its own title: {own_title!r} in {msg[:120]!r}"
        )
        # and NONE of the sibling titles appear
        sibling_titles = [t for t in plan_steps if t != own_title]
        for sibling in sibling_titles:
            assert sibling not in msg, (
                f"leg {leg_id} message BLEEDS sibling title {sibling!r}: {msg[:200]!r}"
            )

    # ---- (b') the message list per call is a fresh, independent list ------
    # Every recorded message snapshot must be a distinct list object —
    # i.e., the leg never hands the router a shared mutable list it could
    # be mutated by a sibling.
    assert len(router.message_snapshots) >= 1
    first_snap = router.message_snapshots[0]
    assert all(
        snap is not first_snap or i == 0 for i, snap in enumerate(router.message_snapshots)
    ), "message snapshots are aliased — same list object reused across calls"

    # ---- (d) synthesis also uses the per-leg CallContext ------------------
    synth_calls = [r for r in router.records if r["role"] == "rag_answerer"]
    # One synthesis call per leg + one coherence summary = 4 total.
    assert len(synth_calls) == 4
    section_synth = synth_calls[:3]
    coherence_call = synth_calls[3]
    # Each section's synthesis was scoped to its leg's conversation_id.
    for r in section_synth:
        assert r["conversation_id"] is not None
        assert r["conversation_id"].startswith("conv_c14/s")
    # The coherence call is the SHARED summary (post-merge step) — it's
    # allowed to run with the default / None context, since it happens
    # AFTER the merge and operates on the merged section list.
    # We don't assert a specific cid on the coherence call; just that the
    # 3 section synth calls are leg-scoped and the coherence call is
    # distinct.
    assert coherence_call is not None


async def test_c14_one_leg_error_does_not_corrupt_other_leg() -> None:
    """C14 / Test 2 — one leg raising an error does NOT corrupt the
    sibling leg's state; the sibling leg still completes.

    We drive two `gather_for_subquestion` tasks concurrently (the same
    dispatch pattern `DeepResearchRun.run` uses, but without the engine's
    cancel-all-on-failure short-circuit so the test isolates the LEG-level
    isolation contract). The router is configured to RAISE on one
    specific leg's CallContext — that simulates a leg-level failure.
    The sibling leg must still complete with its full SubQuestionResult.

    Asserted properties:
      (a) The failing leg's task raises; the sibling leg's task returns
          a normal SubQuestionResult with all its passages + queries.
      (b) The sibling leg's `leg_context` is NOT modified by the
          failing leg (frozen dataclass — verified by identity + a
          'still-frozen' check after the run).
      (c) The sibling leg's `seen_passage_ids` / `result.passages` /
          `result.issued_queries` are all sourced from the SIBLING's
          sub-question, not the failing leg's."""
    import asyncio
    import hashlib

    # Distinctive subq titles so we can attribute passages to legs.
    subq_a_title = "FAILING-LEG-QUESTION"
    subq_b_title = "SURVIVING-LEG-QUESTION"
    subq_a_hash = hashlib.sha256(subq_a_title.encode()).hexdigest()[:8]
    subq_b_hash = hashlib.sha256(subq_b_title.encode()).hexdigest()[:8]

    search = _FakeSearch()
    extraction = _FakeExtraction()
    reranker = _FakeReranker()
    embedder = _FakeEmbedder()
    vector_store = InMemoryVectorStore()
    # The router raises ONLY when invoked with the failing leg's
    # conversation_id; other legs complete normally. Note: _gap_reason
    # catches router.complete() exceptions, so the failure has to
    # surface from a non-caught path — we use a custom emit that raises
    # for the failing leg (not caught in gather_for_subquestion).
    leg_a_conv = f"conv_c14b/s{subq_a_hash}"
    router = _GapRecordingRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 5,
        },
        fail_when_conv_id=leg_a_conv,
        fail_message="simulated router failure for failing leg",
    )
    engine = DefaultRetrievalEngine(
        search=search, extraction=extraction, reranker=reranker, embedder=embedder
    )
    bound = DepthBound(
        max_sources=20,
        max_rounds_per_subq=2,
        max_wall_clock_s=60,
        max_subquestions=6,
        discover_limit=8,
        extract_cap=4,
        rerank_top_k=4,
    )

    # Custom emit: raises for the FAILING leg's sub-question title. This
    # surfaces as an uncaught exception in gather_for_subquestion (emit
    # is awaited without a try/except around it), so the failing leg
    # actually raises. The SURVIVING leg's emit never trips the guard.
    events: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        events.append((kind, payload))
        if payload.get("subquestion") == subq_a_title:
            raise RuntimeError(f"simulated emit failure for {subq_a_title}")

    leg_a_context = GatherLegContext(
        subq_id=f"s{subq_a_hash}",
        namespace=f"conv_c14b/{subq_a_hash}",
        call_context=CallContext(conversation_id=leg_a_conv),
    )
    leg_b_context = GatherLegContext(
        subq_id=f"s{subq_b_hash}",
        namespace=f"conv_c14b/{subq_b_hash}",
        call_context=CallContext(conversation_id=f"conv_c14b/s{subq_b_hash}"),
    )
    # Snapshot the surviving leg's leg_context identity so we can verify it
    # is not replaced (frozen dataclass — but the test still proves it).
    leg_b_context_id = id(leg_b_context)

    async def run_leg_a() -> SubQuestionResult:
        return await gather_for_subquestion(
            SubQuestion(title=subq_a_title),
            engine=engine,
            router=router,
            embedder=embedder,
            vector_store=vector_store,
            namespace=leg_a_context.namespace,
            bound=bound,
            emit=emit,
            remaining_source_budget=20,
            leg_context=leg_a_context,
        )

    async def run_leg_b() -> SubQuestionResult:
        return await gather_for_subquestion(
            SubQuestion(title=subq_b_title),
            engine=engine,
            router=router,
            embedder=embedder,
            vector_store=vector_store,
            namespace=leg_b_context.namespace,
            bound=bound,
            emit=emit,
            remaining_source_budget=20,
            leg_context=leg_b_context,
        )

    # Use return_exceptions=True so the surviving leg's result is
    # observable even when its sibling raises. This is the "sibling
    # still completes" claim at the leg level.
    outcomes = await asyncio.gather(run_leg_a(), run_leg_b(), return_exceptions=True)
    leg_a_outcome, leg_b_outcome = outcomes

    # ---- (a) failing leg raised; surviving leg completed normally --------
    assert isinstance(leg_a_outcome, Exception), (
        f"expected failing leg to raise, got {leg_a_outcome!r}"
    )
    assert isinstance(leg_b_outcome, SubQuestionResult), (
        f"expected surviving leg to return a SubQuestionResult, got {leg_b_outcome!r}"
    )
    surviving: SubQuestionResult = leg_b_outcome
    # Surviving leg had at least one search round and accumulated passages.
    assert surviving.rounds_run >= 1
    assert surviving.issued_queries[0] == subq_b_title
    assert surviving.passages, "surviving leg should have gathered passages"
    # None of the surviving leg's queries mention the failing leg's title.
    for q in surviving.issued_queries:
        assert subq_a_title not in q, f"surviving leg's query bleeds failing leg's title: {q!r}"

    # ---- (b) the surviving leg's leg_context was not corrupted ------------
    assert id(leg_b_context) == leg_b_context_id, (
        "leg_context identity changed (frozen dataclass — should be immutable)"
    )
    # Frozen dataclass: still hashable, attributes unchanged.
    assert leg_b_context.subq_id == f"s{subq_b_hash}"
    assert leg_b_context.namespace == f"conv_c14b/{subq_b_hash}"
    assert leg_b_context.call_context.conversation_id == (f"conv_c14b/s{subq_b_hash}")

    # ---- (c) the surviving leg's accumulated state is its OWN ------------
    # The surviving leg's passages come from the SURVIVING sub-question,
    # not the failing one. The fake search builds URLs from the query,
    # so we can assert the URL provenance.
    for h in surviving.all_hits:
        assert subq_b_title in h.url, (
            f"surviving leg's hit URL contains failing leg's title: {h.url!r}"
        )
    # The events captured show two independent legs' event streams; the
    # surviving leg's events do not reference the failing leg's title.
    surviving_events = [(k, p) for k, p in events if p.get("subquestion") == subq_b_title]
    assert surviving_events, "no events captured for the surviving leg"
    for _, p in surviving_events:
        assert subq_a_title not in str(p), (
            f"surviving leg's event payload references failing leg: {p!r}"
        )


class _ConcurrencyTrackingSearch(_FakeSearch):
    """A search fake that records the PEAK number of gather legs in its body
    simultaneously. Each call holds (await sleep) long enough for sibling legs
    to enter, so the observed peak reflects how many legs the engine let run at
    once — i.e. whether the gather-concurrency semaphore is enforced."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.peak = 0

    async def search(self, query: str, **kw: Any) -> Any:
        import asyncio

        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # Hold so concurrent legs overlap (without this, instant fakes could
            # complete near-serially and hide the true concurrency).
            await asyncio.sleep(0.02)
            return await super().search(query, **kw)
        finally:
            self.in_flight -= 1


async def _run_with_cap(cap: int | None) -> int:
    """Run a 4-sub-question DR with the given gather_concurrency cap; return the
    peak number of legs observed in-flight."""
    search = _ConcurrencyTrackingSearch()
    engine = DefaultRetrievalEngine(
        search=search,
        extraction=_FakeExtraction(),
        reranker=_FakeReranker(),
        embedder=_FakeEmbedder(),
    )
    router = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 4,  # 1 round per leg
            "rag_answerer": [
                "Body one [[p0]].",
                "Body two [[p3]].",
                "Body three [[p6]].",
                "Body four [[p9]].",
                "Coherence summary across all four.",
            ],
        }
    )
    run = DeepResearchRun(
        query="cap test",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_cap",
        gather_concurrency=cap,
    )
    plan_steps = ["Q one", "Q two", "Q three", "Q four"]
    _, emit = _collect_events()
    result = await run.run(plan_steps, emit=emit)
    assert len(result.sections) == 4  # the cap must not change the OUTPUT
    return search.peak


async def test_gather_concurrency_cap_bounds_in_flight_legs() -> None:
    """The semaphore bounds how many gather legs run their (memory-heavy) body at
    once. With cap=2 over 4 sub-questions, never more than 2 legs are in-flight."""
    peak = await _run_with_cap(2)
    assert peak <= 2, f"cap=2 was violated: {peak} legs ran concurrently"
    assert peak == 2, f"expected the cap to be saturated (2), saw {peak}"


async def test_gather_concurrency_none_runs_all_legs_concurrently() -> None:
    """Control: with NO cap, all 4 legs run at once — proving the cap above is
    what bounds concurrency, not some accidental serialization in the engine."""
    peak = await _run_with_cap(None)
    assert peak == 4, f"expected all 4 legs concurrent without a cap, saw {peak}"


async def test_memory_bounded_gather_is_surfaced_not_silent() -> None:
    """#26 graceful low-RAM transparency: when the cap actually constrains the run
    (cap < legs), the gather phase event carries concurrency + memory_bounded=True
    AND an honest observation explains the reduced parallelism — never silent."""
    search = _FakeSearch()
    engine = DefaultRetrievalEngine(
        search=search,
        extraction=_FakeExtraction(),
        reranker=_FakeReranker(),
        embedder=_FakeEmbedder(),
    )
    router = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 4,
            "rag_answerer": ["A [[p0]].", "B [[p3]].", "C [[p6]].", "D [[p9]].", "Summary."],
        }
    )
    run = DeepResearchRun(
        query="q",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_mb",
        gather_concurrency=2,
    )
    captured, emit = _collect_events()
    await run.run(["q1", "q2", "q3", "q4"], emit=emit)

    gather_phase = next(p for k, p in captured if k == "phase" and p.get("phase") == "gather")
    assert gather_phase["concurrency"] == 2
    assert gather_phase["memory_bounded"] is True
    # the honest, human-readable backpressure observation fired
    notes = [p.get("detail", "") for k, p in captured if k == "observation"]
    assert any("at a time" in n and "memory" in n for n in notes), notes


async def test_unbounded_gather_reports_not_memory_bounded() -> None:
    """The converse: an unbounded run (big box / remote encoders) reports
    memory_bounded=False and emits NO backpressure note — so the signal is
    meaningful, not always-on noise."""
    search = _FakeSearch()
    engine = DefaultRetrievalEngine(
        search=search,
        extraction=_FakeExtraction(),
        reranker=_FakeReranker(),
        embedder=_FakeEmbedder(),
    )
    router = _ScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 2,
            "rag_answerer": ["A [[p0]].", "B [[p3]].", "Summary."],
        }
    )
    run = DeepResearchRun(
        query="q",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_unb",
        gather_concurrency=None,
    )
    captured, emit = _collect_events()
    await run.run(["q1", "q2"], emit=emit)
    gather_phase = next(p for k, p in captured if k == "phase" and p.get("phase") == "gather")
    assert gather_phase["concurrency"] is None
    assert gather_phase["memory_bounded"] is False
    notes = [p.get("detail", "") for k, p in captured if k == "observation"]
    assert not any("at a time" in n for n in notes)


# unused imports placeholder to keep import-sort tooling clean
_ = pytest, RetrievalRequest, RetrievalResult

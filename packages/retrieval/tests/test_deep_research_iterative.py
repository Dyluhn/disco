"""A4.4 — iterative-research ENGINE GLUE, hermetic test.

Drives `DeepResearchRun.run(...)` with `iterative=True` end-to-end with hermetic
fakes, plus a router that distinguishes the LLM-JUDGE calls (A4.0) from
synthesis/coherence calls. The judge is scripted to return WEAK verdicts on the
first judging pass, then SUPPORTED after a refinement round — so the loop is
forced to:

  1. judge the first-synthesis sections (weak → below the 0.8 target),
  2. RE-GATHER + RE-SYNTHESIZE the weak section(s) (a fresh search leg fires),
  3. re-judge (now SUPPORTED) and converge.

It also asserts the OFF default (`iterative=False`) runs NEITHER the judge nor the
re-gather — the load-bearing byte-identical safety guarantee (the existing
`test_deep_research.py` proves byte-identity across the full lifecycle; here we
assert it explicitly on the same scenario).

Fakes are kept local (the tests dir is a flat, non-package layout, so the
neighbouring `test_deep_research.py` fakes can't be imported as a module).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import DeepResearchRun, DepthTier
from disco.retrieval.engine import DefaultRetrievalEngine
from disco.retrieval.models import ExtractedDoc, Passage, SearchHit
from disco.retrieval.vectorstore import InMemoryVectorStore

# A judge call is identifiable by the judge prompt's signature text.
_JUDGE_MARKER = "strict claim-grounding judge"


# ---- hermetic fakes ---------------------------------------------------------


class _FakeSearch:
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
    name = "fake_extract"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._counter = 0
        self._by_url: dict[str, int] = {}

    async def extract(self, url: str) -> ExtractedDoc:
        return (await self.extract_many([url]))[0]

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        self.calls.extend(urls)
        out: list[ExtractedDoc] = []
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
        return sorted(passages, key=lambda p: p.id)[:top_k]


class _FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [(sum(ord(c) for c in t[i::8]) % 97) / 97.0 for i in range(8)]
            out.append(vec)
        return out


class _FakeNLI:
    def entail(self, premise: str, hypothesis: str) -> str:
        if not premise or not hypothesis:
            return "neutral"
        shared = set(premise.lower().split()) & set(hypothesis.lower().split())
        return "entail" if shared else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


def _collect_events() -> tuple[list[tuple[str, dict]], Any]:
    captured: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        captured.append((kind, payload))

    return captured, emit


class _JudgeScriptedRouter(LLMRouter):
    """Routes RAG_ANSWERER calls into one of two queues based on whether the
    prompt is a JUDGE prompt (A4.0) or a synthesis/coherence prompt — so a test
    can script weak-then-SUPPORTED judge verdicts independently of section bodies.

    `judge` queue: one VERDICT word per `judge_claim` call.
    `rag_answerer` queue: synthesis section bodies + coherence summary.
    `query_rewriter` queue: gap-reasoner decisions."""

    def __init__(self, scripts: dict[str, list[str]] | None = None) -> None:
        self._scripts: dict[str, list[str]] = {
            "query_rewriter": [],
            "rag_answerer": [],
            "judge": [],
        }
        if scripts:
            for k, v in scripts.items():
                self._scripts[k] = list(v)
        self.calls: list[tuple[str, str]] = []
        self.judge_call_count = 0

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        role = (
            request.profile.role.value
            if hasattr(request.profile.role, "value")
            else str(request.profile.role)
        )
        last_msg = request.messages[-1].content if request.messages else ""
        is_judge = role == "rag_answerer" and _JUDGE_MARKER in last_msg
        effective_role = "judge" if is_judge else role
        if is_judge:
            self.judge_call_count += 1
        self.calls.append((effective_role, last_msg))
        queue = self._scripts.get(effective_role, [])
        if queue:
            text = queue.pop(0)
        elif effective_role == "query_rewriter":
            text = "SUFFICIENT\nnone"
        elif effective_role == "judge":
            text = "SUPPORTED"  # benign converged default
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


def _build_engine() -> tuple[_FakeSearch, DefaultRetrievalEngine]:
    search = _FakeSearch()
    return search, DefaultRetrievalEngine(
        search=search,
        extraction=_FakeExtraction(),
        reranker=_FakeReranker(),
        embedder=_FakeEmbedder(),
    )


# ---- tests ------------------------------------------------------------------


async def test_iterative_loop_re_searches_weak_sections_then_converges() -> None:
    """iterative=True: judge says weak on round 0 → the loop re-gathers the weak
    section (a fresh search fires) → re-judge says SUPPORTED → converged.

    One section: first body cites [[p0]]; the judge calls it UNSUPPORTED (below
    the 0.8 target) so the loop refines. The refine path re-synthesizes (a second
    body), and the re-judging returns SUPPORTED → convergence."""
    search, engine = _build_engine()
    router = _JudgeScriptedRouter(
        {
            # gap-reasoner: stop each gather leg after 1 round (initial + refine leg)
            "query_rewriter": ["SUFFICIENT\nnone"] * 6,
            # [0] initial section body, [1] re-synthesized body, [2] coherence summary.
            "rag_answerer": [
                "X is established by the data [[p0]].",
                "X is established by stronger fresh data [[p0]].",
                "This report surveys X.",
            ],
            # round-0 judging → UNSUPPORTED (weak → refine); post-refine → SUPPORTED.
            "judge": ["UNSUPPORTED", "SUPPORTED"],
        }
    )
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=None,  # fall back to gathered passages → deterministic ids
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_iter",
        iterative=True,
    )
    captured, emit = _collect_events()
    result = await run.run(["What is X?"], emit=emit)

    # The judge ran (OFF path would never call it): round-0 + post-refine judging.
    assert router.judge_call_count >= 2, (
        f"expected judge to run on round 0 + after refine, saw {router.judge_call_count}"
    )
    # A re-gather happened: the initial leg + the refine leg both searched.
    assert len(search.calls) > 1, f"expected a re-search leg to fire, calls: {search.calls}"
    # The section was REPLACED by the re-synthesized body (the loop improved it).
    assert len(result.sections) == 1
    assert "stronger fresh data" in result.sections[0].markdown
    # An iterate telemetry phase was emitted.
    phases = [p for k, p in captured if k == "phase"]
    assert any(p.get("phase") == "iterate" for p in phases)
    # The report still assembles into a valid event.
    ev = result.to_event()
    assert ev.kind.value == "report"
    assert len(ev.sections) == 1


async def test_off_default_runs_no_judge_and_no_re_search() -> None:
    """The OFF path (iterative=False, the default): the SAME scenario runs the
    judge ZERO times and fires NO refine leg — the section is exactly the first
    synthesis. The load-bearing byte-identical safety guarantee."""
    search, engine = _build_engine()
    router = _JudgeScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 6,
            "rag_answerer": [
                "X is established by the data [[p0]].",
                "X is established by stronger fresh data [[p0]].",
                "This report surveys X.",
            ],
            "judge": ["UNSUPPORTED", "SUPPORTED"],
        }
    )
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_off",
        # iterative defaults to False — do not set it.
    )
    captured, emit = _collect_events()
    result = await run.run(["What is X?"], emit=emit)

    # NO judge call at all on the OFF path.
    assert router.judge_call_count == 0
    # The section is the FIRST synthesis — never the refine body.
    assert len(result.sections) == 1
    assert "stronger fresh data" not in result.sections[0].markdown
    assert result.sections[0].markdown.startswith("X is established by the data")
    # NO iterate telemetry phase emitted.
    phases = [p for k, p in captured if k == "phase"]
    assert not any(p.get("phase") == "iterate" for p in phases)


# ---- embedder-path fakes (close the embedder=None blind spot) ---------------


class _SeqSearch:
    """Search that returns a DIFFERENT url per distinct query, so the initial
    leg and the refine leg gather DIFFERENT passages (deterministic, predictable
    ids via _SeqExtraction)."""

    name = "seq_search"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._query_idx: dict[str, int] = {}

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
        if query not in self._query_idx:
            self._query_idx[query] = len(self._query_idx)
        qi = self._query_idx[query]
        return [
            SearchHit(
                url=f"https://example.com/q{qi}",
                title=f"source for q{qi}",
                snippet=f"snippet q{qi}",
                source_engine="fake",
                rank=0,
            )
        ]


class _SeqExtraction:
    """Extraction that yields ONE passage per url with a stable id derived from
    the url's `q{n}` marker (so the initial leg → `pq0`, the refine leg → `pq1`).
    Each passage's text is UNIQUE + recognizable so a test can assert which
    passages reached a synthesis/judge prompt."""

    name = "seq_extract"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def extract(self, url: str) -> ExtractedDoc:
        return (await self.extract_many([url]))[0]

    async def extract_many(self, urls: list[str]) -> list[ExtractedDoc]:
        self.calls.extend(urls)
        out: list[ExtractedDoc] = []
        for url in urls:
            marker = url.rsplit("/", 1)[-1]  # "q0" / "q1"
            pid = f"p{marker}"
            text = f"UNIQUE-EVIDENCE-{marker}: detailed finding from {marker} in 2024."
            out.append(
                ExtractedDoc(
                    url=url,
                    title=f"Title {marker}",
                    content=text,
                    passages=[
                        Passage(
                            id=pid,
                            source_url=url,
                            source_title=f"Title {marker}",
                            text=text,
                        )
                    ],
                    fetched_ok=True,
                    status="ok",
                )
            )
        return out


def _build_seq_engine() -> tuple[_SeqSearch, DefaultRetrievalEngine]:
    search = _SeqSearch()
    return search, DefaultRetrievalEngine(
        search=search,
        extraction=_SeqExtraction(),
        reranker=_FakeReranker(),
        embedder=_FakeEmbedder(),
    )


async def test_refine_combined_corpus_includes_originals_under_embedder() -> None:
    """Defect-1: with a REAL (fake) embedder present, the refine leg's
    re-synthesis must see the COMBINED corpus — the section's ORIGINAL passages
    (`pq0`) AND the fresh refine passages (`pq1`).

    `_retrieve_for_section` queries the refine namespace's vector store when an
    embedder is present, so unless the originals are upserted into that namespace
    they would be DROPPED (the embedder=None test could not catch this). We prove
    the fix by asserting the ORIGINAL passage's unique text appears in the refine
    leg's SYNTHESIS prompt — i.e. it was actually retrieved for re-synthesis."""
    search, engine = _build_seq_engine()
    router = _JudgeScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 6,
            # [0] initial body cites the original pq0; [1] refine body cites the
            # fresh pq1; [2] coherence summary.
            "rag_answerer": [
                "X is established by the original data [[pq0]].",
                "X is confirmed by fresh corroborating data [[pq1]].",
                "This report surveys X.",
            ],
            "judge": ["UNSUPPORTED", "SUPPORTED"],
        }
    )
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=_FakeEmbedder(),  # the blind-spot closer: embedder PRESENT
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_embed_corpus",
        iterative=True,
    )
    _captured, emit = _collect_events()
    result = await run.run(["What is X?"], emit=emit)

    # A refine leg fired (a second, distinct search query).
    assert len(search.calls) > 1
    # Identify the refine leg's SYNTHESIS prompt: a rag_answerer (non-judge) call
    # whose instruction carries the fresh passage marker pq1. It MUST also carry
    # the ORIGINAL passage's unique text — proving the combined corpus.
    synth_prompts = [
        msg
        for role, msg in router.calls
        if role == "rag_answerer" and "UNIQUE-EVIDENCE-q1" in msg
    ]
    assert synth_prompts, "expected a refine synthesis prompt that saw the fresh pq1"
    refine_prompt = synth_prompts[0]
    assert "UNIQUE-EVIDENCE-q0" in refine_prompt, (
        "the ORIGINAL passage (pq0) must be in the refine leg's combined corpus "
        "under an embedder — it was dropped (Defect-1 not fixed)"
    )
    # And the loop still converged + produced a valid report.
    assert len(result.sections) == 1


async def test_fresh_refine_evidence_is_judged_and_in_report_under_embedder() -> None:
    """Defect-2: fresh refine evidence must NOT be discarded. After a refine that
    cites a FRESH passage (`pq1`):
      (i) the RE-JUDGE round must SEE that fresh-cited claim (extract_section_claims
          skips claims whose ids have no text — so a fresh citation would vanish and
          the section would score as vacuously 'converged' if the fresh passage's
          text were not merged into the judge's passage map), and
      (ii) the fresh passage must be RESOLVABLE in the final report (so the new
           `[[pq1]]` citation renders as a source card)."""
    search, engine = _build_seq_engine()
    router = _JudgeScriptedRouter(
        {
            "query_rewriter": ["SUFFICIENT\nnone"] * 6,
            "rag_answerer": [
                "X is established by the original data [[pq0]].",
                "X is confirmed by fresh corroborating data [[pq1]].",
                "This report surveys X.",
            ],
            # round-0 judge (pq0 claim) → UNSUPPORTED → refine; the post-refine
            # judging of the pq1 claim → SUPPORTED → converge.
            "judge": ["UNSUPPORTED", "SUPPORTED"],
        }
    )
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine,
        embedder=_FakeEmbedder(),
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id="conv_embed_fresh",
        iterative=True,
    )
    _captured, emit = _collect_events()
    result = await run.run(["What is X?"], emit=emit)

    # (i) A JUDGE prompt judged the FRESH-cited claim: the judge prompt must carry
    # both the fresh claim text AND the fresh passage's unique evidence (pq1). If
    # the fresh passage text were not merged back, extract_section_claims would
    # have dropped the claim and no such judge call would exist.
    judge_prompts = [msg for role, msg in router.calls if role == "judge"]
    fresh_judged = [
        m
        for m in judge_prompts
        if "fresh corroborating data" in m and "UNIQUE-EVIDENCE-q1" in m
    ]
    assert fresh_judged, (
        "the fresh-cited claim was never judged — fresh evidence was discarded "
        "(Defect-2 not fixed)"
    )

    # The refined body (citing pq1) replaced the section.
    assert "fresh corroborating data" in result.sections[0].markdown

    # (ii) The fresh passage pq1 is resolvable in the final report's passage set.
    report_ids = {p.id for p in result.cited_passages}
    assert "pq1" in report_ids, (
        "fresh refine passage pq1 missing from the report — its citation could "
        "not resolve to a source card (Defect-2 not fixed)"
    )

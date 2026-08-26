"""Deep Research v2 engine — hermetic end-to-end test.

Drives `DeepResearchRun` across the whole v2 arc with fake providers (search,
extract, rerank, NLI) and ONE scripted router dispatched by prompt shape: the
agentic research loop's turns (brief + searches → readiness), then the whole-report
writer's single pass and its fixed-rubric review.

Asserts the load-bearing run properties:

  1. A full run produces an executive summary + '## ' sections + a claim
     ledger, and `.to_event()` returns a valid ReportEvent whose cited and
     reviewed passage sets are disjoint and whose `completed_probes` are the
     queries the agent actually issued (never the legacy plan steps).
  2. Stop is REAL: a cancelled run returns a CHECKPOINT — no prose, the
     gathered pool preserved, the trail's queries as `completed_probes`.
  3. Resume seeds the pool from that checkpoint and writes the report.
  4. The tier bounds terminate the run honestly: source-budget exhaustion →
     `bounded_by="sources"`, research wall clock → `bounded_by="wall_clock"`,
     and each tier reaches retrieval with its own strategy and limits.
  5. `decompose_query` (the legacy plan surface the agent-server still calls)
     still works.

The progress callback's events are captured so the agent-server's
event-emission shape can be validated.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import (
    DeepResearchRun,
    DepthTier,
    bounds_for,
    decompose_query,
)
from disco.retrieval.deep_research import agent as agent_mod
from disco.retrieval.deep_research.agent import run_research_agent
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
    assert which queries actually reached the web."""

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
        del domains_allow, domains_deny, time_filter
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
    passage ids (p0, p1, p2, ...) so the writer's citations resolve."""

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
                f"Body for {url} — measured evidence relevant to the research "
                "question, with reported figures and collected data from 2024."
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
        del query
        return sorted(passages, key=lambda p: p.id)[:top_k]


class _FakeNLI:
    """Entails when premise and claim share a word; otherwise neutral."""

    def entail(self, premise: str, hypothesis: str) -> str:
        if not premise or not hypothesis:
            return "neutral"
        return (
            "entail"
            if set(premise.lower().split()) & set(hypothesis.lower().split())
            else "neutral"
        )

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


_TURN0 = (
    '{"brief": "The question asks about X. I will map the players, the '
    'numbers, and independent criticism.", "decision_summary": "Map the '
    'question before checking load-bearing claims.", "coverage": '
    '{"covered": [], "open": ["players", "numbers", "criticism"], '
    '"contradictions_checked": []}, "queries": ["what is X", '
    '"X criticism"], "ready_to_write": false}'
)
_TURN_RESUME = (
    '{"brief": "Resuming: the carried pool covers the basics, so I chase the '
    'one open thread.", "decision_summary": "Close the remaining thread.", '
    '"coverage": {"covered": [], "open": ["follow-up detail"], '
    '"contradictions_checked": []}, "queries": ["X follow-up detail"], '
    '"ready_to_write": false}'
)
_DONE = (
    '{"brief": "The evidence is sufficient.", "decision_summary": '
    '"The gathered evidence covers the requested angles.", "coverage": '
    '{"covered": [{"angle": "overview", "evidence_ids": ["p0"]}], '
    '"open": [], "contradictions_checked": ["X"]}, '
    '"queries": [], "ready_to_write": true}'
)
_CLEAN_REVIEW = '{"passes": true, "failures": []}'

_EVIDENCE_ID = re.compile(r"(?m)^\[([\w-]+)\] ")


def _auto_report(prompt: str) -> str:
    """A whole-report draft built from the ids in the writer's OWN evidence
    block: a ≥30-word summary and two ≥40-word sections whose every sentence
    is cited and entailed by the overlap NLI, so the fixed rubric's
    deterministic bar passes without hand-scripting prose per test."""
    ids = list(dict.fromkeys(_EVIDENCE_ID.findall(prompt)))[:2]
    if not ids:
        return ""
    first, second = ids[0], ids[-1]
    summary = " ".join(
        [
            "The measured evidence answers the research question with collected "
            f"data and reported figures [[{first}]].",
            "Independent evidence adds context about the scope and limitations of "
            f"the available data [[{second}]].",
            "Taken together, the evidence supports a qualified answer while leaving "
            f"important measurement limits visible [[{first}]] [[{second}]].",
        ]
    )
    findings = " ".join(
        [
            "The collected data provide a consistent baseline for evaluating the "
            f"research question [[{first}]].",
            "Reported figures from another source permit a direct comparison with "
            f"that baseline [[{second}]].",
            "The evidence converges on the central finding across the available "
            f"measurements [[{first}]] [[{second}]].",
            "This agreement is strongest where both sources document observed data "
            f"rather than projections [[{first}]] [[{second}]].",
        ]
    )
    limits = " ".join(
        [
            "The evidence also leaves limits because the reported data cover only "
            f"the documented settings [[{second}]].",
            "Those boundaries constrain how far the measured finding can be "
            f"generalized beyond the collected sample [[{first}]].",
            "A stronger conclusion would require additional data collected under "
            f"different conditions [[{second}]].",
            "The best supported judgment therefore preserves the measured finding "
            f"and its remaining uncertainty [[{first}]] [[{second}]].",
        ]
    )
    return (
        f"{summary}\n\n"
        f"## Convergent findings\n{findings}\n\n"
        f"## Limits of the data\n{limits}"
    )


class _ScriptedRouter(LLMRouter):
    """v2 whole-run double: dispatch by PROMPT SHAPE, one queue per stage.

      - research turns (the lead-researcher system prompt) replay `turns`,
        defaulting to a readiness decision once the script is exhausted;
      - the whole-report call gets an auto-generated report citing the real
        pool ids from its own evidence block (genuine grounding, not markup);
      - the self-review returns a clean pass unless `reviews` is scripted;
      - QUERY_REWRITER calls (`decompose_query`) replay `rewrites`.
    """

    def __init__(
        self,
        *,
        turns: list[str] | None = None,
        reviews: list[str] | None = None,
        rewrites: list[str] | None = None,
    ) -> None:
        self._turns = list(turns or [])
        self._reviews = list(reviews or [])
        self._rewrites = list(rewrites or [])
        self.calls: list[tuple[str, str]] = []  # (kind, full prompt text)

    @staticmethod
    def _kind(request: CompletionRequest) -> str:
        role = getattr(request.profile.role, "value", str(request.profile.role))
        if role == "query_rewriter":
            return "rewrite"
        joined = "\n".join(message.content for message in request.messages)
        if "Fix EXACTLY these deficiencies" in request.messages[-1].content:
            return "rework"
        if "FIXED rubric" in joined:
            return "review"
        if "WRITE THE REPORT" in joined:
            return "report"
        return "turn"

    def _text(self, kind: str, joined: str) -> str:
        if kind == "turn":
            return self._turns.pop(0) if self._turns else _DONE
        if kind == "rewrite":
            return self._rewrites.pop(0) if self._rewrites else ""
        if kind == "review":
            return self._reviews.pop(0) if self._reviews else _CLEAN_REVIEW
        if kind in ("report", "rework"):
            return _auto_report(joined)
        return "(no scripted response)"

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        kind = self._kind(request)
        joined = "\n".join(message.content for message in request.messages)
        self.calls.append((kind, joined))
        return CompletionResponse(
            text=self._text(kind, joined),
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


def _collect_events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    """Build a capture list + an async emit callback the engine can await."""
    captured: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        captured.append((kind, payload))

    return captured, emit


def _engine() -> tuple[_FakeSearch, DefaultRetrievalEngine]:
    search = _FakeSearch()
    return search, DefaultRetrievalEngine(
        search=search, extraction=_FakeExtraction(), reranker=_FakeReranker()
    )


def _make_run(
    router: LLMRouter,
    *,
    conversation_id: str = "conv_e2e",
    engine: DefaultRetrievalEngine | None = None,
    **bound_overrides: Any,
) -> DeepResearchRun:
    """A standard-tier run over fake providers, with the report word range
    shrunk to the fakes' scale so the writer's length bar is meetable."""
    run = DeepResearchRun(
        query="the state of X",
        router=router,
        retrieval_engine=engine or _engine()[1],
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_FakeNLI(),
        depth=DepthTier.STANDARD_DEEP,
        conversation_id=conversation_id,
    )
    run._bound = replace(
        run._bound,
        report_min_words=40,
        report_max_words=4_000,
        minimum_research_turns=1,
        minimum_useful_sources=4,
        min_evidence_sources=4,
        min_evidence_themes=1,
        **bound_overrides,
    )
    return run


# ---- the decompose step -----------------------------------------------------


async def test_decompose_returns_sub_questions() -> None:
    router = _ScriptedRouter(
        rewrites=["What is X?\nHow does X work today?\nWhere is X going?"]
    )
    subqs = await decompose_query(router, "the state of X", max_subq=6)
    assert [s.title for s in subqs] == [
        "What is X?",
        "How does X work today?",
        "Where is X going?",
    ]


async def test_decompose_falls_back_when_empty() -> None:
    router = _ScriptedRouter(rewrites=[""])
    subqs = await decompose_query(router, "the state of X", max_subq=6)
    # never returns an empty list — falls back to the original query as 1 step
    assert len(subqs) == 1
    assert subqs[0].title == "the state of X"


# ---- the orchestrator: end-to-end ------------------------------------------


async def test_full_run_produces_multi_section_report() -> None:
    """End-to-end: the agent researches (brief → searches → readiness), the writer
    writes the whole report in one pass, the review passes it, and the
    assembled ReportFromRun converts to a valid ReportEvent. The headline
    contract."""
    router = _ScriptedRouter(turns=[_TURN0, _DONE])
    run = _make_run(router)
    captured, emit = _collect_events()
    # Legacy plan steps are accepted and IGNORED (the v2 agent picks its own
    # searches) — they must never become report structure or probe state.
    legacy_plan = ["What is X?", "How does X work today?"]
    result = await run.run(legacy_plan, emit=emit)

    # the headline: an executive summary + '## ' sections written in one pass
    assert result.summary.strip() and "[[" in result.summary
    assert [s.title for s in result.sections] == ["Convergent findings", "Limits of the data"]
    assert not ({s.title for s in result.sections} & set(legacy_plan))
    for section in result.sections:
        assert section.markdown and "[[" in section.markdown
        assert section.cited_passage_ids, f"section {section.id} cited nothing resolvable"
        assert section.confidence in ("high", "mixed", "low")
    # nothing hit a cap, and the review loop left no residual rubric misses
    assert result.bounded_by is None
    assert result.review_notes == []
    assert result.claims

    # One writer pass + one review — the rubric loop reworks only on failure.
    assert [kind for kind, _ in router.calls] == ["turn", "turn", "report", "review"]

    # the assembled report event is constructible + has the expected shape
    ev = result.to_event()
    assert ev.kind.value == "report"
    assert ev.query == "the state of X"
    assert len(ev.sections) == 2
    assert ev.passages  # the cited subset is populated
    assert ev.reviewed_passages == [p.model_dump() for p in result.reviewed_passages]
    assert ev.claims
    assert {p["id"] for p in ev.passages}.isdisjoint({p["id"] for p in ev.reviewed_passages})
    # completed_probes are the queries the AGENT issued, not the legacy steps
    assert ev.completed_probes == ["what is X", "X criticism"]

    # progress callback fired the right shape (phases + searches + synthesize)
    phases = [p for k, p in captured if k == "phase"]
    assert any(p.get("phase") == "gather" and p.get("mode") == "agent" for p in phases)
    assert any(p.get("phase") == "writing" for p in phases)
    assert any(p.get("phase") == "reviewing" for p in phases)
    assert any(p.get("phase") == "synthesize" for p in phases)
    assert [p["title"] for _, p in [(k, p) for k, p in captured if k == "section_done"]] == [
        "Convergent findings",
        "Limits of the data",
    ]


async def test_should_cancel_halts_at_checkpoint_with_partial_report() -> None:
    """Stop is REAL: `should_cancel` is polled at every turn boundary; when it
    trips, the run halts there and returns a CHECKPOINT — the gathered evidence
    and the queries already issued, no report prose — with
    bounded_by='stopped'. (Regression for "Stop is useless".)"""
    router = _ScriptedRouter(turns=[_TURN0, _DONE])
    run = _make_run(router, conversation_id="conv_stop")
    _, emit = _collect_events()

    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # turn 0 proceeds; the next boundary halts

    result = await run.run(emit=emit, should_cancel=should_cancel)

    assert result.bounded_by == "stopped"  # halted by Stop, not by a cap
    # A Stop checkpoint carries evidence + probe state and NO report prose:
    # the report is written once, on resume, never manufactured here.
    assert result.sections == []
    assert result.summary == ""
    assert result.reviewed_passages  # the gathered evidence is preserved
    assert result.completed_probes == ["what is X", "X criticism"]
    checkpoint = result.to_event()
    assert checkpoint.kind.value == "research_checkpoint"
    assert checkpoint.completed_queries == result.completed_probes
    # The writer was never called on a checkpoint.
    assert [kind for kind, _ in router.calls] == ["turn"]


async def test_resume_skips_completed_sections_and_finishes_the_rest() -> None:
    """Checkpointed resume: the stopped run's evidence and issued queries seed
    the second run, which researches on and writes the whole report from the
    COMBINED pool. (Regression for 'Deep Research resume redoes everything from
    scratch'.)"""
    # ---- run 1: stop at the boundary after the first turn ----
    router1 = _ScriptedRouter(turns=[_TURN0, _DONE])
    run1 = _make_run(router1, conversation_id="conv_resume")
    _, emit1 = _collect_events()
    n = {"c": 0}

    def cancel1() -> bool:
        n["c"] += 1
        return n["c"] > 1

    partial = await run1.run(emit=emit1, should_cancel=cancel1)
    assert partial.bounded_by == "stopped"
    assert partial.sections == []
    carried_ids = {p.id for p in partial.reviewed_passages}
    assert carried_ids

    # ---- run 2: resume — carry the evidence, finish the report ----
    search2, engine2 = _engine()
    router2 = _ScriptedRouter(turns=[_TURN_RESUME, _DONE])
    run2 = _make_run(router2, conversation_id="conv_resume", engine=engine2)
    _, emit2 = _collect_events()
    final = await run2.run(
        emit=emit2,
        resume_sections=partial.sections,
        resume_passages=[*partial.cited_passages, *partial.reviewed_passages],
        resume_all_hits=partial.all_hits,
        resume_completed_probes=partial.completed_probes,
        resume_pending_probes=partial.pending_probes,
    )

    assert final.sections and final.summary.strip()
    assert final.bounded_by is None  # it finished
    # The carried evidence seeded the pool instead of being re-gathered: the
    # resumed run issued only its ONE new follow-up query.
    assert len(search2.calls) == 1
    ledger_ids = {p.id for p in [*final.cited_passages, *final.reviewed_passages]}
    assert carried_ids <= ledger_ids
    # …and the prior run's queries stay on the cumulative probe record.
    assert set(partial.completed_probes) <= set(final.completed_probes)


async def test_no_should_cancel_runs_to_completion() -> None:
    """Without a cancel hook the run is uninterruptible (baseline) — it runs to
    a full report."""
    router = _ScriptedRouter(turns=[_TURN0, _DONE])
    run = _make_run(router, conversation_id="conv_nostop")
    _, emit = _collect_events()
    result = await run.run(emit=emit)  # no should_cancel
    assert result.bounded_by is None
    assert len(result.sections) == 2


# ---- tier bounds: the run always terminates, and says what stopped it -------


async def test_source_cap_hit_produces_bounded_by_sources() -> None:
    """Source-budget exhaustion ends research honestly: `bounded_by='sources'`,
    at most the tier's cap admitted, and the report is still written from what
    was gathered."""
    router = _ScriptedRouter(turns=[_TURN0, _DONE])
    run = _make_run(router, conversation_id="conv_cap", max_sources=2)
    _, emit = _collect_events()
    result = await run.run(emit=emit)

    assert result.bounded_by == "sources"
    pool = [*result.cited_passages, *result.reviewed_passages]
    assert len(pool) == 2  # never over the cap
    assert result.sections and result.summary.strip()  # a complete report anyway
    assert result.to_event().bounded_by == "sources"


async def test_research_wall_clock_produces_bounded_by_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The research wall clock bounds RESEARCH only: the loop stops at the
    deadline mid-investigation and the report is then written normally from the
    evidence already gathered, with `bounded_by='wall_clock'`."""
    # `started` and turn 0's check read 0; every later read is past the cap.
    clock = iter([0.0, 0.0])
    monkeypatch.setattr(agent_mod, "_monotonic", lambda: next(clock, 10_000.0))
    router = _ScriptedRouter(turns=[_TURN0, _DONE])
    run = _make_run(router, conversation_id="conv_clock")
    _, emit = _collect_events()
    result = await run.run(emit=emit)

    assert result.bounded_by == "wall_clock"
    assert result.sections and result.summary.strip()
    # Turn 0's evidence survived the deadline and reached the report.
    assert [*result.cited_passages, *result.reviewed_passages]


# ---- depth-tier bound smoke ----


def test_bounds_for_three_tiers_are_distinct_and_ordered() -> None:
    quick = bounds_for(DepthTier.QUICK)
    std = bounds_for(DepthTier.STANDARD_DEEP)
    deep = bounds_for(DepthTier.EXHAUSTIVE)
    assert quick.max_sources < std.max_sources < deep.max_sources
    assert quick.max_subquestions < std.max_subquestions < deep.max_subquestions
    assert quick.max_wall_clock_s < std.max_wall_clock_s < deep.max_wall_clock_s
    assert [quick.retrieval_depth, std.retrieval_depth, deep.retrieval_depth] == [
        "shallow",
        "standard",
        "deep",
    ]


def test_bounds_for_accepts_string_value() -> None:
    a = bounds_for("standard_deep")
    b = bounds_for(DepthTier.STANDARD_DEEP)
    assert a == b


def test_report_targets_are_part_of_the_depth_bound() -> None:
    """The writing envelope is a tier property, separate from source admission
    — the writer reads it from the bound, never from retrieval's leftovers."""
    assert bounds_for("quick").report_spec == {
        "min_words": 1500,
        "max_words": 2500,
        "reserved_writing_tokens": 4000,
    }
    assert bounds_for("standard_deep").report_spec == {
        "min_words": 4000,
        "max_words": 7000,
        "reserved_writing_tokens": 11200,
    }
    assert bounds_for("exhaustive").report_spec == {
        "min_words": 8000,
        "max_words": 12000,
        "reserved_writing_tokens": 19200,
    }


@pytest.mark.parametrize(
    ("tier", "expected_depth"),
    [
        (DepthTier.QUICK, "shallow"),
        (DepthTier.STANDARD_DEEP, "standard"),
        (DepthTier.EXHAUSTIVE, "deep"),
    ],
)
async def test_each_tier_reaches_retrieval_with_its_real_strategy_and_limits(
    tier: DepthTier, expected_depth: str
) -> None:
    requests: list[RetrievalRequest] = []

    class _CaptureEngine:
        async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
            requests.append(req)
            passage_id = f"p{len(requests) - 1}"
            passage = Passage(
                id=passage_id,
                source_url=f"https://example.test/{passage_id}",
                source_title=passage_id,
                text="Substantive measured evidence documents the research question.",
            )
            return RetrievalResult(
                passages=[passage],
                all_hits=[
                    SearchHit(
                        url=passage.source_url,
                        title=passage.source_title,
                        snippet="evidence",
                        rank=0,
                    )
                ],
                extracted=[],
                issued_queries=[req.query],
            )

    _, emit = _collect_events()
    bound = replace(
        bounds_for(tier),
        minimum_research_turns=1,
        minimum_useful_sources=1,
        min_evidence_sources=1,
        min_evidence_themes=0,
    )
    await run_research_agent(
        "the state of X",
        router=_ScriptedRouter(turns=[_TURN0, _DONE]),
        retrieval_engine=_CaptureEngine(),
        bound=bound,
        namespace="conv_tier",
        emit=emit,
    )
    request = requests[0]
    assert request.depth == expected_depth
    assert request.discover_limit == bound.discover_limit
    assert request.extract_cap == bound.extract_cap

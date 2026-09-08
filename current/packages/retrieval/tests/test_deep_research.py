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
     `bounded_by="sources"`, research-turn exhaustion → `bounded_by="turns"`,
     and each tier reaches retrieval with its own strategy and limits.
  5. `decompose_query` (the legacy plan surface the agent-server still calls)
     still works.

The progress callback's events are captured so the agent-server's
event-emission shape can be validated.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import quote

import pytest
from _writer_doubles import assessed_review
from disco.core import ReportSection
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
    ReportFromRun,
    bounds_for,
    decompose_query,
)
from disco.retrieval.deep_research._search_turn import _retrieve_one
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
                url=f"https://example.com/{quote(query, safe='')}/{i}",
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

    def __init__(self, prior: Sequence[Passage] = ()) -> None:
        self.calls: list[str] = []
        # A restarted fake must not reuse a carried source ID for a new URL.
        self._by_url = {p.source_url: int(p.id[1:]) for p in prior}
        self._counter = max(self._by_url.values(), default=-1) + 1

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
    '"queries": ["https://example.com/X%20criticism/0"], "ready_to_write": true}'
)

_READ_TURN = (
    '{"brief": "The selected search leads are now being read.", '
    '"decision_summary": "Read the returned primary leads before finishing.", '
    '"coverage": {"covered": [], "open": ["measurements"], '
    '"contradictions_checked": []}, "queries": '
    '["https://example.com/what%20is%20X/0", '
    '"https://example.com/what%20is%20X/1", '
    '"https://example.com/what%20is%20X/2"], "ready_to_write": false}'
)
_RESUME_READ = (
    '{"brief": "The follow-up lead is now read.", '
    '"decision_summary": "Read the returned follow-up lead.", '
    '"coverage": {"covered": [], "open": [], '
    '"contradictions_checked": ["X"]}, "queries": '
    '["https://example.com/X%20follow-up%20detail/0"], '
    '"ready_to_write": false}'
)

_SEARCH_PROBES = ["what is X", "X criticism"]
_READ_PROBES = [
    "https://example.com/what%20is%20X/0",
    "https://example.com/what%20is%20X/1",
    "https://example.com/what%20is%20X/2",
    "https://example.com/X%20criticism/0",
]
_PROBES_AFTER_READ = [*_SEARCH_PROBES, *_READ_PROBES]
_PROBES_AFTER_SELECTION = [*_SEARCH_PROBES, *_READ_PROBES[:3]]
_CLEAN_REVIEW = assessed_review('{"passes": true, "failures": []}')

_EVIDENCE_ID = re.compile(r"(?m)^\[([\w-]+)\] ")


def _auto_report(prompt: str) -> str:
    """A whole-report draft built from the ids in the writer's OWN evidence
    block: a ≥30-word summary and two sections over the substantive-body floor
    (120 prose words each), every sentence distinct, cited, and entailed by the
    overlap NLI, so the fixed rubric's deterministic bar passes without
    hand-scripting prose per test."""
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
            "Where the two records diverge, the difference lies in how each one "
            f"defines its measured population [[{first}]].",
            "That definitional gap explains most of the spread between the two "
            f"reported figures rather than any disagreement [[{second}]].",
            "Reading the observed values side by side leaves the direction of the "
            f"finding unchanged across both records [[{first}]] [[{second}]].",
            "The comparison therefore supports the central answer while marking "
            f"exactly where the two accounts part company [[{second}]].",
            "Read against the question, the converging measurements answer it "
            f"directly rather than by analogy [[{first}]].",
            "Nothing in the second record contradicts that reading of the "
            f"collected measurements [[{second}]].",
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
            "Neither record measures the longer horizon over which the effect "
            f"would have to hold [[{first}]].",
            "The available observations are also concentrated in a narrow window "
            f"of the documented deployments [[{second}]].",
            "New measurements outside that window are what would move this "
            f"judgment in either direction [[{first}]] [[{second}]].",
            "Until they exist the honest reading keeps the finding and its stated "
            f"uncertainty together [[{second}]].",
            "The best supported judgment therefore preserves the measured finding "
            f"and its remaining uncertainty [[{first}]] [[{second}]].",
            "Stating that uncertainty plainly is more useful here than a "
            f"confident reading the collected data cannot carry [[{first}]].",
            "The two records agree on that much even where their reported figures "
            f"differ in scope [[{second}]].",
        ]
    )
    return f"{summary}\n\n## Convergent findings\n{findings}\n\n## Limits of the data\n{limits}"


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
        report_finish: list[str] | None = None,
    ) -> None:
        self._turns = list(turns or [])
        self._reviews = list(reviews or [])
        self._rewrites = list(rewrites or [])
        # finish_reason per whole-report call, in order; "stop" once exhausted.
        # A "length" here is the ONLY thing that opens a continuation round.
        self._report_finish = list(report_finish or [])
        self.calls: list[tuple[str, str]] = []  # (kind, full prompt text)

    @staticmethod
    def _kind(request: CompletionRequest) -> str:
        role = getattr(request.profile.role, "value", str(request.profile.role))
        if role == "query_rewriter":
            return "rewrite"
        joined = "\n".join(message.content for message in request.messages)
        if "Your draft has been reviewed" in request.messages[-1].content:
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
        finish = "stop"
        if kind == "report" and self._report_finish:
            finish = self._report_finish.pop(0)
        return CompletionResponse(
            text=self._text(kind, joined),
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason=finish,
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


def _engine(prior: Sequence[Passage] = ()) -> tuple[_FakeSearch, DefaultRetrievalEngine]:
    search = _FakeSearch()
    return search, DefaultRetrievalEngine(
        search=search, extraction=_FakeExtraction(prior), reranker=_FakeReranker()
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
        minimum_research_turns=1,
        minimum_useful_sources=4,
        min_evidence_sources=4,
        min_evidence_themes=1,
        **bound_overrides,
    )
    return run


# ---- the decompose step -----------------------------------------------------


async def test_decompose_returns_sub_questions() -> None:
    router = _ScriptedRouter(rewrites=["What is X?\nHow does X work today?\nWhere is X going?"])
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
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
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
    # The scripted verdict includes a validated evidence assessment.
    assert result.bounded_by is None
    assert not result.review_notes
    assert result.review_outcome == "verdict"
    assert result.claims

    # One writer pass + one review — the rubric loop reworks only on failure.
    assert [kind for kind, _ in router.calls] == ["turn", "turn", "turn", "report", "review"]

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
    assert ev.completed_probes == _PROBES_AFTER_READ

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
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run = _make_run(router, conversation_id="conv_stop")
    _, emit = _collect_events()

    should_cancel = _cancel_after(router, "turn", 2)

    result = await run.run(emit=emit, should_cancel=should_cancel)

    assert result.bounded_by == "stopped"  # halted by Stop, not by a cap
    # A Stop checkpoint carries evidence + probe state and NO report prose:
    # the report is written once, on resume, never manufactured here.
    assert result.sections == []
    assert result.summary == ""
    assert result.reviewed_passages  # the gathered evidence is preserved
    assert result.completed_probes == _PROBES_AFTER_SELECTION
    checkpoint = result.to_event()
    assert checkpoint.kind.value == "research_checkpoint"
    assert checkpoint.completed_queries == result.completed_probes
    # The writer was never called on a checkpoint.
    assert [kind for kind, _ in router.calls] == ["turn", "turn"]


# ---- Stop in the WRITER (L29) ----------------------------------------------
#
# The research loop has polled the flag since v2; the writer never saw it, so a
# Stop pressed during draft → review → rework did nothing until the report
# landed — ten to twenty-five minutes at `exhaustive`. Each test below trips the
# flag at exactly one writer boundary and asserts the same three things: the run
# answers with the CHECKPOINT (pool + trail intact, no prose), the writer's
# work in flight is discarded, and no further model call is made.

_FAILING_REVIEW = assessed_review(
    '{"passes": false, "failures": [{"rubric": "R3", "section": "Convergent '
    'findings", "where": "The evidence converges on the central finding", '
    '"fix": "Attribute the load-bearing claim to a cited source."}]}'
)


def _cancel_after(router: _ScriptedRouter, kind: str, count: int = 1):
    """Trip Stop once the router has answered `count` calls of one stage.

    Reading the router's own call log is what makes the boundary exact: the
    flag becomes true between the call that has just returned and whatever the
    writer would do next, which is precisely where a poll has to see it.
    """

    def should_cancel() -> bool:
        return sum(1 for seen, _ in router.calls if seen == kind) >= count

    return should_cancel


def _assert_stop_checkpoint(result: ReportFromRun, probes: list[str] = _PROBES_AFTER_READ) -> None:
    """A Stop answer, whichever boundary produced it: evidence and probes kept,
    no prose, and an event a resume can be rebuilt from."""
    assert result.bounded_by == "stopped"
    assert result.sections == []
    assert result.summary == ""
    assert result.reviewed_passages
    assert result.completed_probes == probes
    checkpoint = result.to_event()
    assert checkpoint.kind.value == "research_checkpoint"
    assert checkpoint.completed_queries == result.completed_probes
    assert checkpoint.trail  # the audit trail survives with the pool


async def test_stop_before_the_writer_opens_its_first_stream() -> None:
    """Pressed while the last research turn was settling: the research loop had
    already decided it was ready to write, so the flag has to be re-read once
    more before a writer stream is spent."""
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run = _make_run(router, conversation_id="conv_stop_pre_writer")
    _, emit = _collect_events()

    result = await run.run(emit=emit, should_cancel=_cancel_after(router, "turn", 3))

    _assert_stop_checkpoint(result)
    assert [kind for kind, _ in router.calls] == ["turn", "turn", "turn"]


async def test_stop_after_the_draft_call_discards_the_draft() -> None:
    """The draft is written and Stop is already pressed: it is never reviewed,
    never repaired, and never published as a shorter report."""
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run = _make_run(router, conversation_id="conv_stop_draft")
    captured, emit = _collect_events()

    result = await run.run(emit=emit, should_cancel=_cancel_after(router, "report"))

    _assert_stop_checkpoint(result)
    assert [kind for kind, _ in router.calls] == ["turn", "turn", "turn", "report"]
    # The phase the writer announced is on the log; nothing claims a review ran.
    assert any(k == "phase" and p.get("phase") == "writing" for k, p in captured)
    assert not any(k == "review" for k, _ in captured)


async def test_stop_after_a_review_verdict_discards_the_candidate() -> None:
    """A verdict has been returned and Stop is pressed before the rework it
    would have ordered."""
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE], reviews=[_FAILING_REVIEW])
    run = _make_run(router, conversation_id="conv_stop_review")
    _, emit = _collect_events()

    result = await run.run(emit=emit, should_cancel=_cancel_after(router, "review"))

    _assert_stop_checkpoint(result)
    assert [kind for kind, _ in router.calls] == ["turn", "turn", "turn", "report", "review"]


async def test_stop_after_a_rework_pass_discards_the_repaired_report() -> None:
    """A rework has just returned a repaired candidate. It is still discarded —
    a report that was in the middle of a bounded repair arc is not a report."""
    router = _ScriptedRouter(
        turns=[_TURN0, _READ_TURN, _DONE], reviews=[_FAILING_REVIEW, _FAILING_REVIEW]
    )
    run = _make_run(router, conversation_id="conv_stop_rework")
    _, emit = _collect_events()

    result = await run.run(emit=emit, should_cancel=_cancel_after(router, "rework"))

    _assert_stop_checkpoint(result)
    assert [kind for kind, _ in router.calls] == [
        "turn",
        "turn",
        "turn",
        "report",
        "review",
        "rework",
    ]


async def test_stop_inside_the_continuation_loop() -> None:
    """The longest unattended stretch the writer has: a draft the server cut
    off (`finish_reason == "length"`) is continued up to three times, each one
    a full model call. The flag is read before each of them."""
    # The draft AND its first continuation both come back cut off.
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE], report_finish=["length", "length"])
    run = _make_run(router, conversation_id="conv_stop_continuation")
    captured, emit = _collect_events()

    result = await run.run(emit=emit, should_cancel=_cancel_after(router, "report", 2))

    _assert_stop_checkpoint(result)
    # Draft + exactly one continuation; the second continuation never opens.
    assert [kind for kind, _ in router.calls] == ["turn", "turn", "turn", "report", "report"]
    assert [(k, p) for k, p in captured if k == "continuation"] == [
        ("continuation", {"k": 1, "of": 3})
    ]


async def test_no_stop_flag_leaves_the_writer_byte_identical() -> None:
    """The OFF path: a run with no cancel flag installed reaches the same
    finished report it always did."""
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run = _make_run(router, conversation_id="conv_stop_off")
    _, emit = _collect_events()

    result = await run.run(emit=emit, should_cancel=None)

    assert result.bounded_by != "stopped"
    assert result.sections and result.summary
    assert result.to_event().kind.value == "report"


async def test_resume_skips_completed_sections_and_finishes_the_rest() -> None:
    """Checkpointed resume: the stopped run's evidence and issued queries seed
    the second run, which researches on and writes the whole report from the
    COMBINED pool. (Regression for 'Deep Research resume redoes everything from
    scratch'.)"""
    # ---- run 1: stop at the boundary after the first turn ----
    router1 = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run1 = _make_run(router1, conversation_id="conv_resume")
    _, emit1 = _collect_events()
    cancel1 = _cancel_after(router1, "turn", 2)

    partial = await run1.run(emit=emit1, should_cancel=cancel1)
    assert partial.bounded_by == "stopped"
    assert partial.sections == []
    carried_ids = {p.id for p in partial.reviewed_passages}
    assert carried_ids

    # ---- run 2: resume — carry the evidence, finish the report ----
    search2, engine2 = _engine(partial.reviewed_passages)
    router2 = _ScriptedRouter(turns=[_TURN_RESUME, _RESUME_READ, _DONE])
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


async def test_resume_after_a_writer_phase_stop_finishes_the_report() -> None:
    """A writer-phase Stop produces the SAME checkpoint a research-loop Stop
    does, so resume re-enters the research loop with the pool seeded and writes
    the report. The stopping wall promises exactly this; it has to hold for the
    boundary the writer just gained, not only for the one the loop always had.
    """
    router1 = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE], reviews=[_FAILING_REVIEW])
    run1 = _make_run(router1, conversation_id="conv_resume_writer")
    _, emit1 = _collect_events()

    # Stop pressed while the first rework was in flight — deep inside writing.
    partial = await run1.run(emit=emit1, should_cancel=_cancel_after(router1, "rework"))
    assert partial.bounded_by == "stopped"
    carried_ids = {p.id for p in partial.reviewed_passages}
    assert carried_ids

    search2, engine2 = _engine(partial.reviewed_passages)
    router2 = _ScriptedRouter(turns=[_TURN_RESUME, _RESUME_READ, _DONE])
    run2 = _make_run(router2, conversation_id="conv_resume_writer", engine=engine2)
    _, emit2 = _collect_events()
    final = await run2.run(
        emit=emit2,
        resume_passages=[*partial.cited_passages, *partial.reviewed_passages],
        resume_all_hits=partial.all_hits,
        resume_completed_probes=partial.completed_probes,
        resume_research_trail=partial.research_trail,
    )

    assert final.sections and final.summary.strip()
    assert final.bounded_by is None
    assert len(search2.calls) == 1  # the carried pool was not re-gathered
    ledger_ids = {p.id for p in [*final.cited_passages, *final.reviewed_passages]}
    assert carried_ids <= ledger_ids


async def test_no_should_cancel_runs_to_completion() -> None:
    """Without a cancel hook the run is uninterruptible (baseline) — it runs to
    a full report."""
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
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
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run = _make_run(router, conversation_id="conv_cap", max_sources=2)
    _, emit = _collect_events()
    result = await run.run(emit=emit)

    assert result.bounded_by == "sources"
    pool = [*result.cited_passages, *result.reviewed_passages]
    assert len(pool) == 2  # never over the cap
    assert result.sections and result.summary.strip()  # a complete report anyway
    assert result.to_event().bounded_by == "sources"


async def test_research_wall_clock_produces_bounded_by_wall_clock() -> None:
    """The research WORK budget bounds RESEARCH only: the loop stops when its
    turns are spent, mid-investigation, and the report is then written normally
    from the evidence already gathered, with `bounded_by='turns'`.

    (Named for the wall-clock budget this replaced; runs persisted under that
    budget still carry `bounded_by='wall_clock'` and every reader accepts it.)
    """
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE])
    run = _make_run(router, conversation_id="conv_clock", max_research_turns=2)
    _, emit = _collect_events()
    result = await run.run(emit=emit)

    assert result.bounded_by == "turns"
    assert result.sections and result.summary.strip()
    # Turn 0's evidence survived the bound and reached the report.
    assert [*result.cited_passages, *result.reviewed_passages]
    assert result.to_event().bounded_by == "turns"


# ---- depth-tier bound smoke ----


def test_bounds_for_three_tiers_are_distinct_and_ordered() -> None:
    quick = bounds_for(DepthTier.QUICK)
    std = bounds_for(DepthTier.STANDARD_DEEP)
    deep = bounds_for(DepthTier.EXHAUSTIVE)
    assert quick.max_sources < std.max_sources < deep.max_sources
    assert quick.max_subquestions < std.max_subquestions < deep.max_subquestions
    assert quick.max_research_turns < std.max_research_turns < deep.max_research_turns
    assert [quick.retrieval_depth, std.retrieval_depth, deep.retrieval_depth] == [
        "shallow",
        "standard",
        "deep",
    ]


def test_verifier_failures_reach_the_report_events_metadata() -> None:
    """A run whose grounding checks partially no-op'd says so in the persisted
    event's metadata, alongside the existing review notes."""
    report = ReportFromRun(
        query="q",
        summary="A grounded summary of the subject [[p1]].",
        sections=[
            ReportSection(id="r0", title="Findings", markdown="Body [[p1]].", cited_passage_ids=[])
        ],
        cited_passages=[],
        reviewed_passages=[],
        all_hits=[],
        unsupported_count=0,
        bounded_by=None,
        depth_tier="standard_deep",
        review_notes=["Grounding verification degraded: 3 verifier call(s) failed"],
        verifier_failures=3,
    )

    meta = report.to_event().meta
    assert meta["verifier_failures"] == 3
    assert meta["review_notes"] == ["Grounding verification degraded: 3 verifier call(s) failed"]


async def test_a_review_finding_is_reworked_once_and_the_report_ships() -> None:
    """The reviewer names a part and quotes a sentence; the writer rewrites that
    part once; the report ships. The trail records the one review and the one
    rework by part name — no second review, no repair arc."""
    miss = assessed_review(
        '{"passes": false, "failures": [{"rubric": "R3", "section": "Convergent '
        'findings", "where": "The evidence converges on the central finding", '
        '"fix": "Attribute this sentence to a cited source"}]}'
    )
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE], reviews=[miss, miss])
    run = _make_run(router)
    _captured, emit = _collect_events()

    result = await run.run(emit=emit)

    assert result.summary.strip() and result.sections
    assert [kind for kind, _ in router.calls] == [
        "turn",
        "turn",
        "turn",
        "report",
        "review",
        "rework",
    ]
    rows = {row["kind"]: row for row in result.research_trail if "kind" in row}
    review = rows["report_review"]
    assert review["outcome"] == "verdict"
    assert review["attempts"] == 1
    assert review["findings"] == review["review"] == 1
    assert review["unplaced"] == 0
    rework = rows["report_rework"]
    assert rework["named"] == ["Convergent findings"]
    assert "Convergent findings" in rework["applied"]
    # Nothing the system could not verify, so the channel stays closed.
    assert result.unverified_sentences == []
    assert "unverified_sentences" not in result.to_event().meta


async def test_a_reviewer_naming_no_part_orders_no_rework() -> None:
    """A failure the system cannot place in any part is counted, not acted on:
    the model never receives a finding it cannot locate."""
    vague = assessed_review(
        '{"passes": false, "failures": [{"rubric": "R3", "section": "Unknown", '
        '"where": "somewhere", '
        '"fix": "Cite it"}]}'
    )
    router = _ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE], reviews=[vague])
    run = _make_run(router)
    _captured, emit = _collect_events()

    result = await run.run(emit=emit)

    assert result.summary.strip() and result.sections
    assert [kind for kind, _ in router.calls] == ["turn", "turn", "turn", "report", "review"]
    review = next(row for row in result.research_trail if row.get("kind") == "report_review")
    assert review["findings"] == 0
    assert review["unplaced"] == 1
    assert not any(row.get("kind") == "report_rework" for row in result.research_trail)


def test_a_clean_run_declares_no_unverified_sentences() -> None:
    report = ReportFromRun(
        query="q",
        summary="A grounded summary of the subject [[p1]].",
        sections=[
            ReportSection(id="r0", title="Findings", markdown="Body [[p1]].", cited_passage_ids=[])
        ],
        cited_passages=[],
        reviewed_passages=[],
        all_hits=[],
        unsupported_count=0,
        bounded_by=None,
        depth_tier="standard_deep",
    )

    assert report.unverified_sentences == []
    assert "unverified_sentences" not in report.to_event().meta


def test_unverified_sentences_reach_the_event_metadata() -> None:
    """What the verifier could not ground ships beside the report, verbatim."""
    report = ReportFromRun(
        query="q",
        summary="A grounded summary of the subject [[p1]].",
        sections=[
            ReportSection(id="r0", title="Findings", markdown="Body [[p1]].", cited_passage_ids=[])
        ],
        cited_passages=[],
        reviewed_passages=[],
        all_hits=[],
        unsupported_count=1,
        bounded_by=None,
        depth_tier="standard_deep",
        unverified_sentences=["Body [[p1]]."],
    )

    assert report.to_event().meta["unverified_sentences"] == ["Body [[p1]]."]


def test_a_healthy_run_carries_no_verifier_metadata() -> None:
    report = ReportFromRun(
        query="q",
        summary="A grounded summary of the subject [[p1]].",
        sections=[
            ReportSection(id="r0", title="Findings", markdown="Body [[p1]].", cited_passage_ids=[])
        ],
        cited_passages=[],
        reviewed_passages=[],
        all_hits=[],
        unsupported_count=0,
        bounded_by=None,
        depth_tier="standard_deep",
    )

    assert "verifier_failures" not in report.to_event().meta


def test_bounds_for_accepts_string_value() -> None:
    a = bounds_for("standard_deep")
    b = bounds_for(DepthTier.STANDARD_DEEP)
    assert a == b


def test_the_writing_provision_is_part_of_the_depth_bound() -> None:
    """The writer's token provision is a tier property, separate from source
    admission — the writer reads it from the bound, never from retrieval's
    leftovers. There is no word target: the evidence decides the length."""
    assert bounds_for("quick").report_spec == {"reserved_writing_tokens": 6_000}
    assert bounds_for("standard_deep").report_spec == {"reserved_writing_tokens": 14_000}
    assert bounds_for("exhaustive").report_spec == {"reserved_writing_tokens": 24_000}
    for tier in ("quick", "standard_deep", "exhaustive"):
        assert not hasattr(bounds_for(tier), "report_min_words")
        assert not hasattr(bounds_for(tier), "report_max_words")


class _RecordingSearch:
    """Records the exact query string and limit the engine asked for, and
    returns a full `limit` worth of hits so the kept-hit count is observable."""

    name = "recording_search"

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.limits: list[int] = []

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
        self.queries.append(query)
        self.limits.append(limit)
        return [
            SearchHit(
                url=f"https://example.test/hit-{i}",
                title=f"hit {i}",
                snippet="",
                source_engine="recording",
                rank=i,
            )
            for i in range(limit)
        ]


@pytest.mark.parametrize("tier", [DepthTier.QUICK, DepthTier.STANDARD_DEEP, DepthTier.EXHAUSTIVE])
async def test_deep_research_issues_the_models_query_once_at_every_tier(
    tier: DepthTier,
) -> None:
    """Regression: the host used to author the search query on the model's
    behalf — one LLM paraphrase at `standard`, four at `deep`, each keyword-
    compressed. Measured over 16 recorded runs, the model's own wording reached
    the provider in 20% of searches, one exhaustive query became four, and the
    12-term compression cap dropped the `site:` operator or the year from a
    quarter of the queries carrying one. Every tier now issues the model's query
    once, byte for byte, and keeps the tier's `discover_limit` of what returns.
    """
    query = 'site:bis.doc.gov "interim final rule" 88 FR 73424 October 2023'
    search = _RecordingSearch()
    engine = DefaultRetrievalEngine(
        search=search, extraction=_FakeExtraction(), reranker=_FakeReranker()
    )
    bound = bounds_for(tier)

    result = await _retrieve_one(
        engine,
        query,
        bound,
        slots=bound.max_sources,
        recency_window=None,
        corpus_ids=frozenset(),
    )

    assert search.queries == [query]  # once, as written — no rewrite, no fan-out
    assert search.limits == [bound.discover_limit]
    assert result.issued_queries == [query]
    assert len(result.all_hits) == bound.discover_limit
    trace = result.notes["retrieval_trace"]
    assert trace["planned_query"] == trace["issued_queries"][0] == query


def test_discover_limit_keeps_what_one_search_returns() -> None:
    """Breadth comes from keeping one search's results, not from issuing four
    near-duplicate searches. A SearXNG call returns roughly 30 rows; exhaustive
    now keeps 32 of them where it used to keep 12 per paraphrase."""
    quick, standard, exhaustive = (
        bounds_for(DepthTier.QUICK),
        bounds_for(DepthTier.STANDARD_DEEP),
        bounds_for(DepthTier.EXHAUSTIVE),
    )
    assert [quick.discover_limit, standard.discover_limit, exhaustive.discover_limit] == [
        8,
        20,
        32,
    ]
    # Extraction stays its own, much smaller budget: a wider discovery pool
    # gives the ranker more to choose from, it does not fetch more pages.
    assert [quick.extract_cap, standard.extract_cap, exhaustive.extract_cap] == [4, 6, 8]


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
        router=_ScriptedRouter(turns=[_TURN0, _READ_TURN, _DONE]),
        retrieval_engine=_CaptureEngine(),
        bound=bound,
        namespace="conv_tier",
        emit=emit,
    )
    request = requests[0]
    assert request.depth == expected_depth
    assert request.discover_limit == bound.discover_limit
    assert request.extract_cap == bound.extract_cap

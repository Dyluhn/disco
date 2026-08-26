"""Hermetic tests for the v2 whole-report writer (`deep_research.writer`).

Scripted router dispatched by prompt shape (the `test_deep_research.py`
    pattern): single-pass parse into summary + sections, the fixed-rubric rework
    loop driven by precise deficiency feedback, and fail-closed handling for
    unresolved hard defects and empty evidence.

The final section pins the WRITING CONTRACT itself — the rules text
(synthesize-don't-summarize, vendor attribution, measured register, the
platform-attribution prohibition, the citation contract, visual grounding)
and the fixed rubric. These carried over from the v1 per-section prompt when
the whole-report writer replaced it; they are what distinguishes analysis
from a sourced summary, so they are asserted as text, not inferred from
output.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from disco.core.inspect import registry
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import report_compiler
from disco.retrieval.deep_research import writer as writer_mod
from disco.retrieval.deep_research.agent import ResearchOutcome
from disco.retrieval.deep_research.depth import DepthBound
from disco.retrieval.deep_research.report_compiler import ReportCompilationError
from disco.retrieval.deep_research.writer import RESEARCH_REPORT_RUBRIC, write_report
from disco.retrieval.models import Passage

# ---- doubles ----------------------------------------------------------------


class _WriterRouter(LLMRouter):
    """Dispatches scripted responses by prompt shape: report generation
    ("WRITE THE REPORT"), self-review ("FIXED rubric"), and rework ("Fix
    EXACTLY these deficiencies")."""

    def __init__(self, scripts: dict[str, list[str | tuple[str, str]]]) -> None:
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list[tuple[str, str]] = []  # (kind, full prompt text)
        self.request_max_tokens: list[tuple[str, int | None]] = []
        self.requests: list[tuple[str, CompletionRequest]] = []

    def _classify(self, request: CompletionRequest) -> str:
        joined = "\n".join(m.content for m in request.messages)
        if "Fix EXACTLY these deficiencies" in request.messages[-1].content:
            return "rework"
        if "FIXED rubric" in joined:
            return "review"
        if "WRITE THE REPORT" in joined:
            return "report"
        return "other"

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        kind = self._classify(request)
        self.calls.append((kind, "\n".join(m.content for m in request.messages)))
        self.request_max_tokens.append((kind, request.max_tokens))
        self.requests.append((kind, request))
        queue = self._scripts.get(kind, [])
        scripted = queue.pop(0) if queue else "(no scripted response)"
        text, finish_reason = scripted if isinstance(scripted, tuple) else (scripted, "stop")
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason=finish_reason,
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


class _OverlapNLI:
    """Entails when premise and claim share a word — deterministic."""

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


def _collect_events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        captured.append((kind, payload))

    return captured, emit


_POOL = [
    Passage(
        id="p1",
        source_url="https://example.com/a",
        source_title="Alpha study",
        text=(
            "The measured evidence documents the subject with collected data "
            "and reported figures across several independent deployments."
        ),
    ),
    Passage(
        id="p2",
        source_url="https://example.org/b",
        source_title="Beta report",
        text=(
            "Independent analysis of the subject confirms the reported "
            "figures and documents measured limitations in current data."
        ),
    ),
]


def _outcome(
    *, passages: list[Passage] | None = None, coverage: dict[str, Any] | None = None
) -> ResearchOutcome:
    return ResearchOutcome(
        brief="The question asks about the subject; I mapped data and criticism.",
        passages=_POOL if passages is None else passages,
        all_hits=[],
        trail=[
            {"kind": "search", "query": "subject overview", "admitted": 2},
            {
                "kind": "search",
                "query": "subject criticism",
                "admitted": 0,
                "error": "ConnectionError",
            },
        ],
        bounded_by=None,
        coverage={} if coverage is None else coverage,
    )


def _bound() -> DepthBound:
    return DepthBound(
        max_sources=10,
        max_rounds_per_subq=2,
        max_wall_clock_s=600,
        max_subquestions=4,
        discover_limit=5,
        extract_cap=4,
        rerank_top_k=4,
        report_min_words=40,
        report_max_words=4_000,
    )


def _sentence(text: str, cite: str) -> str:
    return f"{text} [[{cite}]]."


# A draft that passes the deterministic bar: a ≥30-word summary, two ≥40-word
# sections, every sentence cited with resolving ids and entailed by overlap.
_CITED_LINE = _sentence(
    "The measured evidence documents the subject with collected data and "
    "reported figures across independent deployments",
    "p1",
)
_CITED_LINE_2 = _sentence(
    "Independent analysis confirms the reported figures and documents "
    "measured limitations in the current data",
    "p2",
)
_GOOD = (
    f"{_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE}\n\n"
    f"## Convergent findings\n{_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE} {_CITED_LINE_2}\n\n"
    f"## Limitations in the data\n{_CITED_LINE_2} {_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE}"
)

_CLEAN_REVIEW = '{"passes": true, "failures": []}'

_GHOST_LINE = _sentence("A fabricated figure appears nowhere in the evidence", "ghost9")
_FLAWED = (
    f"{_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE}\n\n"
    f"## Convergent findings\n{_CITED_LINE} {_GHOST_LINE} {_CITED_LINE_2} {_CITED_LINE}\n\n"
    f"## Limitations in the data\n{_CITED_LINE_2} {_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE}"
)

_DIAGNOSTIC_SUMMARY = (
    "The research could not determine a reliable answer about the subject "
    "because available evidence was incomplete and conflicting across independent "
    "sources, timelines, measurements, documented limitations, and the requested "
    "comparison context [[p1]]."
)
_DIAGNOSTIC = (
    f"{_DIAGNOSTIC_SUMMARY}\n\n"
    f"## Convergent findings\n{_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE} {_CITED_LINE_2}\n\n"
    f"## Limitations in the data\n{_CITED_LINE_2} {_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE}"
)
_FENCED_CHART = (
    _GOOD.split("\n\n##", 1)[0]
    + "\n\n## Convergent findings\n"
    + "```chart\n"
    + '{"chart_type":"bar","data":[{"label":"Subject","value":42}]}\n'
    + "```\n"
    + f"{_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE} {_CITED_LINE_2}\n\n"
    + "## Limitations in the data\n"
    + f"{_CITED_LINE_2} {_CITED_LINE} {_CITED_LINE_2} {_CITED_LINE}"
)


# ---- normal path ------------------------------------------------------------


async def test_single_pass_parses_summary_and_sections() -> None:
    router = _WriterRouter({"report": [_GOOD], "review": [_CLEAN_REVIEW]})
    captured, emit = _collect_events()
    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )
    # One writer call + one self-review; no rework needed.
    assert [kind for kind, _ in router.calls] == ["report", "review"]
    # Summary is the pre-heading text; both sections parsed with resolving ids.
    assert "measured evidence documents the subject" in written.summary
    assert [s.title for s in written.sections] == [
        "Convergent findings",
        "Limitations in the data",
    ]
    assert written.sections[0].cited_passage_ids == ["p1", "p2"]
    assert written.review_notes == []
    assert written.unsupported_count == 0
    assert written.claims  # ledger built for the event contract
    assert any(claim["section_id"] == "summary" for claim in written.claims)
    # Event contract: truthful writing/reviewing phases, then synthesize + section_done
    # per parsed section of the final candidate.
    phases = [p for k, p in captured if k == "phase"]
    assert {"phase": "writing"} in phases
    assert {"phase": "reviewing", "attempt": 1} in phases
    assert {"phase": "synthesize", "section": 1} in phases
    assert {"phase": "synthesize", "section": 2} in phases
    assert phases.index({"phase": "reviewing", "attempt": 1}) < phases.index(
        {"phase": "synthesize", "section": 1}
    )
    section_done = [p for k, p in captured if k == "section_done"]
    assert [p["title"] for p in section_done] == [
        "Convergent findings",
        "Limitations in the data",
    ]
    # The writer prompt carried the question, the brief, and the tier range.
    report_prompt = router.calls[0][1]
    assert "the state of the subject" in report_prompt
    assert "I mapped data and criticism" in report_prompt
    assert "40-4000 prose words, inclusive" in report_prompt


def test_report_prompt_carries_compact_coverage_map() -> None:
    outcome = _outcome(
        coverage={
            "covered": [
                {"angle": "Mechanism", "evidence_ids": ["p1", ""]},
                {"angle": "Economics", "evidence_ids": []},
                {"angle": "", "evidence_ids": ["p2"]},
            ],
            "open": [],
            "contradictions_checked": ["deployment timeline", ""],
        }
    )
    prompt = writer_mod._report_instruction(
        "the state of the subject", outcome, _bound(), None
    )
    assert "COVERAGE MAP" in prompt
    assert "- Mechanism (evidence: p1)" in prompt
    assert "- Economics" in prompt
    assert "deployment timeline" in prompt
    assert "Develop every supported covered angle" in prompt
    assert "Do not answer this as a questionnaire" in prompt
    assert "copy these angle names as headings mechanically" in prompt
    assert "p2" not in writer_mod._coverage_instruction(outcome.coverage)


def test_empty_coverage_does_not_add_a_placeholder_block() -> None:
    prompt = writer_mod._report_instruction("the state of the subject", _outcome(), _bound(), None)
    assert writer_mod._coverage_instruction({}) == ""
    assert "COVERAGE MAP" not in prompt


async def test_model_io_trace_records_writer_and_reviewer_visible_io(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    router = _WriterRouter({"report": [_GOOD], "review": [_CLEAN_REVIEW]})
    _, emit = _collect_events()

    await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
        conversation_id="writer_trace",
    )

    snapshot = registry().snapshot("writer_trace")
    assert snapshot is not None
    rows = snapshot["model_io"]
    assert [row["stage"] for row in rows] == ["report_draft", "report_review"]
    assert "WRITE THE REPORT" in rows[0]["request"]["messages"][-1]["content"]
    assert rows[0]["response"]["text"] == _GOOD
    assert rows[1]["declared_decision"] == {"passes": True, "failures": []}


async def test_rubric_rework_gets_exact_deficiencies_and_ships_fix() -> None:
    review_failure = (
        '{"passes": false, "failures": [{"rubric": "R4", "where": "Source A '
        'says", "fix": "connect the cited claims into an argument"}]}'
    )
    router = _WriterRouter(
        {
            "report": [_FLAWED],
            "review": [review_failure, _CLEAN_REVIEW],
            "rework": [_GOOD],
        }
    )
    captured, emit = _collect_events()
    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )
    del captured
    assert [kind for kind, _ in router.calls] == ["report", "review", "rework", "review"]
    rework_prompt = router.calls[2][1]
    # The rework carries the full draft + the combined numbered deficiency
    # list: the deterministic unknown-id line, the quoted unsupported
    # sentence, and the self-review's rubric failure — verbatim.
    assert _FLAWED in rework_prompt
    assert "Fix EXACTLY these deficiencies" in rework_prompt
    assert "[[ghost9]]" in rework_prompt  # nonresolving id named BY ID
    assert "A fabricated figure appears nowhere in the evidence" in rework_prompt
    assert 'R4 — at "Source A says": connect the cited claims' in rework_prompt
    assert "1." in rework_prompt and "2." in rework_prompt
    # The fixed candidate shipped clean.
    assert written.review_notes == []
    assert "ghost9" not in written.sections[0].markdown
    assert written.unsupported_count == 0


async def test_soft_synthesis_failure_is_reworked_before_it_can_ship() -> None:
    review_failure = (
        '{"passes": false, "failures": [{"rubric": "R4", "where": "Source A '
        'says", "fix": "synthesize across the cited sources"}]}'
    )
    router = _WriterRouter(
        {
            "report": [_GOOD],
            "review": [review_failure, _CLEAN_REVIEW],
            "rework": [_GOOD],
        }
    )
    _, emit = _collect_events()

    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )

    assert [kind for kind, _ in router.calls] == ["report", "review", "rework", "review"]
    assert "synthesize across the cited sources" in router.calls[2][1]
    assert written.review_notes == []


async def _assert_unresolved_unsupported_prose_fails_closed() -> None:
    # The writer never fixes the flawed draft; hard grounding defects cannot
    # become a successful report or be hidden in review metadata.
    router = _WriterRouter(
        {
            "report": [_FLAWED],
            "review": [_CLEAN_REVIEW, _CLEAN_REVIEW, _CLEAN_REVIEW],
            "rework": [_FLAWED, _FLAWED],
        }
    )
    captured, emit = _collect_events()
    with pytest.raises(ReportCompilationError, match="hard deficiencies"):
        await write_report(
            "the state of the subject",
            _outcome(),
            router=router,
            nli=_OverlapNLI(),
            bound=_bound(),
            emit=emit,
        )
    del captured
    kinds = [kind for kind, _ in router.calls]
    assert kinds == ["report", "review", "rework", "review", "rework", "review"]
    assert kinds[-1] == "review"


async def test_unresolved_unsupported_prose_fails_closed() -> None:
    await _assert_unresolved_unsupported_prose_fails_closed()


async def test_word_range_deficiency_uses_exact_numbers() -> None:
    short = f"{_CITED_LINE}\n\n## Only section\n{_CITED_LINE}"
    router = _WriterRouter(
        {
            "report": [short],
            "review": [_CLEAN_REVIEW, _CLEAN_REVIEW],
            "rework": [_GOOD],
        }
    )
    _, emit = _collect_events()
    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )
    rework_prompt = router.calls[2][1]
    assert "the assigned range is 40-4000" in rework_prompt
    assert "LENGTH IS A HARD ACCEPTANCE REQUIREMENT" in rework_prompt
    assert "40-4000 prose words, inclusive" in rework_prompt
    assert "complete report" in rework_prompt
    assert written.review_notes == []


async def test_residual_word_range_miss_is_a_review_note_after_bounded_rework() -> None:
    """A valid report survives when two repairs cannot meet the length cap."""
    bound = replace(_bound(), report_max_words=100)
    router = _WriterRouter(
        {
            "report": [_GOOD],
            "review": [_CLEAN_REVIEW, _CLEAN_REVIEW, _CLEAN_REVIEW],
            "rework": [_GOOD, _GOOD],
        }
    )
    _, emit = _collect_events()

    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=bound,
        emit=emit,
    )

    assert [kind for kind, _ in router.calls] == [
        "report",
        "review",
        "rework",
        "review",
        "rework",
        "review",
    ]
    assert any("Report is 166 words; the assigned range is 40-100" in note
               for note in written.review_notes)
    assert written.sections
    assert written.summary


async def test_shorter_under_minimum_rework_cannot_replace_better_draft() -> None:
    original = "word " * 39
    router = _WriterRouter({"rework": ["tiny fragment"]})
    kept = await writer_mod._rework_report(
        router,
        [],
        original,
        ["Report is 39 words; the assigned range is 40-4000. Expand the analysis."],
        bound=_bound(),
        max_tokens=writer_mod._writer_max_tokens(_bound()),
        conversation_id=None,
        attempt=2,
    )
    assert kept == original


def test_word_range_accepts_only_the_exact_inclusive_bounds() -> None:
    bound = _bound()
    assert writer_mod._length_deficiencies("word " * 40, bound) == []
    assert writer_mod._length_deficiencies("word " * 4_000, bound) == []

    too_short = writer_mod._length_deficiencies("word " * 39, bound)
    too_long = writer_mod._length_deficiencies("word " * 4_001, bound)
    assert len(too_short) == 1
    assert len(too_long) == 1
    assert "Report is 39 words; the assigned range is 40-4000" in too_short[0]
    assert "Report is 4001 words; the assigned range is 40-4000" in too_long[0]


async def test_summary_citation_deficiency_is_reworked_and_ledgered() -> None:
    summary, body = _GOOD.split("\n\n##", 1)
    uncited_summary = summary.replace(" [[p1]]", "").replace(" [[p2]]", "")
    uncited = uncited_summary + "\n\n##" + body
    router = _WriterRouter(
        {"report": [uncited], "review": [_CLEAN_REVIEW, _CLEAN_REVIEW], "rework": [_GOOD]}
    )
    _, emit = _collect_events()
    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )
    assert "Executive summary contains no citations" in router.calls[2][1]
    assert any(claim["section_id"] == "summary" for claim in written.claims)


async def test_diagnostic_failure_digest_summary_is_reworked() -> None:
    router = _WriterRouter(
        {"report": [_DIAGNOSTIC], "review": [_CLEAN_REVIEW, _CLEAN_REVIEW], "rework": [_GOOD]}
    )
    _, emit = _collect_events()

    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )

    assert "research/process failure digest" in router.calls[2][1]
    assert written.summary != _DIAGNOSTIC_SUMMARY
    assert "unable to determine" not in written.summary


async def test_fenced_chart_is_reworked_into_groundable_report() -> None:
    router = _WriterRouter(
        {"report": [_FENCED_CHART], "review": [_CLEAN_REVIEW, _CLEAN_REVIEW], "rework": [_GOOD]}
    )
    _, emit = _collect_events()

    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )

    assert "Fenced chart blocks are not allowed" in router.calls[2][1]
    assert all("```chart" not in section.markdown for section in written.sections)


# ---- empty evidence ---------------------------------------------------------


async def test_empty_evidence_is_not_rendered_as_a_report() -> None:
    router = _WriterRouter({})
    _, emit = _collect_events()
    with pytest.raises(ReportCompilationError, match="no usable evidence"):
        await write_report(
            "the state of the subject",
            _outcome(passages=[]),
            router=router,
            nli=_OverlapNLI(),
            bound=_bound(),
            emit=emit,
        )
    assert router.calls == []


async def test_duplicate_passage_ids_fail_before_the_writer_call() -> None:
    duplicate = _POOL[0].model_copy(update={"source_url": "https://mirror.example/a"})
    router = _WriterRouter({})
    _, emit = _collect_events()

    with pytest.raises(ReportCompilationError, match="duplicate passage ids"):
        await write_report(
            "the state of the subject",
            _outcome(passages=[_POOL[0], duplicate]),
            router=router,
            nli=_OverlapNLI(),
            bound=_bound(),
            emit=emit,
        )

    assert router.calls == []


async def test_malformed_reviewer_fails_closed_after_retry() -> None:
    router = _WriterRouter({"report": [_GOOD], "review": ["not json", "still not json"]})
    _, emit = _collect_events()
    with pytest.raises(ReportCompilationError, match="reviewer returned malformed"):
        await write_report(
            "the state of the subject",
            _outcome(),
            router=router,
            nli=_OverlapNLI(),
            bound=_bound(),
            emit=emit,
        )


async def test_length_truncated_review_gets_provider_neutral_budget_headroom() -> None:
    router = _WriterRouter(
        {
            "report": [_GOOD],
            "review": [("", "length"), _CLEAN_REVIEW],
        }
    )
    _, emit = _collect_events()

    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )

    assert written.summary
    assert [budget for kind, budget in router.request_max_tokens if kind == "review"] == [
        writer_mod._REVIEW_MAX_TOKENS,
        writer_mod._REVIEW_MAX_TOKENS * 2,
    ]
    reviews = [request for kind, request in router.requests if kind == "review"]
    assert all(request.response_format == "json" for request in reviews)
    assert all(request.enable_thinking is False for request in reviews)
    assert all(
        not any(message.role == "assistant" for message in request.messages)
        for request in reviews
    )


async def test_empty_stop_review_gets_same_provider_neutral_retry() -> None:
    router = _WriterRouter(
        {
            "report": [_GOOD],
            "review": [("", "stop"), _CLEAN_REVIEW],
        }
    )
    _, emit = _collect_events()

    written = await write_report(
        "the state of the subject",
        _outcome(),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )

    assert written.summary
    assert [budget for kind, budget in router.request_max_tokens if kind == "review"] == [
        writer_mod._REVIEW_MAX_TOKENS,
        writer_mod._REVIEW_MAX_TOKENS * 2,
    ]


async def test_reviewer_false_without_failures_cannot_pass() -> None:
    router = _WriterRouter(
        {
            "report": [_GOOD],
            "review": ['{"passes": false, "failures": []}'] * 3,
            "rework": [_GOOD, _GOOD],
        }
    )
    _, emit = _collect_events()
    with pytest.raises(ReportCompilationError, match="hard deficiencies"):
        await write_report(
            "the state of the subject",
            _outcome(),
            router=router,
            nli=_OverlapNLI(),
            bound=_bound(),
            emit=emit,
        )


async def test_headingless_candidate_cannot_be_promoted_to_a_section() -> None:
    headingless = _GOOD.split("\n\n##", 1)[0]
    router = _WriterRouter(
        {
            "report": [headingless],
            "review": [_CLEAN_REVIEW] * 3,
            "rework": [headingless, headingless],
        }
    )
    _, emit = _collect_events()
    with pytest.raises(ReportCompilationError, match="hard deficiencies"):
        await write_report(
            "the state of the subject",
            _outcome(),
            router=router,
            nli=_OverlapNLI(),
            bound=_bound(),
            emit=emit,
        )


# ---- the writing contract (prompt + rubric text) ----------------------------


def test_section_prompt_contains_platform_prohibition():
    """The rules must forbid attributing a source by its publishing PLATFORM."""
    assert "PLATFORM" in writer_mod._WRITING_RULES


def test_section_prompt_names_platform_examples():
    """The prohibition must call out concrete examples so the model understands."""
    assert any(
        name in writer_mod._WRITING_RULES for name in ("Medium", "Reddit", "YouTube", "Substack")
    )


def test_section_prompt_names_good_alternative():
    """The prompt must suggest what to write instead of the platform name."""
    assert any(
        phrase in writer_mod._WRITING_RULES
        for phrase in (
            "what the source IS",
            "independent analyst",
            "industry report",
            "community discussion",
        )
    )


def test_writing_rules_attribute_vendor_claims():
    """Self-interested claims are ATTRIBUTED, never asserted as independent
    fact; projections are marked as projections."""
    rules = writer_mod._WRITING_RULES
    assert "ATTRIBUTE vendor claims" in rules
    assert "PROJECTED" in rules


def test_writing_rules_demand_a_measured_register():
    """Hype vocabulary is named and banned — the reader is an analyst."""
    rules = writer_mod._WRITING_RULES
    assert "MEASURED, ANALYTICAL REGISTER" in rules
    assert all(word in rules for word in ("transformative", "revolutionary", "game-changing"))


def test_format_no_longer_hard_forces_four_to_seven_short_paragraphs():
    """The old rigid 'FORMAT: 4–7 short paragraphs' mould must stay gone."""
    assert "FORMAT: 4–7 short paragraphs" not in writer_mod._WRITING_RULES
    assert "FORMAT: 4–7 short paragraphs" not in writer_mod._REPORT_PROMPT


def test_format_allows_structural_variety():
    """The prompt must invite lists / tables / callouts, not prose-only — while
    still bounding the length through the tier's assigned word range."""
    lowered = writer_mod._WRITING_RULES.lower()
    assert "list" in lowered
    assert "table" in lowered
    assert "callout" in lowered
    assert "{min_words}-{max_words} prose words, inclusive" in writer_mod._REPORT_PROMPT


def test_format_explicitly_warns_against_one_shape_for_every_section():
    """Variety is the point — the prompt says 'vary', it does not force a mould."""
    assert "vary" in writer_mod._WRITING_RULES.lower()


def test_chart_guidance_broadened_beyond_hard_numbers():
    """The visual rule is offered when it AIDS COMPREHENSION and names the table
    option for comparing entities across attributes."""
    rules = writer_mod._WRITING_RULES
    assert "AIDS COMPREHENSION" in rules
    assert "TABLE" in rules
    # Opaque chart payloads bypass sentence-level grounding; tables remain visible
    # prose and are the only generated visual contract.
    assert "```chart" not in rules
    assert "Do not emit fenced chart blocks" in rules


def test_visuals_must_be_built_only_from_cited_values():
    """The visual guidance must forbid inventing data to fill a chart."""
    lowered = writer_mod._WRITING_RULES.lower()
    assert "never invent" in lowered
    assert "cited sources" in lowered


def test_citation_contract_intact_for_all_structures():
    """Every factual sentence — including list items and table cells — must end
    in [[id]]. Broadening structure never weakened the citation rule."""
    rules = writer_mod._WRITING_RULES
    assert "ends with [[id]] citations" in rules
    lowered = rules.lower()
    assert "list items" in lowered and "table cells" in lowered
    assert "must end in [[id]] citations" in writer_mod._REPORT_PROMPT


def test_platform_prohibition_and_grounding_rules_still_present():
    """The pre-existing grounding rails survived the move to one whole-report
    writer."""
    rules = writer_mod._WRITING_RULES
    assert "PLATFORM" in rules
    assert "STAY GROUNDED" in rules
    assert "SYNTHESIZE, don't summarize" in rules


def test_rubric_is_fixed_and_reaches_the_review_prompt_verbatim():
    """Decision #4: the bar is a fixed, numbered rubric, handed to the reviewer
    verbatim on every pass — no criterion may appear between passes."""
    assert all(f"R{index}." in RESEARCH_REPORT_RUBRIC for index in range(1, 9))
    prompt = writer_mod._SELF_REVIEW_PROMPT.format(
        rubric=RESEARCH_REPORT_RUBRIC,
        previously_flagged="",
        audit_context="(audit)",
        draft="(draft)",
    )
    assert RESEARCH_REPORT_RUBRIC in prompt
    assert "apply the same bar on every pass" in prompt


def test_deterministic_review_rejects_duplicate_body_analysis() -> None:
    summary, sections = writer_mod.parse_report(_GOOD)
    repeated = sections[0][1]
    draft = f"{summary}\n\n## First placement\n{repeated}\n\n## Second placement\n{repeated}"
    parsed_summary, parsed_sections = writer_mod.parse_report(draft)

    deficiencies = writer_mod._deterministic_deficiencies(
        draft, parsed_summary, parsed_sections, {"p1", "p2"}, _bound()
    )

    duplicate = next(item for item in deficiencies if "Near-duplicate body paragraphs" in item)
    assert not writer_mod._is_hard_deficiency(duplicate)


def test_quality_signals_reach_the_existing_reviewer_without_a_new_gate() -> None:
    claims = [
        writer_mod.ClaimRecord(
            claim_id="r1:0",
            text="The measured result improved by 42% in version 2.1.",
            source_ids=("p1",),
            section_id="r1",
        ),
        writer_mod.ClaimRecord(
            claim_id="r1:1",
            text="The deployment remained stable.",
            source_ids=("p1",),
            section_id="r1",
        ),
    ]

    context = writer_mod._quality_audit_context(
        claims,
        [("Findings", "This may hold.\n\nThis may remain uncertain.")],
        {p.id: p for p in _POOL},
    )

    assert "fewer than two distinct works" in context
    assert "42%" in context
    assert "supports 100% of all claims" in context
    assert "may x2" in context


def test_synthesis_temperature_is_a_small_bump():
    """Non-zero (so drafts vary) but small — grounding is enforced by the review
    loop, not by greedy decoding."""
    assert 0.0 < writer_mod._DRAFT_TEMPERATURE <= 0.5


def test_verification_helper_still_imported_unchanged():
    """The per-claim NLI verifier remains the grounding gate after generation:
    the claim ledger the report event carries is built from it."""
    assert hasattr(report_compiler, "_verify_claims")


@pytest.mark.asyncio
async def test_no_scissors_unsupported_prose_ships_with_metadata() -> None:
    """Historical ID retained while unsupported prose now fails closed."""
    await _assert_unresolved_unsupported_prose_fails_closed()


@pytest.mark.asyncio
async def test_dead_end_path_only_via_outcome_flag() -> None:
    """An empty/dead-end outcome cannot be rendered as a report artifact."""
    router = _WriterRouter({})
    _, emit = _collect_events()
    with pytest.raises(ReportCompilationError, match="no usable evidence"):
        await write_report(
            "the state of the subject",
            _outcome(passages=[]),
            router=router,
            nli=_OverlapNLI(),
            bound=_bound(),
            emit=emit,
        )
    assert router.calls == []

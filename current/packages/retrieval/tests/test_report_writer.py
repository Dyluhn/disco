"""Hermetic tests for the v2 whole-report writer (`deep_research.writer`).

Scripted router dispatched by prompt shape (the `test_deep_research.py`
pattern): single-pass parse into summary + sections, the fixed-rubric rework
loop driven by precise deficiency feedback, the no-scissors guarantee
(unsupported prose ships with metadata when the writer doesn't fix it), and
the system-gated dead-end path (reachable ONLY via `outcome.dead_end`).

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
from typing import Any

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
from disco.retrieval.deep_research.writer import RESEARCH_REPORT_RUBRIC, write_report
from disco.retrieval.models import Passage

# ---- doubles ----------------------------------------------------------------

_DEAD_END_MARKER = "evidence store is EMPTY"


class _WriterRouter(LLMRouter):
    """Dispatches scripted responses by prompt shape: report generation
    ("WRITE THE REPORT"), self-review ("FIXED rubric"), rework ("Fix EXACTLY
    these deficiencies"), and the dead-end account (its gated marker)."""

    def __init__(self, scripts: dict[str, list[str]]) -> None:
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list[tuple[str, str]] = []  # (kind, full prompt text)

    def _classify(self, request: CompletionRequest) -> str:
        joined = "\n".join(m.content for m in request.messages)
        if _DEAD_END_MARKER in joined:
            return "dead_end"
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
        queue = self._scripts.get(kind, [])
        text = queue.pop(0) if queue else "(no scripted response)"
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


def _outcome(*, dead_end: bool = False, passages: list[Passage] | None = None) -> ResearchOutcome:
    return ResearchOutcome(
        brief="The question asks about the subject; I mapped data and criticism.",
        passages=_POOL if passages is None else passages,
        all_hits=[],
        trail=[
            {"kind": "search", "query": "subject overview", "admitted": 2},
            {"kind": "search", "query": "subject criticism", "admitted": 0,
             "error": "ConnectionError"},
        ],
        bounded_by=None,
        dead_end=dead_end,
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
    # Event contract: coherence before review, then synthesize + section_done
    # per parsed section of the final candidate.
    phases = [p for k, p in captured if k == "phase"]
    assert {"phase": "coherence"} in phases
    assert {"phase": "synthesize", "section": 1} in phases
    assert {"phase": "synthesize", "section": 2} in phases
    assert phases.index({"phase": "coherence"}) < phases.index(
        {"phase": "synthesize", "section": 1}
    )
    section_done = [p for k, p in captured if k == "section_done"]
    assert [p["title"] for p in section_done] == [
        "Convergent findings",
        "Limitations in the data",
    ]
    # The writer prompt carried the question, the brief, and the tier range —
    # and never the gated dead-end prompt.
    report_prompt = router.calls[0][1]
    assert "the state of the subject" in report_prompt
    assert "I mapped data and criticism" in report_prompt
    assert "40-4000 words" in report_prompt.replace(",", "")
    assert all(_DEAD_END_MARKER not in prompt for _, prompt in router.calls)


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


async def test_no_scissors_unsupported_prose_ships_with_metadata() -> None:
    # The writer never fixes the flawed draft; after the bounded reworks the
    # fewest-deficiency candidate ships UNCUT, with the misses as metadata.
    router = _WriterRouter(
        {
            "report": [_FLAWED],
            "review": [_CLEAN_REVIEW, _CLEAN_REVIEW, _CLEAN_REVIEW],
            "rework": [_FLAWED, _FLAWED],
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
    kinds = [kind for kind, _ in router.calls]
    assert kinds == ["report", "review", "rework", "review", "rework", "review"]
    # The unsupported sentence is STILL in the final artifact — no scissors.
    assert "A fabricated figure appears nowhere in the evidence" in (
        written.sections[0].markdown
    )
    # …and honestly surfaced as metadata.
    assert written.unsupported_count >= 1
    assert written.sections[0].unsupported_count >= 1
    assert any("ghost9" in note for note in written.review_notes)
    assert any("Unsupported sentence" in note for note in written.review_notes)


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
    assert written.review_notes == []


# ---- dead end (system-gated) -----------------------------------------------


async def test_dead_end_path_only_via_outcome_flag() -> None:
    account = (
        "No usable sources could be admitted for this question.\n\n"
        "## Where the searches hit walls\n"
        "Both search angles were attempted; one returned nothing usable and "
        "one failed with a connection error. Narrowing the question or "
        "attaching documents may help."
    )
    router = _WriterRouter({"dead_end": [account]})
    captured, emit = _collect_events()
    written = await write_report(
        "the state of the subject",
        _outcome(dead_end=True, passages=[]),
        router=router,
        nli=_OverlapNLI(),
        bound=_bound(),
        emit=emit,
    )
    # Exactly ONE call, on the separate gated prompt, fed from the trail.
    assert [kind for kind, _ in router.calls] == ["dead_end"]
    prompt = router.calls[0][1]
    assert _DEAD_END_MARKER in prompt
    assert "subject overview" in prompt  # searched queries from the trail
    assert "search failed (ConnectionError)" in prompt
    assert "fabricate nothing" in prompt.lower()
    # Parsed into summary + ONE model-titled section; no claims, no citations.
    assert written.summary.startswith("No usable sources")
    assert [s.title for s in written.sections] == ["Where the searches hit walls"]
    assert written.claims == [] and written.unsupported_count == 0
    assert [k for k, _ in captured if k == "section_done"] == ["section_done"]


# ---- the writing contract (prompt + rubric text) ----------------------------


def test_section_prompt_contains_platform_prohibition():
    """The rules must forbid attributing a source by its publishing PLATFORM."""
    assert "PLATFORM" in writer_mod._WRITING_RULES


def test_section_prompt_names_platform_examples():
    """The prohibition must call out concrete examples so the model understands."""
    assert any(
        name in writer_mod._WRITING_RULES
        for name in ("Medium", "Reddit", "YouTube", "Substack")
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
    assert "{min_words}-{max_words} words" in writer_mod._REPORT_PROMPT


def test_format_explicitly_warns_against_one_shape_for_every_section():
    """Variety is the point — the prompt says 'vary', it does not force a mould."""
    assert "vary" in writer_mod._WRITING_RULES.lower()


def test_chart_guidance_broadened_beyond_hard_numbers():
    """The visual rule is offered when it AIDS COMPREHENSION and names the table
    option for comparing entities across attributes."""
    rules = writer_mod._WRITING_RULES
    assert "AIDS COMPREHENSION" in rules
    assert "TABLE" in rules
    # the exact chart fence contract is still present (the renderer parses ```chart)
    assert "```chart" in rules


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
    assert all(f"R{index}." in RESEARCH_REPORT_RUBRIC for index in range(1, 8))
    prompt = writer_mod._SELF_REVIEW_PROMPT.format(
        rubric=RESEARCH_REPORT_RUBRIC, previously_flagged="", draft="(draft)"
    )
    assert RESEARCH_REPORT_RUBRIC in prompt
    assert "apply the same bar on every pass" in prompt


def test_synthesis_temperature_is_a_small_bump():
    """Non-zero (so drafts vary) but small — grounding is enforced by the review
    loop, not by greedy decoding."""
    assert 0.0 < writer_mod._DRAFT_TEMPERATURE <= 0.5


def test_verification_helper_still_imported_unchanged():
    """The per-claim NLI verifier remains the grounding gate after generation:
    the claim ledger the report event carries is built from it."""
    assert hasattr(report_compiler, "_verify_claims")

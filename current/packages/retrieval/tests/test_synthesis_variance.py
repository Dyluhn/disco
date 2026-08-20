"""W-11: Deep Research visual variance — generator side.

The renderer used the same template for every section and the generator forced
a single rigid shape ("4–7 short paragraphs") with chart guidance gated on hard
numbers only, so every report read the same. These tests pin the BROADENED
contract WITHOUT loosening grounding:

  - the FORMAT no longer hard-forces "4–7 short paragraphs"; it allows lists,
    tables, and callouts as structural variety;
  - the chart/visual guidance is broadened beyond strictly-numeric data;
  - the citation/grounding contract is UNCHANGED — every factual sentence
    (including list items / table cells / callouts) still ends in [[id]], the
    visual must be built only from values present in the cited sources, and the
    platform-attribution prohibition + disputed-note extraction still hold;
  - the synthesis prose temperature got a small, bounded bump (prose only — the
    verification path is untouched).
"""

from __future__ import annotations

import datetime

import pytest
from disco.core import ReportSection
from disco.core.llm import CompletionResponse, TokenUsage
from disco.retrieval import InMemoryVectorStore, Passage
from disco.retrieval.deep_research import synthesis
from disco.retrieval.deep_research.synthesis import (
    _COHERENCE_PROMPT,
    _SECTION_PROMPT,
    _SYNTHESIS_TEMPERATURE,
    _coherence_outline,
    _extract_disputed_notes,
    _format_passages,
    _retrieve_for_section,
    coherence_pass,
)

# ---------------------------------------------------------------------------
# Structural variety: the rigid single-shape FORMAT is relaxed into a range
# ---------------------------------------------------------------------------


def test_format_no_longer_hard_forces_four_to_seven_short_paragraphs():
    """The old rigid 'FORMAT: 4–7 short paragraphs' mould must be gone."""
    assert "FORMAT: 4–7 short paragraphs" not in _SECTION_PROMPT


def test_format_allows_structural_variety():
    """The prompt must invite lists / tables / callouts, not prose-only."""
    lowered = _SECTION_PROMPT.lower()
    assert "list" in lowered
    assert "table" in lowered
    assert "callout" in lowered
    # And it must still bound the length (a range, not 'write anything').
    assert "3–7" in _SECTION_PROMPT or "3-7" in _SECTION_PROMPT


def test_format_explicitly_warns_against_one_shape_for_every_section():
    """Variety is the point — the prompt should say 'vary' and not force a mould."""
    assert "vary" in _SECTION_PROMPT.lower()


# ---------------------------------------------------------------------------
# Chart guidance broadened beyond strictly-numeric data
# ---------------------------------------------------------------------------


def test_chart_guidance_broadened_beyond_hard_numbers():
    """The visual rule should be offered when it AIDS COMPREHENSION, and should
    name the table option for comparing entities across attributes."""
    assert "AIDS COMPREHENSION" in _SECTION_PROMPT
    # table-for-comparison is now an explicitly suggested visual
    assert "TABLE" in _SECTION_PROMPT
    # the exact chart fence contract is still present (renderer parses ```chart)
    assert "```chart" in _SECTION_PROMPT


# ---------------------------------------------------------------------------
# Grounding is UNCHANGED — the broadened guidance must not become a license to
# fabricate. (These assertions are the factuality guardrail for W-11.)
# ---------------------------------------------------------------------------


def test_visuals_must_be_built_only_from_cited_values():
    """The broadened visual guidance must forbid inventing data to fill a chart."""
    lowered = _SECTION_PROMPT.lower()
    assert "never invent" in lowered
    # must require the visual to come from the cited sources
    assert "cited sources" in lowered


def test_citation_contract_intact_for_all_structures():
    """Every factual sentence — including list items / table cells — must still
    end in [[id]]. Broadening structure did not weaken the citation rule."""
    assert "ends in [[id]]" in _SECTION_PROMPT
    lowered = _SECTION_PROMPT.lower()
    assert "list items" in lowered and "table cells" in lowered


def test_platform_prohibition_and_grounding_rules_still_present():
    """The pre-existing grounding rails must survive the W-11 edits."""
    assert "PLATFORM" in _SECTION_PROMPT
    assert "STAY GROUNDED" in _SECTION_PROMPT
    assert "SYNTHESIZE, don't summarize" in _SECTION_PROMPT


def test_disputed_note_extraction_unchanged():
    """The grounded-conflict extractor still requires a cue word + [[id]]; an
    ungrounded hedge is still dropped (no regression to the honesty surface)."""
    grounded = (
        "Studies A and B disagree on the growth rate: 39% CAGR [[id1]] versus"
        " 22% in the 2023 peer review [[id2]]."
    )
    assert len(_extract_disputed_notes(grounded)) == 1
    assert _extract_disputed_notes("However, tensions remain unresolved.") == []


# ---------------------------------------------------------------------------
# Temperature: a small, bounded bump for PROSE only; verification path untouched
# ---------------------------------------------------------------------------


def test_synthesis_temperature_is_a_small_bump():
    """Non-zero (so reports vary) but small (factuality is still enforced by the
    downstream NLI gate, not by greedy decoding)."""
    assert 0.0 < _SYNTHESIS_TEMPERATURE <= 0.5


def test_verification_helper_still_imported_unchanged():
    """The per-claim NLI verifier remains the grounding gate after generation —
    the temperature bump is orthogonal to it."""
    # `_verify_claims` is reused verbatim from streaming; its presence is the
    # contract that grounding is enforced post-generation.
    assert hasattr(synthesis, "_verify_claims")


def test_coherence_outline_retains_concrete_names_beyond_old_short_lead():
    padding = "Background qualification. " * 20
    outline = _coherence_outline(
        [
            ReportSection(
                id="release",
                title="Recent releases",
                markdown=f"{padding}Muse Glimmer shipped this week [[cnbc]].",
            )
        ]
    )

    assert "Muse Glimmer shipped this week" in outline
    assert "[[cnbc]]" in outline
    assert "Never claim none occurred" in _COHERENCE_PROMPT


def test_section_evidence_keeps_tail_qualifications_and_url():
    passage = Passage(
        id="p1",
        source_url="https://example.com/release",
        source_title="Release report",
        text="A" * 2_000 + " The release was limited to a private preview.",
    )
    formatted = _format_passages([passage])
    assert "https://example.com/release" in formatted
    assert "private preview" in formatted
    assert "middle omitted" in formatted


async def test_empty_vector_result_falls_back_to_gathered_passages():
    class _Embedder:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    fallback = [
        Passage(
            id="p1",
            source_url="https://example.com/source",
            source_title="Source",
            text="Evidence that must survive an empty vector result.",
        )
    ]
    selected = await _retrieve_for_section(
        "topic",
        namespace="empty",
        embedder=_Embedder(),
        vector_store=InMemoryVectorStore(),
        fallback_passages=fallback,
        top_k=4,
    )
    assert selected == fallback


class _SummaryRouter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests = []

    async def complete(self, request):  # noqa: ANN001
        self.requests.append(request)
        return CompletionResponse(
            text=self.text,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="summary-test",
            request_id=request.request_id,
        )


class _ReleaseNLI:
    @staticmethod
    def _negative(text: str) -> bool:
        lowered = text.lower()
        return "no significant" in lowered or "not released" in lowered

    def entail(self, premise: str, hypothesis: str) -> str:
        if "release" in premise.lower() and "release" in hypothesis.lower():
            return (
                "contradict" if self._negative(premise) != self._negative(hypothesis) else "entail"
            )
        overlap = set(premise.lower().split()) & set(hypothesis.lower().split())
        return "entail" if overlap else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


@pytest.mark.asyncio
async def test_coherence_rejects_grounded_claim_that_contradicts_another_section():
    passages = [
        Passage(
            id="released",
            source_url="https://example.com/new",
            source_title="Current release",
            text="Muse Glimmer was released as open source this week.",
            published_at=datetime.date(2026, 8, 11),
        ),
        Passage(
            id="negative",
            source_url="https://example.com/old",
            source_title="Earlier survey",
            text="No significant open-source models were released this week.",
            published_at=datetime.date(2026, 8, 8),
        ),
    ]
    sections = [
        ReportSection(
            id="new",
            title="New release",
            markdown="Muse Glimmer was released as open source this week [[released]].",
        ),
        ReportSection(
            id="old",
            title="Earlier survey",
            markdown="No significant open-source models were released this week [[negative]].",
        ),
    ]
    router = _SummaryRouter(
        "No significant open-source models were released this week [[negative]]."
    )

    summary = await coherence_pass(
        "What open-source models were released this week?",
        sections,
        router=router,  # type: ignore[arg-type]
        passages=passages,
        nli=_ReleaseNLI(),
        recency_window="week",
    )

    assert "Muse Glimmer was released" in summary
    assert "[[released]]" in summary
    assert "No significant" not in summary
    prompt = router.requests[0].messages[-1].content
    assert "Grounding-supported cited findings" in prompt
    assert "published=2026-08-11" in prompt


@pytest.mark.asyncio
async def test_coherence_allows_newer_supported_state_to_supersede_old_state():
    passages = [
        Passage(
            id="old",
            source_url="https://example.com/old",
            source_title="Old status",
            text="Muse Glimmer was not released.",
            published_at=datetime.date(2026, 8, 1),
        ),
        Passage(
            id="new",
            source_url="https://example.com/new",
            source_title="New status",
            text="Muse Glimmer was released.",
            published_at=datetime.date(2026, 8, 11),
        ),
    ]
    sections = [
        ReportSection(
            id="old-section",
            title="Earlier status",
            markdown="Muse Glimmer was not released [[old]].",
        )
    ]
    router = _SummaryRouter("Muse Glimmer was released [[new]].")

    summary = await coherence_pass(
        "Was Muse Glimmer released?",
        sections,
        router=router,  # type: ignore[arg-type]
        passages=passages,
        nli=_ReleaseNLI(),
    )

    assert summary == "Muse Glimmer was released [[new]]."


@pytest.mark.asyncio
async def test_coherence_fallback_retains_dated_history_without_recency_scope():
    passages = [
        Passage(
            id="old",
            source_url="https://example.com/old",
            source_title="Old status",
            text="Muse Glimmer was not released.",
            published_at=datetime.date(2026, 8, 1),
        ),
        Passage(
            id="new",
            source_url="https://example.com/new",
            source_title="New status",
            text="Muse Glimmer was released.",
            published_at=datetime.date(2026, 8, 11),
        ),
    ]
    sections = [
        ReportSection(
            id="old-section",
            title="Earlier status",
            markdown="Muse Glimmer was not released [[old]].",
        ),
        ReportSection(
            id="new-section",
            title="Current status",
            markdown="Muse Glimmer was released [[new]].",
        ),
    ]

    summary = await coherence_pass(
        "How did the release status change?",
        sections,
        router=_SummaryRouter(""),  # type: ignore[arg-type]
        passages=passages,
        nli=_ReleaseNLI(),
    )

    assert summary.startswith("Sources disagree on some current-state findings.")
    assert "Muse Glimmer was not released" in summary
    assert "[[old]]" in summary
    assert "Muse Glimmer was released" in summary
    assert "[[new]]" in summary
    assert "\n\n" in summary


def test_fallback_summary_keeps_markdown_markers_out_of_the_paragraph_body():
    from disco.retrieval.deep_research._summary import (
        _fallback_findings,
        _SupportedFinding,
    )

    findings = [
        _SupportedFinding(
            text="> **As of August 16, 2026**, the release is public",
            cited_ids=("release",),
            section_index=0,
            published_at=datetime.date(2026, 8, 16),
        ),
        _SupportedFinding(
            text="The repository includes model weights",
            cited_ids=("weights",),
            section_index=1,
            published_at=datetime.date(2026, 8, 16),
        ),
    ]

    summary = _fallback_findings(findings, _ReleaseNLI(), None)

    assert summary.startswith("**As of August 16, 2026**")
    assert not summary.startswith(">")
    assert "\n\n" in summary


def test_successful_summary_is_split_when_provider_ignores_paragraph_contract():
    from disco.retrieval.deep_research._summary import _readable_summary

    summary = _readable_summary(
        "First supported finding [[one]]. Second supported finding [[two]]. "
        "Third supported finding [[three]]. Fourth supported finding [[four]]."
    )

    assert summary.count("\n\n") == 3
    assert "[[one]]" in summary
    assert "[[four]]" in summary


def test_successful_summary_preserves_existing_markdown_paragraphs():
    from disco.retrieval.deep_research._summary import _readable_summary

    summary = _readable_summary(
        "**Bottom line.** The first finding is supported [[one]].\n\n"
        "The qualification is also supported [[two]]."
    )

    assert summary == (
        "**Bottom line.** The first finding is supported [[one]].\n\n"
        "The qualification is also supported [[two]]."
    )


# ---------------------------------------------------------------------------
# Empty-evidence honesty: the junk filter and the honest fallback paragraph.
# Junk rows are REAL fragments from an exported report that shipped as
# executive-summary prose before the filter existed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fragment", "is_junk"),
    [
        # scrape metadata stub (also ends in an ellipsis)
        ("    Last verified: August 19, 2026 ….", True),
        # site-navigation boilerplate with an escaped relative-path link
        (
            "For more details including relating to our methodology, "
            "see our \\[FAQs.\\](/faq)",
            True,
        ),
        # raw scraped-table fragments ('|' debris)
        ("| Vendor | Q2 revenue | Growth |", True),
        ("Owner | Fleet size | 12", True),
        # mid-sentence truncation stubs
        ("…and the remainder of the fleet was retrofitted by June", True),
        ("which is why the market cannot yet be sized before 2030", True),
        ("The market grew rapidly in the first half and …", True),
        # too short to inform
        ("Read more", True),
        # subscribe/cookie boilerplate
        ("Subscribe to our newsletter for weekly updates on the sector.", True),
        ("Accept all cookies to continue reading this article.", True),
        # genuine findings must all survive
        ("Muse Glimmer was released", False),
        ("The repository includes model weights", False),
        ("> **As of August 16, 2026**, the release is public", False),
        (
            "Samsung announced a solid-state battery with a claimed 600-mile range",
            False,
        ),
    ],
)
def test_junk_evidence_filter(fragment: str, is_junk: bool):
    from disco.retrieval.deep_research._summary import _is_junk_evidence

    assert _is_junk_evidence(fragment) is is_junk


def test_fallback_summary_drops_junk_findings_but_keeps_genuine_ones():
    from disco.retrieval.deep_research._summary import (
        _fallback_findings,
        _SupportedFinding,
    )

    findings = [
        _SupportedFinding(
            text="Last verified: August 19, 2026 ….",
            cited_ids=("nav1",),
            section_index=0,
            published_at=None,
        ),
        _SupportedFinding(
            text="| Vendor | Q2 revenue | Growth |",
            cited_ids=("tbl1",),
            section_index=0,
            published_at=None,
        ),
        _SupportedFinding(
            text="The repository includes model weights",
            cited_ids=("weights",),
            section_index=1,
            published_at=datetime.date(2026, 8, 16),
        ),
    ]

    summary = _fallback_findings(findings, _ReleaseNLI(), None)

    assert "The repository includes model weights" in summary
    assert "[[weights]]" in summary
    assert "Last verified" not in summary
    assert "|" not in summary


def test_fallback_summary_with_only_junk_findings_is_one_honest_paragraph():
    from disco.retrieval.deep_research._summary import (
        _UNGROUNDED_SUMMARY_FALLBACK,
        _fallback_findings,
        _SupportedFinding,
    )

    findings = [
        _SupportedFinding(
            text=(
                "For more details including relating to our methodology, "
                "see our \\[FAQs.\\](/faq)"
            ),
            cited_ids=("faq",),
            section_index=0,
            published_at=None,
        ),
        _SupportedFinding(
            text="…and the remainder of the fleet was retrofitted by June",
            cited_ids=("stub",),
            section_index=1,
            published_at=None,
        ),
    ]

    summary = _fallback_findings(findings, _ReleaseNLI(), None)

    assert summary == _UNGROUNDED_SUMMARY_FALLBACK
    assert summary.startswith("The research could not ground an executive summary")
    assert "per-section detail" in summary
    assert "FAQs" not in summary
    assert not summary.startswith("Sources disagree")


def test_fallback_conflict_checks_are_bounded_without_dropping_unchecked_findings():
    from disco.retrieval.deep_research._summary import (
        _MAX_FALLBACK_CONFLICT_CHECKS,
        _fallback_findings,
        _SupportedFinding,
    )

    class _CountingNLI(_ReleaseNLI):
        def __init__(self) -> None:
            self.calls = 0

        def entail(self, premise: str, hypothesis: str) -> str:
            self.calls += 1
            return super().entail(premise, hypothesis)

    findings = [
        _SupportedFinding(
            text=("Muse Glimmer was not released" if index % 2 else "Muse Glimmer was released"),
            cited_ids=(f"p{index}",),
            section_index=index % 12,
            published_at=datetime.date(2026, 1, 1) + datetime.timedelta(days=index),
        )
        for index in range(500)
    ]
    nli = _CountingNLI()

    summary = _fallback_findings(findings, nli, "week")

    assert nli.calls <= _MAX_FALLBACK_CONFLICT_CHECKS * 2
    assert "[[p499]]" in summary

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

from disco.retrieval.deep_research import synthesis
from disco.retrieval.deep_research.synthesis import (
    _SECTION_PROMPT,
    _SYNTHESIS_TEMPERATURE,
    _extract_disputed_notes,
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

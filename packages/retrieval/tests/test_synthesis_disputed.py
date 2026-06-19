"""Tests for _extract_disputed_notes (WALK-06: C2+C3 tightening) and the
_SECTION_PROMPT platform-attribution prohibition (C2).

Behavioral contract after the fix:
  - Vacuous hedge sentences with no [[id]] citation are EXCLUDED.
  - Short hedge-only sentences are EXCLUDED even if they happen to contain a
    conflict-cue word, because they carry no grounded claim.
  - Genuine conflict sentences WITH a [[id]] citation are INCLUDED.
  - Conflict-cue sentences that lack a citation are EXCLUDED (not grounded).
  - The _SECTION_PROMPT explicitly forbids platform attribution by name.
"""

from __future__ import annotations

from disco.retrieval.deep_research.synthesis import _SECTION_PROMPT, _extract_disputed_notes

# ---------------------------------------------------------------------------
# C3 — vacuous hedges must be excluded
# ---------------------------------------------------------------------------


def test_vacuous_hedge_no_citation_excluded():
    """'the sources leave important tensions unresolved.' has no [[id]] — excluded."""
    text = "However, the sources leave important tensions unresolved."
    assert _extract_disputed_notes(text) == []


def test_short_conflict_cue_no_citation_excluded():
    """Even a clear conflict word without a citation must not surface."""
    text = "Sources disagree."
    assert _extract_disputed_notes(text) == []


def test_conflict_cue_without_citation_excluded():
    """A sentence with a cue word but no [[id]] is excluded (not grounded)."""
    text = (
        "Analysts disagree on whether the market will grow at 39% or 22% annually,"
        " with projections varying widely across firms."
    )
    assert _extract_disputed_notes(text) == []


# ---------------------------------------------------------------------------
# C3 — genuine, grounded conflict sentences must be included
# ---------------------------------------------------------------------------


def test_grounded_disagree_included():
    """disagree + [[id]] → included."""
    text = (
        "Studies A and B disagree on the growth rate: 39% CAGR [[id1]] versus"
        " 22% in the 2023 peer review [[id2]]."
    )
    result = _extract_disputed_notes(text)
    assert len(result) == 1
    assert "disagree" in result[0]


def test_grounded_contradict_included():
    """contradict + [[id]] → included."""
    text = (
        "The vendor projection contradicts independent lab results, which show"
        " a 15% efficiency gain rather than the claimed 40% [[src_abc]]."
    )
    result = _extract_disputed_notes(text)
    assert len(result) == 1
    assert "contradict" in result[0].lower()


def test_grounded_inconsistent_included():
    """inconsistent + [[id]] → included."""
    text = (
        "The timeline reported by the firm is inconsistent with regulatory filings"
        " dated Q4 2023 [[reg_filing_p0]]."
    )
    result = _extract_disputed_notes(text)
    assert len(result) == 1


def test_multiple_grounded_conflicts_capped_at_three():
    """At most 3 notes are returned even if more sentences qualify."""
    note = "Sources conflict on the data point [[id{n}]]."
    text = " ".join(note.format(n=i) for i in range(6))
    result = _extract_disputed_notes(text)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# C3 — old `however[, ].+sources?` branch is gone
# ---------------------------------------------------------------------------


def test_however_plus_sources_no_longer_matches():
    """The removed branch must not match even with 'however ... sources'."""
    text = "However, the sources leave some tension on this point."
    assert _extract_disputed_notes(text) == []


def test_however_with_citation_but_no_real_conflict_excluded():
    """'however' alone is not a conflict cue; needs disagree/conflict/etc. + [[id]]."""
    text = "However, this should be noted [[id1]]."
    assert _extract_disputed_notes(text) == []


# ---------------------------------------------------------------------------
# C2 — _SECTION_PROMPT must forbid platform attribution
# ---------------------------------------------------------------------------


def test_section_prompt_contains_platform_prohibition():
    """_SECTION_PROMPT must mention PLATFORM prohibition for C2 compliance."""
    assert "PLATFORM" in _SECTION_PROMPT


def test_section_prompt_names_platform_examples():
    """The prohibition must call out concrete examples so the model understands."""
    # at least one of the canonical bad examples must appear
    assert any(p in _SECTION_PROMPT for p in ("Medium", "Reddit", "YouTube", "Substack"))


def test_section_prompt_names_good_alternative():
    """The prompt must suggest what to write instead of the platform name."""
    assert any(
        phrase in _SECTION_PROMPT
        for phrase in (
            "what the source IS",
            "independent analyst",
            "industry report",
            "community discussion",
        )
    )

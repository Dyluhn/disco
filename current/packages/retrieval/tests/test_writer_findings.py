"""The deterministic walls the whole-report review grades with.

Every producer in `_writer_findings` turns one measurable defect into ONE
finding that names ONE part of the report, quotes the text, and states the
change. That shape is what makes the section-scoped rework possible, so these
tests pin the shape as hard as the verdict: which part a finding lands on, what
it quotes, and what it asks for. Nothing here removes a sentence — a finding is
a wall with an angle, and the writer makes the edit.

Each producer gets a positive case and a "nothing to find" case, because a
ruler that fires on a clean report is worse than no ruler at all.
"""

from __future__ import annotations

import pytest
from disco.retrieval.deep_research._writer_findings import (
    _MAX_REPEATED,
    _MAX_REVIEW,
    _MAX_UNSUPPORTED,
    KIND_ABSENCE,
    KIND_PROCESS,
    KIND_REPEATED,
    KIND_REVIEW,
    KIND_UNRESOLVED_CITATION,
    KIND_UNSUPPORTED,
    SUMMARY_WHERE,
    Finding,
    absence_findings,
    named_parts,
    process_language_findings,
    render_findings,
    repetition_findings,
    review_findings,
    unresolved_citation_findings,
    unsupported_findings,
)

# ---- repetition -------------------------------------------------------------


def test_review_quote_owns_finding_when_reviewer_names_wrong_section():
    sections = [
        ("Concurrency", "Forum discussion describes a shared-memory requirement [[p1]]."),
        ("Network filesystems", "Network locking has additional reliability risks [[p2]]."),
    ]
    findings, unplaced = review_findings(
        {
            "failures": [
                {
                    "section": "Network filesystems",
                    "where": "Forum discussion describes a shared-memory requirement",
                    "fix": "State the requirement directly with its citation.",
                    "rubric": "R5",
                }
            ]
        },
        "The deployment constraints differ.",
        sections,
    )
    assert unplaced == 0
    assert len(findings) == 1
    assert findings[0].where == "Concurrency"
    assert named_parts(findings, [title for title, _ in sections]) == ["Concurrency"]


def test_ambiguous_review_quote_requires_a_matching_heading():
    sections = [
        ("First", "Shared qualification occurs here."),
        ("Second", "Shared qualification occurs here."),
    ]
    payload = {
        "failures": [
            {
                "section": "Unknown",
                "where": "Shared qualification occurs here",
                "fix": "Clarify the qualification.",
            }
        ]
    }
    assert review_findings(payload, "A separate summary.", sections) == ([], 1)
    payload["failures"][0]["section"] = "Second"
    findings, unplaced = review_findings(payload, "A separate summary.", sections)
    assert unplaced == 0
    assert findings[0].where == "Second"


_REPEAT = "The measured throughput fell short of the vendor projection [[p1]]."
_OPENER = "Baseline numbers frame the comparison across the two records [[p1]]."
_CLOSER = "The remaining spread is explained by the sampling window [[p2]]."

_PARAGRAPH = (
    "The two records agree on the direction of the effect while differing on "
    "magnitude, and that difference tracks the sampling window each one used "
    "rather than any disagreement about the underlying measurement [[p1]]."
)
_OTHER_PARAGRAPH = (
    "Neither record measures the longer horizon over which the effect would "
    "have to hold, so the reading here is bounded by the window that was "
    "actually observed in both of them [[p2]]."
)


def test_missing_coverage_is_a_named_repair_even_without_an_existing_section() -> None:
    findings, unplaced = review_findings(
        {
            "passes": False,
            "failures": [
                {
                    "rubric": "R2",
                    "action": "add_section",
                    "section": "Network limits",
                    "after": "Findings",
                    "where": "network filesystem restrictions",
                    "fix": "Explain the restriction using the admitted primary documentation.",
                }
            ],
        },
        "A supported summary.",
        [("Findings", "A supported comparison.")],
    )

    assert unplaced == 0
    assert len(findings) == 1
    assert findings[0].where == "Network limits"
    assert findings[0].after == "Findings"
    assert named_parts(findings, ["Findings"]) == ["Network limits"]
    assert "Add this missing section after" in render_findings(
        findings, ["Findings"], summary_heading="Executive summary"
    )


@pytest.mark.parametrize("after,rubric", [("Invented anchor", "R2"), ("Findings", "R9")])
def test_section_insertion_requires_a_real_anchor_and_a_coverage_finding(
    after: str,
    rubric: str,
) -> None:
    findings, unplaced = review_findings(
        {
            "passes": False,
            "failures": [
                {
                    "rubric": rubric,
                    "action": "add_section",
                    "section": "Network limits",
                    "after": after,
                    "where": "network restrictions",
                    "fix": "Add the missing angle.",
                }
            ],
        },
        "A summary.",
        [("Findings", "A supported comparison.")],
    )

    assert findings == []
    assert unplaced == 1


def test_a_sentence_printed_twice_in_one_section_is_named_there() -> None:
    """The sentence clause, located: one finding on the section that prints it
    twice, telling the writer to keep exactly one occurrence."""
    findings = repetition_findings([("Findings", f"{_OPENER} {_REPEAT} {_CLOSER} {_REPEAT}")])

    assert len(findings) == 1
    assert findings[0].where == "Findings"
    assert findings[0].kind == KIND_REPEATED
    assert findings[0].rubric == "R4"
    assert "The measured throughput fell short of the vendor projection" in findings[0].quote
    assert "appears 2 times in this section" in findings[0].fix
    assert "Keep exactly one occurrence" in findings[0].fix


def test_a_sentence_repeated_across_sections_is_named_in_the_later_one() -> None:
    """A repeat spanning two sections names the LATER section and points at the
    earlier one as the keeper — otherwise a writer rewriting both could keep
    both, or neither."""
    findings = repetition_findings(
        [("Findings", f"{_OPENER} {_REPEAT}"), ("Limits", f"{_CLOSER} {_REPEAT}")]
    )

    assert [finding.where for finding in findings] == ["Limits"]
    assert "already appears in 'Findings'" in findings[0].fix


def test_a_paragraph_printed_twice_is_named_as_a_paragraph() -> None:
    """The paragraph clause of the same ruler, reported in its own vocabulary."""
    section = f"{_PARAGRAPH}\n\n{_OTHER_PARAGRAPH}\n\n{_PARAGRAPH}"
    findings = repetition_findings([("Findings", section)])

    assert all(finding.where == "Findings" for finding in findings)
    assert any("This paragraph appears 2 times in this section" in f.fix for f in findings)


def test_a_report_that_never_repeats_itself_produces_nothing() -> None:
    findings = repetition_findings([("Findings", f"{_OPENER} {_CLOSER}")])

    assert findings == []


def test_the_repetition_findings_are_bounded() -> None:
    """One badly broken draft still produces a findings list a writer can read."""
    sentences = [
        f"Distinct framing sentence number {index} carries its own argument [[p1]]."
        for index in range(_MAX_REPEATED + 2)
    ]
    body = " ".join(f"{sentence} {sentence}" for sentence in sentences)

    assert len(repetition_findings([("Findings", body)])) == _MAX_REPEATED


# ---- process / host language ------------------------------------------------


def test_process_language_in_the_summary_is_named_against_the_summary() -> None:
    """Rubric R7: the report discusses its subject, never how the research ran."""
    findings = process_language_findings(
        "We searched for the vendor's own filings and found little [[p1]].",
        [("Findings", "The sampling window is narrow in both records [[p2]].")],
    )

    assert [finding.where for finding in findings] == [SUMMARY_WHERE]
    assert findings[0].kind == KIND_PROCESS
    assert findings[0].rubric == "R7"
    assert "We searched for the vendor's own filings" in findings[0].quote


def test_process_language_in_a_section_is_named_against_that_section() -> None:
    findings = process_language_findings(
        "The subject is measured directly by both records [[p1]].",
        [("Findings", "The search process returned nothing for that angle [[p1]].")],
    )

    assert [finding.where for finding in findings] == ["Findings"]
    assert "search process" in findings[0].fix


def test_a_heading_that_names_the_research_is_a_rename_finding() -> None:
    """Host vocabulary in a heading is a heading problem: the line stays where
    it is and becomes about the subject."""
    findings = process_language_findings(
        "The subject is measured directly by both records [[p1]].",
        [("Source budget limitations", "The window is narrow in both records [[p2]].")],
    )

    assert [finding.where for finding in findings] == ["Source budget limitations"]
    assert findings[0].quote == "Source budget limitations"
    assert "The heading itself names the research process" in findings[0].fix


def test_the_pool_vocabulary_is_process_language_in_summary_and_body() -> None:
    """Prompt-local references are narration; material evidence limits are allowed."""
    findings = process_language_findings(
        "The supplied evidence gives annual additions but not durations [[p1]].",
        [
            ("Deployments", "None of the sources above report a commissioned cell [[p2]]."),
            ("Costs", "The evidence does not include a primary cost figure [[p2]]."),
        ],
    )

    assert [finding.where for finding in findings] == ["Deployments"]
    assert all(finding.rubric == "R7" for finding in findings)


def test_analytical_english_about_the_evidence_is_not_pool_vocabulary() -> None:
    """The ruler lists only phrases that can ONLY mean the pool: ordinary
    analytical sentences about the subject's evidence never match."""
    findings = process_language_findings(
        "The available evidence suggests a narrow effect [[p1]].",
        [
            ("Trials", "The evidence does not support a simple failure narrative [[p1]]."),
            ("Trials", "There are gaps in the evidence base for younger patients [[p2]]."),
            ("Trials", "The trial evidence provided by SELECT is strong [[p1]]."),
            ("Sources", "The sources provided by the ministry are dated [[p2]]."),
        ],
    )

    assert findings == []


def test_a_sentence_convicted_by_two_patterns_is_named_once() -> None:
    """The rulers' R7 vocabulary and the host vocabulary overlap; the writer is
    handed one finding per sentence, not one per pattern."""
    findings = process_language_findings(
        "The research process could not reach the vendor filings [[p1]].", []
    )

    assert len(findings) == 1
    assert findings[0].where == SUMMARY_WHERE


def test_a_report_about_its_subject_produces_no_process_finding() -> None:
    findings = process_language_findings(
        "The subject is measured directly by both records [[p1]].",
        [("Findings", "The observed window is narrow in both records [[p2]].")],
    )

    assert findings == []


def test_a_summary_that_says_it_was_unable_to_answer_is_named() -> None:
    """The failure-digest shape an executive summary can take: a summary that
    reports on the research instead of answering the question."""
    findings = process_language_findings(
        "This report was unable to answer the question from the available sources [[p1]].",
        [("Findings", "The window is narrow in both records [[p2]].")],
    )

    assert [finding.where for finding in findings] == [SUMMARY_WHERE]


# ---- citations that resolve to nothing --------------------------------------


def test_an_unresolvable_citation_quotes_its_sentence_and_names_the_valid_range() -> None:
    """The wall the model already hit, now with the angle: the exact ids it may
    cite. Every citation here is canonical, so an id that still fails to resolve
    is one the run never had."""
    findings = unresolved_citation_findings(
        "The subject is measured directly by both records [[p1]].",
        [("Findings", "A body sentence citing nothing real [[zzz]]. Another one [[p1]].")],
        {"p1"},
        " The valid citation ids are s1-s2.",
    )

    assert [finding.where for finding in findings] == ["Findings"]
    assert findings[0].kind == KIND_UNRESOLVED_CITATION
    assert findings[0].rubric == "R3"
    assert findings[0].quote == "A body sentence citing nothing real [[zzz]]."
    assert "[[zzz]]" in findings[0].fix
    assert findings[0].fix.endswith(" The valid citation ids are s1-s2.")


def test_citations_that_all_resolve_produce_nothing() -> None:
    findings = unresolved_citation_findings(
        "Summary [[p1]].", [("Findings", "Body [[p2]].")], {"p1", "p2"}, " ids"
    )

    assert findings == []


# ---- absence asserted over an angle research never reached -------------------

_ANGLE = "FDA approval for drug X in pediatric patients"
_ABSENCE = "No approvals for drug X in pediatric patients exist."


def test_an_absence_over_an_untested_angle_is_named() -> None:
    """Two conditions must both hold: a strong absence assertion, AND overlap
    with a query whose every issue died in a host outage."""
    findings = absence_findings(f"{_ABSENCE} [[p1]]", [], [_ANGLE])

    assert [finding.where for finding in findings] == [SUMMARY_WHERE]
    assert findings[0].kind == KIND_ABSENCE
    assert findings[0].rubric == "R3"
    assert findings[0].quote == _ABSENCE
    assert f'the angle "{_ANGLE}"' in findings[0].fix


def test_an_absence_in_a_section_is_named_against_that_section() -> None:
    findings = absence_findings(
        "The subject is measured directly [[p1]].", [("Findings", f"{_ABSENCE} [[p1]]")], [_ANGLE]
    )

    assert [finding.where for finding in findings] == ["Findings"]


def test_an_absence_that_overlaps_no_untested_angle_is_left_alone() -> None:
    """A report is allowed to say the evidence is thin. The check is tuned for
    precision: a false flag teaches the writer to hedge a claim it had every
    right to make."""
    unrelated = ["battery chemistry manufacturing yields"]

    assert absence_findings(f"{_ABSENCE} [[p1]]", [], unrelated) == []


def test_a_run_that_tested_every_query_it_issued_produces_nothing() -> None:
    assert absence_findings(f"{_ABSENCE} [[p1]]", [], []) == []


# ---- sentences the verifier could not support -------------------------------


def test_unsupported_sentences_carry_their_part_quote_and_citations() -> None:
    findings = unsupported_findings([("Findings", "The vendor's number is unverifiable", ("p1",))])

    assert findings[0].where == "Findings"
    assert findings[0].kind == KIND_UNSUPPORTED
    assert findings[0].quote == "The vendor's number is unverifiable"
    assert "([[p1]])" in findings[0].fix
    assert "Retain it if supported" in findings[0].fix
    assert "not proof that the sentence is wrong" in findings[0].fix


def test_an_uncited_sentence_contradicted_by_its_paragraph_names_that_evidence() -> None:
    findings = unsupported_findings(
        [(SUMMARY_WHERE, "A sentence citing nothing", (), ("p1", "p2"))]
    )

    assert findings[0].fix.startswith(
        "No citation, and the evidence cited around it ([[p1]], [[p2]])"
    )
    assert "Retain supported synthesis" in findings[0].fix
    assert "not proof that the sentence is wrong" in findings[0].fix


def test_an_uncited_sentence_in_an_uncited_paragraph_says_so_in_its_fix() -> None:
    for row in [
        (SUMMARY_WHERE, "A sentence citing nothing", ()),
        (SUMMARY_WHERE, "A sentence citing nothing", (), ()),
    ]:
        findings = unsupported_findings([row])

        assert findings[0].fix.startswith("No citation, and this paragraph cites no evidence")


def test_the_unsupported_findings_are_bounded() -> None:
    rows = [("Findings", f"sentence {index}", ("p1",)) for index in range(_MAX_UNSUPPORTED + 6)]

    assert len(unsupported_findings(rows)) == _MAX_UNSUPPORTED


# ---- the reviewer's verdict, resolved to parts -------------------------------

_SUMMARY = "The subject is measured directly by both records [[p1]]."
_SECTIONS = [
    ("Findings", "The convergence holds across the observed window [[p1]]."),
    ("Limits", "The observed window is narrow in both records [[p2]]."),
]


def _one_failure(**item: object) -> tuple[list[Finding], int]:
    return review_findings({"failures": [item]}, _SUMMARY, _SECTIONS)


@pytest.mark.parametrize("section", ["Findings", "findings", "## Findings", "  Findings  "])
def test_a_failure_is_placed_by_the_heading_it_names(section: str) -> None:
    """The reviewer names the part by its heading; case and the '## ' prefix
    are tolerated because the reviewer is reading rendered markdown."""
    placed, unplaced = _one_failure(section=section, where="quoted text", fix="Cite it.")

    assert [finding.where for finding in placed] == ["Findings"]
    assert placed[0].kind == KIND_REVIEW
    assert unplaced == 0


@pytest.mark.parametrize(
    "section", ["executive summary", "summary", "overview", "executive overview", "Summary"]
)
def test_the_summary_answers_to_every_name_a_reviewer_gives_it(section: str) -> None:
    placed, _unplaced = _one_failure(section=section, where="quoted text", fix="Cite it.")

    assert [finding.where for finding in placed] == [SUMMARY_WHERE]


def test_a_failure_naming_no_known_part_is_placed_by_its_quote() -> None:
    """A quote of three words or more is searched in the summary first, then in
    the sections, so a reviewer that mis-titles a part still lands."""
    in_summary, _ = _one_failure(section="Chapter 2", where="measured directly by both", fix="f")
    in_section, _ = _one_failure(section="Chapter 2", where="convergence holds across the", fix="f")

    assert [finding.where for finding in in_summary] == [SUMMARY_WHERE]
    assert [finding.where for finding in in_section] == ["Findings"]


def test_a_failure_nobody_can_place_is_counted_and_dropped() -> None:
    """A rework cannot rewrite "the part with this problem" if no part can be
    found, so the failure contributes a count and nothing else."""
    placed, unplaced = _one_failure(section="", where="somewhere", fix="Cite it.")

    assert placed == []
    assert unplaced == 1


def test_referring_back_to_the_reports_evidence_is_not_internal_process_narration():
    sentence = "Conditional guidance follows from the transport and fallback evidence above [[p1]]."
    assert process_language_findings(sentence, [("Guidance", sentence)]) == []


@pytest.mark.parametrize("anchor", ["executive summary", "## Findings"])
def test_a_new_section_accepts_the_markdown_heading_used_by_the_review_contract(anchor):
    placed, unplaced = _one_failure(
        action="add_section",
        section="## Comparative assessment",
        after=anchor,
        rubric="R1",
        where="Analysis currently in the summary",
        fix="Move the detailed comparison into this section.",
    )
    assert unplaced == 0
    assert placed[0].where == "Comparative assessment"
    assert placed[0].after == anchor.removeprefix("## ")


def test_a_failure_that_is_not_an_object_is_skipped_silently() -> None:
    placed, unplaced = review_findings({"failures": ["nope", 7, None]}, _SUMMARY, _SECTIONS)

    assert (placed, unplaced) == ([], 0)


def test_a_failure_with_neither_a_fix_nor_a_quote_is_skipped() -> None:
    placed, unplaced = review_findings(
        {"failures": [{"section": "Findings", "rubric": "R3"}]}, _SUMMARY, _SECTIONS
    )

    assert (placed, unplaced) == ([], 0)


def test_a_failure_with_a_quote_but_no_fix_still_asks_for_the_change() -> None:
    placed, _unplaced = _one_failure(section="Findings", where="quoted text")

    assert placed[0].fix == "Fix the quoted text so it meets the rubric."


def test_a_verdict_without_a_failures_list_finds_nothing() -> None:
    assert review_findings({"passes": True}, _SUMMARY, _SECTIONS) == ([], 0)
    assert review_findings({"failures": "R3"}, _SUMMARY, _SECTIONS) == ([], 0)


@pytest.mark.parametrize(
    ("given", "kept"), [("R3", "R3"), ("r3", "R3"), ("R10", "R10"), ("R11", ""), ("R", "")]
)
def test_a_rubric_tag_is_kept_only_when_it_is_one_of_the_ten(given: str, kept: str) -> None:
    placed, _unplaced = _one_failure(section="Findings", where="q", fix="f", rubric=given)

    assert placed[0].rubric == kept


def test_the_reviewers_findings_are_bounded() -> None:
    failures = [
        {"section": "Findings", "where": f"quote {index}", "fix": "Cite it."}
        for index in range(_MAX_REVIEW + 3)
    ]

    placed, _unplaced = review_findings({"failures": failures}, _SUMMARY, _SECTIONS)
    assert len(placed) == _MAX_REVIEW


# ---- the list the writer reads ----------------------------------------------

_ORDER = ["Findings", "Limits"]
_FINDINGS = [
    Finding(where="Limits", quote="q1", fix="Fix one.", kind=KIND_REVIEW, rubric="R3"),
    Finding(where=SUMMARY_WHERE, quote="", fix="Fix two.", kind=KIND_PROCESS),
    Finding(where="Limits", quote="q3", fix="Fix three.", kind=KIND_REPEATED, rubric="R4"),
    Finding(where="Nowhere", quote="q4", fix="Fix four.", kind=KIND_REVIEW),
]


def test_named_parts_puts_the_summary_first_then_report_order() -> None:
    """The parts handed to the rework, in the order the report reads. A finding
    on a part the report does not have names nothing."""
    assert named_parts(_FINDINGS, _ORDER) == [SUMMARY_WHERE, "Limits"]
    assert named_parts([], _ORDER) == []


def test_render_findings_groups_them_under_the_heading_they_belong_to() -> None:
    """One numbered list, grouped by part, each finding carrying its rubric tag,
    its quote, and its fix — the bytes the writer reads back."""
    rendered = render_findings(_FINDINGS, _ORDER, summary_heading="Executive summary")

    assert rendered == (
        "## Executive summary\n"
        "1. Fix two.\n"
        "## Limits\n"
        '2. (R3) "q1" — Fix one.\n'
        '3. (R4) "q3" — Fix three.'
    )

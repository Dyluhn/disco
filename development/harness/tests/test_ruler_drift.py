"""The harness and the product must grade with the SAME rulers.

The final 27B acceptance batch shipped 5/5 reports with zero run errors, and
3/5 of them failed invariants here that the writer's own deterministic review
had called clean: a framing sentence repeated four and five times, prose
narrating the research process, and a section under the substantive-body floor.
Every one of those is deterministically measurable. The product simply owned a
different ruler than this batch did.

So the invariants below import ``disco.retrieval.deep_research._report_rulers``
instead of restating its normalization and thresholds, and these tests pin the
agreement on fixture reports: for each fixture, the harness invariant and the
product's deterministic findings (``_writer_findings``) reach the same verdict.
When they cannot, a failure here is the finding — a report that passes the
product's review and fails this batch on the same bytes is not evidence about
the product, it is evidence about the two rulers.

Two clauses are deliberately ONE-SIDED and are named as such below: too little
prose to judge repetition at all, and the substantive-body floor (the product
retired its deterministic thin-section finding — the reviewer model's rubric R2
names stubs now — while the harness still measures one).

`test_research_thrash.py` pins the same property for query identity (slice 6);
the last test below names that pairing so the pattern is visible from here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from disco.retrieval.deep_research import _report_rulers as rulers
from disco.retrieval.deep_research import _search_outcomes, _writer_findings
from disco.retrieval.deep_research._writer_parts import finalize_report
from harness.research_harness import Observation, check_invariants
from harness.research_harness_parts import _thrash

# ---- fixture reports --------------------------------------------------------
#
# Written as the product writes them (summary + '## ' sections) and read as the
# harness reads them (a normalized report mapping), so ONE fixture can be put
# to both graders without either side being handed a shape it never sees.

_LEXICON = (
    "measured", "evidence", "documents", "subject", "collected", "data",
    "reported", "figures", "independent", "deployments", "analysis",
    "confirms", "limitations", "current", "several", "across",
)


def _line(seed: int) -> str:
    picked = [_LEXICON[(seed * 7 + step * 5) % len(_LEXICON)] for step in range(12)]
    return f"Observation {seed} weighs the " + " ".join(picked) + " [[p0]]."


def _body(seed: int, lines: int = 9) -> str:
    return " ".join(_line(seed + index) for index in range(lines))


_SUMMARY = (
    "The measured evidence answers the question with collected data and "
    "reported figures from the documented deployments [[p0]]."
)
_POOL_IDS = {"p0"}
_CLEAN_SECTIONS = [("Convergent findings", _body(10)), ("Limits of the data", _body(40))]

_REFRAIN = _line(500)
_REPEATED_SECTIONS = [
    ("Convergent findings", _body(10)),
    (
        "Limits of the data",
        " ".join([_REFRAIN if index % 2 else _line(600 + index) for index in range(10)]),
    ),
]

# One block of table rows, printed under two different sections. Table lines are
# not sentences to either grader, so ONLY the paragraph clause can catch this.
_TABLE = "\n".join(
    [
        "| Deployment | Measured throughput | Reported latency |",
        "| --- | --- | --- |",
        *[
            f"| Site {index} | {index * 11} documents per hour | {index * 3} seconds |"
            for index in range(1, 5)
        ],
    ]
)
_DUPLICATE_PARAGRAPH_SECTIONS = [
    ("Convergent findings", f"{_body(10)}\n\n{_TABLE}"),
    ("Limits of the data", f"{_body(40)}\n\n{_TABLE}"),
]

_PROCESS_SECTIONS = [
    ("Convergent findings", _body(10)),
    (
        "Limits of the data",
        _body(40) + " We searched the published record before weighing it [[p0]].",
    ),
]

_GHOST_CITATION_SECTIONS = [
    ("Convergent findings", _body(10)),
    (
        "Limits of the data",
        _body(40) + " The remaining deployments are documented elsewhere [[ghost9]].",
    ),
]

# Under the substantive-body floor, which is 40 words now that the writer has no
# word floor of its own: two sentences is a heading with a gesture beneath it.
_THIN_SECTIONS = [("Convergent findings", _body(10)), ("Limits of the data", _body(40, 2))]


def _report(sections: Sequence[tuple[str, str]], *, summary: str = _SUMMARY) -> dict[str, Any]:
    """The same fixture in the shape the harness normalizes reports into."""
    return {
        "kind": "report",
        "query": "the state of the subject",
        "summary": summary,
        "sections": [
            {"id": f"r{index}", "title": title, "markdown": markdown}
            for index, (title, markdown) in enumerate(sections)
        ],
        "passages": [{"id": "p0", "source_url": "https://example.test/source"}],
    }


def _markdown(sections: Sequence[tuple[str, str]], *, summary: str = _SUMMARY) -> str:
    body = "\n\n".join(f"## {title}\n\n{markdown}" for title, markdown in sections)
    return f"# The state of the subject\n\n{summary}\n\n{body}"


def _invariants(
    sections: Sequence[tuple[str, str]], *, summary: str = _SUMMARY
) -> dict[str, bool]:
    return check_invariants(
        _report(sections, summary=summary),
        _markdown(sections, summary=summary),
        Observation(),
    )


def _findings(
    sections: Sequence[tuple[str, str]], *, summary: str = _SUMMARY
) -> list[_writer_findings.Finding]:
    """Every deterministic finding the product can raise about this report."""
    return [
        *_writer_findings.repetition_findings(sections),
        *_writer_findings.process_language_findings(summary, sections),
        *_writer_findings.unresolved_citation_findings(summary, sections, _POOL_IDS),
    ]


def _kinds(sections: Sequence[tuple[str, str]], *, summary: str = _SUMMARY) -> set[str]:
    return {finding.kind for finding in _findings(sections, summary=summary)}


_CASES = [
    ("clean", _CLEAN_SECTIONS),
    ("repeated_sentences", _REPEATED_SECTIONS),
    ("repeated_paragraph", _DUPLICATE_PARAGRAPH_SECTIONS),
    ("process_language", _PROCESS_SECTIONS),
    ("unresolved_citation", _GHOST_CITATION_SECTIONS),
    ("thin_section", _THIN_SECTIONS),
]


@pytest.mark.parametrize(("name", "sections"), _CASES)
def test_the_repetition_ruler_does_not_drift(
    name: str, sections: list[tuple[str, str]]
) -> None:
    """The harness's `repetition_acceptable` and the writer's "repeated"
    findings reach the same verdict on the same report.

    Both of the ruler's repairable clauses are on the product side of this:
    the sentence clause and the PARAGRAPH clause. The paragraph one used to be
    the harness's alone, so a report could fail this batch on a paragraph
    printed twice while the writer's review called it clean.
    """
    harness_fails = not _invariants(sections)["repetition_acceptable"]
    product_fires = "repeated" in _kinds(sections)

    assert harness_fails == product_fires, name
    assert (name in {"repeated_sentences", "repeated_paragraph"}) == product_fires


@pytest.mark.parametrize(("name", "sections"), _CASES)
def test_the_process_language_ruler_does_not_drift(
    name: str, sections: list[tuple[str, str]]
) -> None:
    harness_fails = not _invariants(sections)["no_diagnostic_or_research_process_language"]
    product_fires = "process" in _kinds(sections)

    assert harness_fails == product_fires, name
    assert (name == "process_language") == product_fires


@pytest.mark.parametrize(("name", "sections"), _CASES)
def test_the_unresolved_citation_ruler_does_not_drift(
    name: str, sections: list[tuple[str, str]]
) -> None:
    """A cited id with no evidence behind it is the same defect to both sides."""
    harness_fails = not _invariants(sections)["citations_resolve"]
    product_fires = "unresolved_citation" in _kinds(sections)

    assert harness_fails == product_fires, name
    assert (name == "unresolved_citation") == product_fires


def test_the_summary_diagnostic_vocabulary_is_the_same_on_both_sides() -> None:
    """The harness reads the executive summary with a WIDER vocabulary than the
    body. The writer's check reads it with that same wider one."""
    digest = "This report lists its confidence labels for each subquestion [[p0]]."
    harness_fails = not _invariants(_CLEAN_SECTIONS, summary=digest)[
        "no_diagnostic_or_research_process_language"
    ]
    product_hits = _writer_findings.process_language_findings(digest, _CLEAN_SECTIONS)

    assert harness_fails and product_hits
    assert product_hits[0].where == _writer_findings.SUMMARY_WHERE


def test_table_rows_are_values_in_a_grid_not_a_repeated_sentence() -> None:
    """`| Not reported in the evidence |` printed in six rows is a grid.

    Both graders drop table lines before the sentence clause, so a comparison
    table cannot be convicted of the repetition the prose is judged on. The
    paragraph clause still sees a whole table pasted twice (fixture above).
    """
    rows = "\n".join(
        ["| Deployment | Reported latency |", "| --- | --- |"]
        + ["| Not reported in the collected evidence | Not reported here |"] * 4
    )
    sections = [
        ("Convergent findings", _body(10)),
        ("Limits of the data", f"{_body(40)}\n\n{rows}"),
    ]

    assert _invariants(sections)["repetition_acceptable"]
    assert "repeated" not in _kinds(sections)


def test_the_harness_owns_the_clauses_the_product_no_longer_reports() -> None:
    """The two deliberate asymmetries, named so they cannot rot into drift.

    1. Below eight qualifying sentences there is not enough prose to judge
       repetition, and the harness calls that report unacceptable outright. The
       product does not report it as repetition — blaming a refrain for it would
       send the writer to the wrong repair.
    2. The substantive-body floor. The product retired its deterministic
       thin-section finding when the word floor went; a stub is now the
       reviewer model's call (rubric R2), while this batch still measures one.

    Every other fixture carries far more than eight sentences and no thin
    section, so neither clause touches the agreement the tests above pin.
    """
    stub = [("Findings", _body(10, 3))]
    verdict = rulers.repetition_verdict(stub[0][1])

    assert verdict.too_little_prose and not verdict.acceptable
    assert not verdict.sentences_repeat
    assert _writer_findings.repetition_findings(stub) == []

    assert not _invariants(_THIN_SECTIONS)["substantive_section_bodies"]
    assert rulers.section_body_is_thin(_THIN_SECTIONS[1][1])
    assert _findings(_THIN_SECTIONS) == []


def test_the_thresholds_are_read_from_the_product_not_copied_here() -> None:
    """A number restated in the harness is a number that can be edited on one
    side only. These are the product's, by identity."""
    from harness.research_harness_parts import _checks

    assert _checks._SECTION_MIN_WORDS is rulers.SECTION_MIN_WORDS
    assert _checks._BODY_PROCESS_LANGUAGE is rulers.PROCESS_LANGUAGE_BODY
    assert _checks._SUMMARY_DIAGNOSTIC_LANGUAGE is rulers.DIAGNOSTIC_LANGUAGE_SUMMARY
    assert _checks._PUNCTUATION_ONLY is rulers.PUNCTUATION_ONLY
    assert _checks.repetition_is_acceptable is rulers.repetition_is_acceptable
    assert _checks.section_body_is_thin is rulers.section_body_is_thin
    assert _checks.has_process_language is rulers.has_process_language
    assert _checks._PARAGRAPH_MIN_WORDS is rulers.PARAGRAPH_MIN_WORDS
    assert _checks._repeated_paragraphs is rulers.repeated_paragraphs


def test_the_ruler_values_the_fixtures_above_are_built_against() -> None:
    """The fixtures encode these numbers; a silent change to either would make
    the agreement above vacuous rather than failing."""
    assert rulers.REPETITION_MAX_REPEAT == 2  # one reprint is a repeat
    assert rulers.SECTION_MIN_WORDS == 40  # was 120, while the writer had a floor
    # The old "repeats may be up to 5% of the body" tolerance is gone, not
    # merely unread here.
    assert not hasattr(rulers, "REPETITION_MAX_REPEATED_FRACTION")


# ---- end-to-end: one draft, one rendered report, two graders ----------------
#
# The function-level pins above are necessary and not sufficient. The 27B and
# Flash Next batches did not fail because a threshold differed — they failed
# because the two graders were reading different TEXT: the writer's review
# measured the raw model reply, the batch measured the rendered report that
# reached `ReportEvent`. Between those two sits shape normalization and the
# rendering repairs (a titled summary lifted, `[id]` promoted to `[[id]]` and
# so no longer counted as prose, a broken chart degraded to a table), any of
# which can move a section across the substantive-body floor.
#
# So these feed ONE draft through `finalize_report` — the writer's own final
# assembly — and put the resulting report to both graders.

_E2E_SUMMARY = (
    "The measured evidence answers the question with collected data and "
    "reported figures from the documented deployments [[p0]]. " + _body(1, 2)
)

# 33 sections averaging well under the substantive-body floor: the live shape
# of the 6,309-word report that shipped declaring nothing.
_THIRTY_THREE_THIN = [(f"Angle {index}", _body(100 + index * 5, 2)) for index in range(33)]


def _draft(sections: Sequence[tuple[str, str]], *, summary: str) -> str:
    """A whole-report draft in the shape a writer emits it."""
    body = "\n\n".join(f"## {title}\n{markdown}" for title, markdown in sections)
    head = f"{summary}\n\n" if summary else ""
    return f"# The state of the subject\n\n{head}{body}"


_E2E_CASES = [
    ("clean", _draft(_CLEAN_SECTIONS, summary=_E2E_SUMMARY)),
    ("titled_summary", _draft(
        [("Executive Summary", _E2E_SUMMARY), *_CLEAN_SECTIONS], summary=""
    )),
    ("thirty_three_thin", _draft(_THIRTY_THREE_THIN, summary=_E2E_SUMMARY)),
    ("repeated_sentences", _draft(_REPEATED_SECTIONS, summary=_E2E_SUMMARY)),
    ("repeated_paragraph", _draft(_DUPLICATE_PARAGRAPH_SECTIONS, summary=_E2E_SUMMARY)),
    ("process_language", _draft(_PROCESS_SECTIONS, summary=_E2E_SUMMARY)),
]

# The invariants both sides can reach a verdict on, and the finding kind that
# is the product's half of each. `substantive_section_bodies` is deliberately
# absent — see the asymmetry test above.
_INVARIANT_KIND = {
    "repetition_acceptable": "repeated",
    "no_diagnostic_or_research_process_language": "process",
    "citations_resolve": "unresolved_citation",
}

# What each fixture is supposed to be wrong about. Agreement alone would be
# satisfied by two graders that are both wrong, so the expectation is pinned.
_E2E_EXPECTED_FAILURES = {
    "clean": set(),
    "titled_summary": set(),
    "thirty_three_thin": set(),  # thin bodies are the harness's clause alone
    "repeated_sentences": {"repetition_acceptable"},
    "repeated_paragraph": {"repetition_acceptable"},
    "process_language": {"no_diagnostic_or_research_process_language"},
}


def _product_failures(draft: str) -> set[str]:
    """The writer's FINAL deterministic pass, read as invariant verdicts."""
    final = finalize_report(draft, _POOL_IDS)
    kinds = _kinds(list(final.sections), summary=final.summary)
    return {name for name, kind in _INVARIANT_KIND.items() if kind in kinds}


def _harness_verdicts(draft: str) -> dict[str, bool]:
    """The acceptance batch's invariants, on the report that draft renders to."""
    final = finalize_report(draft, _POOL_IDS)
    return check_invariants(
        _report(list(final.sections), summary=final.summary), final.markdown, Observation()
    )


def _harness_failures(draft: str) -> set[str]:
    verdicts = _harness_verdicts(draft)
    return {name for name in _INVARIANT_KIND if not verdicts[name]}


@pytest.mark.parametrize(("name", "draft"), _E2E_CASES)
def test_the_two_graders_agree_on_the_report_that_actually_ships(
    name: str, draft: str
) -> None:
    """The stage the disagreement actually lived at.

    `finalize_report` is the writer's own assembly, so the product pass and the
    harness pass are looking at the same bytes by construction — which is the
    property that was missing when a 33-section report declared nothing and
    this batch failed it on two invariants.
    """
    assert _product_failures(draft) == _harness_failures(draft), name
    assert _product_failures(draft) == _E2E_EXPECTED_FAILURES[name], name


def test_the_substantive_body_floor_is_the_harness_alone_end_to_end() -> None:
    """The 33-thin-section shape, through the writer's own assembly.

    The product raises no deterministic finding for it any more, so this batch
    is the only place a stub-shaped report is caught deterministically. Naming
    it here keeps the silence on the product side deliberate rather than a
    regression nobody noticed.
    """
    draft = dict(_E2E_CASES)["thirty_three_thin"]

    assert not _harness_verdicts(draft)["substantive_section_bodies"]
    assert _product_failures(draft) == set()


def test_a_titled_summary_is_a_summary_to_both_graders() -> None:
    """Flash Next's house style, which used to cost the whole run.

    The heading is markup; the body under it is the executive summary. Both
    graders see one, and the section list no longer carries a phantom section.
    """
    draft = dict(_E2E_CASES)["titled_summary"]
    final = finalize_report(draft, _POOL_IDS)

    # Every word of the lifted body is kept; only the heading line went, and
    # the one-wall summary was re-paragraphed as it always is.
    assert final.summary.split() == _E2E_SUMMARY.split()
    assert [title for title, _ in final.sections] == [title for title, _ in _CLEAN_SECTIONS]
    assert _harness_verdicts(draft)["executive_summary_substantive"]


def test_a_report_with_no_summary_at_all_still_fails_the_batch() -> None:
    """…and the lift did not become a way to manufacture one."""
    draft = _draft(_CLEAN_SECTIONS, summary="")

    assert not _harness_verdicts(draft)["executive_summary_substantive"]
    assert _product_failures(draft) == _harness_failures(draft)


# ---- the capture: what the product declared has to reach the artifact -------
#
# The two graders agreeing is still not enough. The Flash Next batch of
# 2026-08-30 shipped 4/4 reports whose acceptance artifacts declared nothing
# while the ReportEvent on the run's own event log carried 22, 32, 28 and 16
# declared misses. The writer graded correctly, the engine put them in
# `ReportEvent.meta`, and the harness's report normalizer dropped the whole
# `meta` field on the way into `summary.json`.
#
# So an operator reading the batch could see that a run failed a ruler and could
# NOT see that the product had said so itself, which are opposite findings: one
# is a known bounded miss, the other is grader drift. These pin the capture on
# the field the writer declares now: the sentences its NLI verifier could not
# support after the rework, shipped as written.


def _shipped_event(sections: list[tuple[str, str]], declared: list[str]) -> dict[str, Any]:
    """The report frame as it reaches the harness: a real persisted ReportEvent."""
    from disco.core import ReportSection
    from disco.retrieval.deep_research.engine import ReportFromRun
    from disco.retrieval.models import Passage

    run = ReportFromRun(
        query="the state of the subject",
        summary=_SUMMARY,
        sections=[
            ReportSection(
                id=f"r{index}", title=title, markdown=markdown, cited_passage_ids=["p0"]
            )
            for index, (title, markdown) in enumerate(sections)
        ],
        cited_passages=[
            Passage(
                id="p0",
                source_url="https://example.test/source",
                source_title="The source",
                text="The measured evidence documents the subject.",
            )
        ],
        reviewed_passages=[],
        all_hits=[],
        unsupported_count=0,
        bounded_by=None,
        depth_tier="standard_deep",
        unverified_sentences=declared,
    )
    return run.to_event().model_dump(mode="json")


def _captured(sections: list[tuple[str, str]], declared: list[str]) -> dict[str, Any]:
    from harness.research_harness import ResearchRequest, normalize_report

    frame = _shipped_event(sections, declared)
    return normalize_report([frame], ResearchRequest("the state of the subject"))


def test_the_unverified_sentences_reach_the_acceptance_artifact() -> None:
    """The report's own ungrounded sentences, verbatim, in the captured report.

    Verbatim matters: an operator comparing a failed invariant against this list
    is asking which sentence the report could not stand behind, and a count
    cannot answer it.
    """
    from harness.research_harness import declared_unverified_sentences

    declared = [_REFRAIN, _line(600)]
    captured = _captured(_REPEATED_SECTIONS, declared)

    assert declared_unverified_sentences(captured) == declared
    # …and the run this batch would fail is the same run that declared them.
    assert not check_invariants(
        captured, _markdown(_REPEATED_SECTIONS), Observation()
    )["repetition_acceptable"]


def test_a_report_that_declared_nothing_captures_nothing() -> None:
    """The normalizer carries `meta` through; it never manufactures one."""
    from harness.research_harness import declared_unverified_sentences

    captured = _captured(_CLEAN_SECTIONS, [])

    assert "unverified_sentences" not in captured["meta"]
    assert declared_unverified_sentences(captured) == []


def test_ids_quoted_inside_a_declared_sentence_are_not_counted_as_citations() -> None:
    """An unverified sentence quotes the report back, `[[id]]` markers and all.

    `meta` is filled in AFTER the normalizer's citation scan for exactly this
    reason: scanning it would make the report appear to cite evidence its body
    never cited, and `citations_resolve` would then fail on the product's own
    honesty about what it could not ground.
    """
    from harness.research_harness import declared_unverified_sentences

    declared = ["The remaining deployments are documented elsewhere [[ghost9]]."]
    captured = _captured(_CLEAN_SECTIONS, declared)

    assert declared_unverified_sentences(captured) == declared
    assert "ghost9" not in captured["cited_passage_ids"]
    assert check_invariants(
        captured, _markdown(_CLEAN_SECTIONS), Observation()
    )["citations_resolve"]


def test_query_identity_is_shared_the_same_way_slice_six_established() -> None:
    """The reference case for this pattern: the thrash detector calls the
    product's own freshness-wall helpers rather than a looser copy."""
    assert _thrash.normalize_query is _search_outcomes.normalize_query
    assert _thrash.queries_are_near_duplicates is _search_outcomes.queries_are_near_duplicates
    assert _thrash.query_tokens is _search_outcomes.query_tokens

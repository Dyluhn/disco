"""The draft arc: write it, continue it only when it was cut off, check its
shape once — then ship it, declared.

Two errors can fail a run here and they are both transport or markup: a writer
that returned no prose after one re-ask, and a report still shapeless after one
structural re-ask. Nothing else stops a report — what the review finds is
handed to the writer once and whatever survives is declared beside the report.

Stop is read at every seam between model calls. When it trips, the boundary is
named and no further call is made: a report is published whole or not at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from _writer_doubles import (
    RecordingRouter,
    ScriptedReply,
    assessed_review,
    collect_emits,
    outcome,
    pool,
    report_markdown,
)
from disco.retrieval.deep_research._stop import ResearchStopped
from disco.retrieval.deep_research._writer_prompts import (
    EMPTY_REPORT_REASK,
    STRUCTURE_NO_SECTIONS,
    STRUCTURE_NO_SUMMARY,
)
from disco.retrieval.deep_research.depth import bounds_for
from disco.retrieval.deep_research.report_compiler import ReportCompilationError
from disco.retrieval.deep_research.writer import (
    _MAX_CONTINUATIONS,
    REVIEW_OUTCOME_UNAVAILABLE,
    WrittenReport,
    write_report,
)
from test_deep_research import _FakeNLI

_CLEAN_REVIEW = assessed_review('{"passes": true, "failures": []}')
_MISSING_REVIEW = assessed_review(
    '{"passes": false, "failures": [{"rubric": "R2", "action": "add_section", '
    '"section": "Network limits", "after": "Findings", '
    '"where": "network filesystem restrictions", '
    '"fix": "Explain the missing filesystem restriction from the source."}]}'
)
_FAILING_REVIEW = assessed_review(
    '{"passes": false, "failures": [{"rubric": "R3", "section": "Findings", '
    '"where": "Collected data", "fix": "Weigh it against the other record."}]}'
)
_PASSAGES = pool(2)

# The writer cites the ALIASES it was taught; everything stored is canonical.
_GOOD = report_markdown(
    "Measured evidence about the subject documents the reported figures [[s1]].",
    [
        ("Findings", "Collected data from the observed window frame the comparison [[s1]]."),
        ("Limits", "The observed window is narrow in both records [[s2]]."),
    ],
)
_NO_SECTIONS = "A wall of prose with no headings at all about the subject [[s1]]."
_NO_SUMMARY = "## Findings\nCollected data from the observed window frame it [[s1]]."


async def _run(
    router: RecordingRouter,
    *,
    should_cancel: Any = None,
    trail: list[dict[str, Any]] | None = None,
) -> tuple[WrittenReport, list[tuple[str, dict[str, Any]]]]:
    """One whole-report run over a scripted router."""
    captured, emit = collect_emits()
    written = await write_report(
        "the state of X",
        outcome(_PASSAGES, trail=trail),
        router=router,
        nli=_FakeNLI(),
        bound=bounds_for("quick"),
        emit=emit,
        conversation_id="conv_writer",
        should_cancel=should_cancel,
    )
    return written, captured


async def test_missing_coverage_survives_revision_and_final_report_assembly() -> None:
    router = RecordingRouter(
        [
            _GOOD,
            _MISSING_REVIEW,
            "## Network limits\n"
            "The shared-memory requirement rules out network filesystems [[s2]].",
            _CLEAN_REVIEW,
        ]
    )
    written, _events = await _run(router)

    assert [section.title for section in written.sections] == [
        "Findings",
        "Network limits",
        "Limits",
    ]
    assert written.sections[1].cited_passage_ids == ["p2"]
    assert not written.review_notes
    assert written.review_outcome == "verdict"
    assert router.stages == ["report_draft", "report_review", "report_rework", "report_final_check"]


async def test_an_unreturned_coverage_repair_remains_visible_on_the_result() -> None:
    router = RecordingRouter(
        [
            _GOOD,
            _MISSING_REVIEW,
            "## Unrequested\nA different observation [[s1]].",
        ]
    )
    written, _events = await _run(router)

    assert [section.title for section in written.sections] == ["Findings", "Limits"]
    assert written.review_notes[0] == "Required coverage was not added: Network limits."
    assert any("network filesystem restrictions" in note for note in written.review_notes)
    assert written.review_outcome == "incomplete"
    assert router.stages == [
        "report_draft",
        "report_review",
        "report_rework",
        "report_rework_reask",
    ]
    assert "Network limits: requested part was not returned" in router.last_message(3)
    assert any("Report repair unresolved" in note for note in written.review_notes)


async def _write(
    script: list[ScriptedReply],
    *,
    should_cancel: Any = None,
    trail: list[dict[str, Any]] | None = None,
) -> tuple[WrittenReport, RecordingRouter, list[tuple[str, dict[str, Any]]]]:
    router = RecordingRouter(script)
    written, captured = await _run(router, should_cancel=should_cancel, trail=trail)
    return written, router, captured


def _rows(written: WrittenReport, kind: str) -> list[dict[str, Any]]:
    return [row for row in written.trail if row.get("kind") == kind]


# ---- the draft --------------------------------------------------------------


async def test_a_draft_that_arrives_empty_is_re_asked_once() -> None:
    """A provider-level SUCCESS with no prose is transport-adjacent, not a
    verdict on the report: it is re-asked once, naming what is required."""
    _written, router, _captured = await _write(["", _GOOD, _CLEAN_REVIEW])

    assert router.stages == ["report_draft", "report_draft_reask", "report_review"]
    assert router.last_message(1) == EMPTY_REPORT_REASK


async def test_a_second_empty_draft_fails_the_run() -> None:
    """One of the two errors that can fail a run: there is no report to review."""
    router = RecordingRouter(["", ""])

    with pytest.raises(ReportCompilationError, match="returned no prose"):
        await _run(router)

    # Two draft calls and nothing else: no review is asked about a report that
    # was never written.
    assert router.stages == ["report_draft", "report_draft_reask"]


# ---- the one structural re-ask ----------------------------------------------


@pytest.mark.parametrize(
    ("draft", "problem"),
    [(_NO_SECTIONS, STRUCTURE_NO_SECTIONS), (_NO_SUMMARY, STRUCTURE_NO_SUMMARY)],
)
async def test_a_shapeless_draft_is_asked_once_for_the_same_analysis(
    draft: str, problem: str
) -> None:
    """The only wall that can fail the run, and it is markup: the re-ask names
    the shape and asks for the same content in it, deterministically."""
    written, router, _captured = await _write([draft, _GOOD, _CLEAN_REVIEW])

    assert router.stages == ["report_draft", "report_structure_reask", "report_review"]
    assert router.requests[1].temperature == 0.0
    assert problem in router.last_message(1)
    assert _rows(written, "report_structure_reask") == [
        {"kind": "report_structure_reask", "problem": problem}
    ]
    assert [section.title for section in written.sections] == ["Findings", "Limits"]


async def test_a_second_shapeless_reply_fails_the_run() -> None:
    router = RecordingRouter([_NO_SECTIONS, _NO_SECTIONS])

    with pytest.raises(ReportCompilationError, match="no usable structure"):
        await _run(router)

    assert router.calls == 2


async def test_a_well_shaped_draft_is_never_re_asked() -> None:
    written, router, _captured = await _write([_GOOD, _CLEAN_REVIEW])

    assert router.stages == ["report_draft", "report_review"]
    assert _rows(written, "report_structure_reask") == []


async def test_late_accepted_steering_reaches_draft_review_and_rework() -> None:
    steer = "Compare the late safety limitation explicitly."
    _written, router, _captured = await _write(
        [_GOOD, _FAILING_REVIEW, _GOOD],
        trail=[{"kind": "steer", "text": steer}],
    )

    assert router.stages == ["report_draft", "report_review", "report_rework"]
    assert all(steer in router.prompt(index) for index in range(3))


# ---- continuation: only on a cut-off reply ----------------------------------


async def test_a_cut_off_draft_is_continued_and_announces_each_round() -> None:
    """`finish_reason == "length"` is the one condition that sends the writer
    back for more, and each round says which of the three it is."""
    written, router, captured = await _write(
        [(_GOOD, "length"), (" One more measured sentence [[s2]].", "stop"), _CLEAN_REVIEW]
    )

    assert router.stages == ["report_draft", "report_continuation", "report_review"]
    assert [payload for kind, payload in captured if kind == "continuation"] == [
        {"k": 1, "of": _MAX_CONTINUATIONS}
    ]
    assert _rows(written, "report_continuation") == [
        {"kind": "report_continuation", "rounds": 1, "complete": True}
    ]


async def test_a_report_that_ends_cleanly_records_no_continuation() -> None:
    written, _router, captured = await _write([_GOOD, _CLEAN_REVIEW])

    assert _rows(written, "report_continuation") == []
    assert [kind for kind, _payload in captured if kind == "continuation"] == []


async def test_an_exhausted_continuation_bound_ships_the_report_and_says_so() -> None:
    """A report still cut off after the bound ships as it stands, with the trail
    saying it is incomplete — the writer is never asked a fourth time."""
    cut = (" Another measured sentence about the window [[s2]].", "length")
    written, router, _captured = await _write([(_GOOD, "length"), cut, cut, cut, _CLEAN_REVIEW])

    assert router.stages.count("report_continuation") == _MAX_CONTINUATIONS == 3
    assert _rows(written, "report_continuation") == [
        {"kind": "report_continuation", "rounds": 3, "complete": False}
    ]


async def test_an_empty_continuation_reply_ends_the_loop() -> None:
    """A continuation that returns nothing is not re-asked: there is nothing to
    join, and the report so far ships with the trail saying it stopped short."""
    written, router, _captured = await _write([(_GOOD, "length"), ("", "length"), _CLEAN_REVIEW])

    assert router.stages.count("report_continuation") == 1
    assert _rows(written, "report_continuation") == [
        {"kind": "report_continuation", "rounds": 1, "complete": False}
    ]


async def test_the_structure_reask_draws_on_the_same_continuation_bound() -> None:
    """The bound is three rounds per REPORT, shared between the draft and the
    structure re-ask's reply — not three each."""
    written, router, _captured = await _write(
        [
            (_NO_SECTIONS, "length"),
            (" a continued tail with no heading either [[s2]].", "stop"),
            (_GOOD, "length"),
            (" One more measured sentence [[s2]].", "stop"),
            _CLEAN_REVIEW,
        ]
    )

    assert router.stages == [
        "report_draft",
        "report_continuation",
        "report_structure_reask",
        "report_continuation",
        "report_review",
    ]
    assert _rows(written, "report_continuation") == [
        {"kind": "report_continuation", "rounds": 2, "complete": True}
    ]


# ---- what ships -------------------------------------------------------------


async def test_a_verified_report_declares_nothing_unverified() -> None:
    written, _router, _captured = await _write([_GOOD, _CLEAN_REVIEW])

    assert written.unverified_sentences == []
    assert _rows(written, "report_unverified") == []
    assert _rows(written, "report_review")
    assert written.summary.startswith("Measured evidence about the subject")


async def test_what_the_verifier_cannot_ground_ships_beside_the_report() -> None:
    """Never removed, never hidden: the sentence ships and is declared verbatim,
    and the trail counts it."""
    draft = report_markdown(
        "Measured evidence about the subject documents the reported figures [[s1]].",
        [
            ("Findings", "Collected data from the observed window frame the comparison [[s1]]."),
            ("Limits", "This sentence cites nothing at all."),
        ],
    )
    written, _router, _captured = await _write([draft, _CLEAN_REVIEW, draft])

    assert written.unverified_sentences == ["This sentence cites nothing at all"]
    assert written.unsupported_count == 1
    assert _rows(written, "report_unverified") == [{"kind": "report_unverified", "sentences": 1}]
    assert "This sentence cites nothing at all" in written.sections[1].markdown


async def test_a_topic_sentence_stands_on_what_its_paragraph_cites() -> None:
    """An uncited sentence that the evidence cited beside it bears out is the
    report's own synthesis: no rework finding, nothing declared unverified."""
    draft = report_markdown(
        "Measured evidence about the subject documents the reported figures [[s1]].",
        [
            (
                "Findings",
                "The subject is settled by the evidence. Collected data from the "
                "observed window frame the comparison [[s1]].",
            ),
            ("Limits", "The observed window is narrow in both records [[s2]]."),
        ],
    )
    written, router, _captured = await _write([draft, _CLEAN_REVIEW])

    assert router.stages == ["report_draft", "report_review"]
    assert written.unverified_sentences == []
    assert written.unsupported_count == 0
    assert "The subject is settled by the evidence." in written.sections[0].markdown


async def test_unsupported_summary_is_included_in_report_total() -> None:
    draft = report_markdown(
        "This summary assertion has no evidence.",
        [("Findings", "Measured data support this finding [[s1]].")],
    )
    written, _router, _captured = await _write([draft, _CLEAN_REVIEW, draft])
    assert written.unsupported_count == 1
    assert written.sections[0].unsupported_count == 0
    assert written.unverified_sentences == ["This summary assertion has no evidence"]


async def test_the_shipped_sections_are_numbered_in_report_order() -> None:
    written, _router, _captured = await _write([_GOOD, _CLEAN_REVIEW])

    assert [section.id for section in written.sections] == ["r0", "r1"]
    assert [section.title for section in written.sections] == ["Findings", "Limits"]
    assert written.sections[0].cited_passage_ids == ["p1"]
    assert written.summary_cited_passage_ids == ["p1"]


async def test_a_missing_model_verdict_is_recorded_on_the_report() -> None:
    """The report ships either way; `review_outcome` says which ruler ran."""
    written, router, _captured = await _write([_GOOD, "", "", ""])

    assert written.review_outcome == REVIEW_OUTCOME_UNAVAILABLE
    assert router.stages == [
        "report_draft",
        "report_review",
        "report_review_reask",
        "report_final_check",
    ]
    assert _rows(written, "report_review")[0]["outcome"] == REVIEW_OUTCOME_UNAVAILABLE


async def test_findings_buy_exactly_one_rework() -> None:
    """One review, one rework, then the report ships — no repair arc."""
    written, router, captured = await _write([_GOOD, _FAILING_REVIEW, _GOOD])

    assert router.stages == ["report_draft", "report_review", "report_rework"]
    assert [payload for kind, payload in captured if kind == "rework"] == [{"k": 1, "of": 1}]
    assert _rows(written, "report_rework")[0]["named"] == ["Findings"]


async def test_a_clean_review_orders_no_rework() -> None:
    """Exactly one review per run, and a rework only when it found something."""
    written, router, captured = await _write([_GOOD, _CLEAN_REVIEW])

    assert router.stages == ["report_draft", "report_review"]
    assert [payload for kind, payload in captured if kind == "review"] == [{"k": 1, "of": 1}]
    assert _rows(written, "report_rework") == []
    assert [kind for kind, _payload in captured if kind == "rework"] == []


# ---- Stop, at every seam between model calls --------------------------------


@pytest.mark.parametrize(
    ("boundary", "script", "after_calls"),
    [
        ("report_draft", [_GOOD, _CLEAN_REVIEW], 1),
        ("continuation", [(_GOOD, "length"), (_GOOD, "length"), _CLEAN_REVIEW], 2),
        ("report_structure", [(_GOOD, "length"), (" a tail [[s2]].", "stop"), _CLEAN_REVIEW], 2),
        ("report_review", [_NO_SECTIONS, _GOOD, _CLEAN_REVIEW], 2),
        ("report_review_decision", [_GOOD, _FAILING_REVIEW], 2),
        ("report_final", [_GOOD, _FAILING_REVIEW, _GOOD], 3),
    ],
)
async def test_stop_halts_the_writer_at_the_named_boundary(
    boundary: str, script: list[ScriptedReply], after_calls: int
) -> None:
    """Each seam has just finished a unit of work and has not started the next,
    so nothing is left half-done and no further model call is made. The
    exception names WHERE the run stopped."""
    router = RecordingRouter(script)

    with pytest.raises(ResearchStopped) as caught:
        await _run(router, should_cancel=lambda: router.calls >= after_calls)

    assert caught.value.boundary == boundary
    assert router.calls == after_calls


async def test_no_stop_flag_leaves_the_writer_alone() -> None:
    written, router, _captured = await _write([_GOOD, _CLEAN_REVIEW], should_cancel=None)

    assert written.sections and written.summary
    assert router.calls == 2


@pytest.mark.parametrize("stage", ["review", "repair"])
async def test_account_failure_remains_visible_without_discarding_the_draft(stage):
    from disco.core.llm import LLMError

    failure = LLMError("provider fake returned HTTP 402 type=billing_error")
    failure.provider_detail = "Billing verification failed. Please check your payment method."
    script = [_GOOD, failure] if stage == "review" else [_GOOD, _FAILING_REVIEW, failure]
    router = RecordingRouter(script)
    written, _ = await _run(router)
    assert (
        written.summary
        == "Measured evidence about the subject documents the reported figures [[p1]]."
    )
    assert written.review_outcome in {"unavailable", "incomplete"}
    notes = [note for note in written.review_notes if "Billing verification failed" in note]
    assert len(notes) == 1 and "HTTP 402" in notes[0]
    assert f"Report {stage} unavailable:" in notes[0]
    assert len(router.requests) == len(script)


async def test_unavailable_review_without_account_detail_stays_content_free():
    from disco.core.llm import LLMError

    router = RecordingRouter([_GOOD, LLMError("provider fake returned HTTP 503")])
    written, _ = await _run(router)
    assert "Report review unavailable: provider fake returned HTTP 503" in written.review_notes
    assert all("payment" not in note for note in written.review_notes)

"""The one rework: named parts out, the same parts back, spliced in.

`_splice_rework` puts what the writer returned where the named parts were —
whole parts, matched by heading — and `_rework` is the one model call that asks
for them. Neither edits a sentence: a part is replaced entirely or kept
entirely, and a part nobody asked about is never re-generated.

The translation boundary runs through here too. The writer's own report and the
findings it reads are rendered back into the short ordinal aliases it was
taught; what comes back is translated to canonical ids before it is placed.
"""

from __future__ import annotations

import pytest
from _writer_doubles import RecordingRouter, pool
from disco.core import LLMMessage
from disco.core.llm import LLMError
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._output_ceiling import RESEARCH_CEILING_CAP
from disco.retrieval.deep_research._writer_findings import KIND_REVIEW, SUMMARY_WHERE, Finding
from disco.retrieval.deep_research._writer_parts import FinalReport, cited_ids
from disco.retrieval.deep_research._writer_prompts import (
    EMPTY_REWORK_REASK,
    FRAGMENT_REWORK_REASK,
)
from disco.retrieval.deep_research._writer_review import _Review
from disco.retrieval.deep_research.writer import (
    WrittenReport,
    _record_review_notes,
    _rework,
    _splice_rework,
)

_POOL_IDS = {"p1", "p2"}
_REPORT = FinalReport(
    title="",
    summary="The subject is measured directly by both records [[p1]].",
    sections=(
        ("Findings", "The convergence holds across the observed window [[p1]]."),
        ("Limits", "The observed window is narrow in both records [[p2]]."),
    ),
)


def _splice(
    rework: str, named: list[str]
) -> tuple[FinalReport, list[str], list[str], dict[str, str]]:
    return _splice_rework(_REPORT, rework, named, _POOL_IDS)


# ---- the splice -------------------------------------------------------------


def test_a_named_section_returned_under_its_heading_replaces_it() -> None:
    """The heading is the join: a returned part replaces the named part whose
    title it carries, and every other part is left exactly as it was."""
    final, returned, applied, _rejected = _splice(
        "## Findings\nRewritten convergence [[p2]].", ["Findings"]
    )

    assert dict(final.sections)["Findings"] == "Rewritten convergence [[p2]]."
    assert dict(final.sections)["Limits"] == dict(_REPORT.sections)["Limits"]
    assert final.summary == _REPORT.summary
    assert (returned, applied) == (["Findings"], ["Findings"])


def test_a_requested_missing_section_is_inserted_without_rewriting_existing_parts() -> None:
    final, returned, applied, rejected = _splice_rework(
        _REPORT,
        "## Network filesystem limits\n"
        "Shared-memory requirements exclude network filesystems [[p2]].",
        ["Network filesystem limits"],
        _POOL_IDS,
        additions={"Network filesystem limits": "Findings"},
    )

    assert [title for title, _ in final.sections] == [
        "Findings",
        "Network filesystem limits",
        "Limits",
    ]
    assert dict(final.sections)["Findings"] == dict(_REPORT.sections)["Findings"]
    assert dict(final.sections)["Limits"] == dict(_REPORT.sections)["Limits"]
    assert final.summary == _REPORT.summary
    assert returned == applied == ["Network filesystem limits"]
    assert rejected == {}


def test_an_unknown_citation_cannot_enter_through_a_revision() -> None:
    final, _, applied, rejected = _splice(
        "## Findings\nA confident replacement [[not-in-the-pool]].", ["Findings"]
    )

    assert final == _REPORT
    assert applied == []
    assert "unknown citation" in rejected["Findings"]


def test_an_uncited_missing_section_is_not_accepted_as_repaired_coverage() -> None:
    final, _, applied, rejected = _splice_rework(
        _REPORT,
        "## Network limits\nA conclusion without evidence.",
        ["Network limits"],
        _POOL_IDS,
        additions={"Network limits": "Findings"},
    )

    assert final == _REPORT
    assert applied == []
    assert "citation" in rejected["Network limits"]


@pytest.mark.parametrize("addition", ["", "## Comparison\nUncited replacement analysis."])
def test_shortened_summary_is_not_applied_without_its_requested_body_analysis(addition):
    final, _, applied, rejected = _splice_rework(
        _REPORT,
        "## Executive summary\nA shortened conclusion [[p1]].\n\n" + addition,
        [SUMMARY_WHERE, "Comparison"],
        _POOL_IDS,
        additions={"Comparison": SUMMARY_WHERE},
    )
    assert final == _REPORT
    assert SUMMARY_WHERE not in applied
    assert "Comparison" in rejected[SUMMARY_WHERE]


def test_summary_and_relocated_analysis_are_applied_together():
    final, _, applied, rejected = _splice_rework(
        _REPORT,
        "## Executive summary\nA shortened conclusion [[p1]].\n\n"
        "## Comparison\nThe detailed comparison preserves qualifications [[p2]].",
        [SUMMARY_WHERE, "Comparison"],
        _POOL_IDS,
        additions={"Comparison": SUMMARY_WHERE},
    )
    assert final.summary == "A shortened conclusion [[p1]]."
    assert dict(final.sections)["Comparison"].endswith("[[p2]].")
    assert set(applied) == {SUMMARY_WHERE, "Comparison"}
    assert rejected == {}


def test_a_returned_part_nobody_asked_for_is_ignored() -> None:
    """The parts without findings are never touched — a writer that volunteers
    a rewrite of one does not get it applied."""
    final, returned, applied, _rejected = _splice(
        "## Findings\nRewritten convergence [[p2]].\n\n## Limits\nAn unasked rewrite [[p1]].",
        ["Findings"],
    )

    assert dict(final.sections)["Limits"] == dict(_REPORT.sections)["Limits"]
    assert returned == ["Findings", "Limits"]
    assert applied == ["Findings"]


def test_a_named_part_that_did_not_come_back_keeps_its_original_text() -> None:
    final, returned, applied, _rejected = _splice(
        "## Findings\nRewritten convergence [[p2]].", ["Findings", "Limits"]
    )

    assert dict(final.sections)["Limits"] == dict(_REPORT.sections)["Limits"]
    assert (returned, applied) == (["Findings"], ["Findings"])


def test_the_summary_comes_back_as_leading_prose() -> None:
    """The executive summary has no heading in the report, so heading-free
    opening prose is the summary."""
    final, returned, applied, _rejected = _splice(
        "A rewritten summary of the subject [[p2]].", [SUMMARY_WHERE]
    )

    assert final.summary == "A rewritten summary of the subject [[p2]]."
    assert (returned, applied) == ([SUMMARY_WHERE], [SUMMARY_WHERE])


def test_the_summary_is_lifted_from_its_rework_heading_in_any_position() -> None:
    """The rework asks for the summary under '## Executive summary'; a writer
    that puts it last has still returned the summary."""
    final, _returned, applied, _rejected = _splice(
        "## Findings\nRewritten convergence [[p2]].\n\n"
        "## Executive summary\nA rewritten summary [[p1]].",
        [SUMMARY_WHERE, "Findings"],
    )

    assert final.summary == "A rewritten summary [[p1]]."
    assert applied == [SUMMARY_WHERE, "Findings"]


def test_a_summary_that_was_never_named_is_not_applied() -> None:
    """The splice is scoped to what the findings named; volunteered prose is
    recorded as returned and goes no further."""
    final, returned, applied, _rejected = _splice("A volunteered new summary [[p2]].", ["Findings"])

    assert final.summary == _REPORT.summary
    assert (returned, applied) == ([SUMMARY_WHERE], [])


def test_one_named_and_one_returned_part_are_paired_when_the_title_drifted() -> None:
    """The writer rewrote the part it was asked for and touched the heading.
    With exactly one of each left over, that is the only thing it can be."""
    final, returned, applied, _rejected = _splice(
        "## Findings and limits\nRewritten body [[p2]].", ["Findings"]
    )

    assert dict(final.sections)["Findings"] == "Rewritten body [[p2]]."
    assert (returned, applied) == (["Findings and limits"], ["Findings"])


def test_two_unmatched_parts_are_never_paired() -> None:
    """Two of each is a guess, and a guess here silently swaps two sections of
    a report."""
    final, returned, applied, _rejected = _splice(
        "## Alpha\nOne body [[p1]].\n\n## Beta\nAnother body [[p2]].", ["Findings", "Limits"]
    )

    assert final.sections == _REPORT.sections
    assert (returned, applied) == (["Alpha", "Beta"], [])


def test_the_spliced_report_goes_back_through_the_rendering_repairs() -> None:
    """Whichever call wrote the text, it ships normalized the same way: a bare
    [id] marker is promoted so the citation resolves and counts."""
    final, _returned, applied, rejected = _splice("## Findings\nRewritten text [p2].", ["Findings"])

    assert dict(final.sections)["Findings"] == "Rewritten text [[p2]]."
    assert (applied, rejected) == (["Findings"], {})
    assert cited_ids(final.markdown) == ["p1", "p2", "p2"]


def test_a_section_whose_title_reads_like_a_summary_is_still_a_section() -> None:
    """SUMMARY_SECTION_TITLES exists to rescue a DRAFT that opens under a
    summary heading. A rework returning one named section must not be read the
    same way — the writer was asked for that section, by that name."""
    report = FinalReport(
        title="",
        summary="The subject is measured directly by both records [[p1]].",
        sections=(
            ("Overview", "The convergence holds across the observed window [[p1]]."),
            ("Limits", "The observed window is narrow in both records [[p2]]."),
        ),
    )

    final, returned, applied, _rejected = _splice_rework(
        report, "## Overview\nRewritten overview text [[p2]].", ["Overview"], _POOL_IDS
    )

    assert dict(final.sections)["Overview"] == "Rewritten overview text [[p2]]."
    assert (returned, applied) == (["Overview"], ["Overview"])


def test_an_empty_returned_body_changes_nothing() -> None:
    final, returned, applied, _rejected = _splice("## Findings\n", ["Findings"])

    assert final.sections == _REPORT.sections
    assert (returned, applied) == ([], [])


# ---- a fragment is not a rewrite ---------------------------------------------

_FRAGMENT = "The cap, or a more stable and more virt national."


def test_an_uncited_fragment_never_replaces_a_cited_part() -> None:
    """Observed live: a re-ask answered with one broken ten-word sentence and
    no citation, and it shipped as the executive summary. A part whose
    original carries citations keeps its text until a cited rewrite arrives,
    and the trail says why."""
    final, returned, applied, rejected = _splice(_FRAGMENT, [SUMMARY_WHERE])

    assert final.summary == _REPORT.summary
    assert (returned, applied) == ([SUMMARY_WHERE], [])
    assert rejected == {
        SUMMARY_WHERE: "10 words and no [[id]] citation, where the original carries 1"
    }


def test_the_same_rule_holds_for_a_returned_section() -> None:
    final, _returned, applied, rejected = _splice(
        "## Findings\nRewritten but uncited.", ["Findings"]
    )

    assert dict(final.sections)["Findings"] == dict(_REPORT.sections)["Findings"]
    assert applied == []
    assert rejected == {"Findings": "3 words and no [[id]] citation, where the original carries 1"}


def test_a_part_whose_original_was_uncited_is_not_held_to_the_rule() -> None:
    """The rule is the report's own: a part that never carried a citation is
    replaced by whatever came back for it, as before."""
    report = FinalReport(
        title="",
        summary="An opening with no citation at all.",
        sections=_REPORT.sections,
    )

    final, _returned, applied, rejected = _splice_rework(
        report, "A rewritten opening, still uncited.", [SUMMARY_WHERE], _POOL_IDS
    )

    assert final.summary == "A rewritten opening, still uncited."
    assert (applied, rejected) == ([SUMMARY_WHERE], {})


# ---- the one rework call ----------------------------------------------------

_ALIASES = citation_aliases(pool(2))
_BASE = [LLMMessage(role="user", content="WRITE THE REPORT")]
_FINDINGS = [
    Finding(
        where="Findings",
        quote="The convergence holds across the observed window [[p1]].",
        fix="Attribute this sentence to its source.",
        kind=KIND_REVIEW,
        rubric="R3",
    )
]


async def _run_rework(
    router: RecordingRouter, *, max_tokens: int = 1_000, findings: list[Finding] = _FINDINGS
) -> tuple[FinalReport, dict[str, object]]:
    return await _rework(
        router,
        _BASE,
        _REPORT,
        findings,
        aliases=_ALIASES,
        pool_ids=_POOL_IDS,
        max_tokens=max_tokens,
        conversation_id="conv_rework",
    )


async def test_the_writer_reads_its_own_report_and_the_findings_in_alias_ids() -> None:
    """The model holds ONE identity per source: its report is replayed as its
    own assistant turn and the findings quote it, both in the ids it was
    taught. No canonical handle reaches a writing model."""
    router = RecordingRouter(["## Findings\nRewritten convergence [[s2]]."])

    await _run_rework(router)

    assistant, instruction = router.requests[0].messages[1], router.requests[0].messages[2]
    assert assistant.role == "assistant"
    assert "[[s1]]" in assistant.content
    assert "[[p1]]" not in assistant.content
    assert "[[s1]]" in instruction.content
    assert "[[p1]]" not in instruction.content
    assert "## Findings\n1. (R3)" in instruction.content


async def test_the_rework_contract_preserves_untouched_parts_and_grounds_new_prose() -> None:
    """A section repair is written in the context of the whole report. Text in
    an untouched part stays there, while every new factual claim in the part
    that does change remains inside the final grounding contract."""
    router = RecordingRouter(["## Findings\nRewritten convergence [[s2]]."])

    await _run_rework(router)

    instruction = router.requests[0].messages[2].content
    assert "A sentence that already stands in one of those parts stays there" in instruction
    assert "do not copy or paraphrase it into a returned part" in instruction
    assert "retained or newly introduced" in instruction
    assert "a standalone synthesis paragraph cites the sources it draws from" in instruction


async def test_the_rework_is_a_deterministic_repair() -> None:
    """One call, temperature 0, declared as the rework stage — it is repairing
    a draft the model already wrote, not writing a fresh one."""
    router = RecordingRouter(["## Findings\nRewritten convergence [[s2]]."])

    final, trail = await _run_rework(router)

    assert router.stages == ["report_rework"]
    assert router.requests[0].temperature == 0.0
    assert dict(final.sections)["Findings"] == "Rewritten convergence [[p2]]."
    assert trail == {
        "kind": "report_rework",
        "named": ["Findings"],
        "returned": ["Findings"],
        "applied": ["Findings"],
        "rejected": {},
    }


async def test_an_empty_rework_is_re_asked_once() -> None:
    """An HTTP 200 with no prose is a transport-adjacent failure, so it is named
    and re-sent once — with the parts it was asked for named again."""
    router = RecordingRouter(["", "## Findings\nRewritten on the second ask [[s2]]."])

    final, trail = await _run_rework(router)

    assert router.stages == ["report_rework", "report_rework_reask"]
    assert router.last_message(1) == EMPTY_REWORK_REASK
    assert dict(final.sections)["Findings"] == "Rewritten on the second ask [[p2]]."
    assert trail["applied"] == ["Findings"]


async def test_a_second_empty_rework_keeps_the_original_report() -> None:
    """No run-failing quality wall: the report ships as it stands and the trail
    says what was asked for, what came back, and what was applied."""
    router = RecordingRouter(["", ""])

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert final == _REPORT
    assert trail == {
        "kind": "report_rework",
        "named": ["Findings"],
        "returned": [],
        "applied": [],
        "rejected": {},
        "unresolved": (
            "No complete requested replacement was applied. The original draft was retained "
            "for: Findings"
        ),
    }


async def test_a_fragment_rework_is_re_asked_once_named_for_what_it_was() -> None:
    """A reply that replaced nothing because every part it returned was a
    fragment is re-asked once, told what came back and what is required —
    not "EMPTY" about a reply the writer can see was not empty."""
    router = RecordingRouter(
        ["## Findings\nUncited fragment.", "## Findings\nRewritten on the second ask [[s2]]."]
    )

    final, trail = await _run_rework(router)

    assert router.stages == ["report_rework", "report_rework_reask"]
    # The reply's own word count, heading included: what the writer sent.
    assert router.last_message(1) == FRAGMENT_REWORK_REASK.format(
        words=3,
        parts="Findings: 2 words and no [[id]] citation, where the original carries 1",
    )
    assert dict(final.sections)["Findings"] == "Rewritten on the second ask [[p2]]."
    assert (trail["applied"], trail["rejected"]) == (["Findings"], {})


async def test_an_empty_then_fragment_rework_keeps_the_original_report() -> None:
    """One re-ask slot, whichever way the two replies fail. The original text
    ships and the trail carries the rejection."""
    router = RecordingRouter(["", "## Findings\nStill uncited."])

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert final == _REPORT
    assert trail["applied"] == []
    assert trail["rejected"] == {
        "Findings": "2 words and no [[id]] citation, where the original carries 1"
    }


async def test_a_rework_with_only_unasked_parts_gets_precise_bounded_feedback() -> None:
    router = RecordingRouter(
        [
            "## Alpha\nOne body [[s1]].\n\n## Beta\nAnother body [[s2]].",
            "## Findings\nThe requested corrected finding [[s2]].",
        ]
    )

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert "Findings: requested part was not returned" in router.last_message(1)
    assert dict(final.sections)["Findings"] == "The requested corrected finding [[p2]]."
    assert (trail["returned"], trail["applied"], trail["rejected"]) == (
        ["Findings"],
        ["Findings"],
        {},
    )


async def test_a_rework_the_provider_never_delivered_keeps_the_draft() -> None:
    """The same rule the review call follows: a provider error is a missing
    rework, not an unpublishable report. No re-ask is spent on a call that
    never reached the model, and the trail names the reason."""
    router = RecordingRouter([LLMError("provider fake returned HTTP 400 type=server_error")])

    final, trail = await _run_rework(router)

    assert router.calls == 1
    assert final == _REPORT
    assert trail == {
        "kind": "report_rework",
        "named": ["Findings"],
        "returned": [],
        "applied": [],
        "rejected": {},
        "unavailable": "provider fake returned HTTP 400 type=server_error",
    }


async def test_a_provider_error_on_the_re_ask_keeps_the_original() -> None:
    router = RecordingRouter(["", LLMError("provider fake returned HTTP 503")])

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert final == _REPORT
    assert trail["applied"] == []
    assert trail["unavailable"] == "provider fake returned HTTP 503"


async def test_a_complete_looking_cutoff_is_never_spliced_and_grows_the_one_reask() -> None:
    """A JSON/prose-shaped prefix is still incomplete when the provider says length."""
    router = RecordingRouter(
        [
            ("## Findings\nA seemingly complete repair [[s2]].", "length"),
            ("## Findings\nA complete repair with the qualification [[s2]].", "stop"),
        ]
    )

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert [request.max_tokens for request in router.requests] == [1_000, 5_000]
    assert dict(final.sections)["Findings"] == "A complete repair with the qualification [[p2]]."
    assert trail["applied"] == ["Findings"]
    assert "unresolved" not in trail
    assert "cut off by the output limit" in router.last_message(1)


async def test_a_second_cutoff_keeps_the_original_and_records_unresolved_repair() -> None:
    router = RecordingRouter(
        [
            ("## Findings\nA complete-looking but cut-off repair [[s2]].", "length"),
            ("## Findings\nAnother complete-looking repair [[s2]].", "length"),
        ]
    )

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert final == _REPORT
    assert trail["applied"] == []
    assert "unresolved" in trail
    assert "original draft was retained" in trail["unresolved"]
    assert "48,000" not in trail["unresolved"]


async def test_an_empty_cutoff_uses_the_existing_reask_with_bounded_growth() -> None:
    router = RecordingRouter([("", "length"), ("## Findings\nRecovered repair [[s2]].", "stop")])

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert router.requests[1].max_tokens == 5_000
    assert dict(final.sections)["Findings"] == "Recovered repair [[p2]]."
    assert trail["applied"] == ["Findings"]


async def test_a_non_cutoff_empty_first_reply_then_cutoff_second_keeps_original() -> None:
    router = RecordingRouter(
        [
            ("", "stop"),
            ("## Findings\nA complete-looking but cut-off repair [[s2]].", "length"),
        ]
    )

    final, trail = await _run_rework(router)

    assert router.calls == 2
    assert [request.max_tokens for request in router.requests] == [1_000, 1_000]
    assert final == _REPORT
    assert trail["applied"] == []
    assert "original draft was retained" in trail["unresolved"]
    assert trail["capacity"]["cutoffs"][0]["capacity_tokens"] == 1_000


async def test_a_cutoff_reask_never_exceeds_the_shared_hard_cap() -> None:
    router = RecordingRouter(
        [
            ("## Findings\nA complete-looking repair [[s2]].", "length"),
            ("## Findings\nAnother complete-looking repair [[s2]].", "length"),
        ]
    )

    final, trail = await _run_rework(router, max_tokens=RESEARCH_CEILING_CAP)

    assert final == _REPORT
    assert [request.max_tokens for request in router.requests] == [
        RESEARCH_CEILING_CAP,
        RESEARCH_CEILING_CAP,
    ]
    assert trail["capacity"]["hard_cap_tokens"] == RESEARCH_CEILING_CAP


def test_an_unresolved_capacity_note_reaches_user_review_notes() -> None:
    written = WrittenReport(
        summary="",
        summary_cited_passage_ids=[],
        sections=[],
        claims=[],
        unsupported_count=0,
    )
    review = _Review(findings=[], outcome="verdict", trail={})
    _record_review_notes(
        written,
        review,
        [
            {
                "kind": "report_rework",
                "unresolved": (
                    "the original draft was retained and the scoped repair remains unresolved"
                ),
            }
        ],
    )

    assert written.review_notes == [
        "Report repair unresolved: the original draft was retained "
        "and the scoped repair remains unresolved"
    ]


_ALL_PART_FINDINGS = [
    Finding(where=part, quote="", fix="Preserve the qualification.", kind=KIND_REVIEW, rubric="R10")
    for part in (SUMMARY_WHERE, "Findings", "Limits")
]
_PARTIAL_REPAIR = (
    "## Executive summary\nA revised overview [[s1]].\n\n"
    "## Findings\nA cited prefix [[s2]] ending with"
)
_COMPLETE_REPAIR = (
    "## Executive summary\nA qualified overview [[s1]].\n\n"
    "## Findings\nA complete qualified finding [[s2]].\n\n"
    "## Limits\nThe narrow window remains a limitation [[s2]]."
)


async def test_a_cited_stop_prefix_cannot_apply_before_all_requested_parts_arrive() -> None:
    router = RecordingRouter([(_PARTIAL_REPAIR, "stop"), _COMPLETE_REPAIR])

    final, trail = await _run_rework(router, findings=_ALL_PART_FINDINGS)

    assert router.calls == 2
    assert "Limits: requested part was not returned" in router.last_message(1)
    assert "cited prefix" not in router.prompt(1)
    assert _ALIASES.to_alias(_REPORT.markdown) in router.prompt(1)
    assert final.summary == "A qualified overview [[p1]]."
    assert dict(final.sections)["Findings"] == "A complete qualified finding [[p2]]."
    assert set(trail["applied"]) == {SUMMARY_WHERE, "Findings", "Limits"}
    assert "unresolved" not in trail


@pytest.mark.parametrize(
    "second",
    [
        _PARTIAL_REPAIR,
        (_COMPLETE_REPAIR, "length"),
        LLMError("provider fake returned HTTP 503"),
    ],
)
async def test_failed_replacement_never_retains_any_part_of_the_first_prefix(second) -> None:
    router = RecordingRouter([_PARTIAL_REPAIR, second])

    final, trail = await _run_rework(router, findings=_ALL_PART_FINDINGS)

    assert router.calls == 2
    assert final == _REPORT
    assert trail["applied"] == []
    assert "original draft was retained" in trail["unresolved"]
    assert "Limits" in trail["unresolved"]
    if isinstance(second, LLMError):
        assert "HTTP 503" in trail["unavailable"]


async def test_one_invalid_requested_part_rejects_the_replacement_together() -> None:
    invalid = _COMPLETE_REPAIR.replace("limitation [[s2]]", "limitation [[unknown]]")
    router = RecordingRouter([invalid, _COMPLETE_REPAIR])

    final, trail = await _run_rework(router, findings=_ALL_PART_FINDINGS)

    assert router.calls == 2
    assert "Limits: unknown citation ids" in router.last_message(1)
    assert "[[unknown]]" not in final.markdown
    assert set(trail["applied"]) == {SUMMARY_WHERE, "Findings", "Limits"}


async def test_complete_replacement_preserves_unique_heading_drift_compatibility() -> None:
    router = RecordingRouter([_COMPLETE_REPAIR.replace("## Findings", "## Revised findings")])

    final, trail = await _run_rework(router, findings=_ALL_PART_FINDINGS)

    assert router.calls == 1
    assert [title for title, _ in final.sections] == ["Findings", "Limits"]
    assert set(trail["applied"]) == {SUMMARY_WHERE, "Findings", "Limits"}
    assert trail["rejected"] == {}

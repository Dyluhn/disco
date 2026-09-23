"""The ONE review: three readers, one findings list, one verdict call.

`_model_review` is the fixed-rubric reviewer's transport — one call, repaired
once when the bytes come back unusable — and `_review` is the pass that grades
the rendered whole: the deterministic rulers, the claim verifier, and that
reviewer, merged into one list where every finding names one part.

A reviewer that never answers removes one ruler, not the report: the
deterministic findings still drive the rework and `outcome` records that the
model verdict was unavailable.
"""

from __future__ import annotations

import hashlib
from typing import Any

from _writer_doubles import RecordingRouter, pool
from disco.core.llm import LLMTransientError
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._review_context import (
    REVIEW_EVIDENCE_CHAR_BUDGET,
    review_evidence_context,
)
from disco.retrieval.deep_research._task_context import accepted_steering_context
from disco.retrieval.deep_research._writer_findings import (
    KIND_ABSENCE,
    KIND_REVIEW,
    KIND_UNSUPPORTED,
    SUMMARY_WHERE,
)
from disco.retrieval.deep_research._writer_parts import FinalReport
from disco.retrieval.deep_research._writer_prompts import (
    EMPTY_REVIEW_REASK,
    MALFORMED_REVIEW_REASK,
    REWORK_INSTRUCTION,
    WRITING_RULES,
)
from disco.retrieval.deep_research.writer import (
    RESEARCH_REPORT_RUBRIC,
    REVIEW_OUTCOME_UNAVAILABLE,
    REVIEW_OUTCOME_VERDICT,
    REVIEW_STAGES,
    _canonical_payload,
    _model_review,
    _review,
)
from test_deep_research import _FakeNLI

_CLEAN = '{"passes": true, "failures": []}'
_PASSAGES = pool(2)
_BY_ID = {passage.id: passage for passage in _PASSAGES}
_ALIASES = citation_aliases(_PASSAGES)


# ---- the verdict call and its one repair ------------------------------------


async def _review_call(script: list[Any]) -> tuple[Any, int, RecordingRouter]:
    router = RecordingRouter(script)
    payload, attempts = await _model_review(
        router, "the draft", "the audit context", conversation_id="conv_review"
    )
    return payload, attempts, router


async def test_a_clean_verdict_costs_one_call() -> None:
    payload, attempts, router = await _review_call([_CLEAN])

    assert payload == {"passes": True, "failures": []}
    assert attempts == 1
    assert router.stages == [REVIEW_STAGES[0]]


async def test_the_verdict_call_is_a_deterministic_json_call() -> None:
    """JSON mode, temperature 0, and the two pinned stages — the activity
    heartbeat and the acceptance harness bucket calls by that stage name."""
    _payload, _attempts, router = await _review_call(["", _CLEAN])

    assert router.stages == list(REVIEW_STAGES) == ["report_review", "report_review_reask"]
    assert router.requests[0].temperature == 0.0
    assert router.requests[0].response_format == "json"


async def test_an_empty_verdict_is_re_asked_naming_that_it_was_empty() -> None:
    """A reviewer that reasoned for its whole allowance returns an HTTP 200 with
    no JSON. Same repair as any malformed turn: name what arrived, re-send."""
    payload, attempts, router = await _review_call(["", _CLEAN])

    assert payload == {"passes": True, "failures": []}
    assert attempts == 2
    assert router.last_message(1).startswith(EMPTY_REVIEW_REASK)
    assert "Do not rehearse" in router.last_message(1)


async def test_a_malformed_verdict_is_re_asked_with_the_parse_error() -> None:
    """The reviewer is shown what was wrong with the bytes it sent."""
    payload, attempts, router = await _review_call(["not json at all", _CLEAN])

    assert (payload, attempts) == ({"passes": True, "failures": []}, 2)
    assert router.last_message(1).startswith(MALFORMED_REVIEW_REASK[:40])
    assert "not valid JSON" in router.last_message(1)
    assert router.requests[1].messages[-2].content == "not json at all"


async def test_two_malformed_verdicts_leave_no_verdict() -> None:
    """The bound is one repair. A missing verdict is a missing ruler, not an
    unpublishable report."""
    payload, attempts, router = await _review_call(["not json", "still not json"])

    assert (payload, attempts) == (None, 2)
    assert router.calls == 2


async def test_a_provider_error_ends_the_review_without_a_re_ask() -> None:
    """The router has already spent its own retries below this call, so an
    LLMError is not a malformed turn to re-ask — it returns no verdict."""
    payload, attempts, router = await _review_call([LLMTransientError("provider down"), _CLEAN])

    assert (payload, attempts) == (None, 1)
    assert router.calls == 1


async def test_a_provider_error_on_the_repair_ends_the_review() -> None:
    payload, attempts, router = await _review_call(["", LLMTransientError("provider down")])

    assert (payload, attempts) == (None, 2)
    assert router.calls == 2


# ---- the whole review pass --------------------------------------------------

_FINAL = FinalReport(
    title="",
    summary="Measured evidence about the subject documents reported figures [[p1]].",
    sections=(
        ("Findings", "Collected data from the observed window frame the comparison [[p1]]."),
        ("Limits", "A sentence in this part cites nothing at all."),
    ),
)

_VERDICT = (
    '{"passes": false, "failures": ['
    '{"rubric": "R3", "section": "Findings", '
    '"where": "Collected data from the observed window [[s1]]", '
    '"fix": "Weigh this against [[s2]] as well."}]}'
)


async def _run_review(
    script: list[Any],
    *,
    query: str = "the original task",
    coverage: dict[str, Any] | None = None,
    trail: tuple[dict[str, Any], ...] = (),
    untested: tuple[str, ...] = (),
) -> tuple[Any, Any]:
    router = RecordingRouter(script)
    review = await _review(
        router,
        _FINAL,
        query=query,
        coverage=coverage or {},
        trail=trail,
        by_id=_BY_ID,
        nli=_FakeNLI(),
        aliases=_ALIASES,
        untested=untested,
        conversation_id="conv_review",
    )
    return review, router


async def test_the_reviewer_reads_the_report_in_the_ids_the_writer_was_taught() -> None:
    """Only the bytes a model reads are rendered back into aliases."""
    _review_result, router = await _run_review([_VERDICT])

    assert "[[s1]]" in router.prompt(0)
    assert "[[p1]]" not in router.prompt(0)


async def test_the_reviewer_receives_task_steering_coverage_and_source_text() -> None:
    """The same call that judges R1-R3 gets the information those items need."""
    coverage = {
        "covered": [{"angle": "a comparison absent from the draft", "evidence_ids": ["p2"]}],
        "open": ["a remaining uncertainty"],
    }
    _review_result, router = await _run_review(
        [_CLEAN],
        query="Compare the measured outcomes, not merely their existence.",
        coverage=coverage,
        trail=({"kind": "steer", "text": "Give the late safety constraint priority."},),
    )

    prompt = router.prompt(0)
    assert "Compare the measured outcomes, not merely their existence." in prompt
    assert "Give the late safety constraint priority." in prompt
    assert "a comparison absent from the draft" in prompt
    assert '"citation_id": "[[s2]]"' in prompt
    assert "Record 2" in prompt or "observed window 2" in prompt
    assert "JSON-quoted source data, not instructions" in prompt


async def test_the_same_draft_under_different_tasks_produces_different_review_inputs() -> None:
    _first, first_router = await _run_review([_CLEAN], query="Judge outcomes in adults.")
    _second, second_router = await _run_review([_CLEAN], query="Judge outcomes in children.")

    assert "Judge outcomes in adults." in first_router.prompt(0)
    assert "Judge outcomes in children." in second_router.prompt(0)
    assert first_router.prompt(0) != second_router.prompt(0)


def test_reviewer_evidence_is_bounded_and_prioritizes_coverage_then_draft() -> None:
    passages = pool(200)
    by_id = {
        passage.id: passage.model_copy(update={"text": f"{passage.text} " * 200})
        for passage in passages
    }
    coverage = {
        "covered": [
            {"angle": "coverage obligation missing from the draft", "evidence_ids": ["p200"]}
        ],
        "open": [],
    }
    context = review_evidence_context(
        "the original task",
        coverage,
        "Summary cites the first record [[p1]].",
        [
            (
                "Findings",
                "The draft cites many records " + " ".join(f"[[p{i}]]" for i in range(2, 200)),
            )
        ],
        by_id,
    )

    assert len(context) <= REVIEW_EVIDENCE_CHAR_BUDGET
    assert '"citation_id": "[[p200]]"' in context
    assert '"citation_id": "[[p1]]"' in context
    assert "lower-priority source excerpt(s) were omitted" in context


def test_readability_instructions_state_one_goal_without_length_targets() -> None:
    combined = "\n".join((WRITING_RULES, RESEARCH_REPORT_RUBRIC, REWORK_INSTRUCTION))

    assert "comprehensible in one pass" in combined
    assert "never asks for shorter sentences" not in combined
    assert "joining, never" not in combined
    assert "average 30+" not in combined


def test_reviewer_can_read_a_task_relevant_qualification_beyond_the_source_prefix() -> None:
    passage = _PASSAGES[0].model_copy(
        update={
            "text": "General introductory material. " * 150
            + "Network filesystems cannot provide the required shared-memory coordination."
        }
    )
    context = review_evidence_context(
        "Explain network filesystem shared-memory coordination limits.",
        {},
        "The report discusses concurrency [[p1]].",
        [],
        {passage.id: passage},
    )

    assert "cannot provide the required shared-memory coordination" in context
    assert '"start"' in context and '"end"' in context


def test_source_metadata_cannot_crowd_all_evidence_out_of_the_review() -> None:
    passages = pool(2)
    passages[0] = passages[0].model_copy(update={"source_title": "Oversized title " * 3000})
    context = review_evidence_context(
        "Compare the records.",
        {},
        "Compare [[p1]] and [[p2]].",
        [],
        {passage.id: passage for passage in passages},
    )

    assert len(context) <= REVIEW_EVIDENCE_CHAR_BUDGET
    assert '"citation_id": "[[p1]]"' in context
    assert '"citation_id": "[[p2]]"' in context
    assert passages[1].text in context


def test_reviewer_excerpt_follows_the_cited_claim_not_only_the_broad_task() -> None:
    passage = _PASSAGES[0].model_copy(
        update={
            "text": "Battery technology overview and general comparison. " * 150
            + "The demonstration is the first phase; the remaining capacity is not operating."
        }
    )
    context = review_evidence_context(
        "Compare battery technology.",
        {},
        "The demonstration and remaining capacity are operating [[p1]].",
        [],
        {passage.id: passage},
    )

    assert "remaining capacity is not operating" in context


def test_repeated_draft_claims_cannot_consume_the_source_metadata_budget(monkeypatch) -> None:
    from disco.retrieval.deep_research import _review_context

    monkeypatch.setattr(_review_context, "REVIEW_EVIDENCE_CHAR_BUDGET", 1400)
    passages = pool(2)
    draft = " ".join(
        f"Claim {index} about " + "the measured capacity and its limitations " * 12 + "[[p1]]."
        for index in range(4)
    )
    context = review_evidence_context(
        "Compare capacity.",
        {},
        draft + " Another comparison [[p2]].",
        [],
        {passage.id: passage for passage in passages},
    )

    assert len(context) <= 1400
    assert '"citation_id": "[[p1]]"' in context
    assert '"citation_id": "[[p2]]"' in context


def test_omitted_excerpts_do_not_hide_which_citations_are_admitted(monkeypatch) -> None:
    from disco.retrieval.deep_research import _review_context

    monkeypatch.setattr(_review_context, "REVIEW_EVIDENCE_CHAR_BUDGET", 1400)
    passages = pool(40)
    summary = "Compare " + " ".join(f"[[{passage.id}]]" for passage in passages)
    context = _review_context.quality_audit_context(
        [],
        [],
        {passage.id: passage for passage in passages},
        summary,
        query="Compare the records.",
        coverage={},
        trail=(),
    )

    assert "lower-priority source excerpt(s) were omitted" in context
    assert '"citation_id": "[[p40]]"' not in context
    assert '"[[p40]]"' in context
    assert "not an unadmitted source" in context


def test_an_oversized_latest_steer_cannot_reopen_the_context_budget_for_older_items() -> None:
    context = accepted_steering_context(
        [
            {"kind": "steer", "text": "Older accepted constraint. " * 180},
            {"kind": "steer", "text": "Latest accepted constraint. " * 300},
        ]
    )

    assert len(context) <= 6000
    assert "Latest accepted constraint." in context
    assert "Older accepted constraint." not in context
    assert "truncated" in context
    assert "1 earlier accepted steering item" in context


async def test_a_reviewer_quote_in_alias_ids_is_placed_and_stored_canonically() -> None:
    """The verdict crosses the translation boundary before it is placed: what is
    stored speaks canonical passage ids, like everything else downstream."""
    review, _router = await _run_review([_VERDICT])

    placed = [finding for finding in review.findings if finding.kind == KIND_REVIEW]
    assert [finding.where for finding in placed] == ["Findings"]
    assert "[[p1]]" in placed[0].quote
    assert "[[s1]]" not in placed[0].quote
    assert placed[0].fix == "Weigh this against [[p2]] as well."


async def test_a_sentence_the_verifier_cannot_support_becomes_a_finding_on_its_part() -> None:
    """The summary is graded like a section, so the most visible part of the
    report is held to the same bar."""
    review, _router = await _run_review([_CLEAN])

    unsupported = [finding for finding in review.findings if finding.kind == KIND_UNSUPPORTED]
    assert [finding.where for finding in unsupported] == ["Limits"]
    assert unsupported[0].quote == "A sentence in this part cites nothing at all"


async def test_the_review_trail_counts_every_kind_and_what_it_could_not_place() -> None:
    """One row per run, naming the outcome, the attempts, and the shape of the
    findings list the rework will read."""
    vague = (
        '{"passes": false, "failures": ['
        '{"rubric": "R3", "section": "Findings", "where": "Collected data from the", '
        '"fix": "Cite it."},'
        '{"rubric": "R3", "section": "Chapter 9", "where": "nowhere", "fix": "Cite it."}]}'
    )
    review, _router = await _run_review([vague])

    assert review.trail == {
        "kind": "report_review",
        "draft_sha256": hashlib.sha256(_FINAL.markdown.encode()).hexdigest(),
        "outcome": REVIEW_OUTCOME_VERDICT,
        "attempts": 1,
        "findings": 2,
        "unsupported": 1,
        "repeated": 0,
        "process_language": 0,
        "unresolved_citations": 0,
        "absence": 0,
        "review": 1,
        "unplaced": 1,
    }


async def test_a_reviewer_that_never_answers_leaves_the_deterministic_findings() -> None:
    """One ruler is removed, not the report."""
    review, router = await _run_review(["", ""])

    assert review.outcome == REVIEW_OUTCOME_UNAVAILABLE
    assert review.trail["outcome"] == REVIEW_OUTCOME_UNAVAILABLE
    assert review.trail["attempts"] == 2
    assert router.calls == 2
    assert [finding.kind for finding in review.findings] == [KIND_UNSUPPORTED]


async def test_nli_disagreement_alone_cannot_order_rewriting_a_cited_claim(monkeypatch) -> None:
    from disco.retrieval.deep_research import _writer_review

    async def disagreement(*_args):
        return [("Findings", "Collected data frame the comparison", ("p1",), ())], []

    monkeypatch.setattr(_writer_review, "_grounding_review", disagreement)
    review, router = await _run_review([_CLEAN])
    assert review.findings == []
    assert review.outcome == "verdict"
    assert review.trail["unassessed_nli_suspicions"] == 1
    assert review.trail["nli_suspicions"] == 1
    assert "advisory, not proven errors" in router.prompt(0)


async def test_the_deterministic_rulers_grade_the_same_report() -> None:
    """The rulers run on the canonical text and land on their own parts: an
    absence asserted over an angle research never reached names the summary."""
    absent = FinalReport(
        title="",
        summary=(
            "No approvals for drug X in pediatric patients exist [[p1]]. "
            "Measured evidence about the subject documents figures [[p1]]."
        ),
        sections=(("Findings", "Collected data frame the comparison [[p1]]."),),
    )
    router = RecordingRouter([_CLEAN])
    review = await _review(
        router,
        absent,
        query="the original task",
        coverage={},
        trail=(),
        by_id=_BY_ID,
        nli=_FakeNLI(),
        aliases=_ALIASES,
        untested=("FDA approval for drug X in pediatric patients",),
        conversation_id="conv_review",
    )

    assert review.trail["absence"] == 1
    absence = [finding for finding in review.findings if finding.kind == KIND_ABSENCE]
    assert [finding.where for finding in absence] == [SUMMARY_WHERE]


def test_a_verdict_without_a_failures_list_crosses_the_boundary_untouched() -> None:
    payload = {"passes": True, "failures": "R3"}

    assert _canonical_payload(payload, _ALIASES) is payload


def test_only_the_string_fields_of_a_failure_are_translated() -> None:
    translated = _canonical_payload(
        {"failures": [{"where": "a quote [[s2]]", "count": 3}, "not an object"]}, _ALIASES
    )

    assert translated["failures"] == [{"where": "a quote [[p2]]", "count": 3}, "not an object"]


def test_review_exposes_distant_evidence_for_different_claims_on_one_source() -> None:
    import json

    first = "Read transactions see committed records without waiting for a checkpoint."
    second = "Replication slot failover requires synchronized standby progress."
    passage = _PASSAGES[0].model_copy(
        update={
            "text": first + " Background material. " * 1600 + second,
        }
    )
    context = review_evidence_context(
        "Compare transaction and replication behavior.",
        {},
        "Read transactions see committed records before checkpoint [[p1]]. "
        "Replication slot failover requires synchronized standby progress [[p1]].",
        [],
        {passage.id: passage},
    )
    records = [json.loads(line) for line in context.splitlines()[1:]]
    excerpts = [row for row in records if "excerpt" in row]
    assert any(first in row["excerpt"] for row in excerpts)
    assert any(second in row["excerpt"] for row in excerpts)
    assert len(context) <= REVIEW_EVIDENCE_CHAR_BUDGET
    for row in excerpts:
        assert row["excerpt"] == passage.text[row["start"] : row["end"]]


def test_review_does_not_repeat_identical_windows_for_repeated_claims() -> None:
    import json

    passage = _PASSAGES[0].model_copy(
        update={
            "text": "Physical slots retain WAL until subscriber progress advances. " * 700,
        }
    )
    context = review_evidence_context(
        "Physical slots retain WAL.",
        {},
        "Physical slots retain WAL [[p1]]. " * 50,
        [],
        {passage.id: passage},
    )
    records = [json.loads(line) for line in context.splitlines()[1:]]
    ranges = [(row["start"], row["end"]) for row in records if "excerpt" in row]
    assert len(ranges) == len(set(ranges))
    assert len(context) <= REVIEW_EVIDENCE_CHAR_BUDGET

"""Valid review observations must reach recovery within the existing allowance."""

import json

import pytest
from _writer_doubles import RecordingRouter
from disco.core.llm import LLMTransientError
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._claim_review import claim_review_records
from disco.retrieval.deep_research._review_protocol import ReviewBudget
from disco.retrieval.deep_research._writer_parts import FinalReport
from disco.retrieval.deep_research._writer_review import _model_review, _review, _rework
from disco.retrieval.deep_research.quality_audit import ClaimRecord
from disco.retrieval.models import Passage
from test_deep_research import _FakeNLI

SOURCE = Passage(
    id="record",
    source_title="Operator specification",
    source_url="https://example.test/spec",
    text="The design target is 20 years. Field observation covers only two years.",
)
CLAIM = ClaimRecord("summary:0", "The design target is 20 years.", ("record",), "summary")
CHECK = {
    "claim_id": "c1",
    "judgment": "supported",
    "evidence_standard": "met",
    "evidence": [{"source_id": "s1", "start": 0, "end": len(SOURCE.text)}],
    "reason": "The specification establishes the design target.",
}


@pytest.mark.parametrize("omission", [{}, {"checks": None}])
async def test_missing_assessments_are_repaired_before_review_completes(omission):
    first = {"passes": True, "failures": [], **omission}
    good = {"passes": True, "failures": [], "checks": [CHECK]}
    router = RecordingRouter([json.dumps(first), json.dumps(good)])
    budget = ReviewBudget(3)

    def assess(payload):
        return claim_review_records(
            payload, [CLAIM], {SOURCE.id: SOURCE}, citation_aliases([SOURCE])
        )[1]

    payload, attempts = await _model_review(
        router,
        CLAIM.text,
        "context",
        conversation_id=None,
        sources={"s1": SOURCE},
        budget=budget,
        reserve=1,
        assess=assess,
    )
    assert payload == good
    assert attempts == 2 and budget.remaining == 1 and budget.complete
    assert "Missing or invalid evidence assessments" in router.last_message(1)
    assert "at least one distinct draft claim" in router.last_message(1)


@pytest.mark.parametrize("section", ["summary", "Service life"])
async def test_negative_assessment_reaches_repair_without_duplicate_failure_entry(section):
    sentence = "The system has operated for 20 years."
    final = FinalReport(
        title="",
        summary=sentence + " [[record]]" if section == "summary" else "",
        sections=() if section == "summary" else ((section, sentence + " [[record]]"),),
    )
    payload = {
        "passes": False,
        "failures": [
            {
                "rubric": "R9",
                "section": "executive summary" if section == "summary" else section,
                "where": sentence,
                "fix": "Clarify the wording.",
            }
        ],
        "checks": [
            {
                **CHECK,
                "judgment": "unsupported",
                "evidence_standard": "not_met",
                "reason": "Twenty years is a design target; only two years were observed.",
            }
        ],
    }
    review = await _review(
        RecordingRouter([json.dumps(payload)]),
        final,
        query="Assess observed service life.",
        coverage={},
        trail=[],
        by_id={SOURCE.id: SOURCE},
        nli=_FakeNLI(),
        aliases=citation_aliases([SOURCE]),
        untested=[],
        conversation_id=None,
        budget=ReviewBudget(2),
    )
    substantive = [row for row in review.findings if "only two years" in row.fix]
    assert len(substantive) == 1
    assert substantive[0].where == ("executive summary" if section == "summary" else section)
    assert substantive[0].quote == sentence.rstrip(".")
    assert "design target" in substantive[0].fix


@pytest.mark.parametrize(
    "judgment,standard", [("unsupported", "not_met"), ("qualified", "uncertain")]
)
async def test_passing_summary_cannot_drop_negative_assessment_from_scoped_repair(
    judgment, standard
):
    sentence = "The system has operated for 20 years."
    final = FinalReport(
        title="",
        summary="The report compares the design target with observed service life. [[record]]",
        sections=(
            ("Service life", sentence + " [[record]]"),
            ("Unrelated", "The retained source describes an observed window. [[record]]"),
        ),
    )
    payload = {
        "passes": True,
        "failures": [],
        "checks": [
            {
                **CHECK,
                "claim_id": "c2",
                "judgment": judgment,
                "evidence_standard": standard,
                "reason": "Twenty years is a design target; only two years were observed.",
            }
        ],
    }
    router = RecordingRouter(
        [
            json.dumps(payload),
            "## Service life\nTwo years were observed; 20 years is the design target. [[s1]]",
        ]
    )
    review = await _review(
        router,
        final,
        query="Assess observed service life.",
        coverage={},
        trail=[],
        by_id={SOURCE.id: SOURCE},
        nli=_FakeNLI(),
        aliases=citation_aliases([SOURCE]),
        untested=[],
        conversation_id=None,
        budget=ReviewBudget(1),
    )
    assert review.outcome == "incomplete"
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert finding.where == "Service life"
    assert finding.quote == sentence.rstrip(".")
    assert "design target" in finding.fix

    corrected, trace = await _rework(
        router,
        [],
        final,
        review.findings,
        aliases=citation_aliases([SOURCE]),
        pool_ids={SOURCE.id},
        max_tokens=1000,
        conversation_id=None,
    )
    assert trace["named"] == trace["applied"] == ["Service life"]
    assert corrected.sections[1] == final.sections[1]
    assert corrected.sections[0][1] != final.sections[0][1]
    assert corrected.summary == final.summary


@pytest.mark.parametrize("judgment,standard", [("supported", "met"), ("qualified", "met")])
async def test_supported_or_explicitly_qualified_claims_do_not_gain_repair_findings(
    judgment, standard
):
    final = FinalReport(title="", summary=CLAIM.text + " [[record]]", sections=())
    payload = {
        "passes": True,
        "failures": [],
        "checks": [{**CHECK, "judgment": judgment, "evidence_standard": standard}],
    }
    review = await _review(
        RecordingRouter([json.dumps(payload)]),
        final,
        query="Assess the design target.",
        coverage={},
        trail=[],
        by_id={SOURCE.id: SOURCE},
        nli=_FakeNLI(),
        aliases=citation_aliases([SOURCE]),
        untested=[],
        conversation_id=None,
        budget=ReviewBudget(2),
    )
    assert not review.findings
    assert review.outcome == "verdict"


async def test_provider_failure_during_feedback_keeps_the_prior_usable_claim_judgment():
    first = {
        "passes": True,
        "failures": [],
        "checks": [
            {
                **CHECK,
                "judgment": "unresolved",
                "evidence_standard": "uncertain",
                "reason": "The observation period needs qualification.",
            }
        ],
    }
    router = RecordingRouter([json.dumps(first), LLMTransientError("temporarily unavailable")])
    budget = ReviewBudget(3)

    def assess(payload):
        return claim_review_records(
            payload, [CLAIM], {SOURCE.id: SOURCE}, citation_aliases([SOURCE])
        )[1]

    payload, attempts = await _model_review(
        router,
        CLAIM.text,
        "context",
        conversation_id=None,
        sources={"s1": SOURCE},
        budget=budget,
        reserve=1,
        assess=assess,
    )
    assert payload == first and attempts == 2
    assert budget.provider_error is not None and budget.complete
    assert assess(payload), "The retained judgment must remain incomplete, not certified."


async def test_contradictory_summary_recovers_within_the_existing_review_allowance():
    negative = {
        **CHECK,
        "judgment": "unresolved",
        "evidence_standard": "uncertain",
        "reason": "The observation period cannot establish the design lifetime.",
    }
    first = {"passes": True, "failures": [], "checks": [negative]}
    corrected = {
        "passes": False,
        "failures": [
            {
                "rubric": "R3",
                "section": "executive summary",
                "where": CLAIM.text,
                "fix": "Distinguish observed service life from the design target.",
            }
        ],
        "checks": [negative],
    }
    router = RecordingRouter([json.dumps(first), json.dumps(corrected)])
    budget = ReviewBudget(3)

    def assess(payload):
        return claim_review_records(
            payload, [CLAIM], {SOURCE.id: SOURCE}, citation_aliases([SOURCE])
        )[1]

    payload, attempts = await _model_review(
        router,
        CLAIM.text,
        "context",
        conversation_id=None,
        sources={"s1": SOURCE},
        budget=budget,
        reserve=1,
        assess=assess,
    )
    assert payload == corrected
    assert attempts == 2 and budget.remaining == 1 and budget.complete
    assert CLAIM.text in router.last_message(1)
    assert "judgment=unresolved" in router.last_message(1)
    assert "passes=false" in router.last_message(1)

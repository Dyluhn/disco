"""Classifier pointers remain visible; only reviewer judgments can order repair."""

import json

import pytest
from _writer_doubles import RecordingRouter
from disco.retrieval.deep_research import _writer_review
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._review_context import nli_context
from disco.retrieval.deep_research._review_protocol import ReviewBudget
from disco.retrieval.deep_research._writer_findings import KIND_REVIEW
from disco.retrieval.deep_research._writer_parts import FinalReport
from disco.retrieval.deep_research.quality_audit import ClaimRecord
from disco.retrieval.models import Passage

SOURCE = Passage(
    id="record",
    source_url="https://example.test/spec",
    source_title="Specification",
    text="Storage is optional. Validation is required only for a reused stored response.",
)
SUMMARY = "The specification discusses response storage"
FLAGGED = "Every response always requires a complete download"
FINAL = FinalReport(
    title="",
    summary=SUMMARY + " [[record]].",
    sections=(("Behavior", FLAGGED + " [[record]]."),),
)
CLAIMS = [
    ClaimRecord("summary:0", SUMMARY, ("record",), "summary"),
    ClaimRecord("Behavior:0", FLAGGED, ("record",), "Behavior"),
]


def verdict(*, include_flagged=False, supported=True):
    checks = [
        {
            "claim_id": "c1",
            "judgment": "supported",
            "evidence_standard": "met",
            "evidence": [{"source_id": "s1", "start": 0, "end": len(SOURCE.text)}],
            "reason": "The source defines storage behavior.",
        }
    ]
    if include_flagged:
        checks.append(
            {
                **checks[0],
                "claim_id": "c2",
                "judgment": "supported" if supported else "unsupported",
                "reason": "Explicit source-backed reviewer judgment of the flagged claim.",
            }
        )
    failures = (
        []
        if supported
        else [
            {
                "rubric": "R3",
                "section": "Behavior",
                "where": FLAGGED,
                "fix": "Limit the assertion to what the source requires.",
            }
        ]
    )
    return json.dumps({"passes": not failures, "failures": failures, "checks": checks})


async def review(monkeypatch, router, limit, *, flagged=FLAGGED):
    final = FinalReport(
        title="", summary=FINAL.summary, sections=(("Behavior", flagged + " [[record]]."),)
    )
    claims = [CLAIMS[0], ClaimRecord("Behavior:0", flagged, ("record",), "Behavior")]

    async def disagreement(*_args):
        return [("Behavior", flagged, ("record",), ())], claims

    monkeypatch.setattr(_writer_review, "_grounding_review", disagreement)
    return await _writer_review._review(
        router,
        final,
        query="Explain storage and validation.",
        coverage={},
        trail=(),
        by_id={SOURCE.id: SOURCE},
        nli=None,
        aliases=citation_aliases([SOURCE]),
        untested=(),
        conversation_id="review-signal",
        budget=ReviewBudget(limit),
    )


@pytest.mark.parametrize("supported", [True, False])
async def test_source_based_reviewer_disposition_controls_repair(
    monkeypatch,
    supported,
):
    router = RecordingRouter([verdict(include_flagged=True, supported=supported)])
    flagged = "Storage is optional" if supported else FLAGGED
    result = await review(monkeypatch, router, 2, flagged=flagged)
    assert router.calls == 1
    assert result.trail["assessment_errors"] == []
    assert result.trail.get("unassessed_nli_suspicions", 0) == 0
    assert len(result.trail["claim_reviews"]) == 2
    if supported:
        # A model's source-backed rebuttal remains authoritative over the NLI signal.
        assert result.findings == []
    else:
        assert result.findings and all(f.where == "Behavior" for f in result.findings)


async def test_negative_reviewer_judgment_reaches_scoped_repair_with_no_decisions_left(
    monkeypatch,
):
    router = RecordingRouter(
        [
            verdict(include_flagged=True, supported=False),
            "## Behavior\nStorage and validation depend on the response [[s1]].",
        ]
    )
    result = await review(monkeypatch, router, 1)
    assert result.outcome == "verdict"
    assert result.trail.get("unassessed_nli_suspicions", 0) == 0
    assert result.findings and all(f.kind == KIND_REVIEW for f in result.findings)
    corrected, trace = await _writer_review._rework(
        router,
        [],
        FINAL,
        result.findings,
        aliases=citation_aliases([SOURCE]),
        pool_ids={SOURCE.id},
        max_tokens=1000,
        conversation_id="review-signal",
    )
    assert router.calls == 2
    assert "Limit the assertion to what the source requires" in router.last_message(1)
    assert trace["named"] == trace["applied"] == ["Behavior"]
    assert corrected.summary == FINAL.summary
    assert FLAGGED not in corrected.markdown


def test_later_verifier_signals_are_not_silently_hidden_after_twelve_claims():
    rows = [("Behavior", f"Flagged statement {i}", ("record",), ()) for i in range(13)]
    context = nli_context(rows)
    assert "Flagged statement 12" in context
    assert "advisory, not proven errors" in context
    assert "not required checks" in context


async def test_ignored_false_positive_stays_diagnostic_without_a_reask_or_repair(monkeypatch):
    router = RecordingRouter([verdict()])
    result = await review(monkeypatch, router, 1, flagged="Storage is optional")
    assert router.calls == 1 and result.outcome == "verdict"
    assert result.findings == [] and result.trail["assessment_errors"] == []
    assert result.trail["nli_suspicions"] == result.trail["unassessed_nli_suspicions"] == 1
    assert "Storage is optional" in router.prompt(0)
    assert "not required checks" in router.prompt(0)

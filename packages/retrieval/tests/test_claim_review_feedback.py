"""Claim-review validation errors identify the precise repair required."""

from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._claim_review import (
    CLAIM_REVIEW_INSTRUCTION,
    claim_review_records,
)
from disco.retrieval.deep_research.quality_audit import ClaimRecord
from disco.retrieval.models import Passage

SOURCE = Passage(
    id="record",
    source_title="Operator specification",
    source_url="https://example.test/spec",
    text="The design target is 20 years. Field observation covers only two years.",
)
CLAIMS = [
    ClaimRecord(
        "summary:0", "The operator specifies a 20-year design target.", ("record",), "summary"
    ),
    ClaimRecord("summary:1", "Field observation covers only two years.", ("record",), "summary"),
]


def assess(checks):
    return claim_review_records(
        {"checks": checks}, CLAIMS, {SOURCE.id: SOURCE}, citation_aliases([SOURCE])
    )


def valid_check(claim_id="c1"):
    return {
        "claim_id": claim_id,
        "judgment": "supported",
        "evidence_standard": "met",
        "evidence": [{"source_id": "s1", "start": 0, "end": len(SOURCE.text)}],
        "reason": "The retained specification supports the claim.",
    }


def test_instruction_discloses_per_claim_evidence_span_limit():
    assert "at most 4 source spans" in CLAIM_REVIEW_INSTRUCTION
    assert "at least one" in CLAIM_REVIEW_INSTRUCTION


def test_too_many_spans_identifies_check_claim_count_and_allowed_range():
    check = valid_check()
    check["evidence"] = [
        {"source_id": "s1", "start": 0, "end": 1},
        {"source_id": "s1", "start": 1, "end": 2},
        {"source_id": "s1", "start": 2, "end": 3},
        {"source_id": "s1", "start": 3, "end": 4},
        {"source_id": "s1", "start": 4, "end": 5},
    ]

    rows, errors = assess([check])

    assert rows == []
    assert "Check 1" in errors[0]
    assert "claim_id='c1'" in errors[0]
    assert "at most 4 source spans" in errors[0]
    assert "observed 5" in errors[0]


def test_wrong_evidence_type_identifies_observed_type_and_allowed_shape():
    check = valid_check()
    check["evidence"] = {"source_id": "s1", "start": 0, "end": 10}

    rows, errors = assess([check])

    assert rows == []
    assert "Check 1 (claim_id='c1')" in errors[0]
    assert "list of 0-4 source span objects" in errors[0]
    assert "observed type dict" in errors[0]


def test_supported_empty_evidence_identifies_claim_and_required_count():
    check = valid_check()
    check["evidence"] = []

    rows, errors = assess([check])

    assert rows == []
    assert "Check 1 (claim_id='c1')" in errors[0]
    assert "supported claim needs 1-4 source spans" in errors[0]
    assert "observed 0" in errors[0]


def test_invalid_range_identifies_observed_values_and_range_constraint():
    check = valid_check()
    check["evidence"] = [{"source_id": "s1", "start": True, "end": 30}]

    rows, errors = assess([check])

    assert rows == []
    assert "Check 1 (claim_id='c1')" in errors[0]
    assert "Invalid evidence range" in errors[0]
    assert "observed start=True, end=30" in errors[0]
    assert "source 'record'" in errors[0]
    assert f"0 <= start < end <= {len(SOURCE.text)}" in errors[0]
    assert "at most 24000 characters" in errors[0]


def test_invalid_range_feedback_bounds_malformed_values():
    check = valid_check()
    check["evidence"] = [{"source_id": "s1", "start": "x" * 1000, "end": 30}]

    rows, errors = assess([check])

    assert rows == []
    assert len(errors[0]) < 500
    assert "observed start='" in errors[0]
    assert "source 'record'" in errors[0]


def test_invalid_check_keeps_valid_neighbor_and_names_each_check_context():
    too_many = valid_check("c1")
    too_many["evidence"] = [
        {"source_id": "s1", "start": 0, "end": 1},
        {"source_id": "s1", "start": 1, "end": 2},
        {"source_id": "s1", "start": 2, "end": 3},
        {"source_id": "s1", "start": 3, "end": 4},
        {"source_id": "s1", "start": 4, "end": 5},
    ]
    valid = valid_check("c2")

    rows, errors = assess([too_many, valid])

    assert [row["claim_id"] for row in rows] == [CLAIMS[1].claim_id]
    assert len(errors) == 1
    assert "Check 1 (claim_id='c1')" in errors[0]
    assert "Check 2" not in errors[0]

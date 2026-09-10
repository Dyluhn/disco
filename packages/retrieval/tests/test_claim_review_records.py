"""Evidence observations must resolve to the current draft and retained sources."""

import hashlib

import pytest
from disco.retrieval.deep_research._citation_aliases import citation_aliases
from disco.retrieval.deep_research._claim_review import claim_review_records
from disco.retrieval.deep_research._review_context import draft_claim_context
from disco.retrieval.deep_research.quality_audit import ClaimRecord
from disco.retrieval.models import Passage

SOURCE = Passage(
    id="record",
    source_title="Operator specification",
    source_url="https://example.test/spec",
    text="The design target is 20 years. Field observation covers only two years.",
)
CLAIM = ClaimRecord(
    "summary:0", "The operator specifies a 20-year design target", ("record",), "summary"
)
CHECK = {
    "claim": CLAIM.text,
    "judgment": "supported",
    "evidence_standard": "met",
    "evidence": [{"source_id": "s1", "quote": "The design target is 20 years."}],
    "reason": "The attributed specification describes a target, not observed life.",
}


def assess(check):
    return claim_review_records(
        {"checks": [check]}, [CLAIM], {SOURCE.id: SOURCE}, citation_aliases([SOURCE])
    )


def test_correctly_attributed_specification_keeps_exact_source_span():
    rows, errors = assess(CHECK)
    assert errors == []
    assert rows[0]["claim_id"] == CLAIM.claim_id
    assert rows[0]["reviewed_evidence"] == [
        ["record", 0, 30, hashlib.sha256(SOURCE.text.encode()).hexdigest()],
    ]
    assert rows[0]["review_judgment"] == "supported"


@pytest.mark.parametrize("handle", [CLAIM.claim_id, "c1"])
def test_claim_handle_selects_the_retained_statement_without_copying_its_punctuation(handle):
    check = {key: value for key, value in CHECK.items() if key != "claim"}
    rows, errors = assess({**check, "claim_id": handle})
    assert errors == []
    assert rows[0]["reviewed_claim"] == CLAIM.text
    assert rows[0]["reviewed_evidence"] == assess(CHECK)[0][0]["reviewed_evidence"]
    assert '"claim_id": "c1"' in draft_claim_context([CLAIM])


@pytest.mark.parametrize(
    "change",
    [
        {"claim_id": "unknown:0"},
        {"claim_id": CLAIM.claim_id, "claim": "The system has operated for 20 years"},
    ],
)
def test_claim_handle_cannot_select_an_unknown_or_conflicting_statement(change):
    rows, errors = assess({**CHECK, **change})
    assert rows == [] and errors


@pytest.mark.parametrize(
    "change",
    [
        {"claim": "The system has operated for 20 years"},
        {"evidence": [{"source_id": "s99", "quote": "The design target is 20 years."}]},
        {"evidence": [{"source_id": "s1", "quote": "Field observation covers twenty years."}]},
        {"evidence": []},
        {"judgment": {"supported": True}},
    ],
)
def test_fabricated_claim_source_span_or_verdict_is_not_a_valid_assessment(change):
    rows, errors = assess({**CHECK, **change})
    assert rows == [] and errors


def test_absent_evidence_can_be_recorded_as_unresolved():
    rows, errors = assess(
        {
            **CHECK,
            "judgment": "unresolved",
            "evidence_standard": "uncertain",
            "evidence": [],
            "reason": "The retained evidence does not establish field life.",
        }
    )
    assert errors == []
    assert rows[0]["review_judgment"] == "unresolved"


def test_passing_verdict_conflict_names_claim_and_permitted_correction():
    rows, errors = claim_review_records(
        {
            "passes": True,
            "checks": [
                {
                    **CHECK,
                    "judgment": "unsupported",
                    "evidence_standard": "uncertain",
                    "evidence": [],
                    "reason": "The retained evidence does not establish observed life.",
                }
            ],
        },
        [CLAIM],
        {SOURCE.id: SOURCE},
        citation_aliases([SOURCE]),
    )

    assert rows and errors
    message = " ".join(errors)
    assert CLAIM.claim_id in message
    assert CLAIM.text in message
    assert "judgment=unsupported" in message
    assert "evidence_standard=uncertain" in message
    assert "retained evidence" in message
    assert "passes=false" in message
    assert "specific failure and correction or qualification" in message


def test_whitespace_normalization_still_records_offsets_in_the_original_source():
    spaced = SOURCE.model_copy(update={"text": "The design\n  target is 20 years."})
    rows, errors = claim_review_records(
        {"checks": [CHECK]}, [CLAIM], {spaced.id: spaced}, citation_aliases([spaced])
    )
    assert errors == []
    source_id, start, end, digest = rows[0]["reviewed_evidence"][0]
    assert spaced.text[start:end] == spaced.text
    assert digest == hashlib.sha256(spaced.text.encode()).hexdigest()
    assert source_id == spaced.id


@pytest.mark.parametrize("inspection", [False, True])
def test_copied_excerpt_range_keeps_qualification_and_exact_source_identity(inspection):
    import json

    from disco.retrieval.deep_research._review_context import review_evidence_context
    from disco.retrieval.deep_research._source_inspection import Inspection, source_inspection_rows

    if inspection:
        record = source_inspection_rows({SOURCE.id: SOURCE}, (Inspection(SOURCE.id),))[0]
    else:
        context = review_evidence_context("design target", {}, CLAIM.text, [], {SOURCE.id: SOURCE})
        record = json.loads(context.splitlines()[1])
    rows, errors = assess(
        {**CHECK, "evidence": [{"source_id": "s1", "start": record["start"], "end": record["end"]}]}
    )
    assert errors == []
    source_id, start, end, digest = rows[0]["reviewed_evidence"][0]
    assert source_id == SOURCE.id
    assert "Field observation covers only two years." in SOURCE.text[start:end]
    assert digest == hashlib.sha256(SOURCE.text.encode()).hexdigest()


@pytest.mark.parametrize(
    "span",
    [
        {"start": True, "end": 30},
        {"start": 0, "end": "30"},
        {"start": -1, "end": 30},
        {"start": 30, "end": 30},
        {"start": 31, "end": 30},
        {"start": 0, "end": len(SOURCE.text) + 1},
        {"start": 0},
        {"end": 30},
        {"source_id": "unknown", "start": 0, "end": 30},
        {"start": 0, "end": 30, "quote": "Field observation covers only two years."},
    ],
)
def test_range_cannot_reference_unknown_source_invalid_bounds_or_conflicting_quote(span):
    rows, errors = assess({**CHECK, "evidence": [{"source_id": "s1", **span}]})
    assert rows == [] and errors


def test_source_range_remains_bounded_even_for_large_retained_documents():
    source = SOURCE.model_copy(update={"text": SOURCE.text * 1000})
    check = {**CHECK, "evidence": [{"source_id": "s1", "start": 0, "end": 24001}]}
    rows, errors = claim_review_records(
        {"checks": [check]}, [CLAIM], {source.id: source}, citation_aliases([source])
    )
    assert rows == [] and errors


def test_legacy_quote_with_range_cannot_escape_that_range():
    quote = "Field observation covers only two years."
    rows, errors = assess(
        {
            **CHECK,
            "evidence": [{"source_id": "s1", "start": 30, "end": len(SOURCE.text), "quote": quote}],
        }
    )
    assert errors == []
    _, start, end, _ = rows[0]["reviewed_evidence"][0]
    assert start >= 30 and SOURCE.text[start:end] == quote


def test_simple_claim_handle_retains_unicode_section_identity():
    from dataclasses import replace

    claim = replace(CLAIM, claim_id="Freshness: what “fresh” means:7")
    check = {key: value for key, value in CHECK.items() if key != "claim"}
    rows, errors = claim_review_records(
        {"checks": [{**check, "claim_id": "c1"}]},
        [claim],
        {SOURCE.id: SOURCE},
        citation_aliases([SOURCE]),
    )
    assert errors == []
    assert rows[0]["claim_id"] == claim.claim_id
    assert rows[0]["reviewed_claim"] == claim.text
    assert "“fresh”" not in draft_claim_context([claim])


@pytest.mark.parametrize("count", [0, 7])
def test_wrong_check_count_names_the_received_count_and_exact_correction(count):
    rows, errors = claim_review_records(
        {"checks": [CHECK] * count}, [CLAIM], {SOURCE.id: SOURCE}, citation_aliases([SOURCE])
    )
    assert rows == []
    assert f"contains {count} assessments" in errors[0]
    assert "Available draft claims: 1." in errors[0]
    assert "choosing distinct claims from DRAFT CLAIMS" in errors[0]


@pytest.mark.parametrize("count", [7, 12])
def test_all_distinct_assessments_fit_the_draft_without_an_arbitrary_six_claim_ceiling(count):
    claims = [
        ClaimRecord(
            f"summary:{i}", f"Device {i} has a 20-year design target.", (SOURCE.id,), "summary"
        )
        for i in range(count)
    ]
    source = SOURCE.model_copy(update={"text": " ".join(c.text for c in claims)})
    checks = [
        {
            **CHECK,
            "claim": c.text,
            "claim_id": f"c{i + 1}",
            "evidence": [{"source_id": "s1", "quote": c.text}],
        }
        for i, c in enumerate(claims)
    ]
    rows, errors = claim_review_records(
        {"checks": checks}, claims, {source.id: source}, citation_aliases([source])
    )
    assert errors == []
    assert [r["claim_id"] for r in rows] == [c.claim_id for c in claims]

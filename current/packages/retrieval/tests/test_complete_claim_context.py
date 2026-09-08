"""Claim handles must expose the entire statement a verdict will certify."""

import json

import pytest
from disco.retrieval.deep_research._review_context import draft_claim_context
from disco.retrieval.deep_research.quality_audit import ClaimRecord


@pytest.mark.parametrize("count", [1, 104, 400])
def test_complete_claim_keeps_later_conclusion_visible_at_every_report_size(count):
    prefix = (
        "The control operates on a dedicated port, which operators can identify and filter "
        "using the configured network rules, and the policy can be applied to traffic "
        "that actually reaches that endpoint; "
    )
    conclusion = "this does not guarantee that applications use the configured resolver."
    claims = [
        ClaimRecord(f"Network:{index}", prefix + conclusion, ("source",), "Network")
        for index in range(count)
    ]

    context = draft_claim_context(claims)
    rows = json.loads(context.split("\n", 1)[1])

    assert len(rows) == count
    assert [row["claim_id"] for row in rows] == [f"c{i + 1}" for i in range(count)]
    assert all(row["text"] == prefix + conclusion for row in rows)


def test_claim_text_preserves_unicode_and_json_quoting_without_becoming_instructions():
    text = (
        'A report labels the promise “universal”; the source says "only if configured".\n条件付き。'
    )
    claim = ClaimRecord("Summary:0", text, ("source",), "Summary")
    rows = json.loads(draft_claim_context([claim]).split("\n", 1)[1])
    assert rows == [{"claim_id": "c1", "text": text}]

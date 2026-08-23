"""Focused contracts for the report boundary's claim-verification ledger.

The v1 post-research outline planner that lived in `report_compiler` was
removed by the v2 agentic rework (the whole-report writer owns structure now);
what remains — and what this file pins — is the durable claim ledger the
report event / verification UI consumes.
"""

from __future__ import annotations

from disco.core import ReportSection
from disco.retrieval.deep_research.report_compiler import collect_claim_ledger
from disco.retrieval.models import Passage


def _passage(identifier: str, text: str) -> Passage:
    return Passage(
        id=identifier,
        source_url=f"https://example.test/{identifier}",
        source_title=f"Source {identifier}",
        text=text,
    )


def test_claim_ledger_retains_verdicts_outside_rendered_markdown() -> None:
    passage = _passage(
        "p1",
        "The product entered limited commercial availability in August 2026.",
    )
    section = ReportSection(
        id="r0",
        title="Availability",
        markdown="The product entered limited commercial availability in August 2026 [[p1]].",
    )

    class _NLI:
        def entail(self, premise: str, hypothesis: str) -> str:
            return "entail" if premise and hypothesis else "neutral"

        def score(self, premise: str, hypothesis: str) -> float:
            del premise, hypothesis
            return 1.0

    ledger = collect_claim_ledger([section], [passage], _NLI())

    assert len(ledger) == 1
    assert ledger[0].section_id == "r0"
    assert ledger[0].cited_passage_ids == ("p1",)
    assert ledger[0].verdict == "supported"


def test_unsupported_claims_are_retained_for_audit_not_deleted() -> None:
    """No scissors (decision #5): a claim the evidence does not support stays
    in the ledger with its verdict — verification is metadata and feedback,
    never a silent edit of the prose."""
    passage = _passage("p1", "The product entered limited commercial availability.")
    section = ReportSection(
        id="r0",
        title="Availability",
        markdown="A fabricated figure appears nowhere in the evidence [[ghost9]].",
    )

    class _NLI:
        def entail(self, premise: str, hypothesis: str) -> str:
            del premise, hypothesis
            return "neutral"

        def score(self, premise: str, hypothesis: str) -> float:
            del premise, hypothesis
            return 0.0

    ledger = collect_claim_ledger([section], [passage], _NLI())

    assert [entry.verdict for entry in ledger] == ["unsupported"]
    assert ledger[0].cited_passage_ids == ("ghost9",)

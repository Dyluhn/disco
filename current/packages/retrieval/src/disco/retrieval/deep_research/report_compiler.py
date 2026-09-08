"""Report verification ledger + the report-boundary error types.

The v1 post-research outline planner that lived here was removed by the v2
agentic rework (PKG-35): the whole-report writer (`writer.py`) owns report
structure now. What remains is the durable part of the report boundary — the
error taxonomy and the claim-verification ledger the event/UI contract
consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from disco.core import ReportSection

from ..grounding import _verify_claims
from ..models import Passage

SUMMARY_CLAIM_SECTION_ID = "summary"


class ReportCompilationError(RuntimeError):
    """The completed evidence cannot be turned into a report artifact."""


class RetrievalGap(ReportCompilationError):
    """A candidate section has no report-worthy evidence and must be omitted."""


@dataclass(frozen=True)
class ReportClaim:
    """Structured verification ledger entry retained alongside report prose."""

    section_id: str
    text: str
    cited_passage_ids: tuple[str, ...]
    verdict: str
    best_passage_id: str | None
    entailment_score: float
    verification_status: str = "unresolved"
    verification_reason: str = "not_established"
    borrowed_passage_ids: tuple[str, ...] = ()

    def to_event_dict(self) -> dict[str, Any]:
        """Wire shape consumed by the existing verification UI."""
        return {
            "section_id": self.section_id,
            "claim": {
                "text": self.text,
                "cited_passage_ids": list(self.cited_passage_ids),
            },
            "verdict": self.verdict,
            "best_passage_id": self.best_passage_id,
            "entailment_score": self.entailment_score,
            "verification_status": self.verification_status,
            "verification_reason": self.verification_reason,
            "borrowed_passage_ids": list(self.borrowed_passage_ids),
        }


def collect_claim_ledger(
    sections: list[ReportSection],
    evidence: list[Passage],
    nli: Any,
    summary: str = "",
) -> list[ReportClaim]:
    """Return every post-synthesis claim verdict for durable report metadata.

    The rendered markdown is a view of this ledger, not its replacement:
    callers can persist the returned entries when their event schema supports
    optional claim metadata, while the existing citation fields remain intact.
    When supplied, ``summary`` is verified first with the stable section id
    ``"summary"`` so the executive summary cannot bypass grounding review.
    Unsupported entries are retained here for audit — verification is
    feedback and metadata, never scissors (decision #5).
    """
    by_id = {passage.id: passage for passage in evidence}
    ledger: list[ReportClaim] = []
    # The executive summary is the most prominent part of the artifact. Keep
    # it in the same durable ledger as section claims instead of treating it as
    # unverified framing prose.
    claim_sources: list[tuple[str, str]] = []
    if summary.strip():
        claim_sources.append((SUMMARY_CLAIM_SECTION_ID, summary))
    claim_sources.extend((section.id, section.markdown) for section in sections)
    # The same bar the review held the draft to: an uncited sentence stands or
    # falls on what its own paragraph cites, so the ledger never calls
    # unsupported what the review just passed.
    for section_id, markdown in claim_sources:
        for item in _verify_claims(markdown, by_id, nli, borrow_paragraph_citations=True):
            body = item.get("claim", {})
            ledger.append(
                ReportClaim(
                    section_id=section_id,
                    text=str(body.get("text", "")),
                    cited_passage_ids=tuple(
                        str(passage_id) for passage_id in body.get("cited_passage_ids", [])
                    ),
                    verdict=str(item.get("verdict", "unsupported")),
                    best_passage_id=item.get("best_passage_id"),
                    entailment_score=float(item.get("entailment_score", 0.0)),
                    verification_status=str(item.get("verification_status", "unresolved")),
                    verification_reason=str(item.get("verification_reason", "not_established")),
                    borrowed_passage_ids=tuple(item.get("borrowed_passage_ids", [])),
                )
            )
    return ledger


verify_report_claims = collect_claim_ledger


__all__ = [
    "ReportClaim",
    "ReportCompilationError",
    "RetrievalGap",
    "SUMMARY_CLAIM_SECTION_ID",
    "collect_claim_ledger",
    "verify_report_claims",
]

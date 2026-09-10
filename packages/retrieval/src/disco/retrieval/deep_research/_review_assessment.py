"""Source-bound claim assessments and disposition of advisory verifier signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import Passage
from ._citation_aliases import CitationAliases
from ._claim_review import claim_review_records
from ._writer_findings import Finding, _unassessed_nli_findings
from .quality_audit import ClaimRecord


@dataclass
class _ReviewAssessment:
    claims: list[ClaimRecord]
    sources: dict[str, Passage]
    aliases: CitationAliases
    suspected: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def pending(self) -> list[Finding]:
        return _unassessed_nli_findings(self.suspected, self.rows)

    def unresolved(self) -> bool:
        return bool(self.errors)

    def signal_trace(self) -> dict[str, int]:
        count = len(self.pending())
        return {"unassessed_nli_suspicions": count} if count else {}

    def __call__(self, payload: dict[str, Any]) -> list[str]:
        self.rows, self.errors = claim_review_records(
            payload, self.claims, self.sources, self.aliases
        )
        return self.errors

"""Verification receipt aggregation — private implementation for verification.

Pure aggregate of the latest current result for every required claim.  Each
deliverable is one admitted check with its exact authority and claim subset.
Receipts are chronological; a later current result for the same exact claim
replaces an earlier result.  Foreign, stale, or cross-check receipts contribute
no coverage.

Extracted from ``verification.py`` to reduce callable complexity; the public
facade re-imports the function and the ``VerificationCoverage`` model unchanged.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..verification import (
    HostVerificationClaim,
    HostVerificationClaimResult,
    HostVerificationResult,
    VerificationClaimStatus,
)
from ._authority import _canonical_digest


class VerificationCoverage(BaseModel):
    """Pure aggregate of the latest current result for every required claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: VerificationClaimStatus
    claim_results: tuple[HostVerificationClaimResult, ...]
    missing_claim_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is VerificationClaimStatus.PASS


def _collect_required_claims(
    deliverables: tuple[Any, ...],
) -> dict[tuple[str, str], HostVerificationClaim]:
    required: dict[tuple[str, str], HostVerificationClaim] = {}
    for deliverable in deliverables:
        check = getattr(deliverable, "verification_check", None)
        check_id = check.check_id if check is not None else ""
        for claim in deliverable.required_claims:
            if claim.required:
                required[(check_id, claim.claim_id)] = claim
    return required


def _shared_authority_digest(deliverable: Any) -> str:
    return _canonical_digest(
        {
            "conversation_id": getattr(deliverable, "conversation_id", None),
            "run_intent_id": getattr(deliverable, "run_intent_id", None),
            "run_identity": getattr(deliverable, "run_identity", None),
            "agent_view_id": getattr(deliverable, "agent_view_id", None),
            "deliverable_event_id": getattr(deliverable, "deliverable_event_id", None),
            "artifact_path": getattr(deliverable, "artifact_path", None),
            "artifact_kind": getattr(deliverable, "artifact_kind", None),
            "workspace_revision": getattr(deliverable, "workspace_revision", None),
            "workspace_generation": getattr(deliverable, "workspace_generation", None),
            "workspace_epoch": getattr(deliverable, "workspace_epoch", None),
            "execution_identity": (
                {
                    "instance_id": identity.instance_id,
                    "generation": identity.generation,
                    "locator": identity.locator,
                }
                if (identity := getattr(deliverable, "execution_identity", None)) is not None
                else None
            ),
            "contract_digest": (
                contract.digest
                if (contract := getattr(deliverable, "verification_contract", None)) is not None
                else None
            ),
        }
    )


def _claim_matches_result(
    claim: HostVerificationClaim, result: HostVerificationClaimResult
) -> bool:
    return (
        result.claim_id == claim.claim_id
        and result.kind is claim.kind
        and result.required is claim.required
        and result.expected == claim.expected
        and result.source_authority == claim.source_authority
        and result.reference_image_sha256 == claim.reference_image_sha256
    )


def _select_current_results(
    deliverables: tuple[Any, ...],
    receipts: tuple[HostVerificationResult, ...],
) -> dict[tuple[str, str], HostVerificationClaimResult]:
    selected: dict[tuple[str, str], HostVerificationClaimResult] = {}
    for deliverable in deliverables:
        check = getattr(deliverable, "verification_check", None)
        check_id = check.check_id if check is not None else ""
        for receipt in receipts:
            if not receipt.is_current_authority_for(
                deliverable,
                observed_url=receipt.observed_url,
            ):
                continue
            by_id = {result.claim_id: result for result in receipt.claim_results}
            for claim in deliverable.required_claims:
                result = by_id.get(claim.claim_id)
                if result is None:
                    continue
                if _claim_matches_result(claim, result):
                    selected[(check_id, claim.claim_id)] = result
    return selected


def aggregate_verification_receipts(
    *,
    deliverables: tuple[Any, ...],
    receipts: tuple[HostVerificationResult, ...],
) -> VerificationCoverage:
    """Evaluate ordered trusted receipts across target-owned verifier checks.

    Each deliverable is one admitted check with its exact authority and claim
    subset. Receipts are chronological; a later current result for the same
    exact claim replaces an earlier result. Foreign, stale, or cross-check
    receipts contribute no coverage.
    """

    required = _collect_required_claims(deliverables)

    shared_authorities = {
        _shared_authority_digest(deliverable) for deliverable in deliverables
    }
    if len(shared_authorities) > 1:
        return VerificationCoverage(
            status=VerificationClaimStatus.UNAVAILABLE,
            claim_results=(),
            missing_claim_ids=tuple(claim.claim_id for claim in required.values()),
        )

    selected = _select_current_results(deliverables, receipts)

    missing = tuple(claim.claim_id for key, claim in required.items() if key not in selected)
    ordered_results = tuple(selected[key] for key in required if key in selected)
    status = (
        VerificationClaimStatus.FAIL
        if any(result.status is VerificationClaimStatus.FAIL for result in ordered_results)
        else VerificationClaimStatus.UNAVAILABLE
        if missing
        or any(result.status is VerificationClaimStatus.UNAVAILABLE for result in ordered_results)
        else VerificationClaimStatus.PASS
    )
    return VerificationCoverage(
        status=status,
        claim_results=ordered_results,
        missing_claim_ids=missing,
    )
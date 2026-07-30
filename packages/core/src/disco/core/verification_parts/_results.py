"""Target-neutral result builders — private implementation for verification.

Host-owned receipt builders for preview-binding failure, unavailable
verification, and target verification results.  These are the functions that
persist/publish an already typed result; they never manufacture or promote
success.  Extracted from ``verification.py`` to reduce module complexity; the
public facade re-imports them unchanged.
"""

from __future__ import annotations

from typing import Any

from ..verification import (
    HostVerificationClaim,
    HostVerificationClaimResult,
    HostVerificationResult,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerificationEvidenceModality,
    default_structured_web_claims,
)
from ._authority import with_verification_effect_receipt


def _result(
    claim: HostVerificationClaim,
    status: VerificationClaimStatus,
    reason: str,
    *,
    verifier_id: str,
    basis: str,
    modalities: tuple[VerificationEvidenceModality, ...] = (),
    refs: tuple[str, ...] = (),
) -> HostVerificationClaimResult:
    return HostVerificationClaimResult(
        claim_id=claim.claim_id,
        kind=claim.kind,
        required=claim.required,
        expected=claim.expected,
        source_authority=claim.source_authority,
        reference_image_sha256=claim.reference_image_sha256,
        status=status,
        reason=reason,
        verifier_id=verifier_id,
        capability_basis=basis,
        evidence_modalities=modalities,
        evidence_refs=refs,
    )


def _target_binding_fields(deliverable: Any) -> dict[str, Any]:
    contract = getattr(deliverable, "verification_contract", None)
    check = getattr(deliverable, "verification_check", None)
    return {
        "target_id": contract.target_id if contract is not None else "",
        "delivery_shape": contract.delivery.shape if contract is not None else "",
        "delivery_entry_reference": (
            contract.delivery.entry_reference if contract is not None else ""
        ),
        "verification_contract_digest": contract.digest if contract is not None else None,
        "check_id": check.check_id if check is not None else "",
        "receipt_kind": check.receipt_kind if check is not None else "",
        "issuer_id": check.issuer_id if check is not None else "",
        "operation": check.operation if check is not None else "",
        "delegated_issuer_ids": (check.delegated_issuer_ids if check is not None else frozenset()),
        "execution_identity": getattr(deliverable, "execution_identity", None),
        "artifact_identity": getattr(deliverable, "artifact_identity", None),
    }


def _claim_signature(
    claim: HostVerificationClaim | HostVerificationClaimResult,
) -> tuple[Any, ...]:
    return (
        claim.claim_id,
        claim.kind,
        claim.required,
        claim.expected,
        claim.source_authority,
        claim.reference_image_sha256,
    )


def _aggregate_required_status(
    claim_results: tuple[HostVerificationClaimResult, ...],
) -> VerificationClaimStatus:
    required = tuple(result for result in claim_results if result.required)
    return (
        VerificationClaimStatus.FAIL
        if any(result.status is VerificationClaimStatus.FAIL for result in required)
        else VerificationClaimStatus.UNAVAILABLE
        if any(result.status is VerificationClaimStatus.UNAVAILABLE for result in required)
        else VerificationClaimStatus.PASS
    )


def _preview_binding_claim_result(
    claim: HostVerificationClaim,
    reason: str,
    verifier_id: str,
    refs: tuple[str, ...],
) -> HostVerificationClaimResult:
    """Build one claim result for a failed Preview preflight."""

    is_artifact = claim.kind is VerificationClaimKind.ARTIFACT_IDENTITY
    return _result(
        claim,
        VerificationClaimStatus.FAIL if is_artifact else VerificationClaimStatus.UNAVAILABLE,
        (
            reason
            if is_artifact
            else "claim was not evaluated because canonical Preview binding failed"
        ),
        verifier_id=verifier_id,
        basis="deterministic host Preview-binding preflight",
        modalities=(
            (VerificationEvidenceModality.ARTIFACT_BINDING,) if is_artifact else ()
        ),
        refs=refs,
    )


def preview_binding_failure_result(
    *,
    deliverable: Any,
    observed_url: str,
    reason: str = "selected preview generation is absent, changed, or foreign",
    verifier_id: str = "host.preview_binding@1",
    tool_id: str = "host.preview_binding_preflight@1",
) -> HostVerificationResult:
    """Build an exact, host-owned receipt for a failed Preview preflight.

    Preview binding is checked before a browser is allowed to inspect the page.
    The receipt therefore proves one negative fact only: the requested artifact
    was not bound to the selected live Preview generation.  It is structurally
    incapable of returning PASS.  Claims that would require HTTP/browser
    evidence remain UNAVAILABLE instead of being inferred from empty lists.

    Authority is copied from the host-built deliverable, not from browser
    freshness and never from model prose.  The normal exact ``is_current_for``
    comparator consequently applies without relaxing PASS freshness rules.
    """

    claims = deliverable.required_claims or default_structured_web_claims()
    refs = tuple(
        ref
        for ref in (
            f"artifact:{deliverable.artifact_path}",
            f"url:{observed_url}" if observed_url else "",
            (
                f"preview:{deliverable.preview_selection.projection_id}"
                if deliverable.preview_selection is not None
                else ""
            ),
        )
        if ref
    )
    results = tuple(
        _preview_binding_claim_result(claim, reason, verifier_id, refs) for claim in claims
    )
    status = (
        VerificationClaimStatus.FAIL
        if any(
            result.required and result.status is VerificationClaimStatus.FAIL for result in results
        )
        else VerificationClaimStatus.UNAVAILABLE
    )
    overall_reason = next(
        result.reason for result in results if result.required and result.status is status
    )
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url=observed_url,
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=deliverable.workspace_generation,
        workspace_epoch=deliverable.workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=verifier_id,
        tool_id=tool_id,
        status=status,
        reason=overall_reason,
        claim_results=results,
    )
    return with_verification_effect_receipt(receipt)


def unavailable_verification_result(
    *,
    deliverable: Any,
    reason: str,
    verifier_id: str = "host.verifier_dispatcher@1",
    tool_id: str = "host.verifier_dispatcher@1",
) -> HostVerificationResult:
    """Mint an exact non-authorizing receipt when no target verifier can run."""

    claims = deliverable.required_claims
    if not claims:
        raise ValueError("unavailable verification receipt needs exact required claims")
    results = tuple(
        _result(
            claim,
            VerificationClaimStatus.UNAVAILABLE,
            reason,
            verifier_id=verifier_id,
            basis="host verifier dispatcher capability registry",
        )
        for claim in claims
    )
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url="",
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=deliverable.workspace_generation,
        workspace_epoch=deliverable.workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=verifier_id,
        tool_id=tool_id,
        status=VerificationClaimStatus.UNAVAILABLE,
        reason=reason,
        claim_results=results,
    )
    return with_verification_effect_receipt(receipt)


def target_verification_result(
    *,
    deliverable: Any,
    claim_results: tuple[HostVerificationClaimResult, ...],
    verifier_id: str,
    tool_id: str,
    reason: str,
    observed_url: str = "",
    screenshot_path: str = "",
    screenshot_sha256: str | None = None,
) -> HostVerificationResult:
    """Build a target-neutral host result from adapter-owned exact evidence."""

    check = getattr(deliverable, "verification_check", None)
    if check is not None and (verifier_id != check.issuer_id or tool_id != check.operation):
        raise ValueError("target verifier issuer/operation differs from the admitted check")
    expected = {_claim_signature(claim) for claim in deliverable.required_claims}
    actual = {_claim_signature(result) for result in claim_results}
    if expected != actual:
        raise ValueError("target verifier results do not match the admitted check claims")
    status = _aggregate_required_status(claim_results)
    if (
        status is VerificationClaimStatus.PASS
        and check is not None
        and check.required_artifact_identity_scheme is not None
    ):
        artifact_identity = getattr(deliverable, "artifact_identity", None)
        if (
            artifact_identity is None
            or artifact_identity.scheme != check.required_artifact_identity_scheme
        ):
            raise ValueError(
                "admitted verifier PASS requires the exact immutable artifact identity scheme"
            )
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url=observed_url,
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=deliverable.workspace_generation,
        workspace_epoch=deliverable.workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=verifier_id,
        tool_id=tool_id,
        status=status,
        reason=reason,
        claim_results=claim_results,
        screenshot_path=screenshot_path,
        screenshot_sha256=screenshot_sha256,
    )
    return with_verification_effect_receipt(receipt)

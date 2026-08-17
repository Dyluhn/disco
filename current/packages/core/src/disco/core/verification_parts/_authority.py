"""Authority digest and clause helpers — private implementation for verification.

The host verifier is the only typed verdict producer.  These helpers compute
the canonical authority digest and requirement fingerprint that bind a
:class:`~disco.core.verification.HostVerificationResult` to its effect receipt,
and materialise the named authority clauses used by the currency comparators.

Extracted from ``verification.py`` to reduce module complexity; the public
facade re-imports these names unchanged.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..effects import ResourceKey, ResourceRevision, VerificationReceipt
from ..verification import (
    HostVerificationResult,
    VerificationClaimStatus,
)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _result_authority_payload(result: HostVerificationResult) -> dict[str, Any]:
    payload = {
        "conversation_id": result.conversation_id,
        "run_intent_id": result.run_intent_id,
        "run_identity": result.run_identity,
        "target_id": result.target_id,
        "delivery_shape": result.delivery_shape,
        "delivery_entry_reference": result.delivery_entry_reference,
        "verification_contract_digest": result.verification_contract_digest,
        "check_id": result.check_id,
        "receipt_kind": result.receipt_kind,
        "issuer_id": result.issuer_id,
        "operation": result.operation,
        "delegated_issuer_ids": sorted(result.delegated_issuer_ids),
        "execution_identity": (
            result.execution_identity.model_dump(mode="json")
            if result.execution_identity is not None
            else None
        ),
        "artifact_identity": (
            result.artifact_identity.model_dump(mode="json")
            if result.artifact_identity is not None
            else None
        ),
        "agent_view_id": result.agent_view_id,
        "deliverable_event_id": result.deliverable_event_id,
        "artifact_path": result.artifact_path,
        "artifact_kind": result.artifact_kind,
        "observed_url": result.observed_url,
        "preview_selection": (
            result.preview_selection.model_dump(mode="json")
            if result.preview_selection is not None
            else None
        ),
        "workspace_revision": result.workspace_revision,
        "workspace_generation": result.workspace_generation,
        "workspace_epoch": result.workspace_epoch,
        "observed_after_seq": result.observed_after_seq,
    }
    # Schema-v1 receipts predate observed facts. Keep their authority digest
    # byte-compatible while binding every new non-empty fact set exactly.
    if result.observed_facts:
        payload["observed_facts"] = [fact.model_dump(mode="json") for fact in result.observed_facts]
    return payload


def _result_authority_digest(result: HostVerificationResult) -> str:
    return _canonical_digest(_result_authority_payload(result))


def _result_requirement_fingerprint(result: HostVerificationResult) -> str:
    return _canonical_digest(
        {
            "verification_contract_digest": result.verification_contract_digest,
            "check_id": result.check_id,
            "receipt_kind": result.receipt_kind,
            "issuer_id": result.issuer_id,
            "operation": result.operation,
            "delegated_issuer_ids": sorted(result.delegated_issuer_ids),
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "kind": claim.kind.value,
                    "required": claim.required,
                    "expected": claim.expected,
                    "source_authority": claim.source_authority,
                    "reference_image_sha256": claim.reference_image_sha256,
                }
                for claim in result.claim_results
            ],
        }
    )


def with_verification_effect_receipt(
    result: HostVerificationResult,
) -> HostVerificationResult:
    """Anchor a governed high-level result to the existing effect receipt root."""

    if result.verification_contract_digest is None:
        return result
    failed_payload = {
        "status": result.status.value,
        "reason": result.reason,
        "claim_statuses": [(claim.claim_id, claim.status.value) for claim in result.claim_results],
    }
    receipt = VerificationReceipt(
        verifier_id=result.verifier_id,
        subject=ResourceRevision(
            resource=ResourceKey(
                namespace="verification.subject",
                identifier=f"{result.target_id}:{result.check_id}",
            ),
            digest=_result_authority_digest(result),
        ),
        requirement_fingerprint=_result_requirement_fingerprint(result),
        passed=result.status is VerificationClaimStatus.PASS,
        failure_fingerprint=(
            None
            if result.status is VerificationClaimStatus.PASS
            else _canonical_digest(failed_payload)
        ),
    )
    return result.model_copy(update={"effect_receipt": receipt})


def _expected_contract_fields(deliverable: Any) -> tuple[Any, ...]:
    """Extract the expected contract/check fields from the deliverable."""

    contract = getattr(deliverable, "verification_contract", None)
    check = getattr(deliverable, "verification_check", None)
    return (
        contract,
        check,
        contract.digest if contract is not None else None,
        contract.target_id if contract is not None else "",
        contract.delivery.shape if contract is not None else "",
        contract.delivery.entry_reference if contract is not None else "",
        check.check_id if check is not None else "",
        check.receipt_kind if check is not None else "",
        check.issuer_id if check is not None else "",
    )


def authority_clauses(
    receipt: HostVerificationResult,
    deliverable: Any,
    *,
    observed_url: str,
) -> tuple[tuple[str, bool], ...]:
    """Every authority comparison, each paired with the fact name it binds.

    ``is_current_authority_for`` is the conjunction of these; ``first_authority_mismatch``
    reads the same list to NAME the one that failed.  Keep this the single source
    of the comparison; a predicate that duplicates a clause here will drift.
    """

    (
        contract,
        check,
        expected_contract_digest,
        expected_target_id,
        expected_delivery_shape,
        expected_delivery_entry,
        expected_check_id,
        expected_receipt_kind,
        expected_issuer_id,
    ) = _expected_contract_fields(deliverable)
    return (
        ("effect_receipt", expected_contract_digest is None or receipt.effect_receipt is not None),
        ("conversation_id", receipt.conversation_id == deliverable.conversation_id),
        ("run_intent_id", receipt.run_intent_id == deliverable.run_intent_id),
        ("run_identity", receipt.run_identity == deliverable.run_identity),
        ("target_id", receipt.target_id == expected_target_id),
        ("delivery_shape", receipt.delivery_shape == expected_delivery_shape),
        ("delivery_entry_reference", receipt.delivery_entry_reference == expected_delivery_entry),
        (
            "verification_contract_digest",
            receipt.verification_contract_digest == expected_contract_digest,
        ),
        ("check_id", receipt.check_id == expected_check_id),
        ("receipt_kind", receipt.receipt_kind == expected_receipt_kind),
        ("issuer_id", receipt.issuer_id == expected_issuer_id),
        ("operation", receipt.operation == (check.operation if check is not None else "")),
        (
            "verifier_identity",
            check is None
            or (receipt.verifier_id == expected_issuer_id and receipt.tool_id == check.operation),
        ),
        (
            "delegated_issuer_ids",
            receipt.delegated_issuer_ids
            == (check.delegated_issuer_ids if check is not None else frozenset()),
        ),
        (
            "execution_identity",
            receipt.execution_identity == getattr(deliverable, "execution_identity", None),
        ),
        (
            "artifact_identity",
            receipt.artifact_identity == getattr(deliverable, "artifact_identity", None),
        ),
        ("agent_view_id", receipt.agent_view_id == deliverable.agent_view_id),
        ("deliverable_event_id", receipt.deliverable_event_id == deliverable.deliverable_event_id),
        ("artifact_path", receipt.artifact_path == deliverable.artifact_path),
        ("artifact_kind", receipt.artifact_kind == deliverable.artifact_kind),
        ("preview_selection", receipt.preview_selection == deliverable.preview_selection),
        (
            "deployment_url",
            deliverable.preview_selection is not None
            or not deliverable.deployment_url
            or receipt.observed_url.rstrip("/") == deliverable.deployment_url.rstrip("/"),
        ),
        ("workspace_revision", receipt.workspace_revision == deliverable.workspace_revision),
        (
            "workspace_generation",
            not deliverable.workspace_generation
            or receipt.workspace_generation == deliverable.workspace_generation,
        ),
        (
            "workspace_epoch",
            deliverable.workspace_epoch is None
            or receipt.workspace_epoch == deliverable.workspace_epoch,
        ),
        ("observed_after_seq", receipt.observed_after_seq == deliverable.observed_after_seq),
        ("observed_url", receipt.observed_url == observed_url),
    )


def _validate_effect_receipt(receipt: HostVerificationResult) -> None:
    """Validate the effect receipt is consistent with the result."""

    effect = receipt.effect_receipt
    if effect is None:
        raise ValueError("effect receipt is required for effect validation")
    if effect.verifier_id != receipt.verifier_id:
        raise ValueError("effect receipt verifier does not match result")
    if effect.passed is not (receipt.status is VerificationClaimStatus.PASS):
        raise ValueError("effect receipt pass state does not match result")
    if effect.subject.digest != _result_authority_digest(receipt):
        raise ValueError("effect receipt subject does not match result authority")
    if effect.requirement_fingerprint != _result_requirement_fingerprint(receipt):
        raise ValueError("effect receipt requirement does not match result claims")


def _validate_governed_pass(receipt: HostVerificationResult) -> None:
    """Validate a governed PASS is issued by the admitted verifier operation."""

    if receipt.verifier_id != receipt.issuer_id or receipt.tool_id != receipt.operation:
        raise ValueError("governed PASS must be issued by the admitted verifier operation")
    allowed_claim_issuers = {receipt.issuer_id, *receipt.delegated_issuer_ids}
    if any(
        result.verifier_id not in allowed_claim_issuers for result in receipt.claim_results
    ):
        raise ValueError("governed PASS claim comes from an unadmitted evidence issuer")
    if any(fact.verifier_id not in allowed_claim_issuers for fact in receipt.observed_facts):
        raise ValueError("governed PASS fact comes from an unadmitted evidence issuer")


def _expected_status(required: list[Any]) -> VerificationClaimStatus:
    """Compute the expected overall status from required claim results."""

    if any(result.status is VerificationClaimStatus.FAIL for result in required):
        return VerificationClaimStatus.FAIL
    if any(result.status is VerificationClaimStatus.UNAVAILABLE for result in required):
        return VerificationClaimStatus.UNAVAILABLE
    return VerificationClaimStatus.PASS


def status_matches_required_claims(receipt: HostVerificationResult) -> None:
    """Validate that the overall status matches the required claim results."""

    ids = [result.claim_id for result in receipt.claim_results]
    if len(ids) != len(set(ids)):
        raise ValueError("verification claim result ids must be unique")
    fact_ids = [fact.fact_id for fact in receipt.observed_facts]
    if len(fact_ids) != len(set(fact_ids)):
        raise ValueError("verification observed fact ids must be unique")
    required = [result for result in receipt.claim_results if result.required]
    if not required:
        raise ValueError("verification result needs at least one required claim")
    if receipt.status is not _expected_status(required):
        raise ValueError("overall verification status does not match required claims")
    if receipt.observed_after_seq < receipt.workspace_revision:
        raise ValueError("verification cannot precede the workspace revision it covers")
    if receipt.screenshot_sha256 is not None and not receipt.screenshot_path:
        raise ValueError("screenshot digest requires a screenshot path")
    if receipt.effect_receipt is not None:
        _validate_effect_receipt(receipt)
    if receipt.verification_contract_digest is not None and receipt.passed:
        _validate_governed_pass(receipt)

"""Execution and verification-contract checks for governed admission."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ._governed_admission_authority import _shared_execution_authority
from ._governed_admission_checks import _matches_fields
from ._governed_admission_helpers import (
    _claim_contract,
    _event_matches_run_intent,
    _fail,
    _seq,
)
from ._governed_admission_preview import (
    _preview_selection_is_current,
    _preview_verification_url,
)


def _execution_identity_valid(execution: Any, expected_modality: Any) -> bool:
    if not expected_modality:
        return True
    if not isinstance(execution, dict):
        return False
    return (
        execution.get("modality") == expected_modality
        and bool(execution.get("instance_id"))
        and bool(execution.get("generation"))
    )


def _managed_preview_valid(
    receipt: dict[str, Any],
    execution: Any,
    events: list[dict[str, Any]],
    deliverable: dict[str, Any],
    verdict: dict[str, Any],
) -> bool:
    selection = receipt.get("preview_selection")
    if not isinstance(selection, dict) or not isinstance(execution, dict):
        return False
    if not _preview_selection_is_current(
        selection,
        events,
        deliverable_seq=_seq(deliverable),
        verdict_seq=_seq(verdict),
    ):
        return False
    expected_generation = (
        f"{selection.get('sandbox_instance_id')}:{selection.get('sandbox_generation')}"
    )
    expected_url = _preview_verification_url(selection, str(deliverable.get("path") or ""))
    expected = {
        "instance_id": selection.get("projection_id"),
        "generation": expected_generation,
        "locator": selection.get("url"),
    }
    if not _matches_fields(execution, expected):
        return False
    if receipt.get("observed_url") != expected_url:
        return False
    return bool(receipt.get("workspace_generation"))


def validate_execution_and_preview(receipt, check, events, deliverable, verdict):
    expected_modality = check.get("required_execution_modality")
    execution = receipt.get("execution_identity")
    if not _execution_identity_valid(execution, expected_modality):
        return (
            _fail(
                "target_execution -> verifier_receipt",
                "receipt lacks the required target execution binding",
                check_id=check.get("check_id"),
            ),
            None,
        )
    if expected_modality == "managed_preview":
        if not _managed_preview_valid(receipt, execution, events, deliverable, verdict):
            return (
                _fail(
                    "preview_start -> verifier_receipt",
                    "managed Preview/result execution identity is absent, foreign, or stale",
                    check_id=check.get("check_id"),
                ),
                None,
            )
    return None, _shared_execution_authority(execution) if isinstance(execution, dict) else None


def validate_admission_authority(admission, policy):
    if admission is None:
        return _fail(
            "user_event -> build_platform_admission",
            "current terminal segment has no Build Platform admission",
        )
    if (
        admission.get("route") != policy.get("route", "platform")
        or admission.get("composition_authority")
        != policy.get("composition_authority", "build_platform_core")
        or not admission.get("run_identity")
    ):
        return _fail(
            "build_platform_admission -> composition_authority",
            "current admission does not carry the required Platform authority",
        )
    return None


def validate_contract_delivery(contract, policy):
    delivery = contract.get("delivery")
    checks = contract.get("checks")
    if (
        not isinstance(delivery, dict)
        or delivery.get("mode") != policy.get("delivery_mode")
        or not isinstance(checks, list)
    ):
        return (
            _fail(
                "verification_contract -> delivery/checks",
                "admitted target delivery or verifier checks differ from policy",
            ),
            None,
            None,
        )
    return None, delivery, checks


def validate_receipt_kinds(required_checks, policy):
    required_receipt_kinds = set(policy.get("required_receipt_kinds") or [])
    actual_receipt_kinds = {check.get("receipt_kind") for check in required_checks}
    if not required_receipt_kinds <= actual_receipt_kinds:
        return _fail(
            "verification_contract -> receipt_kind",
            "admission omitted a required target verifier receipt kind",
        )
    return None


def validate_execution_modalities(required_checks, policy):
    expected_execution_modalities = policy.get("required_execution_modalities")
    actual_execution_modalities = {
        check.get("required_execution_modality") for check in required_checks
    }
    if isinstance(expected_execution_modalities, list):
        modality_mismatch = not all(
            isinstance(m, str) and m for m in expected_execution_modalities
        ) or actual_execution_modalities != set(expected_execution_modalities)
    else:
        expected_execution_modality = policy.get("required_execution_modality")
        modality_mismatch = any(
            check.get("required_execution_modality") != expected_execution_modality
            for check in required_checks
        )
    if modality_mismatch:
        return _fail(
            "verification_contract -> execution_modality",
            "admission omitted or changed the required target execution modality",
        )
    return None


def validate_claim_kinds(required_checks, policy):
    contract_claims = [
        claim
        for check in required_checks
        for claim in check.get("claims") or []
        if isinstance(claim, dict) and claim.get("required", True)
    ]
    required_kind_counts = Counter(policy.get("required_claim_kinds") or {})
    actual_kind_counts = Counter(claim.get("kind") for claim in contract_claims)
    if any(actual_kind_counts[kind] < count for kind, count in required_kind_counts.items()):
        return (
            _fail(
                "verification_contract -> required_claims",
                "admission weakened the scenario's mandatory claim-kind floor",
            ),
            contract_claims,
        )
    return None, contract_claims


def validate_flattened_claims(admission, contract_claims):
    flattened = {
        _claim_contract(claim)
        for claim in admission.get("verification_claims") or []
        if isinstance(claim, dict) and claim.get("required", True)
    }
    if flattened != {_claim_contract(claim) for claim in contract_claims}:
        return _fail(
            "verification_contract -> flattened_claims",
            "admission flattened claims differ from its durable check contract",
        )
    return None


def validate_deliverable(segment, admission, run_intent, contract, delivery, contract_digest):
    deliverable = next(
        (
            event
            for event in reversed(segment)
            if event.get("kind") == "deliverable"
            and _seq(event) > _seq(admission)
            and _event_matches_run_intent(segment, event, run_intent)
        ),
        None,
    )
    if (
        deliverable is None
        or deliverable.get("target_id") != contract.get("target_id")
        or deliverable.get("delivery_contract") != delivery
        or deliverable.get("verification_contract_digest") != contract_digest
    ):
        return (
            _fail(
                "verification_contract -> deliverable",
                "current handoff is absent or not bound to the admitted target delivery",
            ),
            None,
        )
    return None, deliverable

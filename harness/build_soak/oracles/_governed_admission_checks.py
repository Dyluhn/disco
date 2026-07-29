"""Validation stages for the governed verification authority oracle.

Extracted from ``_governed_admission_helpers.py`` to keep that module within
the architecture budget.
"""

from __future__ import annotations

from typing import Any

from ._governed_admission_authority import (
    _authority_change_between,
    _last_authority_seq,
    _last_successful_productive_action_seq,
)
from ._governed_admission_helpers import (
    _artifact_identity_is_exact,
    _effect_receipt_is_exact,
    _fail,
    _seq,
)
from .workspace_contract import normalized_workspace_relative_path


def _matches_fields(value: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False
    return all(value.get(field) == expected_value for field, expected_value in expected.items())


def _validate_receipt_identity(
    receipt,
    check,
    admission,
    contract,
    delivery,
    contract_digest,
    deliverable,
    conversation_id,
    delegated,
):
    expected = {
        "status": "pass",
        "run_intent_id": admission.get("run_intent_id"),
        "run_identity": admission.get("run_identity"),
        "target_id": contract.get("target_id"),
        "delivery_shape": delivery.get("shape"),
        "delivery_entry_reference": delivery.get("entry_reference"),
        "verification_contract_digest": contract_digest,
        "check_id": check.get("check_id"),
        "receipt_kind": check.get("receipt_kind"),
        "issuer_id": check.get("issuer_id"),
        "operation": check.get("operation"),
        "verifier_id": check.get("issuer_id"),
        "tool_id": check.get("operation"),
        "deliverable_event_id": deliverable.get("id"),
        "artifact_path": deliverable.get("path"),
        "artifact_kind": deliverable.get("artifact_kind"),
    }
    if not _matches_fields(receipt, expected):
        return False
    if conversation_id and receipt.get("conversation_id") != conversation_id:
        return False
    if set(receipt.get("delegated_issuer_ids") or []) != delegated:
        return False
    return normalized_workspace_relative_path(receipt.get("artifact_path")) is not None


def _required_claim_failed(claim: Any) -> bool:
    return isinstance(claim, dict) and claim.get("required", True) and claim.get("status") != "pass"


def _passing_claim_has_foreign_issuer(claim: Any, allowed: set[Any]) -> bool:
    return (
        isinstance(claim, dict)
        and claim.get("status") == "pass"
        and claim.get("verifier_id") not in allowed
    )


def _observed_fact_invalid(fact: Any, allowed: set[Any]) -> bool:
    if not isinstance(fact, dict):
        return True
    if not isinstance(fact.get("fact_id"), str) or not fact.get("fact_id"):
        return True
    if fact.get("kind") == "visual_semantic":
        return True
    if not isinstance(fact.get("value"), str) or not fact.get("value"):
        return True
    if fact.get("verifier_id") not in allowed:
        return True
    modalities = fact.get("evidence_modalities")
    return not isinstance(modalities, list) or not modalities


def _validate_receipt_claims(
    claim_results,
    observed_facts,
    expected_claims,
    actual_claims,
    allowed_claim_issuers,
):
    if expected_claims - actual_claims:
        return False
    if any(_required_claim_failed(claim) for claim in claim_results or []):
        return False
    if any(
        _passing_claim_has_foreign_issuer(claim, allowed_claim_issuers)
        for claim in claim_results or []
    ):
        return False
    if observed_facts is not None:
        if not isinstance(observed_facts, list):
            return False
        if any(_observed_fact_invalid(fact, allowed_claim_issuers) for fact in observed_facts):
            return False
    return True


def validate_verdict_receipt(
    verdict,
    receipt,
    claim_results,
    observed_facts,
    check,
    admission,
    contract,
    delivery,
    contract_digest,
    deliverable,
    conversation_id,
    current_view_id,
    expected_claims,
    actual_claims,
    external_required,
    delegated,
    allowed_claim_issuers,
    requires_artifact_identity,
    required_artifact_identity_scheme,
):
    if (
        not isinstance(verdict, dict)
        or verdict.get("source") != "system"
        or verdict.get("verified") is not True
        or verdict.get("verdict") != "pass"
    ):
        return _fail(
            "verifier_check -> current_typed_pass",
            "a required admitted check lacks an exact current PASS receipt",
            check_id=check.get("check_id"),
        )
    if not _validate_receipt_identity(
        receipt,
        check,
        admission,
        contract,
        delivery,
        contract_digest,
        deliverable,
        conversation_id,
        delegated,
    ):
        return _fail(
            "verifier_check -> current_typed_pass",
            "a required admitted check lacks an exact current PASS receipt",
            check_id=check.get("check_id"),
        )
    assert isinstance(receipt, dict)
    if requires_artifact_identity and not _artifact_identity_is_exact(
        receipt.get("artifact_identity"),
        scheme=required_artifact_identity_scheme,
        entry_reference=deliverable.get("path"),
        producer_id=check.get("issuer_id"),
    ):
        return _fail(
            "verifier_check -> current_typed_pass",
            "a required admitted check lacks an exact current PASS receipt",
            check_id=check.get("check_id"),
        )
    if current_view_id is not None and receipt.get("agent_view_id") != current_view_id:
        return _fail(
            "verifier_check -> current_typed_pass",
            "a required admitted check lacks an exact current PASS receipt",
            check_id=check.get("check_id"),
        )
    if not _validate_receipt_claims(
        claim_results, observed_facts, expected_claims, actual_claims, allowed_claim_issuers
    ):
        return _fail(
            "verifier_check -> current_typed_pass",
            "a required admitted check lacks an exact current PASS receipt",
            check_id=check.get("check_id"),
        )
    if not _effect_receipt_is_exact(receipt):
        return _fail(
            "verifier_check -> current_typed_pass",
            "a required admitted check lacks an exact current PASS receipt",
            check_id=check.get("check_id"),
        )
    return None


def _validate_started_identity(
    started,
    check,
    admission,
    contract,
    delivery,
    contract_digest,
    deliverable,
    delegated,
):
    expected = {
        "source": "system",
        "target_id": contract.get("target_id"),
        "run_intent_id": admission.get("run_intent_id"),
        "run_identity": admission.get("run_identity"),
        "delivery_shape": delivery.get("shape"),
        "delivery_entry_reference": delivery.get("entry_reference"),
        "check_id": check.get("check_id"),
        "receipt_kind": check.get("receipt_kind"),
        "issuer_id": check.get("issuer_id"),
        "operation": check.get("operation"),
        "verification_contract_digest": contract_digest,
        "deliverable_event_id": deliverable.get("id"),
        "artifact_path": deliverable.get("path"),
        "artifact_kind": deliverable.get("artifact_kind"),
    }
    if not _matches_fields(started, expected):
        return False
    return set(started.get("delegated_issuer_ids") or []) == delegated


def _validate_started_receipt_binding(
    receipt, started, current_view_id, requires_artifact_identity
):
    return (
        (current_view_id is None or started.get("agent_view_id") == current_view_id)
        and receipt.get("execution_identity") == started.get("execution_identity")
        and receipt.get("preview_selection") == started.get("preview_selection")
        and receipt.get("workspace_revision") == started.get("workspace_revision")
        and receipt.get("workspace_generation") == started.get("workspace_generation")
        and receipt.get("workspace_epoch") == started.get("workspace_epoch")
        and (
            requires_artifact_identity
            or receipt.get("observed_after_seq") == started.get("observed_after_seq")
        )
    )


def validate_started_receipt(
    started,
    receipt,
    check,
    admission,
    contract,
    delivery,
    contract_digest,
    deliverable,
    current_view_id,
    delegated,
    requires_artifact_identity,
):
    if not _validate_started_identity(
        started, check, admission, contract, delivery, contract_digest, deliverable, delegated
    ):
        return _fail(
            "verifier_started -> verifier_receipt",
            "typed result differs from its pre-verification host authority",
            check_id=check.get("check_id"),
        )
    assert isinstance(started, dict)
    if not _validate_started_receipt_binding(
        receipt, started, current_view_id, requires_artifact_identity
    ):
        return _fail(
            "verifier_started -> verifier_receipt",
            "typed result differs from its pre-verification host authority",
            check_id=check.get("check_id"),
        )
    return None


def _claim_cites_event(claim: Any, event_id: Any) -> bool:
    if not isinstance(claim, dict):
        return False
    return f"event:{event_id}" in (claim.get("evidence_refs") or [])


def _compute_output_identity_invalid(
    receipt,
    started,
    verdict,
    segment,
    claim_results,
    expected_authority_seq,
):
    started_seq = _seq(started)
    if started.get("observed_after_seq") != expected_authority_seq:
        return True
    if not isinstance(receipt.get("observed_after_seq"), int):
        return True
    if receipt["observed_after_seq"] <= started_seq or receipt["observed_after_seq"] >= _seq(
        verdict
    ):
        return True
    output_event = next(
        (event for event in segment if _seq(event) == receipt.get("observed_after_seq")), None
    )
    if not isinstance(output_event, dict) or output_event.get("kind") != "observation":
        return True
    if (
        not isinstance(output_event.get("tool_result"), dict)
        or output_event["tool_result"].get("success") is not True
    ):
        return True
    if not any(_claim_cites_event(claim, output_event.get("id")) for claim in claim_results or []):
        return True
    return _authority_change_between(
        segment,
        int(started.get("observed_after_seq") or 0),
        receipt["observed_after_seq"],
    )


def validate_workspace_authority(
    receipt, started, verdict, segment, policy, claim_results, requires_artifact_identity, final_seq
):
    started_seq = _seq(started)
    expected_workspace_revision = _last_successful_productive_action_seq(
        segment, through_seq=started_seq
    )
    expected_authority_seq = _last_authority_seq(segment, through_seq=started_seq)
    output_identity_invalid = (
        _compute_output_identity_invalid(
            receipt, started, verdict, segment, claim_results, expected_authority_seq
        )
        if requires_artifact_identity
        else False
    )
    ordinary_authority_invalid = not requires_artifact_identity and (
        not isinstance(receipt.get("observed_after_seq"), int)
        or receipt["observed_after_seq"] != expected_authority_seq
        or receipt["observed_after_seq"] >= started_seq
    )
    if (
        type(receipt.get("workspace_revision")) is not int
        or receipt.get("workspace_revision") != expected_workspace_revision
        or output_identity_invalid
        or ordinary_authority_invalid
        or (
            policy.get("require_workspace_epoch", False)
            and (type(receipt.get("workspace_epoch")) is not int or receipt["workspace_epoch"] < 1)
        )
        or _authority_change_between(segment, receipt["observed_after_seq"], _seq(verdict))
    ):
        return _fail(
            "workspace_authority -> verifier_receipt",
            "receipt is stale or lacks exact workspace authority",
            check_id=receipt.get("check_id"),
        )
    if _authority_change_between(segment, _seq(verdict), final_seq):
        return _fail(
            "verifier_receipt -> terminal",
            "target authority changed after the selected PASS receipt",
            check_id=receipt.get("check_id"),
        )
    return None

"""Admission-chain and verification-contract resolution helpers."""

from __future__ import annotations

from typing import Any

from ._governed_admission_contracts import (
    validate_admission_authority,
    validate_claim_kinds,
    validate_execution_modalities,
    validate_flattened_claims,
    validate_receipt_kinds,
)
from ._governed_admission_helpers import (
    _canonical_digest,
    _fail,
    _seq,
)
from .schema import OracleResult


def find_verdict_and_started(
    segment: list[dict[str, Any]],
    deliverable: dict[str, Any],
    check: dict[str, Any],
    contract_digest: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the verdict and started events for one check."""
    verdict = next(
        (
            event
            for event in reversed(segment)
            if event.get("kind") == "verifier_verdict"
            and _seq(event) > _seq(deliverable)
            and event.get("check_id") == check.get("check_id")
            and event.get("receipt_kind") == check.get("receipt_kind")
            and event.get("verification_contract_digest") == contract_digest
        ),
        None,
    )
    started = next(
        (
            event
            for event in segment
            if isinstance(verdict, dict)
            and event.get("kind") == "verifier_started"
            and event.get("id") == verdict.get("requested_by_event_id")
            and _seq(deliverable) < _seq(event) < _seq(verdict)
        ),
        None,
    )
    return verdict, started


def run_contract_validations(
    policy: dict[str, Any],
    admission: dict[str, Any],
    contract: dict[str, Any],
    checks: list[dict[str, Any]],
) -> tuple[list[OracleResult] | None, dict[str, Any], list[dict[str, Any]], str]:
    """Run contract validations. Returns (error, delivery, required checks, digest)."""
    required_checks = [
        check for check in checks if isinstance(check, dict) and check.get("required", True)
    ]
    for validator in (validate_receipt_kinds, validate_execution_modalities):
        error = validator(required_checks, policy)
        if error is not None:
            return error, {}, [], ""
    error, contract_claims = validate_claim_kinds(required_checks, policy)
    if error is not None:
        return error, {}, [], ""
    error = validate_flattened_claims(admission, contract_claims)
    if error is not None:
        return error, {}, [], ""
    contract_digest = "sha256:" + _canonical_digest(contract)
    return None, {}, required_checks, contract_digest


def _find_run_intent(
    segment: list[dict[str, Any]],
    admission: dict[str, Any],
) -> dict[str, Any] | None:
    """Find the run intent event for the admission."""
    return next(
        (
            event
            for event in segment
            if event.get("kind") == "workspace_mutation"
            and event.get("id") == admission.get("run_intent_id")
            and str(event.get("operation") or "").startswith("agent.run-intent")
            and _seq(event) < _seq(admission)
        ),
        None,
    )


def _find_current_view(
    segment: list[dict[str, Any]],
    run_intent: dict[str, Any],
    final_seq: int,
) -> dict[str, Any] | None:
    """Find the current admitted model-view authority."""
    return next(
        (
            event
            for event in reversed(segment)
            if event.get("kind") == "workspace_mutation"
            and event.get("operation") == "agent.view-admitted"
            and event.get("run_intent_id") == run_intent.get("id")
            and isinstance(event.get("agent_view_id"), str)
            and bool(event["agent_view_id"])
            and _seq(event) < final_seq
        ),
        None,
    )


def resolve_admission_chain(
    segment: list[dict[str, Any]],
    policy: dict[str, Any],
    final_seq: int,
) -> tuple[list[OracleResult] | None, dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Resolve the admission, run intent, and current model view."""
    admission = next(
        (event for event in reversed(segment) if event.get("kind") == "build_platform_admission"),
        None,
    )
    error = validate_admission_authority(admission, policy)
    if error is not None:
        return error, None, None, None
    assert admission is not None

    run_intent = _find_run_intent(segment, admission)
    if run_intent is None:
        return (
            _fail(
                "run_intent -> build_platform_admission",
                "admission run intent is absent, foreign, or not causally prior",
            ),
            None,
            None,
            None,
        )

    current_view = _find_current_view(segment, run_intent, final_seq)
    if policy.get("require_agent_view_binding", True) and current_view is None:
        return (
            _fail(
                "run_intent -> agent_view",
                "current run has no durable admitted model-view authority",
            ),
            None,
            None,
            None,
        )
    current_view_id = current_view.get("agent_view_id") if isinstance(current_view, dict) else None
    return None, admission, run_intent, current_view_id

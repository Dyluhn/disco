"""Target-neutral governed verification authority oracle.

The actual helper functions and validation stages now live in
:mod:`._governed_admission_helpers`. This module owns the
``GovernedAdmissionOracle`` class and its ``check`` method.
"""

from __future__ import annotations

from typing import Any

from ._governed_admission_authority import _shared_execution_authority
from ._governed_admission_checks import (
    validate_started_receipt,
    validate_verdict_receipt,
    validate_workspace_authority,
)
from ._governed_admission_contracts import (
    validate_contract_delivery,
    validate_deliverable,
    validate_execution_and_preview,
)
from ._governed_admission_helpers import (
    _ORACLE,
    _active_external_claims,
    _claim_contract,
    _fail,
    _policy,
    _terminal_segment,
)
from ._governed_admission_preview import (
    preview_identity_from_pair as _preview_identity_from_pair,
)
from ._governed_admission_resolution import (
    find_verdict_and_started,
    resolve_admission_chain,
    run_contract_validations,
)
from .schema import OracleResult, passing, skipping

__all__ = [
    "GovernedAdmissionOracle",
    "_preview_identity_from_pair",
    "_shared_execution_authority",
]


class GovernedAdmissionOracle:
    """Require exact current PASS receipts for every admitted target check."""

    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        conversation_id: str = "",
    ) -> list[OracleResult]:
        policy = _policy(scenario)
        if policy is None:
            return [skipping(_ORACLE, reason="scenario has no governed verification policy")]
        bounded = _terminal_segment(events)
        if bounded is None:
            return _fail("event_chain -> terminal", "no successful work terminal exists")
        segment, prior_terminal_seq, final_seq = bounded

        error, admission, run_intent, current_view_id = resolve_admission_chain(
            segment, policy, final_seq
        )
        if error is not None:
            return error
        assert admission is not None and run_intent is not None

        contract = admission.get("verification_contract")
        if not isinstance(contract, dict):
            return _fail(
                "build_platform_admission -> verification_contract",
                "current admission omitted the target verification contract",
            )

        error, delivery, checks = validate_contract_delivery(contract, policy)
        if error is not None:
            return error
        assert delivery is not None and checks is not None

        error, _delivery, required_checks, contract_digest = run_contract_validations(
            policy, admission, contract, checks
        )
        if error is not None:
            return error

        error, deliverable = validate_deliverable(
            segment, admission, run_intent, contract, delivery, contract_digest
        )
        if error is not None:
            return error
        assert deliverable is not None

        return self._check_verifier_receipts(
            events,
            segment,
            final_seq,
            prior_terminal_seq,
            policy,
            admission,
            run_intent,
            contract,
            delivery,
            contract_digest,
            deliverable,
            required_checks,
            conversation_id,
            current_view_id,
        )

    def _check_verifier_receipts(
        self,
        events: list[dict[str, Any]],
        segment: list[dict[str, Any]],
        final_seq: int,
        prior_terminal_seq: int,
        policy: dict[str, Any],
        admission: dict[str, Any],
        run_intent: dict[str, Any],
        contract: dict[str, Any],
        delivery: dict[str, Any],
        contract_digest: str,
        deliverable: dict[str, Any],
        required_checks: list[dict[str, Any]],
        conversation_id: str,
        current_view_id: str | None,
    ) -> list[OracleResult]:
        """Check each required verifier receipt."""
        external_required = set(_active_external_claims(events, through_seq=final_seq))
        covered_external: set[tuple[object, ...]] = set()
        execution_authorities: set[tuple[Any, Any, Any]] = set()
        verified_claim_results: list[dict[str, Any]] = []
        verified_observed_facts: list[dict[str, Any]] = []
        verified_artifact_paths: set[str] = set()

        for check in required_checks:
            error, vcr, vof, vap, exec_auth = self._check_one_receipt(
                events,
                segment,
                final_seq,
                policy,
                admission,
                contract,
                delivery,
                contract_digest,
                deliverable,
                check,
                conversation_id,
                current_view_id,
                external_required,
            )
            if error is not None:
                return error
            covered_external.update(
                exec_auth.get("covered", set()) if isinstance(exec_auth, dict) else set()
            )
            if exec_auth and isinstance(exec_auth.get("authority"), tuple):
                execution_authorities.add(exec_auth["authority"])
            verified_claim_results.extend(vcr or [])
            verified_observed_facts.extend(vof or [])
            verified_artifact_paths.update(vap or set())

        if len(execution_authorities) != 1:
            return _fail(
                "verifier_checks -> target_execution",
                "required checks did not certify one shared execution generation",
            )
        if not external_required <= covered_external:
            return _fail(
                "external_requirements -> verifier_receipts",
                "a current mandatory external verification claim was dropped",
            )
        return [
            passing(
                _ORACLE,
                facts={
                    "terminal_seq": final_seq,
                    "prior_terminal_seq": prior_terminal_seq,
                    "target_id": contract.get("target_id"),
                    "required_checks": len(required_checks),
                    "contract_digest": contract_digest,
                    "verified_claim_results": verified_claim_results,
                    "verified_observed_facts": verified_observed_facts,
                    "verified_artifact_paths": sorted(verified_artifact_paths),
                },
            )
        ]

    def _check_one_receipt(
        self,
        events: list[dict[str, Any]],
        segment: list[dict[str, Any]],
        final_seq: int,
        policy: dict[str, Any],
        admission: dict[str, Any],
        contract: dict[str, Any],
        delivery: dict[str, Any],
        contract_digest: str,
        deliverable: dict[str, Any],
        check: dict[str, Any],
        conversation_id: str,
        current_view_id: str | None,
        external_required: set,
    ) -> tuple[
        list[OracleResult] | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        set[str],
        dict[str, Any] | None,
    ]:
        """Check one verifier receipt. Returns (error, claims, facts, paths, exec_auth)."""
        verdict, started = find_verdict_and_started(segment, deliverable, check, contract_digest)
        receipt = verdict.get("verification_result") if isinstance(verdict, dict) else None
        claim_results = receipt.get("claim_results") if isinstance(receipt, dict) else None
        observed_facts = receipt.get("observed_facts") if isinstance(receipt, dict) else None
        actual_claims = {
            _claim_contract(claim) for claim in claim_results or [] if isinstance(claim, dict)
        }
        covered = actual_claims & external_required

        error = self._validate_one_check_chain(
            verdict,
            started,
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
            external_required,
            segment,
            policy,
            events,
            final_seq,
        )
        if error is not None:
            return error, [], [], set(), None
        assert isinstance(receipt, dict)

        vcr = [dict(claim) for claim in claim_results or [] if isinstance(claim, dict)]
        vof = [dict(fact) for fact in observed_facts or [] if isinstance(fact, dict)]
        vap = {str(receipt["artifact_path"])}
        _, exec_auth = validate_execution_and_preview(receipt, check, events, deliverable, verdict)
        return None, vcr, vof, vap, {"covered": covered, "authority": exec_auth}

    def _validate_one_check_chain(
        self,
        verdict: Any,
        started: Any,
        receipt: Any,
        claim_results: Any,
        observed_facts: Any,
        check: dict[str, Any],
        admission: dict[str, Any],
        contract: dict[str, Any],
        delivery: dict[str, Any],
        contract_digest: str,
        deliverable: dict[str, Any],
        conversation_id: str,
        current_view_id: str | None,
        external_required: set,
        segment: list[dict[str, Any]],
        policy: dict[str, Any],
        events: list[dict[str, Any]],
        final_seq: int,
    ) -> list[OracleResult] | None:
        """Run the verdict/started/workspace/execution validation chain for one check."""
        expected_claims = {
            _claim_contract(claim) for claim in check.get("claims") or [] if isinstance(claim, dict)
        }
        actual_claims = {
            _claim_contract(claim) for claim in claim_results or [] if isinstance(claim, dict)
        }
        delegated = set(check.get("delegated_issuer_ids") or [])
        allowed_claim_issuers = {check.get("issuer_id"), *delegated}
        required_artifact_identity_scheme = check.get("required_artifact_identity_scheme")
        requires_artifact_identity = bool(
            isinstance(required_artifact_identity_scheme, str) and required_artifact_identity_scheme
        )

        error = validate_verdict_receipt(
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
        )
        if error is not None:
            return error
        assert isinstance(receipt, dict)

        error = validate_started_receipt(
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
        )
        if error is not None:
            return error
        assert isinstance(started, dict)

        error = validate_workspace_authority(
            receipt,
            started,
            verdict,
            segment,
            policy,
            claim_results,
            requires_artifact_identity,
            final_seq,
        )
        if error is not None:
            return error

        error, _exec_auth = validate_execution_and_preview(
            receipt,
            check,
            events,
            deliverable,
            verdict,
        )
        return error

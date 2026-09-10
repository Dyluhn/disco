"""Derive the host-verification contract owned by a Build composition."""

from __future__ import annotations

from disco.core.verification import (
    AdmittedVerificationContract,
    VerificationCheckContract,
    VerificationDeliveryContract,
    VerificationParameter,
)

from .build_platform_shadow import BuildPlatformRouteRecord


def record_verification_contract(
    record: BuildPlatformRouteRecord | None,
) -> AdmittedVerificationContract | None:
    if record is None:
        return None
    composition = record.composition
    target_plan = composition.target_plan
    verifier_plan = target_plan.verifier
    delivery = target_plan.delivery
    return AdmittedVerificationContract(
        target_id=target_plan.target.canonical,
        verifier_id=composition.profile.verifier.canonical,
        delivery=VerificationDeliveryContract(
            shape=delivery.shape,
            mode=delivery.mode,
            entry_kind=delivery.entry.kind,
            entry_reference=delivery.entry.reference,
            entry_parameters=tuple(
                VerificationParameter(name=parameter.name, value=parameter.value)
                for parameter in delivery.entry.parameters
            ),
        ),
        preview_modality=target_plan.preview.modality,
        checks=tuple(
            VerificationCheckContract(
                check_id=check.check_id,
                receipt_kind=check.receipt_kind,
                issuer_id=(
                    check.issuer.canonical
                    if check.issuer is not None
                    else composition.profile.verifier.canonical
                ),
                operation=check.intent.operation,
                required_execution_modality=check.required_execution_modality,
                required_artifact_identity_scheme=check.required_artifact_identity_scheme,
                required=check.required,
                delegated_issuer_ids=frozenset(
                    issuer.canonical for issuer in check.delegated_issuers
                ),
                accepted_claim_kinds=(
                    check.accepted_claim_kinds or frozenset(claim.kind for claim in check.claims)
                ),
                claims=check.claims,
            )
            for check in verifier_plan.checks
        ),
        required=verifier_plan.policy.required,
        unavailable=verifier_plan.policy.unavailable,
        unverified_finish=verifier_plan.policy.unverified_finish,
    )

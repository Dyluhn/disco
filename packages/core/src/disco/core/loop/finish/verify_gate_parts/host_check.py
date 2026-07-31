"""Running the admitted host verifier checks and assigning external claims.

Owns: splitting a governed contract's external claims across its admitted
verifier checks (`governed_check_deliverables`), running one host verifier
check end to end, and the `gate_host_verify` sequence that drives every
admitted check and turns its outcome into a gate `Disp`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from ....verification import (
    AdmittedVerificationContract,
    VerificationCheckContract,
    aggregate_verification_receipts,
)
from ..common import (
    _LOG,
    AgentStep,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    HostVerificationClaim,
    HostVerificationDeliverable,
    HostVerificationResult,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    VerificationClaimKind,
    VerificationClaimStatus,
    _last_verification_authority_seq,
    _latest_deliverable_event,
)
from .host_authority import (
    emit_browser_unavailable_release,
    emit_host_verdict_audit,
    emit_verifier_started_event,
    prepare_typed_host_verdict,
)
from .host_claims import (
    bounded_host_verdict_screenshot_path,
    governed_verification_contract,
    governed_verification_required,
    host_verification_claims,
    strict_appkit_contract,
)
from .host_deliverable import host_unavailable_verdict, host_unverifiable_verdict
from .host_disposition import (
    governed_contract_refusal,
    governed_non_pass_disposition,
    host_verify_failure_disposition,
)


def _governed_required_checks(
    contract: AdmittedVerificationContract,
) -> tuple[tuple[VerificationCheckContract, ...], dict[str, HostVerificationClaim], set[str]]:
    checks = tuple(check for check in contract.checks if check.required)
    target_claims = {claim.claim_id: claim for check in checks for claim in check.claims}
    return checks, target_claims, set(target_claims)


def _governed_claim_conflict_errors(
    all_claims: tuple[HostVerificationClaim, ...],
    target_claims: dict[str, HostVerificationClaim],
) -> list[str]:
    errors: list[str] = []
    for claim in all_claims:
        target = target_claims.get(claim.claim_id)
        if target is not None and claim != target:
            errors.append(f"external claim {claim.claim_id!r} conflicts with the target contract")
    return errors


def _governed_claim_assignment(
    checks: tuple[VerificationCheckContract, ...],
    external: tuple[HostVerificationClaim, ...],
) -> tuple[dict[str, list[HostVerificationClaim]], list[str]]:
    assigned: dict[str, list[HostVerificationClaim]] = {check.check_id: [] for check in checks}
    errors: list[str] = []
    for claim in external:
        owners = tuple(check for check in checks if claim.kind in check.accepted_claim_kinds)
        if len(owners) != 1:
            if claim.required:
                errors.append(
                    f"claim {claim.claim_id!r} has {len(owners)} admitted verifier owners"
                )
                if checks:
                    assigned[checks[0].check_id].append(claim)
            continue
        assigned[owners[0].check_id].append(claim)
    return assigned, errors


def _governed_preverified_coverage_errors(
    assigned: dict[str, list[HostVerificationClaim]],
    skip_check_ids: frozenset[str],
    preverified_claim_ids: dict[str, frozenset[str]] | None,
) -> list[str]:
    errors: list[str] = []
    for check_id in skip_check_ids:
        covered = (
            preverified_claim_ids.get(check_id, frozenset())
            if preverified_claim_ids is not None
            else frozenset()
        )
        uncovered = [claim for claim in assigned.get(check_id, ()) if claim.claim_id not in covered]
        if uncovered:
            errors.append(f"preverified check {check_id!r} cannot absorb new external claims")
    return errors


async def _governed_bind_execution(
    item: HostVerificationDeliverable,
    host_verifier: Any,
    check: VerificationCheckContract,
) -> tuple[HostVerificationDeliverable, str | None]:
    binder = getattr(host_verifier, "bind_execution", None)
    if binder is None:
        return item, None
    try:
        bound = await binder(item)
        if not isinstance(bound, HostVerificationDeliverable):
            raise TypeError("execution binder returned an invalid deliverable")
        original = item.model_dump(mode="json", exclude={"execution_identity"})
        candidate = bound.model_dump(mode="json", exclude={"execution_identity"})
        if original != candidate:
            raise ValueError("execution binder changed non-execution authority")
        return bound, None
    except Exception as exc:  # noqa: BLE001 — fail closed as unavailable
        return item, f"target execution binding failed for {check.check_id!r}: {exc}"


def _governed_execution_modality_error(
    item: HostVerificationDeliverable, check: VerificationCheckContract
) -> str | None:
    if (
        item.execution_identity is None
        or item.execution_identity.modality != check.required_execution_modality
    ):
        return (
            f"target execution for {check.check_id!r} requires modality "
            f"{check.required_execution_modality!r}"
        )
    return None


async def _governed_check_deliverable(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    check: VerificationCheckContract,
    assigned: dict[str, list[HostVerificationClaim]],
    host_verifier: Any,
) -> tuple[HostVerificationDeliverable, list[str]]:
    errors: list[str] = []
    item = deliverable.model_copy(
        update={
            "verification_check": check,
            "required_claims": (*check.claims, *assigned[check.check_id]),
        }
    )
    item = await gate._with_host_verification_profile(item, events)
    item, bind_error = await _governed_bind_execution(item, host_verifier, check)
    if bind_error is not None:
        errors.append(bind_error)
    modality_error = _governed_execution_modality_error(item, check)
    if modality_error is not None:
        errors.append(modality_error)
    return item, errors


def _governed_execution_authority_errors(
    out: tuple[HostVerificationDeliverable, ...],
) -> list[str]:
    execution_authorities = {
        json.dumps(
            item.execution_identity.model_dump(mode="json")
            if item.execution_identity is not None
            else None,
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in out
    }
    if len(execution_authorities) > 1:
        return ["required verifier checks resolved different target execution generations"]
    return []


async def governed_check_deliverables(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    *,
    skip_check_ids: frozenset[str] = frozenset(),
    preverified_claim_ids: dict[str, frozenset[str]] | None = None,
) -> tuple[tuple[HostVerificationDeliverable, ...], str | None]:
    contract = deliverable.verification_contract
    if contract is None:
        return (deliverable,), None
    checks, target_claims, target_claim_ids = _governed_required_checks(contract)
    all_claims = host_verification_claims(gate._verifier_contract_payload(), events)
    external = tuple(claim for claim in all_claims if claim.claim_id not in target_claim_ids)
    errors = _governed_claim_conflict_errors(all_claims, target_claims)
    assigned, assignment_errors = _governed_claim_assignment(checks, external)
    errors.extend(assignment_errors)
    errors.extend(
        _governed_preverified_coverage_errors(assigned, skip_check_ids, preverified_claim_ids)
    )
    out: list[HostVerificationDeliverable] = []
    host_verifier = getattr(gate._loop, "_host_verifier", None)
    for check in checks:
        if check.check_id in skip_check_ids:
            continue
        item, item_errors = await _governed_check_deliverable(
            gate, deliverable, events, check, assigned, host_verifier
        )
        errors.extend(item_errors)
        out.append(item)
    errors.extend(_governed_execution_authority_errors(tuple(out)))
    return tuple(out), "; ".join(errors) or None


async def run_host_verifier_check(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    *,
    host_verifier: Any,
    authoritative: bool,
    governed_target: bool,
    forced_unavailable: str | None = None,
) -> tuple[dict[str, Any], HostVerificationResult | None, bool, str | None]:
    started = await emit_verifier_started_event(gate._loop, deliverable)
    host_screenshot_path: str | None = None
    if forced_unavailable is not None:
        host_verdict = host_unavailable_verdict(deliverable, forced_unavailable)
    elif deliverable.artifact_kind != "app" and not governed_target:
        host_verdict = host_unverifiable_verdict(deliverable)
    elif host_verifier is None:
        host_verdict = host_unavailable_verdict(
            deliverable,
            "verification could not run: no host verifier is configured.",
        )
    else:
        try:
            raw_host_verdict = await asyncio.wait_for(
                host_verifier.verify(deliverable),
                timeout=max(
                    0.001,
                    float(getattr(gate._loop, "_host_verify_timeout_s", 30.0)),
                ),
            )
            if not isinstance(raw_host_verdict, dict):
                host_verdict = host_unavailable_verdict(
                    deliverable,
                    "verification could not run: host verifier did not return "
                    "a usable verdict.",
                )
            else:
                host_verdict = raw_host_verdict
                host_screenshot_path = bounded_host_verdict_screenshot_path(
                    raw_host_verdict.get("screenshot_path")
                )
        except TimeoutError:
            host_verdict = host_unavailable_verdict(
                deliverable,
                "verification could not run: host verifier timed out.",
            )
        except Exception as exc:  # noqa: BLE001 — verifier failure stays typed
            _LOG.warning(
                "host verifier failed for %s:%s",
                gate._loop.conversation_id,
                deliverable.artifact_path,
                exc_info=True,
            )
            host_verdict = host_unavailable_verdict(
                deliverable,
                f"verification could not run: host verifier failed ({exc}).",
            )
    host_verdict, typed_result, browser_unavailable = await prepare_typed_host_verdict(
        gate,
        deliverable,
        events,
        cast(dict[str, Any], host_verdict),
    )
    host_label = await emit_host_verdict_audit(
        gate,
        deliverable,
        events,
        host_verdict,
        typed_result,
        host_screenshot_path,
        verifier_started_event_id=started.id,
    )
    return host_verdict, typed_result, browser_unavailable, host_label


def _typed_result_has_unavailable_semantic_claim(typed_result: HostVerificationResult) -> bool:
    return any(
        result.required
        and result.status is VerificationClaimStatus.UNAVAILABLE
        and result.kind
        in {VerificationClaimKind.CONTRACT_SEMANTIC, VerificationClaimKind.VISUAL_SEMANTIC}
        for result in typed_result.claim_results
    )


async def _gate_host_verify_unavailable_disposition(
    gate: Any,
    check_deliverable: HostVerificationDeliverable,
    host_verdict: dict[str, Any],
    typed_result: HostVerificationResult | None,
    browser_unavailable: bool,
) -> Disp:
    if typed_result is not None and _typed_result_has_unavailable_semantic_claim(typed_result):
        return await host_verify_failure_disposition(gate, check_deliverable, host_verdict)
    if browser_unavailable:
        return await emit_browser_unavailable_release(gate._loop)
    return Disp.FALLTHROUGH


async def _gate_host_verify_unverifiable_disposition(
    gate: Any,
    check_deliverable: HostVerificationDeliverable,
    host_verdict: dict[str, Any],
) -> Disp:
    if host_verdict.get("model_verifier_applied") is True:
        return await host_verify_failure_disposition(gate, check_deliverable, host_verdict)
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="unverified_release",
        )
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished WITHOUT browser-render verification — "
                    "the host verifier reported the app UNVERIFIABLE "
                    "because its browser infrastructure could not run. "
                    "The deliverable is UNVERIFIED and may be INCOMPLETE; "
                    "do not report it as a verified pass."
                ),
            ),
        )
    )
    gate._loop._browser_verify_refusals = 0
    return Disp.FALLTHROUGH


async def _gate_host_verify_check_disposition(
    gate: Any,
    check_deliverable: HostVerificationDeliverable,
    events: list[Event],
    *,
    authoritative: bool,
    governed_target: bool,
    host_verdict: dict[str, Any],
    typed_result: HostVerificationResult | None,
    browser_unavailable: bool,
    host_label: str | None,
    accepted_receipts: list[HostVerificationResult],
) -> Disp | None:
    """Return a Disp to STOP the check loop, or None to continue to the next check."""

    if not (authoritative and (check_deliverable.artifact_kind == "app" or governed_target)):
        return None
    if host_verdict.get("passed") is True and host_label == "pass":
        if typed_result is not None:
            accepted_receipts.append(typed_result)
        gate._loop._browser_verify_refusals = 0
        return None
    if governed_target:
        return await governed_non_pass_disposition(
            gate, check_deliverable, host_verdict, typed_result, events
        )
    if host_label == "unavailable":
        return await _gate_host_verify_unavailable_disposition(
            gate, check_deliverable, host_verdict, typed_result, browser_unavailable
        )
    if host_label == "unverifiable":
        return await _gate_host_verify_unverifiable_disposition(
            gate, check_deliverable, host_verdict
        )
    return await host_verify_failure_disposition(gate, check_deliverable, host_verdict)


def _gate_host_verify_valid_preverified(
    preverified: tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...],
) -> tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...]:
    return tuple(
        (item, receipt)
        for item, receipt in preverified
        if item.verification_check is not None
        and receipt.passed
        and receipt.is_current_authority_for(item, observed_url=receipt.observed_url)
    )


def _gate_host_verify_preverified_claim_ids(
    valid_preverified: tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...],
) -> dict[str, frozenset[str]]:
    return {
        item.verification_check.check_id: frozenset(
            claim.claim_id for claim in item.required_claims
        )
        for item, _receipt in valid_preverified
        if item.verification_check is not None
    }


def _gate_host_verify_preverified_state(
    preverified: tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...],
) -> tuple[
    tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...],
    frozenset[str],
    dict[str, frozenset[str]],
]:
    valid_preverified = _gate_host_verify_valid_preverified(preverified)
    skip_check_ids = frozenset(
        item.verification_check.check_id
        for item, _receipt in valid_preverified
        if item.verification_check is not None
    )
    return valid_preverified, skip_check_ids, _gate_host_verify_preverified_claim_ids(
        valid_preverified
    )


def _gate_host_verify_deliverable_pool(
    valid_preverified: tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...],
    deliverables: tuple[HostVerificationDeliverable, ...],
) -> tuple[list[HostVerificationDeliverable], list[HostVerificationResult]]:
    all_deliverables = [item for item, _receipt in valid_preverified]
    all_deliverables.extend(deliverables)
    accepted_receipts = [receipt for _item, receipt in valid_preverified]
    return all_deliverables, accepted_receipts


async def _gate_host_verify_coverage_disposition(
    gate: Any,
    events: list[Event],
    all_deliverables: list[HostVerificationDeliverable],
    accepted_receipts: list[HostVerificationResult],
) -> Disp:
    coverage = aggregate_verification_receipts(
        deliverables=tuple(all_deliverables),
        receipts=tuple(accepted_receipts),
    )
    if not coverage.passed:
        return await governed_contract_refusal(
            gate,
            events,
            failure_key=("target:incomplete_aggregate:" + ",".join(coverage.missing_claim_ids)),
            guidance=(
                "the target verifier set did not cover every mandatory claim "
                "on one current execution generation."
            ),
        )
    current_events = await gate._loop._events()
    current_contract = governed_verification_contract(current_events)
    current_handoff = _latest_deliverable_event(current_events)
    cast(Any, gate._loop)._governed_host_pass_key = (
        current_contract.digest if current_contract is not None else "",
        current_handoff.id if current_handoff is not None else "",
        _last_verification_authority_seq(current_events),
    )
    return Disp.FALLTHROUGH


async def gate_host_verify(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    *,
    preverified: tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...] = (),
) -> Disp:
    """Run every admitted verifier check; legacy web remains a compatibility path."""

    contract = governed_verification_contract(events)
    if strict_appkit_contract(contract) and not preverified:
        return Disp.FALLTHROUGH

    governed_target = governed_verification_required(events)
    authoritative = gate._host_verify_authoritative() or governed_target
    host_verifier = getattr(gate._loop, "_host_verifier", None)
    if host_verifier is None and not authoritative:
        return Disp.FALLTHROUGH
    deliverable = await gate._host_verify_deliverable(
        step, events, include_unverifiable=authoritative
    )
    if deliverable is None:
        return Disp.FALLTHROUGH
    valid_preverified, skip_check_ids, preverified_claim_ids = _gate_host_verify_preverified_state(
        preverified
    )
    deliverables, ownership_error = await gate._governed_check_deliverables(
        deliverable,
        events,
        skip_check_ids=skip_check_ids,
        preverified_claim_ids=preverified_claim_ids,
    )
    if ownership_error is not None and not deliverables:
        return await governed_contract_refusal(
            gate,
            events,
            failure_key=f"target:claim_ownership:{ownership_error}",
            guidance=(
                f"{ownership_error}. The admitted verifier set cannot honestly "
                "cover the current mandatory requirement set."
            ),
        )
    if not deliverables and not valid_preverified:
        return Disp.FALLTHROUGH

    all_deliverables, accepted_receipts = _gate_host_verify_deliverable_pool(
        valid_preverified, deliverables
    )
    for index, check_deliverable in enumerate(deliverables):
        (
            host_verdict,
            typed_result,
            browser_unavailable,
            host_label,
        ) = await gate._run_host_verifier_check(
            check_deliverable,
            events,
            host_verifier=host_verifier,
            authoritative=authoritative,
            governed_target=governed_target,
            forced_unavailable=ownership_error if index == 0 else None,
        )
        disposition = await _gate_host_verify_check_disposition(
            gate,
            check_deliverable,
            events,
            authoritative=authoritative,
            governed_target=governed_target,
            host_verdict=host_verdict,
            typed_result=typed_result,
            browser_unavailable=browser_unavailable,
            host_label=host_label,
            accepted_receipts=accepted_receipts,
        )
        if disposition is not None:
            return disposition
    if governed_target:
        return await _gate_host_verify_coverage_disposition(
            gate, events, all_deliverables, accepted_receipts
        )
    return Disp.FALLTHROUGH

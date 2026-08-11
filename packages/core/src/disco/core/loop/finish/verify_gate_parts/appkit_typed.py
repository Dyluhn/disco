"""Strict-AppKit typed verification authority: preflight, START, and verdict.

Owns: binding AppKit's current target and emitting its verifier-START before
strict verification, pairing that START with an exact typed terminal
verifier status, and reading back a recorded typed status/result for the
render-verify sequence to gate on.
"""

from __future__ import annotations

from typing import Any

from ....verification import (
    AdmittedVerificationContract,
    HostVerificationClaimResult,
    VerificationArtifactIdentity,
    VerificationCheckContract,
    VerificationEvidenceModality,
    VerificationExecutionIdentity,
    aggregate_verification_receipts,
    target_verification_result,
)
from ..common import (
    AgentErrorEvent,
    AgentStep,
    DeliverableEvent,
    Disp,
    Event,
    EventSource,
    HostVerificationClaim,
    HostVerificationDeliverable,
    HostVerificationResult,
    ObservationEvent,
    VerificationClaimKind,
    VerificationClaimStatus,
    VerifierStartedEvent,
    VerifierVerdictEvent,
    _last_productive_seq,
    _latest_deliverable_event,
    _safe_deliverable_file_path,
    current_workspace_agent_view_id,
)
from .host_authority import emit_host_verdict_audit, emit_verifier_started_event
from .host_claims import (
    appkit_runtime_identity,
    bounded_host_verdict_screenshot_path,
    handoff_matches_verification_contract,
    host_verification_claims,
    latest_appkit_verification_outcome,
    preview_selection_at,
)


def _single_strict_appkit_check(
    contract: AdmittedVerificationContract,
) -> VerificationCheckContract | None:
    strict_checks = tuple(
        check
        for check in contract.checks
        if check.required
        and check.issuer_id == "disco.appkit_strict_verifier@1"
        and check.receipt_kind == "disco.appkit_strict@1"
        and check.operation == "host.verify_appkit_strict"
    )
    return strict_checks[0] if len(strict_checks) == 1 else None


async def _appkit_preflight_execution(
    gate: Any, check: VerificationCheckContract
) -> tuple[str, VerificationExecutionIdentity] | None:
    """Resolve the adapter's current canonical entry + execution identity."""

    preflight = getattr(gate._loop.executor, "verification_preflight", None)
    if preflight is None:
        return None
    try:
        raw = await preflight(check.operation)
    except Exception:  # noqa: BLE001 — adapter preflight is fail-closed
        return None
    if not isinstance(raw, dict) or raw.get("artifact_kind") != "app":
        return None
    artifact_path = _safe_deliverable_file_path(str(raw.get("artifact_path") or ""))
    try:
        execution_identity = VerificationExecutionIdentity.model_validate(
            raw.get("execution_identity")
        )
    except Exception:
        return None
    if artifact_path is None or execution_identity.modality != check.required_execution_modality:
        return None
    return artifact_path, execution_identity


def _appkit_preview_authority_state(
    events: list[Event], current: DeliverableEvent | None, max_seq: int
) -> tuple[int | None, bool]:
    """Return (current_seq, preview_authority_changed) for the current handoff."""

    current_seq = current.seq if current is not None and type(current.seq) is int else None
    handoff_selection = (
        preview_selection_at(events, through_seq=current_seq) if current_seq is not None else None
    )
    latest_selection = preview_selection_at(events, through_seq=max_seq)
    changed = (
        current is not None and (handoff_selection is None) != (latest_selection is None)
    ) or (
        handoff_selection is not None
        and latest_selection is not None
        and handoff_selection.operational_identity != latest_selection.operational_identity
    )
    return current_seq, changed


def _appkit_needs_new_handoff(
    *,
    current: DeliverableEvent | None,
    contract: AdmittedVerificationContract,
    artifact_path: str,
    current_seq: int | None,
    preview_authority_changed: bool,
    events: list[Event],
) -> bool:
    handoff_precedes_productive_work = current_seq is None or current_seq < _last_productive_seq(
        events
    )
    return (
        current is None
        or not handoff_matches_verification_contract(current, contract)
        or current.path != artifact_path
        or handoff_precedes_productive_work
        or preview_authority_changed
    )


async def _appkit_current_handoff(
    gate: Any,
    events: list[Event],
    contract: AdmittedVerificationContract,
    artifact_path: str,
) -> tuple[DeliverableEvent, list[Event]] | None:
    """Return the current AppKit handoff, minting a fresh one if it's stale."""

    current = _latest_deliverable_event(events)
    max_seq = max((event.seq or 0 for event in events), default=0)
    current_seq, preview_authority_changed = _appkit_preview_authority_state(
        events, current, max_seq
    )
    if not _appkit_needs_new_handoff(
        current=current,
        contract=contract,
        artifact_path=artifact_path,
        current_seq=current_seq,
        preview_authority_changed=preview_authority_changed,
        events=events,
    ):
        assert current is not None
        return current, events
    emitted = await gate._loop._emit(
        DeliverableEvent(
            source=EventSource.SYSTEM,
            agent_view_id=current_workspace_agent_view_id(events),
            title="AppKit app",
            path=artifact_path,
            artifact_kind="app",
            target_id=contract.target_id,
            delivery_contract=contract.delivery,
            verification_contract_digest=contract.digest,
        )
    )
    if not isinstance(emitted, DeliverableEvent):
        return None
    return emitted, await gate._loop._events()


def _appkit_bound_deliverable(
    base: HostVerificationDeliverable,
    *,
    current: DeliverableEvent,
    check: VerificationCheckContract,
    execution_identity: VerificationExecutionIdentity,
    events: list[Event],
    gate: Any,
) -> HostVerificationDeliverable | None:
    bound = base.model_copy(
        update={
            "deliverable_event_id": current.id,
            "artifact_path": current.path,
            "artifact_kind": current.artifact_kind,
            "required_claims": host_verification_claims(
                gate._verifier_contract_payload(),
                events,
                check=check,
                artifact_path=current.path,
            ),
            "verification_check": check,
            "execution_identity": execution_identity,
            "preview_selection": None,
            "preview_binding_required": False,
        }
    )
    if (
        bound.verification_check != check
        or check.issuer_id != "disco.appkit_strict_verifier@1"
        or check.receipt_kind != "disco.appkit_strict@1"
        or check.operation != "host.verify_appkit_strict"
    ):
        return None
    return bound


async def prepare_appkit_typed_authority(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    contract: AdmittedVerificationContract,
) -> tuple[VerifierStartedEvent, HostVerificationDeliverable] | None:
    """Bind AppKit's current target and emit START before strict verification."""

    check = _single_strict_appkit_check(contract)
    if check is None:
        return None
    preflight_result = await _appkit_preflight_execution(gate, check)
    if preflight_result is None:
        return None
    artifact_path, execution_identity = preflight_result
    handoff_result = await _appkit_current_handoff(gate, events, contract, artifact_path)
    if handoff_result is None:
        return None
    current, events = handoff_result
    base = await gate._host_verify_deliverable(step, events, include_unverifiable=True)
    if base is None:
        return None
    bound = _appkit_bound_deliverable(
        base,
        current=current,
        check=check,
        execution_identity=execution_identity,
        events=events,
        gate=gate,
    )
    if bound is None:
        return None
    started = await emit_verifier_started_event(gate._loop, bound)
    return started, bound


def _appkit_outcome_raw(outcome: ObservationEvent | AgentErrorEvent | None) -> dict[str, Any]:
    return (
        outcome.tool_result.structured
        if isinstance(outcome, ObservationEvent)
        and isinstance(outcome.tool_result.structured, dict)
        else {}
    )


def _appkit_raw_passed(
    outcome: ObservationEvent | AgentErrorEvent | None, raw: dict[str, Any]
) -> bool:
    return bool(
        isinstance(outcome, ObservationEvent)
        and outcome.tool_result.success
        and raw.get("passed") is True
    )


def _appkit_artifact_identity_mismatches(
    *,
    artifact_identity: VerificationArtifactIdentity | None,
    raw_entry: str | None,
    deliverable: HostVerificationDeliverable,
    started: VerifierStartedEvent,
    outcome: ObservationEvent | AgentErrorEvent | None,
    runtime_identity: VerificationExecutionIdentity | None,
    check: VerificationCheckContract,
) -> bool:
    return (
        artifact_identity is None
        or raw_entry != deliverable.artifact_path
        or type(started.seq) is not int
        or not isinstance(outcome, ObservationEvent)
        or type(outcome.seq) is not int
        or outcome.seq <= started.seq
        or runtime_identity != deliverable.execution_identity
        or artifact_identity.entry_reference != deliverable.artifact_path
        or artifact_identity.producer_id != check.issuer_id
        or (
            check.required_artifact_identity_scheme is not None
            and artifact_identity.scheme != check.required_artifact_identity_scheme
        )
    )


def _appkit_binding_error(
    *,
    raw_passed: bool,
    raw: dict[str, Any],
    raw_entry: str | None,
    deliverable: HostVerificationDeliverable,
    started: VerifierStartedEvent,
    outcome: ObservationEvent | AgentErrorEvent | None,
    runtime_identity: VerificationExecutionIdentity | None,
    check: VerificationCheckContract,
) -> tuple[VerificationArtifactIdentity | None, str]:
    if not raw_passed:
        return None, ""
    try:
        artifact_identity = VerificationArtifactIdentity.model_validate(
            raw.get("artifact_identity")
        )
    except Exception:
        return None, "strict AppKit PASS omitted a valid immutable artifact identity"
    if _appkit_artifact_identity_mismatches(
        artifact_identity=artifact_identity,
        raw_entry=raw_entry,
        deliverable=deliverable,
        started=started,
        outcome=outcome,
        runtime_identity=runtime_identity,
        check=check,
    ):
        return (
            artifact_identity,
            "strict AppKit PASS did not match the started runtime, canonical entry, "
            "or artifact producer authority",
        )
    return artifact_identity, ""


def _appkit_status(
    *, raw_passed: bool, binding_error: str, outcome: ObservationEvent | AgentErrorEvent | None
) -> VerificationClaimStatus:
    if raw_passed and not binding_error:
        return VerificationClaimStatus.PASS
    unavailable = outcome is None or isinstance(outcome, AgentErrorEvent) or bool(binding_error)
    return VerificationClaimStatus.UNAVAILABLE if unavailable else VerificationClaimStatus.FAIL


def _appkit_observed_after_seq(
    outcome: ObservationEvent | AgentErrorEvent | None,
    started: VerifierStartedEvent,
    deliverable: HostVerificationDeliverable,
) -> int | None:
    if outcome is not None and type(outcome.seq) is int:
        return outcome.seq
    if type(started.seq) is int:
        return started.seq
    return deliverable.observed_after_seq


def _appkit_reason(
    binding_error: str, raw: dict[str, Any], outcome: ObservationEvent | AgentErrorEvent | None
) -> str:
    return str(
        binding_error
        or raw.get("summary")
        or (
            outcome.error
            if isinstance(outcome, AgentErrorEvent)
            else "strict AppKit verifier did not produce a usable result"
        )
    )


def _appkit_claim_status(
    claim: HostVerificationClaim,
    *,
    base_status: VerificationClaimStatus,
    raw_application_title: object,
) -> VerificationClaimStatus:
    if claim.kind is not VerificationClaimKind.APPLICATION_IDENTITY:
        return base_status
    if base_status is VerificationClaimStatus.UNAVAILABLE or not isinstance(
        raw_application_title, str
    ):
        return VerificationClaimStatus.UNAVAILABLE
    if raw_application_title == claim.expected:
        return VerificationClaimStatus.PASS
    return VerificationClaimStatus.FAIL


def _appkit_claim_reason(
    claim: HostVerificationClaim, *, raw_application_title: object, reason: str
) -> str:
    if claim.kind is not VerificationClaimKind.APPLICATION_IDENTITY:
        return reason
    if not isinstance(raw_application_title, str):
        return "strict AppKit verifier omitted authoritative application identity"
    # State BOTH sides: a bare "authoritative AppSpec name is 'X'" on a
    # FAIL reads as a fact with no expectation, which hides whether the
    # app or the requirement is wrong.
    suffix = "" if raw_application_title == claim.expected else f"; required {claim.expected!r}"
    return f"authoritative AppSpec name is {raw_application_title!r}{suffix}"


def _appkit_claim_result(
    claim: HostVerificationClaim,
    *,
    base_status: VerificationClaimStatus,
    raw_application_title: object,
    check: VerificationCheckContract,
    evidence_event_id: str,
    deliverable: HostVerificationDeliverable,
    reason: str,
) -> HostVerificationClaimResult:
    return HostVerificationClaimResult(
        claim_id=claim.claim_id,
        kind=claim.kind,
        required=claim.required,
        expected=claim.expected,
        source_authority=claim.source_authority,
        reference_image_sha256=claim.reference_image_sha256,
        status=_appkit_claim_status(
            claim, base_status=base_status, raw_application_title=raw_application_title
        ),
        reason=_appkit_claim_reason(
            claim, raw_application_title=raw_application_title, reason=reason
        ),
        verifier_id=check.issuer_id,
        capability_basis="configured strict AppKit target verifier",
        evidence_modalities=(VerificationEvidenceModality.TARGET_SPECIFIC,),
        evidence_refs=(
            f"event:{evidence_event_id}",
            f"artifact:{deliverable.artifact_path}",
        ),
    )


def _appkit_result_reason(
    claim_results: tuple[HostVerificationClaimResult, ...], fallback: str
) -> str:
    return next(
        (
            result.reason
            for result in claim_results
            if result.required and result.status is not VerificationClaimStatus.PASS
        ),
        fallback,
    )


async def record_appkit_typed_verdict(
    gate: Any,
    events: list[Event],
    prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable],
) -> VerificationClaimStatus | None:
    """Pair every AppKit START with an exact typed terminal verifier status."""

    started, deliverable = prepared
    outcome = latest_appkit_verification_outcome(events, started)
    raw = _appkit_outcome_raw(outcome)
    check = deliverable.verification_check
    if check is None:
        return None
    raw_passed = _appkit_raw_passed(outcome, raw)
    raw_entry = _safe_deliverable_file_path(str(raw.get("canonical_entry_path") or ""))
    runtime_identity = (
        appkit_runtime_identity(raw.get("preview_runtime"))
        if isinstance(outcome, ObservationEvent)
        else None
    )
    artifact_identity, binding_error = _appkit_binding_error(
        raw_passed=raw_passed,
        raw=raw,
        raw_entry=raw_entry,
        deliverable=deliverable,
        started=started,
        outcome=outcome,
        runtime_identity=runtime_identity,
        check=check,
    )
    base_status = _appkit_status(
        raw_passed=raw_passed, binding_error=binding_error, outcome=outcome
    )
    observed_after_seq = _appkit_observed_after_seq(outcome, started, deliverable)
    bound_deliverable = deliverable.model_copy(
        update={
            "artifact_identity": artifact_identity,
            "observed_after_seq": observed_after_seq,
        }
    )
    url = str(raw.get("url") or "")
    reason = _appkit_reason(binding_error, raw, outcome)
    evidence_event_id = outcome.id if outcome is not None else started.id
    raw_application_title = raw.get("application_title")
    claim_results: tuple[HostVerificationClaimResult, ...] = tuple(
        _appkit_claim_result(
            claim,
            base_status=base_status,
            raw_application_title=raw_application_title,
            check=check,
            evidence_event_id=evidence_event_id,
            deliverable=deliverable,
            reason=reason,
        )
        for claim in bound_deliverable.required_claims
    )
    result_reason = _appkit_result_reason(claim_results, reason)
    screenshot_path = bounded_host_verdict_screenshot_path(raw.get("screenshot_path"))
    typed_result = target_verification_result(
        deliverable=bound_deliverable,
        claim_results=claim_results,
        verifier_id=check.issuer_id,
        tool_id=check.operation,
        reason=result_reason,
        observed_url=url,
        screenshot_path=screenshot_path or "",
    )
    status = typed_result.status
    coverage = aggregate_verification_receipts(
        deliverables=(bound_deliverable,),
        receipts=(typed_result,),
    )
    if status is VerificationClaimStatus.PASS and not coverage.passed:
        return None
    host_verdict = dict(raw)
    host_verdict.update(
        {
            "passed": status is VerificationClaimStatus.PASS,
            "verdict": status.value,
            "url": url,
            "summary": typed_result.reason,
        }
    )
    await emit_host_verdict_audit(
        gate,
        bound_deliverable,
        events,
        host_verdict,
        typed_result,
        screenshot_path,
        verifier_started_event_id=started.id,
    )
    return status


def recorded_appkit_typed_status(
    events: list[Event],
    started: VerifierStartedEvent,
) -> VerificationClaimStatus | None:
    result = recorded_appkit_typed_result(events, started)
    return result.status if result is not None else None


def recorded_appkit_typed_result(
    events: list[Event],
    started: VerifierStartedEvent,
) -> HostVerificationResult | None:
    verdict = next(
        (
            event
            for event in reversed(events)
            if isinstance(event, VerifierVerdictEvent) and event.requested_by_event_id == started.id
        ),
        None,
    )
    return verdict.verification_result if verdict is not None else None


async def appkit_typed_gate_disposition(
    gate: Any,
    *,
    events: list[Event],
    verdict: dict[str, Any] | None,
    tool_name: str,
    prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None,
) -> Disp | None:
    if prepared is None:
        return None
    status = await record_appkit_typed_verdict(gate, events, prepared)
    if verdict is not None and (
        verdict.get("passed") is not True or status is VerificationClaimStatus.PASS
    ):
        return None
    current_events = await gate._loop._events()
    typed_result = recorded_appkit_typed_result(current_events, prepared[0])
    required_non_pass = (
        next(
            (
                result
                for result in typed_result.claim_results
                if result.required and result.status is not VerificationClaimStatus.PASS
            ),
            None,
        )
        if typed_result is not None
        else None
    )
    if required_non_pass is not None:
        failure_key = (
            f"{tool_name}:typed_claim:{required_non_pass.claim_id}:"
            f"{required_non_pass.status.value}:{required_non_pass.reason}"
        )
        guidance = (
            f"mandatory typed claim {required_non_pass.claim_id!r} returned "
            f"{required_non_pass.status.value.upper()}: {required_non_pass.reason}. "
            "Correct the authoritative target state for this claim, then verify again."
        )
    else:
        failure_key = (
            f"{tool_name}:unavailable"
            if verdict is None
            else f"{tool_name}:typed_result_unavailable"
        )
        guidance = (
            f"{tool_name} did not return a usable strict target verdict. Repair "
            "the target verifier/runtime; unavailable verification cannot release "
            "this governed build."
            if verdict is None
            else "the strict target verifier reported PASS, but its result did not "
            "bind the current started runtime and immutable artifact."
        )
    return await gate._governed_contract_refusal(
        current_events,
        failure_key=failure_key,
        guidance=guidance,
    )

"""Governed host-verify dispositions: turning a verdict into a gate `Disp`.

Owns: refuse-and-continue vs. STUCK/HALT vs. fall-through decisions for a
host verifier verdict, both the legacy shadow-verify disposition and the
governed-target typed-PASS disposition, and the shared progress-sensitive
fail-closed refusal every governed target-verification failure uses.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ....verification import HostVerificationClaimResult
from ..common import (
    _VERIFY_MARKER_PREFIX,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    HostVerificationDeliverable,
    HostVerificationResult,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    VerificationClaimStatus,
    _last_verification_authority_seq,
    _prior_verify_marker_fp,
)


async def host_verify_failure_disposition(
    gate: Any, deliverable: HostVerificationDeliverable, verdict: dict
) -> Disp:
    label = gate._verdict_label(verdict) or "fail"
    summary = str(verdict.get("summary") or verdict.get("detail") or "host verifier did not pass")
    next_action = str(verdict.get("next_action") or "")
    first_failure = gate._verdict_first_failure(verdict)
    await gate._record_verifier_failure_to_context(
        message=(first_failure or summary), rel_path=None
    )
    if gate._loop._browser_verify_refusals < 3:
        gate._loop._browser_verify_refusals += 1
        payload = (
            "<system-reminder>\n"
            f"Host verification did not pass for {deliverable.artifact_kind} "
            f"artifact {deliverable.artifact_path!r} ({label}). {summary}\n"
            + (f"first failure: {first_failure}\n" if first_failure else "")
            + (
                f"next step: {next_action}\n"
                if next_action
                else "Fix the issue surfaced by the host verifier, then finish again.\n"
            )
            + "The task is NOT complete until the host verifier passes.\n"
            "</system-reminder>"
        )
        await gate._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=payload),
            )
        )
        return Disp.CONTINUE

    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="unverified_release",
        )
    )
    warn = (
        "⚠ Finished WITHOUT a passing host verifier verdict (3 attempts) — "
        f"the deliverable is UNVERIFIED and may be INCOMPLETE. {summary}"
        + (f" Outstanding: {next_action}" if next_action else "")
        + " Note this clearly in your summary."
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=warn),
        )
    )
    gate._loop._browser_verify_refusals = 0
    return Disp.FALLTHROUGH


def _governed_claim_by_status(
    typed_result: HostVerificationResult | None, status: VerificationClaimStatus
) -> HostVerificationClaimResult | None:
    if typed_result is None:
        return None
    return next(
        (
            result
            for result in typed_result.claim_results
            if result.required and result.status is status
        ),
        None,
    )


def _governed_non_pass_guidance(
    *,
    typed_result: HostVerificationResult | None,
    host_label: str,
    next_action: str,
    summary: str,
    failed_claim: HostVerificationClaimResult | None,
    unavailable_claim: HostVerificationClaimResult | None,
) -> str:
    if typed_result is None:
        return (
            "no current, complete typed receipt certifies the mandatory "
            "structured-browser claims. Start a managed preview with "
            "`preview_start` for the app entry, then finish again — the "
            "host verifier will bind to the canonical Preview generation "
            "and produce a complete typed receipt."
        )
    if failed_claim is not None:
        return (
            f"the typed host receipt reported a FAIL ({failed_claim.reason})."
            + (f" next_action: {next_action}" if next_action else "")
            + " Fix the failed claim, then finish again."
        )
    claim_detail = unavailable_claim.reason if unavailable_claim is not None else summary
    return (
        f"the host verifier returned {host_label} ({claim_detail})."
        + (f" next_action: {next_action}" if next_action else "")
        + " Start a managed preview with `preview_start` or fix the "
        "surfaced issue, then finish again."
    )


async def governed_non_pass_disposition(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    verdict: dict,
    typed_result: HostVerificationResult | None,
    events: list[Event],
) -> Disp:
    """Refuse governed completion without a current, complete typed PASS.

    Recovery is progress-sensitive rather than retry-counted.  The first
    failure returns exact guidance and stamps its stable fingerprint.  The
    same failure repeated with no intervening authority change lands STUCK;
    any productive mutation, Preview lifecycle change, handoff, admission,
    or new user instruction moves the authority floor past that marker and
    permits another honest verification attempt.
    """
    host_label = gate._verdict_label(verdict) or "fail"
    summary = str(
        verdict.get("summary")
        or verdict.get("detail")
        or "host verifier did not produce a current typed PASS receipt"
    )
    next_action = str(verdict.get("next_action") or "")
    first_failure = gate._verdict_first_failure(verdict)
    failed_claim = _governed_claim_by_status(typed_result, VerificationClaimStatus.FAIL)
    unavailable_claim = _governed_claim_by_status(
        typed_result, VerificationClaimStatus.UNAVAILABLE
    )
    guidance = _governed_non_pass_guidance(
        typed_result=typed_result,
        host_label=host_label,
        next_action=next_action,
        summary=summary,
        failed_claim=failed_claim,
        unavailable_claim=unavailable_claim,
    )

    await gate._record_verifier_failure_to_context(
        message=(first_failure or summary), rel_path=None
    )
    raw_fingerprint = str(
        verdict.get("failure_fingerprint")
        or (
            typed_result.reason
            if typed_result is not None
            else "missing_or_mismatched_typed_receipt"
        )
    )
    fingerprint = "host:" + hashlib.sha256(raw_fingerprint.encode()).hexdigest()[:24]
    authority_seq = _last_verification_authority_seq(events)
    if _prior_verify_marker_fp(events, authority_seq) == fingerprint:
        await gate._loop._land_blocked(
            reason=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
            guidance=(
                "Host verification repeated the same governed failure with no "
                f"productive authority change. {guidance}"
            ),
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
        )
        return Disp.HALT

    payload = (
        "<system-reminder>\n"
        f"Host verification did not pass for {deliverable.artifact_kind} "
        f"artifact {deliverable.artifact_path!r} ({host_label}). {guidance}\n"
        "The task is NOT complete until a current, complete typed PASS "
        "receipt covering every required claim exists.\n"
        "</system-reminder>"
    )
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(role="user", content=payload),
        )
    )
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
        )
    )
    return Disp.CONTINUE


async def governed_contract_refusal(
    gate: Any,
    events: list[Event],
    *,
    failure_key: str,
    guidance: str,
) -> Disp:
    """Progress-sensitive fail-closed refusal without a retry release cap."""

    fingerprint = "host:" + hashlib.sha256(failure_key.encode()).hexdigest()[:24]
    authority_seq = _last_verification_authority_seq(events)
    if _prior_verify_marker_fp(events, authority_seq) == fingerprint:
        await gate._loop._land_blocked(
            reason=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
            guidance=(
                "Target verification repeated the same governed failure with no "
                f"productive authority change. {guidance}"
            ),
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
        )
        return Disp.HALT
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"Target verification did not pass. {guidance}\n"
                    "The task is NOT complete until every admitted mandatory "
                    "claim has a current trusted result.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
        )
    )
    return Disp.CONTINUE

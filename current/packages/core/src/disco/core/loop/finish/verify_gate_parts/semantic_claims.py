"""Model-judged semantic claim evaluation for a host verdict.

Owns: binding each CONTRACT_SEMANTIC / VISUAL_SEMANTIC claim to one bounded,
independent model judgement, folding the results into the host verdict, and
the small verdict-medium / failure-detail readers the render-verify gates
share.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..common import (
    AgentStep,
    Event,
    HostVerificationClaim,
    HostVerificationDeliverable,
    HostVerificationResult,
    TypedVerifierVerdict,
    VerificationClaimKind,
    VerifierContextSeed,
    apply_semantic_verifier_result,
)
from .host_claims import bounded_model_verifier_cause


def _semantic_claims_to_judge(
    deliverable: HostVerificationDeliverable,
) -> tuple[HostVerificationClaim, ...]:
    return tuple(
        claim
        for claim in deliverable.required_claims
        if claim.kind
        in {
            VerificationClaimKind.CONTRACT_SEMANTIC,
            VerificationClaimKind.VISUAL_SEMANTIC,
        }
    )


def _parsed_verification_receipt(host_verdict: dict[str, Any]) -> HostVerificationResult | None:
    try:
        raw_receipt = host_verdict.get("verification_result")
        return (
            raw_receipt
            if isinstance(raw_receipt, HostVerificationResult)
            else HostVerificationResult.model_validate(raw_receipt)
        )
    except Exception:
        return None


def _missing_visual_evidence(
    claim: HostVerificationClaim, seed: VerifierContextSeed
) -> tuple[bool, bool]:
    missing_pixels = (
        claim.kind is VerificationClaimKind.VISUAL_SEMANTIC
        and not seed.screenshot.image_data_url
    )
    missing_reference = (
        claim.kind is VerificationClaimKind.VISUAL_SEMANTIC
        and "reference_image_sha256:" in claim.expected
        and not any(
            reference.image_data_url and reference.instruction_complete
            for reference in seed.reference_images
        )
    )
    return missing_pixels, missing_reference


def _visual_evidence_unavailable_verdict(missing_pixels: bool) -> TypedVerifierVerdict:
    detail = (
        "visual claim has no captured output pixel evidence"
        if missing_pixels
        else "visual claim's user reference pixels are unavailable"
    )
    return TypedVerifierVerdict(
        verified=False,
        verdict="unavailable",
        detail=detail,
        failures=[{"kind": "visual_evidence_unavailable", "message": detail}],
        failure_fingerprint="model_verifier_unavailable",
    )


async def _run_semantic_judge(
    gate: Any, judge: Any, seed: VerifierContextSeed
) -> tuple[TypedVerifierVerdict, bool, str]:
    """Run one bounded judge call. Returns (typed, applied, fallback_cause)."""

    try:
        raw_typed = await asyncio.wait_for(
            judge.judge(seed),
            timeout=max(0.001, float(getattr(gate._loop, "_verifier_judge_timeout_s", 30.0))),
        )
        typed = (
            raw_typed
            if isinstance(raw_typed, TypedVerifierVerdict)
            else TypedVerifierVerdict.model_validate(raw_typed)
        )
        fallback_cause = ""
        if (
            typed.verdict in {"unavailable", "unverifiable"}
            and typed.failure_fingerprint == "model_verifier_unavailable"
        ):
            fallback_cause = typed.detail or typed.failure_fingerprint
        return typed, True, fallback_cause
    except TimeoutError:
        typed = TypedVerifierVerdict(
            verified=False,
            verdict="unavailable",
            detail="model verifier deadline exceeded",
            failure_fingerprint="model_verifier_unavailable",
        )
        return typed, False, typed.detail
    except Exception as exc:  # noqa: BLE001 — verifier failure stays bounded
        typed = TypedVerifierVerdict(
            verified=False,
            verdict="unavailable",
            detail=f"judge exception: {type(exc).__name__}",
            failure_fingerprint="model_verifier_unavailable",
        )
        return typed, False, typed.detail


async def judge_semantic_claims(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    host_verdict: dict[str, Any],
) -> dict[str, Any]:
    """Bind each bounded independent judgement to one exact semantic claim."""

    judge = getattr(gate._loop, "_verifier_judge", None)
    if judge is None or gate._verdict_label(host_verdict) in {"unavailable", "unverifiable"}:
        return host_verdict
    semantic_claims = _semantic_claims_to_judge(deliverable)
    if not semantic_claims:
        return host_verdict
    receipt = _parsed_verification_receipt(host_verdict)
    if receipt is None:
        return host_verdict

    contract = gate._verifier_contract_payload()
    out = dict(host_verdict)
    failures: list[dict[str, Any]] = []
    applied = False
    fallback_cause = ""
    for claim in semantic_claims:
        seed = await gate._verifier_context_seed(
            deliverable,
            events,
            host_verdict,
            contract=contract,
            claims=(claim,),
        )
        missing_pixels, missing_reference = _missing_visual_evidence(claim, seed)
        if missing_pixels or missing_reference:
            typed = _visual_evidence_unavailable_verdict(missing_pixels)
            fallback_cause = typed.detail
        else:
            typed, applied_now, cause_now = await _run_semantic_judge(gate, judge, seed)
            if applied_now:
                applied = True
            if cause_now:
                fallback_cause = cause_now
        receipt = apply_semantic_verifier_result(
            receipt,
            verified=typed.verified,
            verdict=typed.verdict,
            detail=typed.detail,
            claim_id=claim.claim_id,
        )
        failures.extend(typed.failures)

    out.update(
        {
            "passed": receipt.passed,
            "verdict": receipt.status.value,
            "summary": receipt.reason,
            "detail": receipt.reason,
            "failures": failures,
            "verification_result": receipt.model_dump(mode="json"),
            "model_verifier": True,
            "model_verifier_status": receipt.status.value,
            "model_verifier_applied": applied,
        }
    )
    if fallback_cause:
        out["model_verifier_cause"] = bounded_model_verifier_cause(fallback_cause)
    return out


async def finish_verification_medium(
    gate: Any,
    step: AgentStep | None,
    events: list[Event],
) -> str:
    if step is None:
        return "web"
    try:
        deliverable = await gate._host_verify_deliverable(step, events)
        if deliverable is None:
            return "web"
        paths = await gate._verifier_deliverable_paths(deliverable, events)
        hint = await gate._verifier_medium_hint(paths)
        return hint.kind if hint is not None else "web"
    except Exception:
        return "web"


def verification_failure_details(
    verdict: dict[str, Any],
    tool_name: str,
) -> tuple[str, str, str, str, str]:
    fp = str(verdict.get("failure_fingerprint") or "")
    summary = str(verdict.get("summary") or f"{tool_name} did not pass")
    next_action = str(verdict.get("next_action") or "")
    screenshot = str(verdict.get("screenshot_path") or "")
    errors = verdict.get("console_errors") or []
    network_failures = verdict.get("network_failures") or []
    if errors:
        first = errors[0]
        where = f" @ {first.get('source')}" if first.get("source") else ""
        first_error = f"{first.get('text', '')}{where}"
    elif network_failures:
        first = network_failures[0]
        marker = first.get("status") or first.get("failure") or "failed"
        first_error = f"{first.get('method', 'GET')} {first.get('url', '')} -> {marker}"
    else:
        first_error = ""
    return fp, summary, next_action, screenshot, first_error

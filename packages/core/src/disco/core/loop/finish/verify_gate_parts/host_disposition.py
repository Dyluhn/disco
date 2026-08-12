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
    _latest_app_deliverable_event,
    _prior_verify_marker_fp,
)
from .host_claims import preview_selection_at

# ATTESTATION-BINDING INVARIANT (F58, 2026-08-07m; enforced 2026-08-07r). These
# two sentences are the agent-facing prose the HALT branches below hand to
# `_land_blocked`, and a test that asserts on them must DERIVE them from here
# rather than restate them — a restatement is a copy, and a copy attests itself.
# They were inline f-string literals inside the two branch bodies, so there was
# no symbol to import and `test_f53_constraint4_confirmed_repeats._HALT_GUIDANCE`
# was a hand-copied duplicate that would have kept passing had this prose been
# reworded. Extracted verbatim: the composed guidance is byte-identical to what
# the inline literals produced.
_HOST_REPEAT_HALT_PREFIX = (
    "Host verification repeated the same governed failure with no productive authority change. "
)
_TARGET_REPEAT_HALT_PREFIX = (
    "Target verification repeated the same governed failure with no productive authority change. "
)


def _verify_marker_fire_count(events: list[Event], fingerprint: str) -> int:
    """How many times THIS exact governed failure has been surfaced, plus this one.

    GROUNDED FEEDBACK constraint 4 (the twice-rule), F51 (2026-08-07d/e). The two
    governed refusal seams below stamp a durable
    ``verify_no_progress:<fingerprint>`` marker after every emit, so the run's own
    log already records how many times the agent has been told this exact thing.
    Nothing read it. `_prior_verify_marker_fp` deliberately scans only markers
    ABOVE the authority floor — that is the loop breaker's question ("same failure,
    no productive change since?") — and every productive edit moves the floor, so a
    run that churns productively while failing host verification identically resets
    that window on every pass. `pilota/001` at 2026-08-07d did exactly that and was
    told the identical sentence **fifteen times**.

    This is the OTHER question, and it needs the WHOLE log: "how many times have we
    said this, ever, in this run?" Ledger-derived, not an instance counter, so the
    count survives a restart or condensation exactly as the run's own evidence does
    — the same reason `turn_control_support._prior_diagnostic_count` reads the log.

    **It changes no decision.** The loop breaker's comparison is untouched; this
    feeds the MESSAGE only. Constraints 1 and 4 are independent: a surface can be
    genuinely state-derived and still emit identical bytes on every fire, because in
    a repeat loop the state it renders has not changed by construction.
    """
    marker = f"{_VERIFY_MARKER_PREFIX}{fingerprint}"
    return 1 + sum(1 for ev in events if isinstance(ev, StatusEvent) and ev.detail == marker)


def _repeat_preamble(repeats: int, cost: str) -> str:
    """The escalation line for a governed refusal that has fired before.

    GROUNDED FEEDBACK constraint 4 (F51). The owner's rule for any surface that
    can fire more than once per run is "repetition-aware at minimum (count
    acknowledged, escalating), ledger-derived where state exists" — and here the
    state exists, in the run's own durable markers.

    The shape is `turn_control_support._serve_duplicate_guidance`'s, written by
    this campaign ten lines below one of the F51 defects: name the COUNT, then
    name the COST. Repeating a sentence to a model that already mis-read it gives
    it nothing new to act on; the count and the consequence are information the
    host already holds, and withholding them is what turns one misread into a
    terminal.

    ``cost`` is passed in rather than templated because the three seams below do
    genuinely different things on the next repeat — the governed seams HALT, the
    legacy shadow-verify seam releases UNVERIFIED. A shared sentence would have to
    be vague enough to be wrong about one of them.
    """
    return (
        f"REPEAT {repeats} — you have now been told this same verification failure "
        f"{repeats} times in this run, and nothing about it has changed. {cost}\n"
    )


async def host_verify_failure_disposition(
    gate: Any, deliverable: HostVerificationDeliverable, verdict: dict
) -> Disp:
    """Release an ordinary/freeform failure honestly without reopening build.

    Admitted target contracts use the separate governed disposition below and
    remain fail-closed. This compatibility path is the terminal host judge for
    an ordinary artifact, not another agent-facing debugging coach.
    """
    summary = str(verdict.get("summary") or verdict.get("detail") or "host verifier did not pass")
    next_action = str(verdict.get("next_action") or "")
    first_failure = gate._verdict_first_failure(verdict)
    await gate._record_verifier_failure_to_context(
        message=(first_failure or summary), rel_path=None
    )
    await gate._loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="unverified_release",
        )
    )
    warn = (
        "⚠ Finished WITHOUT a passing host verifier verdict — "
        f"the deliverable is UNVERIFIED and may be INCOMPLETE. {summary}"
        + (f" First failure: {first_failure}" if first_failure else "")
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


def _preview_rehandoff_required(
    events: list[Event], deliverable: HostVerificationDeliverable
) -> bool:
    """True only when a real app handoff and current Preview have different identities."""
    if deliverable.artifact_kind != "app":
        return False
    handoff = _latest_app_deliverable_event(events)
    if handoff is None or type(handoff.seq) is not int:
        return False
    max_seq = max((event.seq or 0 for event in events), default=0)
    handed_off = preview_selection_at(events, through_seq=handoff.seq)
    current = preview_selection_at(events, through_seq=max_seq)
    return bool(
        handed_off is not None
        and current is not None
        and handed_off.operational_identity != current.operational_identity
    )


def _governed_non_pass_guidance(
    *,
    typed_result: HostVerificationResult | None,
    host_label: str,
    next_action: str,
    summary: str,
    failed_claim: HostVerificationClaimResult | None,
    unavailable_claim: HostVerificationClaimResult | None,
    preview_rehandoff_required: bool = False,
) -> str:
    claim_reasons = " ".join(
        [summary, next_action]
        + [claim.reason for claim in (failed_claim, unavailable_claim) if claim is not None]
    ).casefold()
    if (
        preview_rehandoff_required
        or "selected preview generation is absent, changed" in claim_reasons
    ):
        return (
            "the managed Preview generation changed after the last deliverable handoff. "
            "Keep the current managed preview running; once it is healthy, call `serve` "
            "again for the same app artifact so the handoff binds the current generation, "
            "then call `finish` again. Do not restart a healthy preview."
        )
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
    unavailable_claim = _governed_claim_by_status(typed_result, VerificationClaimStatus.UNAVAILABLE)
    guidance = _governed_non_pass_guidance(
        typed_result=typed_result,
        host_label=host_label,
        next_action=next_action,
        summary=summary,
        failed_claim=failed_claim,
        unavailable_claim=unavailable_claim,
        preview_rehandoff_required=_preview_rehandoff_required(events, deliverable),
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
            guidance=f"{_HOST_REPEAT_HALT_PREFIX}{guidance}",
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
        )
        return Disp.HALT

    # Constraint 4 (F51) — the 15× surface. This body is genuinely state-derived
    # (artifact kind, path, host label, and a guidance projected from the typed
    # receipt's own claim results) and it STILL emitted fifteen byte-identical
    # sentences in `pilota/001` at 2026-08-07d while the run failed to progress.
    # The reason is structural: the loop breaker above scans only markers above
    # the authority floor, every productive edit raises that floor, so a run that
    # keeps editing while failing verification identically never trips the breaker
    # and never hears anything new. The whole-log count is the missing signal, and
    # the run's own markers already carry it.
    repeats = _verify_marker_fire_count(events, fingerprint)
    payload = (
        "<system-reminder>\n"
        + (
            _repeat_preamble(
                repeats,
                "Repeating `finish` cannot change this: the same failure with no "
                "productive change between attempts ENDS the run. Make the one "
                "change named below before finishing again, or finish and state "
                "plainly that it is blocked.",
            )
            if repeats > 1
            else ""
        )
        + f"Host verification did not pass for {deliverable.artifact_kind} "
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
            guidance=f"{_TARGET_REPEAT_HALT_PREFIX}{guidance}",
            legacy_status=ConversationStatus.STUCK,
            legacy_detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
        )
        return Disp.HALT
    # Constraint 4 (F51). Same seam shape and same durable marker as the governed
    # non-pass disposition above, so the same whole-log count applies. This is the
    # surface that composes `host_claims.handoff_refusal_detail`'s text (F51's
    # fifth surface) into an emitted body — that helper is a pure describer with no
    # access to the log, so repetition-awareness belongs HERE, at the seam that
    # emits, which is the only place the run's own history is reachable.
    repeats = _verify_marker_fire_count(events, fingerprint)
    await gate._loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    + (
                        _repeat_preamble(
                            repeats,
                            "Handing off or finishing again without changing what "
                            "the claim measures cannot change the result: a repeat "
                            "with no productive change between attempts ENDS the "
                            "run.",
                        )
                        if repeats > 1
                        else ""
                    )
                    + f"Target verification did not pass. {guidance}\n"
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

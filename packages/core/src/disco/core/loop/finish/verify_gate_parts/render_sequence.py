"""The shared render-verify gate sequence run by every finish path.

Owns: host+browser app-verify (order depends on the authoritative flag),
the strict-AppKit typed preflight/postcheck wrapped around that sequence,
and the target-handoff-shape refusal — THEN the P10 export-render gate for
file deliverables. Extracted so `handle_finish_path` (affirmative `finish()`)
and `_completed_via_notify_finish` (the actionless/notify valve) run the
SAME gates.
"""

from __future__ import annotations

from typing import Any

from ....verification import AdmittedVerificationContract
from ..common import (
    AgentStep,
    Disp,
    Event,
    EventSource,
    HostVerificationDeliverable,
    LLMMessage,
    MessageEvent,
    VerificationClaimStatus,
    VerifierStartedEvent,
    _appkit_scope_active,
    _latest_app_deliverable_event,
    _latest_deliverable_event,
)
from .appkit_typed import (
    prepare_appkit_typed_authority,
    recorded_appkit_typed_result,
    recorded_appkit_typed_status,
)
from .host_claims import (
    governed_structured_browser_target,
    governed_verification_contract,
    governed_verification_required,
    handoff_matches_verification_contract,
    handoff_refusal_detail,
    strict_appkit_compatibility_error,
    strict_appkit_contract,
)

_AppkitPrepared = tuple[VerifierStartedEvent, HostVerificationDeliverable] | None


async def _render_verify_appkit_preflight(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    contract: AdmittedVerificationContract,
) -> tuple[Disp | None, _AppkitPrepared, list[Event]]:
    compatibility_error = strict_appkit_compatibility_error(contract)
    if compatibility_error is not None:
        disp = await gate._governed_contract_refusal(
            events,
            failure_key=f"appkit:unsupported_contract:{compatibility_error}",
            guidance=compatibility_error,
        )
        return disp, None, events
    if gate._active_verify_tool() != "verify_appkit_app":
        disp = await gate._governed_contract_refusal(
            events,
            failure_key="appkit:strict_verifier_unavailable",
            guidance=(
                "the admitted AppKit target requires verify_appkit_app, but that "
                "strict verifier is not available in the current execution scope."
            ),
        )
        return disp, None, events
    appkit_prepared = await prepare_appkit_typed_authority(gate, step, events, contract)
    if appkit_prepared is None:
        disp = await gate._governed_contract_refusal(
            events,
            failure_key="appkit:typed_preflight_unavailable",
            guidance=(
                "the strict AppKit target could not bind its canonical entry "
                "and current managed runtime before verification."
            ),
        )
        return disp, None, events
    events = await gate._loop._events()
    return None, appkit_prepared, events


async def _render_verify_missing_handoff_disposition(
    gate: Any,
    events: list[Event],
    contract: AdmittedVerificationContract | None,
    strict_appkit: bool,
) -> Disp | None:
    handoff = _latest_deliverable_event(events)
    missing_or_foreign_handoff = (
        contract is not None
        and governed_verification_required(events)
        and not strict_appkit
        and (handoff is None or not handoff_matches_verification_contract(handoff, contract))
    )
    if missing_or_foreign_handoff:
        detail = handoff_refusal_detail(handoff, contract)
        return await gate._governed_contract_refusal(
            events,
            failure_key="target:missing_or_foreign_handoff",
            guidance=(
                f"{detail}. Hand off the target-owned entry with the adapter's "
                "interactive-app or artifact-files shape. Serving the SAME entry "
                "again will be ignored as a duplicate and will not clear this — "
                "change the fact named above."
            ),
        )
    legacy_missing_web_handoff = (
        contract is None
        and governed_structured_browser_target(events)
        and not _appkit_scope_active(gate._loop)
        and _latest_app_deliverable_event(events) is None
    )
    if legacy_missing_web_handoff:
        gate._loop._invisible_steps += 1
        await gate._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "finish refused: the current target has no exact, current "
                        "handoff matching its admitted delivery contract. Hand off "
                        "the target-owned entry with `serve`, using the interactive "
                        "app or artifact-files shape requested by the adapter, then "
                        "verify and finish."
                    ),
                ),
                meta={"diagnostic": "finish_target_shape_refused"},
            )
        )
        return await gate._loop._post_noop_valve()
    return None


async def _render_verify_appkit_checks(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    appkit_prepared: _AppkitPrepared,
) -> tuple[Disp, list[Event]]:
    disp = await gate.gate_browser_verify(step, events, appkit_prepared=appkit_prepared)
    if disp is Disp.CONTINUE or disp is Disp.HALT:
        return disp, events
    events = await gate._loop._events()
    appkit_result = (
        recorded_appkit_typed_result(events, appkit_prepared[0])
        if appkit_prepared is not None
        else None
    )
    if appkit_prepared is None or appkit_result is None or not appkit_result.passed:
        disp = await gate._governed_contract_refusal(
            events,
            failure_key="appkit:typed_receipt_unavailable",
            guidance=(
                "the strict AppKit verifier passed, but its result could not "
                "be bound to the current typed target authority."
            ),
        )
        return disp, events
    verified_appkit_deliverable = appkit_prepared[1].model_copy(
        update={
            "artifact_identity": appkit_result.artifact_identity,
            "observed_after_seq": appkit_result.observed_after_seq,
        }
    )
    disp = await gate.gate_host_verify(
        step,
        events,
        preverified=((verified_appkit_deliverable, appkit_result),),
    )
    return disp, events


async def _render_verify_run_checks(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    *,
    strict_appkit: bool,
    appkit_prepared: _AppkitPrepared,
) -> tuple[Disp, list[Event]]:
    if strict_appkit:
        return await _render_verify_appkit_checks(gate, step, events, appkit_prepared)
    if gate._host_verify_authoritative() or governed_verification_required(events):
        disp = await gate.gate_host_verify(step, events)
        if disp is Disp.CONTINUE or disp is Disp.HALT:
            return disp, events
        events = await gate._loop._events()
        disp = await gate.gate_browser_verify(step, events)
        return disp, events
    disp = await gate.gate_browser_verify(step, events)
    events = await gate._loop._events()
    await gate.gate_host_verify(step, events)  # shadow: advisory, records telemetry
    return disp, events


async def _render_verify_appkit_postcheck(
    gate: Any,
    events: list[Event],
    contract: AdmittedVerificationContract,
    appkit_prepared: _AppkitPrepared,
) -> Disp | None:
    handoff = _latest_deliverable_event(events)
    if handoff is None or not handoff_matches_verification_contract(handoff, contract):
        return await gate._governed_contract_refusal(
            events,
            failure_key="appkit:foreign_materialized_handoff",
            guidance=(
                "the strict verifier did not produce an exact current handoff "
                "for the admitted AppKit delivery contract."
            ),
        )
    typed_status = (
        recorded_appkit_typed_status(events, appkit_prepared[0])
        if appkit_prepared is not None
        else None
    )
    if typed_status is not VerificationClaimStatus.PASS:
        return await gate._governed_contract_refusal(
            events,
            failure_key="appkit:typed_receipt_unavailable",
            guidance=(
                "the strict AppKit verifier passed, but its result could not "
                "be bound to the current typed target authority."
            ),
        )
    return None


async def run_finish_verify_gates(gate: Any, step: AgentStep, events: list[Event]) -> Disp:
    """The render-verify gate sequence shared by EVERY finish path: host+browser
    app-verify (order depends on the authoritative flag) THEN the P10 export-render
    gate for file deliverables.

    Extracted so ``handle_finish_path`` (affirmative ``finish()``) and
    ``_completed_via_notify_finish`` (the actionless/notify valve) run the SAME
    gates — they had DRIFTED: the notify path historically skipped both
    ``gate_host_verify`` (so shadow agreement was never measurable on
    notify-completed builds, and an authoritative flip would leave a verify
    bypass) AND ``gate_export_render`` (a blank deck finishing via notify escaped
    the P10 check). Returns CONTINUE (a gate refused — caller must not finish),
    HALT (a gate landed the terminal status / loop-breaker), or FALLTHROUGH (all
    render-verify gates clear).
    """
    disp = await gate.gate_workflow_output_contract(step, events)
    if disp is Disp.CONTINUE or disp is Disp.HALT:
        return disp
    events = await gate._loop._events()

    contract = governed_verification_contract(events)
    strict_appkit = strict_appkit_contract(contract)
    appkit_prepared: _AppkitPrepared = None
    if contract is not None and strict_appkit:
        preflight_disp, appkit_prepared, events = await _render_verify_appkit_preflight(
            gate, step, events, contract
        )
        if preflight_disp is not None:
            return preflight_disp

    handoff_disp = await _render_verify_missing_handoff_disposition(
        gate, events, contract, strict_appkit
    )
    if handoff_disp is not None:
        return handoff_disp

    disp, events = await _render_verify_run_checks(
        gate, step, events, strict_appkit=strict_appkit, appkit_prepared=appkit_prepared
    )
    if disp is Disp.CONTINUE or disp is Disp.HALT:
        return disp
    if contract is not None and strict_appkit:
        events = await gate._loop._events()
        postcheck_disp = await _render_verify_appkit_postcheck(
            gate, events, contract, appkit_prepared
        )
        if postcheck_disp is not None:
            return postcheck_disp
    # [P10] Export render-correctness gate — for a files-deliverable (deck/
    # document) the app/browser gates above fall through, so THIS is the check
    # that a blank/truncated/corrupt export can't report FINISHED.
    events = await gate._loop._events()
    return await gate.gate_export_render(step, events)

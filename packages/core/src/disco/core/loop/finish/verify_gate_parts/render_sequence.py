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
    _latest_deliverable_event,
)
from .appkit_typed import (
    prepare_appkit_typed_authority,
    recorded_appkit_typed_result,
    recorded_appkit_typed_status,
)
from .host_claims import (
    governed_verification_contract,
    governed_verification_required,
    handoff_matches_verification_contract,
    handoff_precondition_missing,
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
                f"the admitted AppKit target {contract.target_id!r} requires "
                "verify_appkit_app, but the current execution scope offers "
                f"{gate._active_verify_tool() or 'no verify tool'}."
            ),
        )
        return disp, None, events
    appkit_prepared = await prepare_appkit_typed_authority(gate, step, events, contract)
    if appkit_prepared is None:
        disp = await gate._governed_contract_refusal(
            events,
            failure_key="appkit:typed_preflight_unavailable",
            guidance=(
                f"the strict AppKit target {contract.target_id!r} could not bind its "
                f"canonical entry ({contract.delivery.mode} delivery, "
                f"{contract.preview_modality} preview) and current managed runtime "
                "before verification."
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
    missing_handoff = handoff_precondition_missing(
        events,
        contract,
        strict_appkit=strict_appkit,
        appkit_scope_active=lambda: _appkit_scope_active(gate._loop),
    )
    if missing_handoff and contract is not None:
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
    if missing_handoff:
        gate._loop._invisible_steps += 1
        # Constraint 4: this refusal fires on EVERY finish attempt until a
        # handoff exists, and `serve` does not count as work — so a static body
        # spends the run down without ever saying so. Counted off the durable
        # log by the diagnostic label it already carried.
        refusals = 1 + sum(
            1
            for event in events
            if isinstance(event, MessageEvent)
            and event.meta.get("diagnostic") == "finish_target_shape_refused"
        )
        escalation = (
            ""
            if refusals <= 1
            else (
                f"\nSTOP — finish has now been refused for a missing handoff "
                f"{refusals} times in this run. Each refused finish spends one turn "
                "toward the no-progress limit that ENDS this run."
            )
        )
        await gate._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "finish refused: the current target has no exact, current "
                        "handoff matching its admitted delivery contract, and no "
                        "app deliverable is on the record at all. Next move: hand "
                        "off the target-owned entry with `serve` using "
                        'kind="app" (the interactive app entry), then verify and '
                        f"finish.{escalation}"
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
        missing = (
            "no typed AppKit preflight authority was prepared"
            if appkit_prepared is None
            else f"no typed AppKit result is recorded for verifier run {appkit_prepared[0].id}"
            if appkit_result is None
            else f"the recorded typed AppKit result for verifier run "
            f"{appkit_prepared[0].id} did not pass"
        )
        disp = await gate._governed_contract_refusal(
            events,
            failure_key="appkit:typed_receipt_unavailable",
            guidance=(
                "the strict AppKit verifier passed, but its result could not be "
                f"bound to the current typed target authority: {missing}."
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
    contract = governed_verification_contract(events)
    if contract is not None and not contract.required and not contract.checks:
        # An admitted artifact target with no checks is an explicit target
        # decision, not an invitation for legacy preview residue or a global
        # advisory flag to arm the web verifier.
        return Disp.FALLTHROUGH, events
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
                "the strict verifier did not produce an exact current handoff for "
                f"the admitted AppKit delivery contract {contract.target_id!r} "
                + (
                    "— no handoff is on the record at all."
                    if handoff is None
                    else f"— the newest handoff on record is {handoff.id}, which "
                    "does not match that contract."
                )
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
                "the strict AppKit verifier passed, but the typed target authority "
                f"records claim status {getattr(typed_status, 'value', typed_status)!r} "
                "for it, not `pass`."
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

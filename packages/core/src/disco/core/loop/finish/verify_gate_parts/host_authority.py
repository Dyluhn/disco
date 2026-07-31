"""Host-verification execution-authority binding and verdict audit trail.

Owns: binding a `HostVerificationDeliverable` to its current run/workspace/
execution authority before a host verifier runs, validating the typed
receipt a verifier returns against that authority, and emitting the shadow +
verdict audit events every host-verify path shares.
"""

from __future__ import annotations

from typing import Any, cast

from ....verification import (
    AdmittedVerificationContract,
    VerificationCheckContract,
    VerificationExecutionIdentity,
)
from ..common import (
    _LOG,
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
    VerifierShadowEvent,
    VerifierStartedEvent,
    VerifierVerdictEvent,
    _last_productive_seq,
    _last_verification_authority_seq,
    _latest_app_deliverable_event,
    _latest_deliverable_event,
    _latest_verify_verdict,
    current_build_platform_admission,
    current_workspace_agent_view_id,
    latest_workspace_run_intent,
)
from .host_claims import (
    _STARTUP_DIAGNOSTIC_SECRET_RE,
    governed_structured_browser_target,
    host_verification_claims,
    preview_selection_at,
)

_MODEL_VERIFIER_META_KEYS = (
    "model_verifier_status",
    "model_verifier_applied",
    "model_verifier_cause",
)


def _stable_preview_selection(
    events: list[Event],
    handoff: Any,
    max_seq: int,
) -> tuple[Any, Any, Any]:
    handoff_selection = (
        preview_selection_at(events, through_seq=handoff.seq)
        if handoff is not None and type(handoff.seq) is int
        else None
    )
    current_selection = preview_selection_at(events, through_seq=max_seq)
    stable_selection = (
        current_selection
        if handoff_selection is None
        else handoff_selection
        if current_selection is not None
        and handoff_selection.operational_identity == current_selection.operational_identity
        else None
    )
    return handoff_selection, current_selection, stable_selection


def _required_execution_checks(
    contract: AdmittedVerificationContract | None,
) -> tuple[VerificationCheckContract | None, bool]:
    required_checks = (
        tuple(check for check in contract.checks if check.required) if contract is not None else ()
    )
    selected_check = required_checks[0] if len(required_checks) == 1 else None
    uses_managed_web_runtime = bool(
        contract is None
        or any(
            check.required and check.required_execution_modality == "managed_preview"
            for check in required_checks
        )
    )
    return selected_check, uses_managed_web_runtime


def _bound_execution_identity(
    *,
    contract: AdmittedVerificationContract | None,
    selected_preview: Any,
    handoff: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    generation: str,
    epoch: int | None,
) -> VerificationExecutionIdentity | None:
    if selected_preview is not None:
        return VerificationExecutionIdentity(
            modality="managed_preview",
            instance_id=selected_preview.projection_id,
            generation=(
                f"{selected_preview.sandbox_instance_id}:{selected_preview.sandbox_generation}"
            ),
            locator=selected_preview.url,
        )
    if contract is None:
        return None
    return VerificationExecutionIdentity(
        modality="workspace_artifact",
        instance_id=handoff.id if handoff is not None else deliverable.artifact_path,
        generation=f"{_last_productive_seq(events)}:{generation or 'none'}:{epoch or 0}",
        locator=deliverable.artifact_path,
    )


def _bound_required_claims(
    gate: Any,
    *,
    contract: AdmittedVerificationContract | None,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    selected_check: VerificationCheckContract | None,
) -> tuple[HostVerificationClaim, ...]:
    if (
        contract is not None
        or deliverable.artifact_kind == "app"
        or governed_structured_browser_target(events)
    ):
        return host_verification_claims(
            gate._verifier_contract_payload(),
            events,
            check=selected_check,
        )
    return ()


def _run_intent_id_for(admission: Any, intent: Any) -> str | None:
    if admission is not None:
        return admission.run_intent_id
    return intent.id if intent is not None else None


def _preview_binding_required(
    *,
    uses_managed_web_runtime: bool,
    artifact_kind: str,
    admission: Any,
    handoff_selection: Any,
    current_selection: Any,
) -> bool:
    if not uses_managed_web_runtime or artifact_kind != "app":
        return False
    return admission is not None or handoff_selection is not None or current_selection is not None


def bind_host_verification_authority(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
) -> HostVerificationDeliverable:
    admission = current_build_platform_admission(events)
    intent = latest_workspace_run_intent(events)
    handoff = (
        _latest_app_deliverable_event(events)
        if deliverable.artifact_kind == "app"
        else _latest_deliverable_event(events)
    )
    executor = getattr(gate._loop, "executor", None)
    generation = str(getattr(executor, "_browser_generation", "") or "")
    raw_epoch = getattr(executor, "_workspace_mutation_epoch", None)
    epoch = raw_epoch if isinstance(raw_epoch, int) and raw_epoch > 0 else None
    max_seq = max((event.seq or 0 for event in events), default=0)
    authority_seq = _last_verification_authority_seq(events)
    handoff_selection, current_selection, stable_selection = _stable_preview_selection(
        events, handoff, max_seq
    )
    contract = (
        admission.verification_contract
        if admission is not None and admission.route == "platform"
        else None
    )
    selected_check, uses_managed_web_runtime = _required_execution_checks(contract)
    selected_preview = stable_selection if uses_managed_web_runtime else None
    execution_identity = _bound_execution_identity(
        contract=contract,
        selected_preview=selected_preview,
        handoff=handoff,
        deliverable=deliverable,
        events=events,
        generation=generation,
        epoch=epoch,
    )
    return deliverable.model_copy(
        update={
            "run_intent_id": _run_intent_id_for(admission, intent),
            "run_identity": admission.run_identity if admission is not None else None,
            "agent_view_id": current_workspace_agent_view_id(events),
            "deliverable_event_id": handoff.id if handoff is not None else None,
            "workspace_revision": _last_productive_seq(events),
            "workspace_generation": generation,
            "workspace_epoch": epoch,
            "observed_after_seq": authority_seq,
            "required_claims": _bound_required_claims(
                gate,
                contract=contract,
                deliverable=deliverable,
                events=events,
                selected_check=selected_check,
            ),
            "verification_contract": contract,
            "verification_check": selected_check,
            "execution_identity": execution_identity,
            "preview_selection": selected_preview,
            "preview_binding_required": _preview_binding_required(
                uses_managed_web_runtime=uses_managed_web_runtime,
                artifact_kind=deliverable.artifact_kind,
                admission=admission,
                handoff_selection=handoff_selection,
                current_selection=current_selection,
            ),
        }
    )


def typed_host_result(
    verdict: dict[str, Any],
    deliverable: HostVerificationDeliverable,
) -> HostVerificationResult | None:
    raw = verdict.get("verification_result")
    try:
        result = HostVerificationResult.model_validate(
            raw.model_dump(mode="json") if isinstance(raw, HostVerificationResult) else raw
        )
    except Exception:
        return None
    return (
        result
        if result.is_current_for(
            deliverable,
            observed_url=str(verdict.get("url") or ""),
        )
        else None
    )


def typed_host_mismatch(
    verdict: dict[str, Any],
    deliverable: HostVerificationDeliverable,
) -> str:
    """Name why the typed receipt was unusable, for the message the agent reads.

    Only called on the failure path. "Omitted or mismatched" tells the agent that one
    of ~30 bound facts disagreed but not which, and the only move it leaves is to guess
    the receipt shape and retry — which reads downstream as a degenerate model rather
    than as a caller starved of the one word it needed.
    """

    raw = verdict.get("verification_result")
    if raw is None:
        return "no receipt was returned"
    try:
        result = HostVerificationResult.model_validate(
            raw.model_dump(mode="json") if isinstance(raw, HostVerificationResult) else raw
        )
    except Exception:
        return "the receipt did not parse as a typed HostVerificationResult"
    mismatch = result.first_currency_mismatch(
        deliverable, observed_url=str(verdict.get("url") or "")
    )
    return f"the receipt does not bind this deliverable's {mismatch}" if mismatch else "unknown"


async def emit_verifier_started_event(
    loop: Any,
    deliverable: HostVerificationDeliverable,
) -> VerifierStartedEvent:
    emitted = await loop._emit(
        VerifierStartedEvent(
            artifact_path=deliverable.artifact_path,
            artifact_kind=deliverable.artifact_kind,
            requested_by_event_id=None,
            target_id=(
                deliverable.verification_contract.target_id
                if deliverable.verification_contract is not None
                else ""
            ),
            run_intent_id=deliverable.run_intent_id,
            run_identity=deliverable.run_identity,
            delivery_shape=(
                deliverable.verification_contract.delivery.shape
                if deliverable.verification_contract is not None
                else ""
            ),
            delivery_entry_reference=(
                deliverable.verification_contract.delivery.entry_reference
                if deliverable.verification_contract is not None
                else ""
            ),
            check_id=(
                deliverable.verification_check.check_id
                if deliverable.verification_check is not None
                else ""
            ),
            receipt_kind=(
                deliverable.verification_check.receipt_kind
                if deliverable.verification_check is not None
                else ""
            ),
            issuer_id=(
                deliverable.verification_check.issuer_id
                if deliverable.verification_check is not None
                else ""
            ),
            operation=(
                deliverable.verification_check.operation
                if deliverable.verification_check is not None
                else ""
            ),
            delegated_issuer_ids=(
                deliverable.verification_check.delegated_issuer_ids
                if deliverable.verification_check is not None
                else frozenset()
            ),
            verification_contract_digest=(
                deliverable.verification_contract.digest
                if deliverable.verification_contract is not None
                else None
            ),
            deliverable_event_id=deliverable.deliverable_event_id,
            execution_identity=deliverable.execution_identity,
            artifact_identity=deliverable.artifact_identity,
            preview_selection=deliverable.preview_selection,
            workspace_revision=deliverable.workspace_revision,
            workspace_generation=deliverable.workspace_generation,
            workspace_epoch=deliverable.workspace_epoch,
            observed_after_seq=deliverable.observed_after_seq,
            agent_view_id=deliverable.agent_view_id,
            meta={"requested_verification": deliverable.requested_verification},
        )
    )
    if not isinstance(emitted, VerifierStartedEvent):
        raise TypeError("verifier start event did not retain its typed authority")
    return emitted


async def prepare_typed_host_verdict(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    host_verdict: dict[str, Any],
) -> tuple[dict[str, Any], HostVerificationResult | None, bool]:
    host_verdict = await gate._model_judged_verdict(deliverable, events, host_verdict)
    browser_unavailable = bool(
        host_verdict.get("browser_unavailable") is True
        or host_verdict.get("failure_fingerprint") == "browser_unavailable"
        or (
            gate._verdict_label(host_verdict) == "unverifiable"
            and host_verdict.get("model_verifier_applied") is not True
        )
    )
    typed_required = (
        deliverable.verification_contract is not None or deliverable.artifact_kind == "app"
    )
    typed_result = typed_host_result(host_verdict, deliverable) if typed_required else None
    if typed_required and typed_result is None:
        host_verdict = gate._host_unavailable_verdict(
            deliverable,
            f"verification could not run: {typed_host_mismatch(host_verdict, deliverable)}.",
        )
    if typed_result is not None:
        host_verdict["passed"] = typed_result.passed
        host_verdict["verdict"] = typed_result.status.value
        host_verdict["summary"] = typed_result.reason
    return host_verdict, typed_result, browser_unavailable


async def emit_browser_unavailable_release(loop: Any) -> Disp:
    await loop._emit(StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_release"))
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished WITHOUT browser-render verification — the host verifier "
                    "reported the structured browser claims unavailable. The deliverable "
                    "is UNVERIFIED and may be INCOMPLETE; do not report it as a verified pass."
                ),
            ),
        )
    )
    loop._browser_verify_refusals = 0
    return Disp.FALLTHROUGH


def _verdict_detail_with_startup_diagnostic(host_verdict: dict[str, Any]) -> str:
    detail = str(host_verdict.get("summary") or host_verdict.get("detail") or "")
    startup_diagnostic = str(host_verdict.get("startup_diagnostic") or "").strip()
    if not startup_diagnostic:
        return detail
    startup_diagnostic = "".join(
        char for char in startup_diagnostic if char in "\n\t" or ord(char) >= 32
    )
    startup_diagnostic = _STARTUP_DIAGNOSTIC_SECRET_RE.sub("<redacted>", startup_diagnostic)[:1280]
    return (
        f"{detail}\nBrowser startup diagnostic: {startup_diagnostic}"
        if detail
        else f"Browser startup diagnostic: {startup_diagnostic}"
    )


def _host_verdict_agreement(host_label: str | None, inline_label: str | None) -> bool | None:
    if host_label is None or inline_label is None:
        return None
    return host_label == inline_label


def _verifier_meta_for_audit(
    deliverable: HostVerificationDeliverable, host_verdict: dict[str, Any]
) -> dict[str, Any]:
    verifier_meta: dict[str, Any] = {"requested_verification": deliverable.requested_verification}
    for key in _MODEL_VERIFIER_META_KEYS:
        if key in host_verdict:
            verifier_meta[key] = host_verdict[key]
    return verifier_meta


def _verdict_event_identity(
    typed_result: HostVerificationResult | None,
) -> tuple[str, str, str, str | None]:
    if typed_result is None:
        return "", "", "", None
    return (
        typed_result.target_id,
        typed_result.check_id,
        typed_result.receipt_kind,
        typed_result.verification_contract_digest,
    )


async def _invoke_verdict_hook(
    gate: Any, verdict_event: VerifierVerdictEvent, artifact_path: str
) -> None:
    hook = getattr(gate._loop, "_host_verifier_verdict_hook", None)
    if hook is None:
        return
    try:
        await hook(verdict_event)
    except Exception:  # noqa: BLE001 — canary bookkeeping is non-authoritative
        _LOG.warning(
            "REL-1d host verifier canary hook failed for %s:%s",
            gate._loop.conversation_id,
            artifact_path,
            exc_info=True,
        )


async def emit_host_verdict_audit(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    host_verdict: dict[str, Any],
    typed_result: HostVerificationResult | None,
    host_screenshot_path: str | None,
    *,
    verifier_started_event_id: str,
) -> str | None:
    inline_verdict = _latest_verify_verdict(
        events,
        _last_verification_authority_seq(events),
        target_url=None,
        tool_name=gate._active_verify_tool() or "verify_web_app",
    )
    host_label = gate._verdict_label(host_verdict)
    inline_label = gate._verdict_label(inline_verdict)
    agreement = _host_verdict_agreement(host_label, inline_label)
    detail = _verdict_detail_with_startup_diagnostic(host_verdict)
    verifier_meta = _verifier_meta_for_audit(deliverable, host_verdict)
    target_id, check_id, receipt_kind, verification_contract_digest = _verdict_event_identity(
        typed_result
    )
    await gate._loop._emit(
        VerifierShadowEvent(
            artifact_path=deliverable.artifact_path,
            artifact_kind=deliverable.artifact_kind,
            inline_verdict=inline_label,
            host_verdict=host_label,
            agreement=agreement,
            detail=detail or None,
            meta=dict(verifier_meta),
        )
    )
    verdict_event = await gate._loop._emit(
        VerifierVerdictEvent(
            artifact_path=deliverable.artifact_path,
            artifact_kind=deliverable.artifact_kind,
            verified=host_verdict.get("passed") is True and host_label == "pass",
            verdict=host_label,
            detail=detail or None,
            failures=gate._verdict_failures(host_verdict),
            screenshot_path=host_screenshot_path,
            verification_result=typed_result,
            target_id=target_id,
            check_id=check_id,
            receipt_kind=receipt_kind,
            verification_contract_digest=verification_contract_digest,
            requested_by_event_id=verifier_started_event_id,
            meta=dict(verifier_meta),
        )
    )
    await _invoke_verdict_hook(
        gate, cast(VerifierVerdictEvent, verdict_event), deliverable.artifact_path
    )
    return host_label

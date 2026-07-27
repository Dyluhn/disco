"""Browser, host, export, and render-verification gates for FinishGate."""

# ruff: noqa: F403,F405 -- mixin split intentionally shares the common import surface
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from ...events import active_verification_requirements_event
from ...verification import (
    AdmittedVerificationContract,
    HostVerificationClaimResult,
    PreviewSelectionIdentity,
    VerificationArtifactIdentity,
    VerificationCheckContract,
    VerificationClaimStatus,
    VerificationEvidenceModality,
    VerificationExecutionIdentity,
    aggregate_verification_receipts,
    requires_structured_browser_runtime,
    target_verification_result,
    unavailable_verification_result,
    validated_image_data_url,
)
from ...verify_medium import (
    VerifierMediumHint,
    detect_html_medium,
    html_manifest_hrefs,
    html_script_srcs,
)
from .common import *
from .common import (
    _FINISH_VERIFY_CAP,
    _LOG,
    _PREVIEW_PORTS,
    _VERIFY_MARKER_PREFIX,
    _appkit_scope_active,
    _artifact_record_kind,
    _bounded_verifier_check_results,
    _browser_content_meaningful,
    _browser_unavailable_observed,
    _browser_verified,
    _deliverable_event_paths,
    _FinishGateProto,
    _is_web_deliverable,
    _last_productive_seq,
    _last_verification_authority_seq,
    _latest_app_deliverable_event,
    _latest_browser_error,
    _latest_browser_screenshot,
    _latest_browser_structured,
    _latest_deliverable_event,
    _latest_host_verifier_event,
    _latest_verify_verdict,
    _missing_steps_all_verify,
    _nonbrowser_static_validation_passed,
    _plan_file_exists_paths,
    _preview_key,
    _prior_verify_marker_fp,
    _real_web_failure_evidence,
    _safe_deliverable_file_path,
    _screenshot_from_verdict,
    _tc_components_installed,
    _vision_mode,
)

_STARTUP_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|token|secret)\b"
    r"(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s;]+"
)
_MODEL_VERIFIER_META_KEYS = (
    "model_verifier_status",
    "model_verifier_applied",
    "model_verifier_cause",
)
_HOST_VERDICT_SCREENSHOT_PATH_MAX_CHARS = 512
_GAME_INTERACTION_CLAIM_ID = "web.interaction:canvas-keyboard-smoke"
_GAME_INTERACTION_EXPECTED = (
    "host browser completed canvas click, Space, ArrowRight, and post-interaction capture"
)
_SAFE_MODEL_VERIFIER_CAUSE_RE = re.compile(
    r"(?:"
    r"model verifier unavailable \("
    r"(?:JSONDecodeError: [A-Za-z0-9 _.-]{1,100} at line \d+ column \d+|"
    r"ValidationError: \d+ field error\(s\) \[[A-Za-z0-9_,.-]{1,160}\]|"
    r"LLM[A-Za-z]+Error(?:: provider [A-Za-z0-9_.-]{1,80} returned HTTP \d{3}"
    r"(?: type=[A-Za-z0-9_.-]{1,80})?|: (?:request timed out|connection error))?)"
    r"\)|"
    r"model verifier deadline exceeded|"
    r"judge exception: [A-Za-z_][A-Za-z0-9_]{0,100}|"
    r"deterministic host failure floor retained"
    r")"
)


def _bounded_model_verifier_cause(value: object) -> str:
    clean = "".join(char for char in str(value or "") if char in "\n\t" or ord(char) >= 32)
    clean = _STARTUP_DIAGNOSTIC_SECRET_RE.sub("<redacted>", clean)
    clean = clean[:256]
    if _SAFE_MODEL_VERIFIER_CAUSE_RE.fullmatch(clean):
        return clean
    return "model verifier unavailable (unclassified structural failure)"


def _bounded_host_verdict_screenshot_path(value: object) -> str | None:
    """Retain a usable, bounded path from the host verifier's raw verdict.

    The value is audit provenance, not a verification signal.  Do not truncate
    an overlong path into a different path, and do not persist control-bearing
    values.  Downstream evidence capture owns workspace jailing and namespace
    validation.
    """

    if not isinstance(value, str):
        return None
    path = value.strip()
    if (
        not path
        or len(path) > _HOST_VERDICT_SCREENSHOT_PATH_MAX_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        return None
    return path


def _host_verification_claims(
    contract: dict[str, Any],
    events: list[Event],
    *,
    check: VerificationCheckContract | None = None,
) -> tuple[HostVerificationClaim, ...]:
    admission = current_build_platform_admission(events)
    claims = list(
        check.claims
        if check is not None
        else admission.verification_claims
        if admission is not None and admission.verification_claims
        else default_structured_web_claims()
    )
    accepted = check.accepted_claim_kinds if check is not None else frozenset(VerificationClaimKind)
    contract_accepted = (
        frozenset(
            kind
            for contract_check in admission.verification_contract.checks
            for kind in contract_check.accepted_claim_kinds
        )
        if check is None and admission is not None and admission.verification_contract is not None
        else accepted
    )
    conditions = dictated_content_conditions_from_events(events)
    # `application.title` is SINGLE-VALUED: an app has exactly one name, so a later
    # revision that dictates a new title SUPERSEDES the earlier one rather than
    # adding a second. Accumulating both minted two mutually exclusive identity
    # claims — the retitle satisfied one and permanently failed the other, so the
    # build could never finish however correctly the model behaved (seed 406431:
    # AppSpec held the requested 'AppKit Restart Recovered 406431' while the
    # superseded 'AppKit Restart 406431' was still required). Visible-text claims
    # are NOT slot-scoped and keep accumulating; only identity collapses.
    latest_title_condition = next(
        (c for c in reversed(conditions) if c.requirement_slot == "application.title"),
        None,
    )
    for condition in conditions:
        digest = hashlib.sha256(condition.literal.encode("utf-8")).hexdigest()[:24]
        if (
            condition.requirement_slot == "application.title"
            and condition is latest_title_condition
            and VerificationClaimKind.APPLICATION_IDENTITY in accepted
            and VerificationClaimKind.APPLICATION_IDENTITY in contract_accepted
        ):
            claims.append(
                HostVerificationClaim(
                    claim_id=f"application.title:{digest}",
                    kind=VerificationClaimKind.APPLICATION_IDENTITY,
                    expected=condition.literal,
                    source_authority=f"user_event:{condition.source_event_id}",
                )
            )
        # A literal the user assigned to a WRITTEN document (REPORT.md, notes.txt)
        # is not a claim about the SERVED page, and demanding it in the rendered DOM
        # makes the build unsatisfiable for an agent that obeys the instruction.
        # Epic-4 seed 460009: "Create REPORT.md headed exactly 'Ledger Audit 460009'
        # ... Update index.html to show 'Ledger Audited 460009'" required both
        # strings in the page; the agent oscillated (fix one, break the other) until
        # the no-progress detector gave up, and the runs that passed passed only
        # because they happened to put the report heading on the page too. Exactly
        # the mutually-exclusive-claims failure the application.title slot above
        # already guards (seed 406431), one slot over.
        if VerificationClaimKind.VISIBLE_TEXT in accepted and condition.document_artifact is None:
            claims.append(
                HostVerificationClaim(
                    claim_id=f"web.visible_text:{digest}",
                    kind=VerificationClaimKind.VISIBLE_TEXT,
                    expected=condition.literal,
                    source_authority=f"user_event:{condition.source_event_id}",
                )
            )
    requested_claims, _ = _user_verification_material(events)
    claims.extend(claim for claim in requested_claims if claim.kind in accepted)
    if (
        contract
        and VerificationClaimKind.CONTRACT_SEMANTIC in accepted
        and (admission is None or admission.verification_contract is None)
    ):
        claims.append(
            HostVerificationClaim(
                claim_id="web.contract_semantic",
                kind=VerificationClaimKind.CONTRACT_SEMANTIC,
                expected=hashlib.sha256(
                    json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                source_authority="registered_build_contract@1",
            )
        )
    return tuple(claims)


def _governed_structured_browser_target(events: list[Event]) -> bool:
    admission = current_build_platform_admission(events)
    contract = admission.verification_contract if admission is not None else None
    if contract is not None:
        return any(
            check.required and check.receipt_kind == "disco.web_functional@1"
            for check in contract.checks
        )
    return bool(
        admission is not None and requires_structured_browser_runtime(admission.verification_claims)
    )


def _governed_verification_contract(
    events: list[Event],
) -> AdmittedVerificationContract | None:
    admission = current_build_platform_admission(events)
    if (
        admission is None
        or admission.route != "platform"
        or admission.composition_authority != "build_platform_core"
    ):
        return None
    return admission.verification_contract


def _governed_verification_required(events: list[Event]) -> bool:
    contract = _governed_verification_contract(events)
    if contract is not None:
        return bool(
            contract.required
            and contract.unverified_finish == "block"
            and any(check.required for check in contract.checks)
        )
    admission = current_build_platform_admission(events)
    return bool(
        admission is not None
        and admission.route == "platform"
        and admission.composition_authority == "build_platform_core"
        and requires_structured_browser_runtime(admission.verification_claims)
    )


def _strict_appkit_contract(
    contract: AdmittedVerificationContract | None,
) -> bool:
    return bool(
        contract is not None
        and any(
            check.required
            and check.issuer_id == "disco.appkit_strict_verifier@1"
            and check.receipt_kind == "disco.appkit_strict@1"
            and check.operation == "host.verify_appkit_strict"
            for check in contract.checks
        )
    )


def _strict_appkit_compatibility_error(
    contract: AdmittedVerificationContract,
) -> str | None:
    strict_checks = tuple(
        check
        for check in contract.checks
        if check.required
        and check.issuer_id == "disco.appkit_strict_verifier@1"
        and check.receipt_kind == "disco.appkit_strict@1"
        and check.operation == "host.verify_appkit_strict"
    )
    if len(strict_checks) != 1:
        return "the strict AppKit compatibility adapter requires exactly one admitted strict check"
    check = strict_checks[0]
    required_claim_ids = tuple(claim.claim_id for claim in check.claims if claim.required)
    if required_claim_ids != ("appkit.strict_contract",):
        return (
            "the strict AppKit compatibility adapter only certifies its exact "
            "appkit.strict_contract aggregate"
        )
    return None


def _handoff_clauses(
    handoff: DeliverableEvent,
    contract: AdmittedVerificationContract,
) -> tuple[tuple[str, bool], ...]:
    """Each handoff/contract comparison paired with the fact name it binds.

    Same reason as `HostVerificationResult.authority_clauses`: an agent told only
    that its handoff does not match, across four bound facts, can do nothing but
    hand off the identical thing again — and the duplicate suppressor then drops
    that, and the actionless valve pauses the run. Naming the fact is what turns
    an unrecoverable loop into one corrective move.
    """

    expected_kind = "app" if contract.delivery.mode == "interactive" else "files"
    return (
        ("artifact_kind", handoff.artifact_kind == expected_kind),
        ("target_id", handoff.target_id == contract.target_id),
        ("delivery_contract", handoff.delivery_contract == contract.delivery),
        ("verification_contract_digest", handoff.verification_contract_digest == contract.digest),
    )


def _handoff_matches_verification_contract(
    handoff: DeliverableEvent,
    contract: AdmittedVerificationContract,
) -> bool:
    return all(ok for _, ok in _handoff_clauses(handoff, contract))


def _handoff_refusal_detail(
    handoff: DeliverableEvent | None,
    contract: AdmittedVerificationContract | None,
) -> str:
    """Say WHY the current handoff cannot serve this target, naming the fact."""

    if handoff is None:
        return "there is no handoff for the current target at all"
    if contract is None:
        return "the latest handoff is not current for this target"
    for name, ok in _handoff_clauses(handoff, contract):
        if not ok:
            return f"the latest handoff does not bind this target's {name}"
    return "the latest handoff is not current for this target"


def _latest_appkit_verification_outcome(
    events: list[Event],
    started: VerifierStartedEvent,
) -> ObservationEvent | AgentErrorEvent | None:
    """Return the latest exact strict-tool outcome causally after ``started``."""

    actions = {event.id: event for event in events if isinstance(event, ActionEvent)}
    for event in reversed(events):
        if type(event.seq) is not int or type(started.seq) is not int or event.seq <= started.seq:
            continue
        if not isinstance(event, (ObservationEvent, AgentErrorEvent)):
            continue
        action = actions.get(event.action_id or "")
        if action is None or action.tool_call.tool_name != "verify_appkit_app":
            continue
        if isinstance(event, ObservationEvent):
            result = event.tool_result
            if (
                result.tool_name != "verify_appkit_app"
                or result.call_id != action.tool_call.call_id
            ):
                continue
        return event
    return None


def _latest_matching_appkit_start(
    events: list[Event],
    contract: AdmittedVerificationContract,
) -> VerifierStartedEvent | None:
    strict_checks = tuple(
        check
        for check in contract.checks
        if check.required
        and check.issuer_id == "disco.appkit_strict_verifier@1"
        and check.receipt_kind == "disco.appkit_strict@1"
        and check.operation == "host.verify_appkit_strict"
    )
    if len(strict_checks) != 1:
        return None
    check = strict_checks[0]
    return next(
        (
            event
            for event in reversed(events)
            if isinstance(event, VerifierStartedEvent)
            and event.verification_contract_digest == contract.digest
            and event.target_id == contract.target_id
            and event.check_id == check.check_id
            and event.receipt_kind == check.receipt_kind
            and event.issuer_id == check.issuer_id
            and event.operation == check.operation
        ),
        None,
    )


def _appkit_runtime_identity(raw: object) -> VerificationExecutionIdentity | None:
    if not isinstance(raw, dict) or raw.get("status") not in {
        "running",
        "unavailable",
    }:
        return None
    projection_id = raw.get("projection_id")
    sandbox_instance_id = raw.get("sandbox_instance_id")
    sandbox_generation = raw.get("sandbox_generation")
    url = raw.get("url")
    if (
        not isinstance(projection_id, str)
        or not projection_id
        or not isinstance(sandbox_instance_id, str)
        or not sandbox_instance_id
        or type(sandbox_generation) is not int
        or not isinstance(url, str)
        or not url
    ):
        return None
    return VerificationExecutionIdentity(
        modality="appkit_strict_runtime",
        instance_id=projection_id,
        generation=f"{sandbox_instance_id}:{sandbox_generation}",
        locator=url,
    )


def _user_verification_material(
    events: list[Event],
) -> tuple[tuple[HostVerificationClaim, ...], tuple[VerifierReferenceImage, ...]]:
    """Fold the current explicit user/scenario requirement snapshot.

    Ordinary user images never imply visual inspection. A replacement is active
    only when it causally names the prior directive event; malformed replay
    retains the prior stricter snapshot rather than weakening it.
    """

    event = active_verification_requirements_event(events)
    if event is None or event.verification_requirements is None:
        return (), ()
    directive = event.verification_requirements
    claims: tuple[HostVerificationClaim, ...] = ()
    references: tuple[VerifierReferenceImage, ...] = ()
    if event is not None:
        raw_instruction = event.message.content or ""
        instruction_complete = len(raw_instruction) <= 8192
        instruction = raw_instruction if instruction_complete else ""
        instruction_digest = hashlib.sha256(raw_instruction.encode()).hexdigest()
        next_references: list[VerifierReferenceImage] = []
        image_identities: dict[int, tuple[str, str]] = {}
        for index, image in enumerate(directive.reference_images):
            valid = validated_image_data_url(image)
            if valid is None:
                continue
            media_type, digest = valid
            image_identities[index] = valid
            next_references.append(
                VerifierReferenceImage(
                    source_event_id=event.id,
                    source_index=index,
                    sha256=digest,
                    media_type=media_type,
                    instruction=instruction,
                    instruction_sha256=instruction_digest,
                    instruction_complete=instruction_complete,
                    image_data_url=image,
                )
            )
        next_claims: list[HostVerificationClaim] = []
        for requested in directive.claims:
            reference = (
                image_identities.get(requested.reference_image_index)
                if requested.reference_image_index is not None
                else None
            )
            next_claims.append(
                HostVerificationClaim(
                    claim_id=requested.claim_id,
                    kind=requested.kind,
                    required=requested.required,
                    expected=requested.expected,
                    source_authority=f"user_event:{event.id}",
                    reference_image_sha256=(reference[1] if reference is not None else None),
                )
            )
        claims = tuple(next_claims)
        references = tuple(next_references)
    return claims, references


def _preview_selection_at(
    events: list[Event],
    *,
    through_seq: int,
) -> PreviewSelectionIdentity | None:
    """Fold the exact host-observed preview selected through one event sequence."""

    actions: dict[str, ActionEvent] = {}
    active: dict[str, tuple[int, PreviewSelectionIdentity]] = {}
    ordered = sorted(
        (event for event in events if type(event.seq) is int and (event.seq or 0) <= through_seq),
        key=lambda event: event.seq or -1,
    )
    for event in ordered:
        if isinstance(event, ActionEvent):
            actions[event.id] = event
            continue
        if not isinstance(event, ObservationEvent):
            continue
        result = event.tool_result
        if result.tool_name == "verify_appkit_app" and result.success is True:
            action = actions.get(event.action_id or "")
            structured = result.structured
            runtime = structured.get("preview_runtime") if isinstance(structured, dict) else None
            if (
                action is None
                or action.tool_call.tool_name != "verify_appkit_app"
                or result.call_id != action.tool_call.call_id
                or event.action_id != action.id
                or not isinstance(structured, dict)
                or structured.get("passed") is not True
                or not isinstance(runtime, dict)
                or runtime.get("status") not in {"running", "unavailable"}
                or type(action.seq) is not int
                or type(event.seq) is not int
            ):
                active.clear()
                continue
            identity = PreviewSelectionIdentity.from_structured(
                action_id=action.id,
                action_seq=action.seq,
                observation_id=event.id,
                observation_seq=event.seq,
                structured=runtime,
            )
            if identity is None:
                active.clear()
                continue
            active[identity.session_name] = (event.seq, identity)
            continue
        if result.tool_name == "preview_start" and result.success is True:
            action = actions.get(event.action_id or "")
            structured = result.structured
            if (
                action is None
                or action.tool_call.tool_name != "preview_start"
                or result.call_id != action.tool_call.call_id
                or event.action_id != action.id
                or not isinstance(structured, dict)
                or structured.get("status") not in {"running", "unavailable"}
                or type(action.seq) is not int
                or type(event.seq) is not int
            ):
                active.clear()
                continue
            identity = PreviewSelectionIdentity.from_structured(
                action_id=action.id,
                action_seq=action.seq,
                observation_id=event.id,
                observation_seq=event.seq,
                structured=structured,
            )
            if identity is None:
                active.clear()
                continue
            active[identity.session_name] = (event.seq, identity)
            continue
        if result.tool_name == "preview_stop" and result.success is True:
            stopped = (
                result.structured.get("stopped") if isinstance(result.structured, dict) else None
            )
            if not isinstance(stopped, list) or not all(
                isinstance(name, str) and name for name in stopped
            ):
                active.clear()
                continue
            for name in stopped:
                active.pop(name, None)
    return max(active.values(), key=lambda item: item[0])[1] if active else None


def _bind_host_verification_authority(
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
    handoff_selection = (
        _preview_selection_at(events, through_seq=handoff.seq)
        if handoff is not None and type(handoff.seq) is int
        else None
    )
    current_selection = _preview_selection_at(events, through_seq=max_seq)
    stable_selection = (
        current_selection
        if handoff_selection is None
        else handoff_selection
        if current_selection is not None
        and handoff_selection.operational_identity == current_selection.operational_identity
        else None
    )
    contract = (
        admission.verification_contract
        if admission is not None and admission.route == "platform"
        else None
    )
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
    selected_preview = stable_selection if uses_managed_web_runtime else None
    execution_identity = (
        VerificationExecutionIdentity(
            modality="managed_preview",
            instance_id=selected_preview.projection_id,
            generation=(
                f"{selected_preview.sandbox_instance_id}:{selected_preview.sandbox_generation}"
            ),
            locator=selected_preview.url,
        )
        if selected_preview is not None
        else VerificationExecutionIdentity(
            modality="workspace_artifact",
            instance_id=handoff.id if handoff is not None else deliverable.artifact_path,
            generation=f"{_last_productive_seq(events)}:{generation or 'none'}:{epoch or 0}",
            locator=deliverable.artifact_path,
        )
        if contract is not None
        else None
    )
    return deliverable.model_copy(
        update={
            "run_intent_id": (
                admission.run_intent_id
                if admission is not None
                else intent.id
                if intent is not None
                else None
            ),
            "run_identity": admission.run_identity if admission is not None else None,
            "agent_view_id": current_workspace_agent_view_id(events),
            "deliverable_event_id": handoff.id if handoff is not None else None,
            "workspace_revision": _last_productive_seq(events),
            "workspace_generation": generation,
            "workspace_epoch": epoch,
            "observed_after_seq": authority_seq,
            "required_claims": (
                _host_verification_claims(
                    gate._verifier_contract_payload(),
                    events,
                    check=selected_check,
                )
                if contract is not None
                or deliverable.artifact_kind == "app"
                or _governed_structured_browser_target(events)
                else ()
            ),
            "verification_contract": contract,
            "verification_check": selected_check,
            "execution_identity": execution_identity,
            "preview_selection": selected_preview,
            "preview_binding_required": (
                uses_managed_web_runtime
                and deliverable.artifact_kind == "app"
                and (
                    admission is not None
                    or handoff_selection is not None
                    or current_selection is not None
                )
            ),
        }
    )


def _typed_host_result(
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


def _typed_host_mismatch(
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


async def _emit_verifier_started_event(
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


async def _prepare_typed_host_verdict(
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
    typed_result = _typed_host_result(host_verdict, deliverable) if typed_required else None
    if typed_required and typed_result is None:
        host_verdict = gate._host_unavailable_verdict(
            deliverable,
            f"verification could not run: {_typed_host_mismatch(host_verdict, deliverable)}.",
        )
    if typed_result is not None:
        host_verdict["passed"] = typed_result.passed
        host_verdict["verdict"] = typed_result.status.value
        host_verdict["summary"] = typed_result.reason
    return host_verdict, typed_result, browser_unavailable


async def _emit_browser_unavailable_release(loop: Any) -> Disp:
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


async def _emit_host_verdict_audit(
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
    agreement = (
        host_label == inline_label if host_label is not None and inline_label is not None else None
    )
    detail = str(host_verdict.get("summary") or host_verdict.get("detail") or "")
    startup_diagnostic = str(host_verdict.get("startup_diagnostic") or "").strip()
    if startup_diagnostic:
        startup_diagnostic = "".join(
            char for char in startup_diagnostic if char in "\n\t" or ord(char) >= 32
        )
        startup_diagnostic = _STARTUP_DIAGNOSTIC_SECRET_RE.sub("<redacted>", startup_diagnostic)[
            :1280
        ]
        detail = (
            f"{detail}\nBrowser startup diagnostic: {startup_diagnostic}"
            if detail
            else f"Browser startup diagnostic: {startup_diagnostic}"
        )

    verifier_meta: dict[str, Any] = {"requested_verification": deliverable.requested_verification}
    for key in _MODEL_VERIFIER_META_KEYS:
        if key in host_verdict:
            verifier_meta[key] = host_verdict[key]
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
            target_id=typed_result.target_id if typed_result is not None else "",
            check_id=typed_result.check_id if typed_result is not None else "",
            receipt_kind=typed_result.receipt_kind if typed_result is not None else "",
            verification_contract_digest=(
                typed_result.verification_contract_digest if typed_result is not None else None
            ),
            requested_by_event_id=verifier_started_event_id,
            meta=dict(verifier_meta),
        )
    )
    hook = getattr(gate._loop, "_host_verifier_verdict_hook", None)
    if hook is not None:
        try:
            await hook(cast(VerifierVerdictEvent, verdict_event))
        except Exception:  # noqa: BLE001 — canary bookkeeping is non-authoritative
            _LOG.warning(
                "REL-1d host verifier canary hook failed for %s:%s",
                gate._loop.conversation_id,
                deliverable.artifact_path,
                exc_info=True,
            )
    return host_label


async def _prepare_appkit_typed_authority(
    gate: Any,
    step: AgentStep,
    events: list[Event],
    contract: AdmittedVerificationContract,
) -> tuple[VerifierStartedEvent, HostVerificationDeliverable] | None:
    """Bind AppKit's current target and emit START before strict verification."""

    strict_checks = tuple(
        check
        for check in contract.checks
        if check.required
        and check.issuer_id == "disco.appkit_strict_verifier@1"
        and check.receipt_kind == "disco.appkit_strict@1"
        and check.operation == "host.verify_appkit_strict"
    )
    if len(strict_checks) != 1:
        return None
    check = strict_checks[0]
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
    current = _latest_deliverable_event(events)
    max_seq = max((event.seq or 0 for event in events), default=0)
    current_seq = current.seq if current is not None and type(current.seq) is int else None
    handoff_selection = (
        _preview_selection_at(events, through_seq=current_seq) if current_seq is not None else None
    )
    latest_selection = _preview_selection_at(events, through_seq=max_seq)
    preview_authority_changed = (
        current is not None and (handoff_selection is None) != (latest_selection is None)
    ) or (
        handoff_selection is not None
        and latest_selection is not None
        and handoff_selection.operational_identity != latest_selection.operational_identity
    )
    handoff_precedes_productive_work = current_seq is None or current_seq < _last_productive_seq(
        events
    )
    if (
        current is None
        or not _handoff_matches_verification_contract(current, contract)
        or current.path != artifact_path
        or handoff_precedes_productive_work
        or preview_authority_changed
    ):
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
        current = emitted
        events = await gate._loop._events()
    base = await gate._host_verify_deliverable(step, events, include_unverifiable=True)
    if base is None:
        return None
    base = base.model_copy(
        update={
            "deliverable_event_id": current.id,
            "artifact_path": current.path,
            "artifact_kind": current.artifact_kind,
            "required_claims": _host_verification_claims(
                gate._verifier_contract_payload(),
                events,
                check=check,
            ),
            "verification_check": check,
            "execution_identity": execution_identity,
            "preview_selection": None,
            "preview_binding_required": False,
        }
    )
    if (
        base.verification_check != check
        or check.issuer_id != "disco.appkit_strict_verifier@1"
        or check.receipt_kind != "disco.appkit_strict@1"
        or check.operation != "host.verify_appkit_strict"
    ):
        return None
    started = await _emit_verifier_started_event(gate._loop, base)
    return started, base


async def _record_appkit_typed_verdict(
    gate: Any,
    events: list[Event],
    prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable],
) -> VerificationClaimStatus | None:
    """Pair every AppKit START with an exact typed terminal verifier status."""

    started, deliverable = prepared
    outcome = _latest_appkit_verification_outcome(events, started)
    raw = (
        outcome.tool_result.structured
        if isinstance(outcome, ObservationEvent)
        and isinstance(outcome.tool_result.structured, dict)
        else {}
    )
    check = deliverable.verification_check
    if check is None:
        return None
    raw_passed = bool(
        isinstance(outcome, ObservationEvent)
        and outcome.tool_result.success
        and raw.get("passed") is True
    )
    raw_entry = _safe_deliverable_file_path(str(raw.get("canonical_entry_path") or ""))
    runtime_identity = (
        _appkit_runtime_identity(raw.get("preview_runtime"))
        if isinstance(outcome, ObservationEvent)
        else None
    )
    artifact_identity: VerificationArtifactIdentity | None = None
    binding_error = ""
    if raw_passed:
        try:
            artifact_identity = VerificationArtifactIdentity.model_validate(
                raw.get("artifact_identity")
            )
        except Exception:
            binding_error = "strict AppKit PASS omitted a valid immutable artifact identity"
        if not binding_error and (
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
        ):
            binding_error = (
                "strict AppKit PASS did not match the started runtime, canonical entry, "
                "or artifact producer authority"
            )
    unavailable = outcome is None or isinstance(outcome, AgentErrorEvent) or bool(binding_error)
    base_status = (
        VerificationClaimStatus.PASS
        if raw_passed and not binding_error
        else VerificationClaimStatus.UNAVAILABLE
        if unavailable
        else VerificationClaimStatus.FAIL
    )
    observed_after_seq = (
        outcome.seq
        if outcome is not None and type(outcome.seq) is int
        else started.seq
        if type(started.seq) is int
        else deliverable.observed_after_seq
    )
    bound_deliverable = deliverable.model_copy(
        update={
            "artifact_identity": artifact_identity,
            "observed_after_seq": observed_after_seq,
        }
    )
    url = str(raw.get("url") or "")
    reason = str(
        binding_error
        or raw.get("summary")
        or (
            outcome.error
            if isinstance(outcome, AgentErrorEvent)
            else "strict AppKit verifier did not produce a usable result"
        )
    )
    evidence_event_id = outcome.id if outcome is not None else started.id
    raw_application_title = raw.get("application_title")
    claim_results: tuple[HostVerificationClaimResult, ...] = tuple(
        HostVerificationClaimResult(
            claim_id=claim.claim_id,
            kind=claim.kind,
            required=claim.required,
            expected=claim.expected,
            source_authority=claim.source_authority,
            reference_image_sha256=claim.reference_image_sha256,
            status=(
                VerificationClaimStatus.UNAVAILABLE
                if base_status is VerificationClaimStatus.UNAVAILABLE
                or (
                    claim.kind is VerificationClaimKind.APPLICATION_IDENTITY
                    and not isinstance(raw_application_title, str)
                )
                else VerificationClaimStatus.PASS
                if claim.kind is VerificationClaimKind.APPLICATION_IDENTITY
                and isinstance(raw_application_title, str)
                and raw_application_title == claim.expected
                else VerificationClaimStatus.FAIL
                if claim.kind is VerificationClaimKind.APPLICATION_IDENTITY
                else base_status
            ),
            reason=(
                # State BOTH sides: a bare "authoritative AppSpec name is 'X'" on a
                # FAIL reads as a fact with no expectation, which hides whether the
                # app or the requirement is wrong.
                f"authoritative AppSpec name is {raw_application_title!r}"
                + (
                    ""
                    if raw_application_title == claim.expected
                    else f"; required {claim.expected!r}"
                )
                if claim.kind is VerificationClaimKind.APPLICATION_IDENTITY
                and isinstance(raw_application_title, str)
                else "strict AppKit verifier omitted authoritative application identity"
                if claim.kind is VerificationClaimKind.APPLICATION_IDENTITY
                else reason
            ),
            verifier_id=check.issuer_id,
            capability_basis="configured strict AppKit target verifier",
            evidence_modalities=(VerificationEvidenceModality.TARGET_SPECIFIC,),
            evidence_refs=(
                f"event:{evidence_event_id}",
                f"artifact:{deliverable.artifact_path}",
            ),
        )
        for claim in bound_deliverable.required_claims
    )
    result_reason = next(
        (
            result.reason
            for result in claim_results
            if result.required and result.status is not VerificationClaimStatus.PASS
        ),
        reason,
    )
    screenshot_path = _bounded_host_verdict_screenshot_path(raw.get("screenshot_path"))
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
    await _emit_host_verdict_audit(
        gate,
        bound_deliverable,
        events,
        host_verdict,
        typed_result,
        screenshot_path,
        verifier_started_event_id=started.id,
    )
    return status


def _recorded_appkit_typed_status(
    events: list[Event],
    started: VerifierStartedEvent,
) -> VerificationClaimStatus | None:
    result = _recorded_appkit_typed_result(events, started)
    return result.status if result is not None else None


def _recorded_appkit_typed_result(
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


async def _appkit_typed_gate_disposition(
    gate: Any,
    *,
    events: list[Event],
    verdict: dict[str, Any] | None,
    tool_name: str,
    prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None,
) -> Disp | None:
    if prepared is None:
        return None
    status = await _record_appkit_typed_verdict(gate, events, prepared)
    if verdict is not None and (
        verdict.get("passed") is not True or status is VerificationClaimStatus.PASS
    ):
        return None
    current_events = await gate._loop._events()
    typed_result = _recorded_appkit_typed_result(current_events, prepared[0])
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


async def _finish_verification_medium(
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


def _verification_failure_details(
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


class _WorkflowPathParams(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_workflow_output_path(template: str, params: dict[str, object]) -> str:
    values = _WorkflowPathParams(
        {key: "" if value is None else value for key, value in params.items()}
    )
    try:
        return template.format_map(values)
    except (IndexError, KeyError, ValueError):
        return template


class _FinishVerifyMixin(_FinishGateProto):
    async def finish_verify_passed(self, command: str) -> tuple[bool, bool]:
        """Run the agent's stated acceptance check before allowing `finish`
        (verify-on-finish post-condition gate). The agent attaches a shell
        command to finish whose exit 0 means the deliverable is good; we run it,
        VISIBLE in the trace, and on failure REFUSE the finish so the agent fixes
        the real problem instead of declaring a broken build complete.

        The verify command is NOT privileged: it passes the same hard-deny gate
        AND the same confirmation policy as any action. A command that would
        normally require confirmation is refused here (we don't silently run a
        gated command as a 'verification') — the agent is told to run it as an
        ordinary, gated action first. Ordinary test/build/lint checks assess as
        MEDIUM and run unimpeded. Returns (passed, malformed): `passed` is True
        iff the check ran and passed; `malformed` is True iff the verify command
        itself is broken (command-not-found / SyntaxError) rather than the task."""
        if getattr(getattr(self._loop, "executor", None), "appkit_phase", None) is not None:
            # Strict AppKit has no raw shell surface. Its authoritative finish
            # verification is the structured `verify_appkit_app` gate that runs
            # later in the shared finish path, so the model-authored shell probe is
            # a redundant impossible check here.
            return True, False

        workflow_run = getattr(self._loop, "_workflow_run", None)
        workflow_tools = tuple(getattr(getattr(workflow_run, "definition", None), "tools", ()))
        if workflow_run is not None and "shell" not in workflow_tools:
            # Defense in depth for stale clients and model hallucinations. The
            # workflow finish schema omits `verify` in this shape, but arguments
            # are still untrusted: never let the virtual finish tool smuggle a
            # raw shell action outside the approved workflow scope.
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "<system-reminder>\n"
                            "The supplied finish verification was ignored because this "
                            "sealed workflow does not grant the shell tool. Host-owned "
                            "workflow output checks remain authoritative.\n"
                            "</system-reminder>"
                        ),
                    ),
                    meta={"workflow_finish_verify_ignored": True},
                )
            )
            return True, False

        call = ToolCall(tool_name="shell", arguments={"command": command})
        # meta marker: this shell action is the GATE'S probe, not the agent's
        # work. Phase-B re-run #6 (2026-06-10): an unmarked probe counted as a
        # real action in _actions_since_last_resume, so a refused first-move
        # finish UNLOCKED the withheld meta tools and the model remember-spammed
        # straight into the valve. The probe must never flip fresh-session.
        action = ActionEvent(
            thought=f"Verifying completion: {command}",
            tool_call=call,
            meta={"verify_probe": True},
        )

        deny = signals.hard_deny_reason(action)
        if deny is not None:
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        f"The verify command attached to finish is hard-denied ({deny}); it "
                        "will not run. Provide a safe verify command, or finish without one.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        risk = self._loop.analyzer.assess(action)
        if self._loop.policy.should_confirm(risk):
            await self._loop._emit(action)
            await self._loop._emit(
                AgentErrorEvent(
                    error=(
                        "<system-reminder>\n"
                        "The verify command attached to finish needs confirmation to run "
                        "and won't be executed silently as a verification. Run that check "
                        "as a normal action first (it will go through the confirm gate), "
                        "then finish.\n"
                        "</system-reminder>"
                    ),
                    action_id=action.id,
                    tool_call_id=call.call_id,
                )
            )
            return False, False

        action = cast(ActionEvent, await self._loop._emit(action))
        await self._loop._execute_and_observe(action)
        # Find the observation correlated to THIS verify action (robust against a
        # trailing sandbox-restart notice that _execute_and_observe may append).
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        passed = isinstance(obs, ObservationEvent) and obs.tool_result.success
        # malformed = the verify COMMAND ITSELF is broken (not the deliverable):
        # command-not-found (127) or an interpreter SyntaxError. A non-zero exit from
        # an unrunnable check is NOT evidence the task failed — the carrier was bad.
        # The caller auto-strips a malformed verify rather than counting it as a
        # failed acceptance. Detected from the result text.
        malformed = False
        # Malformed = the verify CARRIER is broken, which only makes sense if the
        # shell actually RAN the command and reported it (an ObservationEvent). An
        # AgentErrorEvent means the executor raised BEFORE any observation (sandbox
        # down, transport error) — that's an environmental failure, NOT a malformed
        # verify, and must stay a real failure so it isn't auto-stripped into a false
        # "done". (Earlier this read AgentErrorEvent.error text and a stray "command
        # not found" substring there would wrongly strip an environmental failure.)
        if not passed and isinstance(obs, ObservationEvent):
            st = obs.tool_result.structured or {}
            ec = st.get("exit_code")
            exit_code = ec if isinstance(ec, int) and not isinstance(ec, bool) else None
            low = f"{obs.tool_result.content or ''} {obs.tool_result.error or ''}".lower()
            # The shell couldn't find/parse the command: exit 127 (command-not-found,
            # authoritative from the structured result — locale-independent, can't be
            # faked by output text) or an interpreter SyntaxError. Use the real exit
            # code, NOT a regex over output: "exit 1, 127 tests failed" is a REAL
            # failure, not a malformed carrier, and must NOT become a false success.
            malformed = (
                exit_code == 127
                or "syntaxerror" in low
                # content fallbacks only when the structured exit code is unavailable
                or (exit_code is None and "command not found" in low)
                or (exit_code is None and ": not found" in low)
            )
        return passed, malformed

    async def _drive_finish_browser_probe(self, target_url: str | None = None) -> bool:
        """ACTIVE finish-verify: instead of TRUSTING the agent to have browsed the
        deliverable, the gate DRIVES a `browser navigate <preview>` itself and
        judges the result on ground truth. The preview platform assigns a RANDOM
        port, so the gate navigates to the RESOLVED preview (`target_url`, from
        `_detect_preview_url` — backend-aware, never the agent-server's :8000 on the
        shared-host backend); only when no preview is detectable does it fall back
        to the legacy `http://127.0.0.1:8000/` (the isolated-backend app port). This
        closes the
        verification-overclaim hole: an agent that declares done without ever
        looking can no longer land a JS-broken/blank page as FINISHED — the gate
        looks for it.

        Returns True iff the probe RAN and produced a usable browser observation
        (ObservationEvent.tool_result.success). The caller then re-judges via
        `_browser_verified` (console errors) + `_browser_content_meaningful`
        (blank render). Returns False (no-op) when the browser backend is
        unavailable (the process backend ships no `browser` tool) or the probe is
        hard-denied — so the gate degrades to its prior passive nudge/release
        behavior on browserless backends rather than hanging or false-refusing.

        The probe ActionEvent is tagged `meta={"verify_probe": True}` so signals.py
        keeps it OUT of agent-work accounting (actions_since_last_resume /
        productive_actions_since_approval) — the same marker finish_verify_passed
        uses. Running the gate's own probe is never evidence the AGENT did work."""
        # Browserless backend? (process backend exposes no `browser` tool) → no-op,
        # let the gate fall back to today's passive behavior.
        try:
            tool_names = {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — any introspection failure → degrade safely
            tool_names = set()
        if "browser" not in tool_names:
            return False

        nav_url = target_url or "http://127.0.0.1:8000/"
        call = ToolCall(
            tool_name="browser",
            arguments={"action": "navigate", "url": nav_url},
        )
        action = ActionEvent(
            thought=f"Verifying the app renders: navigating to {nav_url}",
            tool_call=call,
            meta={"verify_probe": True},
        )
        # A hard-denied probe (should never happen for a browser navigate, but the
        # contract surface is shared) is a no-op → degrade to passive.
        if signals.hard_deny_reason(action) is not None:
            return False

        action = cast(ActionEvent, await self._loop._emit(action))
        await self._loop._execute_and_observe(action)
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        return isinstance(obs, ObservationEvent) and obs.tool_result.success

    def _available_tool_names(self) -> set[str | None]:
        try:
            return {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — introspection failure → degrade safely
            return set()

    def _verify_tool_available(self) -> bool:
        """W-45 — is a structured app verifier in the execution set?"""
        return self._active_verify_tool() is not None

    def _active_verify_tool(self) -> str | None:
        """Return the structured verifier active for this executor.

        Strict AppKit mode advertises `verify_appkit_app` from the AppKit-only
        allowlist. Prefer it; otherwise fall back to normal Build's `verify_web_app`.
        """
        tool_names = self._available_tool_names()
        if "verify_appkit_app" in tool_names:
            return "verify_appkit_app"
        if "verify_web_app" in tool_names:
            return "verify_web_app"
        return None


async def _judge_semantic_claims(
    gate: Any,
    deliverable: HostVerificationDeliverable,
    events: list[Event],
    host_verdict: dict[str, Any],
) -> dict[str, Any]:
    """Bind each bounded independent judgement to one exact semantic claim."""

    judge = getattr(gate._loop, "_verifier_judge", None)
    if judge is None or gate._verdict_label(host_verdict) in {"unavailable", "unverifiable"}:
        return host_verdict
    semantic_claims = tuple(
        claim
        for claim in deliverable.required_claims
        if claim.kind
        in {
            VerificationClaimKind.CONTRACT_SEMANTIC,
            VerificationClaimKind.VISUAL_SEMANTIC,
        }
    )
    if not semantic_claims:
        return host_verdict
    try:
        raw_receipt = host_verdict.get("verification_result")
        receipt = (
            raw_receipt
            if isinstance(raw_receipt, HostVerificationResult)
            else HostVerificationResult.model_validate(raw_receipt)
        )
    except Exception:
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
        if missing_pixels or missing_reference:
            detail = (
                "visual claim has no captured output pixel evidence"
                if missing_pixels
                else "visual claim's user reference pixels are unavailable"
            )
            typed = TypedVerifierVerdict(
                verified=False,
                verdict="unavailable",
                detail=detail,
                failures=[{"kind": "visual_evidence_unavailable", "message": detail}],
                failure_fingerprint="model_verifier_unavailable",
            )
            fallback_cause = detail
        else:
            try:
                raw_typed = await asyncio.wait_for(
                    judge.judge(seed),
                    timeout=max(
                        0.001,
                        float(getattr(gate._loop, "_verifier_judge_timeout_s", 30.0)),
                    ),
                )
                typed = (
                    raw_typed
                    if isinstance(raw_typed, TypedVerifierVerdict)
                    else TypedVerifierVerdict.model_validate(raw_typed)
                )
                applied = True
                if (
                    typed.verdict in {"unavailable", "unverifiable"}
                    and typed.failure_fingerprint == "model_verifier_unavailable"
                ):
                    fallback_cause = typed.detail or typed.failure_fingerprint
            except TimeoutError:
                typed = TypedVerifierVerdict(
                    verified=False,
                    verdict="unavailable",
                    detail="model verifier deadline exceeded",
                    failure_fingerprint="model_verifier_unavailable",
                )
                fallback_cause = typed.detail
            except Exception as exc:  # noqa: BLE001 — verifier failure stays bounded
                typed = TypedVerifierVerdict(
                    verified=False,
                    verdict="unavailable",
                    detail=f"judge exception: {type(exc).__name__}",
                    failure_fingerprint="model_verifier_unavailable",
                )
                fallback_cause = typed.detail
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
        out["model_verifier_cause"] = _bounded_model_verifier_cause(fallback_cause)
    return out


class _HostVerifyBaseMixin(_FinishGateProto):
    async def _host_artifact_file_exists(self, path: str) -> bool | None:
        sbx = getattr(self._loop.executor, "sandbox", None)
        file_exists: Any = getattr(sbx, "file_exists", None)
        if file_exists is None:
            return None
        try:
            return bool(await file_exists(path))
        except Exception:  # noqa: BLE001 — path resolution is advisory; skip only on known absence
            return None

    async def _artifact_manifest_records(
        self, events: list[Event], *, consumer: str
    ) -> tuple[Any, ...] | None:
        """Read REL-2a artifact manifest records for promoted readers.

        Empty manifests are NOT agreement: the REL-1e flip exposed how easy it is
        to bank a shadow claim when no shadow data was actually recorded. This
        helper only returns records when the manifest has at least one path, and
        always logs the event-projection comparison while the reader soaks.
        """

        if not artifact_manifest_reader_enabled():
            return None
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            _LOG.info(
                "artifact-manifest reader skipped for %s:%s: sandbox unavailable",
                self._loop.conversation_id,
                consumer,
            )
            return None
        try:
            from ...context import ArtifactMemoryStore

            records = tuple(await ArtifactMemoryStore(sbx).read_artifacts())
        except Exception:  # noqa: BLE001 — manifest reader must fail open to legacy readers
            _LOG.warning(
                "artifact-manifest reader failed for %s:%s",
                self._loop.conversation_id,
                consumer,
                exc_info=True,
            )
            return None

        manifest_paths = artifact_paths_from_manifest_records(records)
        projected = artifact_paths_from_events(events)
        if not manifest_paths:
            _LOG.info(
                "artifact-manifest reader skipped for %s:%s: no manifest data (projected=%d)",
                self._loop.conversation_id,
                consumer,
                len(projected),
            )
            return None

        missing, extra = manifest_path_divergence(projected, manifest_paths)
        _LOG.info(
            "artifact-manifest reader compare for %s:%s: projected=%d manifest=%d "
            "missing=%s extra=%s",
            self._loop.conversation_id,
            consumer,
            len(projected),
            len(manifest_paths),
            sorted(missing),
            sorted(extra),
        )
        return records

    async def _host_verify_artifact_path(self, raw_path: str) -> str | None:
        path = _safe_deliverable_file_path(raw_path)
        legacy_index = _safe_deliverable_file_path(raw_path, app_root=True)
        if path is None:
            path = legacy_index
        if path is None:
            return None
        exists = await self._host_artifact_file_exists(path)
        if exists is not False:
            return path
        if legacy_index is not None and legacy_index != path:
            if await self._host_artifact_file_exists(legacy_index) is not False:
                return legacy_index
        return None

    async def _host_verify_manifest_deliverable(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        include_unverifiable: bool,
    ) -> HostVerificationDeliverable | None:
        records = await self._artifact_manifest_records(events, consumer="host_verify")
        if records is None:
            return None

        app_records = [r for r in records if _artifact_record_kind(r) == "app"]
        for record in reversed(app_records):
            raw_path = getattr(record, "path", None)
            if not isinstance(raw_path, str):
                continue
            path = await self._host_verify_artifact_path(raw_path)
            if path is None:
                continue
            return HostVerificationDeliverable(
                conversation_id=self._loop.conversation_id,
                artifact_path=path,
                artifact_kind="app",
                deployment_url="",
                requested_verification=step.requested_verification,
            )

        if not include_unverifiable:
            return None

        file_records = [r for r in records if _artifact_record_kind(r) != "app"]
        shown_records = [r for r in file_records if bool(getattr(r, "shown", False))]
        for record in reversed(shown_records or file_records):
            raw_path = getattr(record, "path", None)
            if not isinstance(raw_path, str):
                continue
            path = _safe_deliverable_file_path(raw_path)
            if path is None:
                continue
            return HostVerificationDeliverable(
                conversation_id=self._loop.conversation_id,
                artifact_path=path,
                artifact_kind=_artifact_record_kind(record),
                deployment_url="",
                requested_verification=step.requested_verification,
            )
        return None

    def _host_verify_authoritative(self) -> bool:
        return bool(getattr(self._loop, "_host_verify_authoritative", False))

    async def _host_verify_deliverable(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        include_unverifiable: bool = False,
    ) -> HostVerificationDeliverable | None:
        """REL-1c — reconstruct the web-like deliverable for host shadow verify.

        The host verifier is advisory in this PR, so missing/ambiguous delivery
        evidence simply skips the shadow path. A first-class app handoff wins;
        otherwise the existing web-finish convention (index.html/server_status) is
        treated as index.html. App-root handoffs such as "." or "dist" are
        resolved to their primary index.html-style file, never verified as the
        directory path itself.
        """

        governed_target = _governed_verification_required(events)
        current_handoff = (
            _latest_deliverable_event(events)
            if governed_target
            else _latest_deliverable_event(events)
        )
        manifest_deliverable = None
        if not (governed_target and current_handoff is not None):
            manifest_deliverable = await self._host_verify_manifest_deliverable(
                step,
                events,
                include_unverifiable=include_unverifiable and not governed_target,
            )
        if manifest_deliverable is not None:
            bound = _bind_host_verification_authority(self, manifest_deliverable, events)
            return await self._with_host_verification_profile(bound, events)

        app_event = _latest_app_deliverable_event(events)
        if governed_target and current_handoff is None:
            return None
        any_event = (
            current_handoff
            if governed_target
            else _latest_deliverable_event(events)
            if include_unverifiable
            else app_event
        )
        if any_event is None and not _is_web_deliverable(events):
            return None
        if any_event is not None and any_event.artifact_kind != "app":
            path = _safe_deliverable_file_path(any_event.path)
        else:
            path = await self._host_verify_artifact_path(
                any_event.path if any_event is not None else "index.html"
            )
        if path is None:
            return None
        deployment_url = any_event.deployment_url if any_event is not None else ""
        artifact_kind = any_event.artifact_kind if any_event is not None else "app"
        deliverable = HostVerificationDeliverable(
            conversation_id=self._loop.conversation_id,
            artifact_path=path,
            artifact_kind=artifact_kind,
            deployment_url=deployment_url or "",
            requested_verification=step.requested_verification,
        )
        bound = _bind_host_verification_authority(self, deliverable, events)
        return await self._with_host_verification_profile(bound, events)

    async def _with_host_verification_profile(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
    ) -> HostVerificationDeliverable:
        """Lower deterministic target-medium evidence into exact host claims."""

        check = deliverable.verification_check
        contract = deliverable.verification_contract
        governed_web = bool(check is not None and check.receipt_kind == "disco.web_functional@1")
        if contract is not None and not governed_web:
            return deliverable.model_copy(update={"verification_medium": contract.preview_modality})
        # Medium selection belongs to the selected artifact only. Historical
        # handoffs/plan paths are useful bounded context for a semantic judge,
        # but reading them here can classify a stale app (or traverse an alias)
        # as the current target.
        paths = [deliverable.artifact_path]
        hint = await self._verifier_medium_hint(paths)
        medium = hint.kind if hint is not None else "web"
        claims = list(deliverable.required_claims)
        if (
            medium == "game"
            and (check is None or VerificationClaimKind.INTERACTION in check.accepted_claim_kinds)
            and not any(claim.claim_id == _GAME_INTERACTION_CLAIM_ID for claim in claims)
        ):
            claims.append(
                HostVerificationClaim(
                    claim_id=_GAME_INTERACTION_CLAIM_ID,
                    kind=VerificationClaimKind.INTERACTION,
                    expected=_GAME_INTERACTION_EXPECTED,
                    source_authority="target.game.functional_interaction@1",
                )
            )
        return deliverable.model_copy(
            update={"verification_medium": medium, "required_claims": tuple(claims)}
        )

    @staticmethod
    def _verdict_label(verdict: dict | None) -> str | None:
        if not verdict:
            return None
        label = verdict.get("verdict")
        if label is not None:
            return str(label)
        if "passed" in verdict:
            return "pass" if verdict.get("passed") is True else "fail"
        return None

    @staticmethod
    def _verdict_failures(verdict: dict) -> list[dict[str, object]]:
        failures: list[dict[str, object]] = []
        for item in verdict.get("failures") or []:
            if isinstance(item, dict):
                failures.append(dict(item))
        for err in verdict.get("console_errors") or []:
            if isinstance(err, dict):
                failures.append({"kind": "console_error", **err})
        for err in verdict.get("network_failures") or []:
            if isinstance(err, dict):
                failures.append({"kind": "network_failure", **err})
        if verdict.get("passed") is not True and not failures:
            summary = str(verdict.get("summary") or verdict.get("detail") or "").strip()
            if summary:
                failures.append({"kind": "summary", "message": summary})
        return failures

    @staticmethod
    def _host_unavailable_verdict(
        deliverable: HostVerificationDeliverable, detail: str
    ) -> dict[str, object]:
        verdict: dict[str, object] = {
            "passed": False,
            "verdict": "unavailable",
            "url": "",
            "http_status": 0,
            "summary": detail,
            "next_action": "",
            "console_errors": [],
            "network_failures": [],
            "failure_fingerprint": "host_verifier_unavailable",
        }
        if (
            deliverable.verification_contract is not None
            and deliverable.verification_check is not None
            and deliverable.required_claims
        ):
            verdict["verification_result"] = unavailable_verification_result(
                deliverable=deliverable,
                reason=detail,
            ).model_dump(mode="json")
        return verdict

    @staticmethod
    def _host_unverifiable_verdict(
        deliverable: HostVerificationDeliverable,
    ) -> dict[str, object]:
        return {
            "passed": False,
            "verdict": "unverifiable",
            "url": deliverable.deployment_url,
            "http_status": 0,
            "summary": (
                f"No host validator is available for artifact kind "
                f"{deliverable.artifact_kind!r}; verification was not claimed."
            ),
            "next_action": "",
            "console_errors": [],
            "network_failures": [],
            "failure_fingerprint": f"host_verifier_unverifiable:{deliverable.artifact_kind}",
        }

    @staticmethod
    def _verdict_first_failure(verdict: dict) -> str:
        errs = verdict.get("console_errors") or []
        nets = verdict.get("network_failures") or []
        if errs:
            e0 = errs[0]
            if isinstance(e0, dict):
                where = f" @ {e0.get('source')}" if e0.get("source") else ""
                return f"{e0.get('text', '')}{where}".strip()
        if nets:
            n0 = nets[0]
            if isinstance(n0, dict):
                marker = n0.get("status") or n0.get("failure") or "failed"
                return f"{n0.get('method', 'GET')} {n0.get('url', '')} -> {marker}".strip()
        failures = _HostVerifyBaseMixin._verdict_failures(verdict)
        if failures:
            message = failures[0].get("message")
            if message:
                return str(message)
        return ""

    def _verifier_contract_payload(self) -> dict[str, Any]:
        alias = getattr(self._loop, "_finish_alias", None)
        if not alias:
            return {}
        try:
            from ...contract.registry import BuildContractRegistry

            reg = BuildContractRegistry.default()
            for kind in reg.kinds():
                c = reg.get(kind)
                if c is not None and c.verify.finalizer == alias:
                    return c.model_dump(mode="json")
        except Exception:  # noqa: BLE001 — verifier seed degrades to finalizer-only
            pass
        return {"verify": {"finalizer": alias}}

    async def _verifier_deliverable_paths(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
    ) -> list[str]:
        paths: list[str] = [deliverable.artifact_path]
        paths.extend(_deliverable_event_paths(events))
        paths.extend(_plan_file_exists_paths(events))
        paths.extend(self._contract_required_deliverable_paths())
        spec = await self._loop.store.get_external_dod_spec(self._loop.conversation_id)
        if spec is not None:
            for pred in spec.predicates:
                if isinstance(pred, FileExistsPredicate):
                    p = _safe_deliverable_file_path(pred.path)
                    if p is not None:
                        paths.append(p)

        out: list[str] = []
        seen: set[str] = set()
        for p in paths:
            safe = _safe_deliverable_file_path(str(p))
            if safe is None or safe in seen:
                continue
            seen.add(safe)
            out.append(safe)
        return out

    async def _read_workspace_bytes(self, path: str) -> bytes | None:
        sbx = getattr(getattr(self._loop, "executor", None), "sandbox", None)
        if sbx is None:
            return None
        try:
            data = await sbx.read_file(path)
        except Exception:
            return None
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
        return None

    async def _manifest_present_for_html(
        self, html_path: str, html_text: str, deliverable_paths: list[str]
    ) -> bool:
        base_dir = posixpath.dirname(html_path)
        candidates: list[str] = []
        for href in html_manifest_hrefs(html_text):
            clean = href.split("#", 1)[0].split("?", 1)[0].strip()
            if not clean or "://" in clean or clean.startswith("//"):
                continue
            safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, clean)))
            if safe is not None:
                candidates.append(safe)
        if not candidates:
            safe_default = _safe_deliverable_file_path(
                posixpath.normpath(posixpath.join(base_dir, "manifest.json"))
            )
            if safe_default is not None:
                candidates.append(safe_default)
        candidates.extend(
            p for p in deliverable_paths if posixpath.basename(p).lower() == "manifest.json"
        )
        seen: set[str] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            if await self._read_workspace_bytes(path) is not None:
                return True
        return False

    async def _related_game_texts_for_html(self, html_path: str, html_text: str) -> list[str]:
        base_dir = posixpath.dirname(html_path)
        candidates: list[str] = []
        for src in html_script_srcs(html_text):
            clean = src.split("#", 1)[0].split("?", 1)[0].strip()
            if not clean or "://" in clean or clean.startswith("//"):
                continue
            safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, clean)))
            if safe is not None:
                candidates.append(safe)
        for name in ("README.md", "NOTES.md"):
            safe = _safe_deliverable_file_path(posixpath.normpath(posixpath.join(base_dir, name)))
            if safe is not None:
                candidates.append(safe)

        texts: list[str] = []
        seen: set[str] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            raw = await self._read_workspace_bytes(path)
            if raw is not None:
                texts.append(raw.decode("utf-8", errors="replace"))
        return texts

    async def _verifier_medium_hint(
        self,
        deliverable_paths: list[str],
    ) -> VerifierMediumHint | None:
        for path in deliverable_paths:
            if not path.lower().endswith((".html", ".htm")):
                continue
            raw = await self._read_workspace_bytes(path)
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace")
            manifest_present = await self._manifest_present_for_html(path, text, deliverable_paths)
            hint = detect_html_medium(
                text,
                manifest_present=manifest_present,
                related_texts=await self._related_game_texts_for_html(path, text),
            )
            if hint is not None:
                return hint
        return None

    async def _verifier_context_seed(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        check_verdict: dict[str, Any],
        *,
        contract: dict[str, Any],
        claims: tuple[HostVerificationClaim, ...] | None = None,
    ) -> VerifierContextSeed:
        deliverable_paths = await self._verifier_deliverable_paths(deliverable, events)
        selected_claims = claims if claims is not None else deliverable.required_claims
        screenshot = _screenshot_from_verdict(check_verdict)
        if not any(
            claim.kind is VerificationClaimKind.VISUAL_SEMANTIC for claim in selected_claims
        ):
            # A screenshot file is still useful provenance, but attaching its
            # pixels would manufacture a VISION requirement for non-visual work.
            screenshot = screenshot.model_copy(update={"image_data_url": ""})
        _, all_references = _user_verification_material(events)
        reference_images = tuple(
            reference
            for reference in all_references
            if any(
                claim.kind is VerificationClaimKind.VISUAL_SEMANTIC
                and claim.reference_image_sha256 == reference.sha256
                for claim in selected_claims
            )
        )
        return VerifierContextSeed(
            contract=contract,
            deliverable_paths=deliverable_paths,
            check_results=_bounded_verifier_check_results(check_verdict),
            screenshot=screenshot,
            reference_images=reference_images,
            medium=await self._verifier_medium_hint(deliverable_paths),
            claims=selected_claims,
        )

    async def _model_judged_verdict(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        host_verdict: dict[str, Any],
    ) -> dict[str, Any]:
        return await _judge_semantic_claims(self, deliverable, events, host_verdict)


class _HostVerifyGateMixin(_HostVerifyBaseMixin):
    async def _host_verify_failure_disposition(
        self, deliverable: HostVerificationDeliverable, verdict: dict
    ) -> Disp:
        label = self._verdict_label(verdict) or "fail"
        summary = str(
            verdict.get("summary") or verdict.get("detail") or "host verifier did not pass"
        )
        next_action = str(verdict.get("next_action") or "")
        first_failure = self._verdict_first_failure(verdict)
        await self._record_verifier_failure_to_context(
            message=(first_failure or summary), rel_path=None
        )
        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
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
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=payload),
                )
            )
            return Disp.CONTINUE

        await self._loop._emit(
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
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=warn),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def _governed_non_pass_disposition(
        self,
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
        host_label = self._verdict_label(verdict) or "fail"
        summary = str(
            verdict.get("summary")
            or verdict.get("detail")
            or "host verifier did not produce a current typed PASS receipt"
        )
        next_action = str(verdict.get("next_action") or "")
        first_failure = self._verdict_first_failure(verdict)
        failed_claim = (
            next(
                (
                    result
                    for result in typed_result.claim_results
                    if result.required and result.status is VerificationClaimStatus.FAIL
                ),
                None,
            )
            if typed_result is not None
            else None
        )
        unavailable_claim = (
            next(
                (
                    result
                    for result in typed_result.claim_results
                    if result.required and result.status is VerificationClaimStatus.UNAVAILABLE
                ),
                None,
            )
            if typed_result is not None
            else None
        )
        if typed_result is None:
            guidance = (
                "no current, complete typed receipt certifies the mandatory "
                "structured-browser claims. Start a managed preview with "
                "`preview_start` for the app entry, then finish again — the "
                "host verifier will bind to the canonical Preview generation "
                "and produce a complete typed receipt."
            )
        elif failed_claim is not None:
            guidance = (
                f"the typed host receipt reported a FAIL ({failed_claim.reason})."
                + (f" next_action: {next_action}" if next_action else "")
                + " Fix the failed claim, then finish again."
            )
        else:
            claim_detail = unavailable_claim.reason if unavailable_claim is not None else summary
            guidance = (
                f"the host verifier returned {host_label} ({claim_detail})."
                + (f" next_action: {next_action}" if next_action else "")
                + " Start a managed preview with `preview_start` or fix the "
                "surfaced issue, then finish again."
            )

        await self._record_verifier_failure_to_context(
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
            await self._loop._land_blocked(
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
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=payload),
            )
        )
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
            )
        )
        return Disp.CONTINUE

    async def _governed_contract_refusal(
        self,
        events: list[Event],
        *,
        failure_key: str,
        guidance: str,
    ) -> Disp:
        """Progress-sensitive fail-closed refusal without a retry release cap."""

        fingerprint = "host:" + hashlib.sha256(failure_key.encode()).hexdigest()[:24]
        authority_seq = _last_verification_authority_seq(events)
        if _prior_verify_marker_fp(events, authority_seq) == fingerprint:
            await self._loop._land_blocked(
                reason=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
                guidance=(
                    "Target verification repeated the same governed failure with no "
                    f"productive authority change. {guidance}"
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
            )
            return Disp.HALT
        await self._loop._emit(
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
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail=f"{_VERIFY_MARKER_PREFIX}{fingerprint}",
            )
        )
        return Disp.CONTINUE

    async def _governed_check_deliverables(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        *,
        skip_check_ids: frozenset[str] = frozenset(),
        preverified_claim_ids: dict[str, frozenset[str]] | None = None,
    ) -> tuple[tuple[HostVerificationDeliverable, ...], str | None]:
        contract = deliverable.verification_contract
        if contract is None:
            return (deliverable,), None
        checks = tuple(check for check in contract.checks if check.required)
        target_claims = {claim.claim_id: claim for check in checks for claim in check.claims}
        target_claim_ids = set(target_claims)
        all_claims = _host_verification_claims(self._verifier_contract_payload(), events)
        external = tuple(claim for claim in all_claims if claim.claim_id not in target_claim_ids)
        assigned: dict[str, list[HostVerificationClaim]] = {check.check_id: [] for check in checks}
        errors: list[str] = []
        for claim in all_claims:
            target = target_claims.get(claim.claim_id)
            if target is not None and claim != target:
                errors.append(
                    f"external claim {claim.claim_id!r} conflicts with the target contract"
                )
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
        for check_id in skip_check_ids:
            covered = (
                preverified_claim_ids.get(check_id, frozenset())
                if preverified_claim_ids is not None
                else frozenset()
            )
            uncovered = [
                claim for claim in assigned.get(check_id, ()) if claim.claim_id not in covered
            ]
            if uncovered:
                errors.append(f"preverified check {check_id!r} cannot absorb new external claims")
        out: list[HostVerificationDeliverable] = []
        host_verifier = getattr(self._loop, "_host_verifier", None)
        for check in checks:
            if check.check_id in skip_check_ids:
                continue
            item = deliverable.model_copy(
                update={
                    "verification_check": check,
                    "required_claims": (
                        *check.claims,
                        *assigned[check.check_id],
                    ),
                }
            )
            item = await self._with_host_verification_profile(item, events)
            binder = getattr(host_verifier, "bind_execution", None)
            if binder is not None:
                try:
                    bound = await binder(item)
                    if not isinstance(bound, HostVerificationDeliverable):
                        raise TypeError("execution binder returned an invalid deliverable")
                    original = item.model_dump(mode="json", exclude={"execution_identity"})
                    candidate = bound.model_dump(mode="json", exclude={"execution_identity"})
                    if original != candidate:
                        raise ValueError("execution binder changed non-execution authority")
                    item = bound
                except Exception as exc:  # noqa: BLE001 — fail closed as unavailable
                    errors.append(f"target execution binding failed for {check.check_id!r}: {exc}")
            if (
                item.execution_identity is None
                or item.execution_identity.modality != check.required_execution_modality
            ):
                errors.append(
                    f"target execution for {check.check_id!r} requires modality "
                    f"{check.required_execution_modality!r}"
                )
            out.append(item)
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
            errors.append(
                "required verifier checks resolved different target execution generations"
            )
        return tuple(out), "; ".join(errors) or None

    async def _run_host_verifier_check(
        self,
        deliverable: HostVerificationDeliverable,
        events: list[Event],
        *,
        host_verifier: Any,
        authoritative: bool,
        governed_target: bool,
        forced_unavailable: str | None = None,
    ) -> tuple[dict[str, Any], HostVerificationResult | None, bool, str | None]:
        started = await _emit_verifier_started_event(self._loop, deliverable)
        host_screenshot_path: str | None = None
        if forced_unavailable is not None:
            host_verdict = self._host_unavailable_verdict(deliverable, forced_unavailable)
        elif deliverable.artifact_kind != "app" and not governed_target:
            host_verdict = self._host_unverifiable_verdict(deliverable)
        elif host_verifier is None:
            host_verdict = self._host_unavailable_verdict(
                deliverable,
                "verification could not run: no host verifier is configured.",
            )
        else:
            try:
                raw_host_verdict = await asyncio.wait_for(
                    host_verifier.verify(deliverable),
                    timeout=max(
                        0.001,
                        float(getattr(self._loop, "_host_verify_timeout_s", 30.0)),
                    ),
                )
                if not isinstance(raw_host_verdict, dict):
                    host_verdict = self._host_unavailable_verdict(
                        deliverable,
                        "verification could not run: host verifier did not return "
                        "a usable verdict.",
                    )
                else:
                    host_verdict = raw_host_verdict
                    host_screenshot_path = _bounded_host_verdict_screenshot_path(
                        raw_host_verdict.get("screenshot_path")
                    )
            except TimeoutError:
                host_verdict = self._host_unavailable_verdict(
                    deliverable,
                    "verification could not run: host verifier timed out.",
                )
            except Exception as exc:  # noqa: BLE001 — verifier failure stays typed
                _LOG.warning(
                    "host verifier failed for %s:%s",
                    self._loop.conversation_id,
                    deliverable.artifact_path,
                    exc_info=True,
                )
                host_verdict = self._host_unavailable_verdict(
                    deliverable,
                    f"verification could not run: host verifier failed ({exc}).",
                )
        host_verdict, typed_result, browser_unavailable = await _prepare_typed_host_verdict(
            self,
            deliverable,
            events,
            cast(dict[str, Any], host_verdict),
        )
        host_label = await _emit_host_verdict_audit(
            self,
            deliverable,
            events,
            host_verdict,
            typed_result,
            host_screenshot_path,
            verifier_started_event_id=started.id,
        )
        return host_verdict, typed_result, browser_unavailable, host_label

    async def gate_host_verify(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        preverified: tuple[tuple[HostVerificationDeliverable, HostVerificationResult], ...] = (),
    ) -> Disp:
        """Run every admitted verifier check; legacy web remains a compatibility path."""

        contract = _governed_verification_contract(events)
        if _strict_appkit_contract(contract) and not preverified:
            return Disp.FALLTHROUGH

        governed_target = _governed_verification_required(events)
        authoritative = self._host_verify_authoritative() or governed_target
        host_verifier = getattr(self._loop, "_host_verifier", None)
        if host_verifier is None and not authoritative:
            return Disp.FALLTHROUGH
        deliverable = await self._host_verify_deliverable(
            step, events, include_unverifiable=authoritative
        )
        if deliverable is None:
            return Disp.FALLTHROUGH
        valid_preverified = tuple(
            (item, receipt)
            for item, receipt in preverified
            if item.verification_check is not None
            and receipt.passed
            and receipt.is_current_authority_for(item, observed_url=receipt.observed_url)
        )
        skip_check_ids = frozenset(
            item.verification_check.check_id
            for item, _receipt in valid_preverified
            if item.verification_check is not None
        )
        deliverables, ownership_error = await self._governed_check_deliverables(
            deliverable,
            events,
            skip_check_ids=skip_check_ids,
            preverified_claim_ids={
                item.verification_check.check_id: frozenset(
                    claim.claim_id for claim in item.required_claims
                )
                for item, _receipt in valid_preverified
                if item.verification_check is not None
            },
        )
        if ownership_error is not None and not deliverables:
            return await self._governed_contract_refusal(
                events,
                failure_key=f"target:claim_ownership:{ownership_error}",
                guidance=(
                    f"{ownership_error}. The admitted verifier set cannot honestly "
                    "cover the current mandatory requirement set."
                ),
            )
        if not deliverables and not valid_preverified:
            return Disp.FALLTHROUGH

        all_deliverables = [item for item, _receipt in valid_preverified]
        all_deliverables.extend(deliverables)
        accepted_receipts = [receipt for _item, receipt in valid_preverified]
        for index, check_deliverable in enumerate(deliverables):
            (
                host_verdict,
                typed_result,
                browser_unavailable,
                host_label,
            ) = await self._run_host_verifier_check(
                check_deliverable,
                events,
                host_verifier=host_verifier,
                authoritative=authoritative,
                governed_target=governed_target,
                forced_unavailable=ownership_error if index == 0 else None,
            )
            if authoritative and (check_deliverable.artifact_kind == "app" or governed_target):
                if host_verdict.get("passed") is True and host_label == "pass":
                    if typed_result is not None:
                        accepted_receipts.append(typed_result)
                    self._loop._browser_verify_refusals = 0
                    continue
                if governed_target:
                    return await self._governed_non_pass_disposition(
                        check_deliverable,
                        host_verdict,
                        typed_result,
                        events,
                    )
                if host_label == "unavailable":
                    if typed_result is not None and any(
                        result.required
                        and result.status is VerificationClaimStatus.UNAVAILABLE
                        and result.kind
                        in {
                            VerificationClaimKind.CONTRACT_SEMANTIC,
                            VerificationClaimKind.VISUAL_SEMANTIC,
                        }
                        for result in typed_result.claim_results
                    ):
                        return await self._host_verify_failure_disposition(
                            check_deliverable, host_verdict
                        )
                    if browser_unavailable:
                        return await _emit_browser_unavailable_release(self._loop)
                    return Disp.FALLTHROUGH
                if host_label == "unverifiable":
                    if host_verdict.get("model_verifier_applied") is True:
                        return await self._host_verify_failure_disposition(
                            check_deliverable, host_verdict
                        )
                    await self._loop._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING,
                            detail="unverified_release",
                        )
                    )
                    await self._loop._emit(
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
                    self._loop._browser_verify_refusals = 0
                    return Disp.FALLTHROUGH
                return await self._host_verify_failure_disposition(check_deliverable, host_verdict)
        if governed_target:
            coverage = aggregate_verification_receipts(
                deliverables=tuple(all_deliverables),
                receipts=tuple(accepted_receipts),
            )
            if not coverage.passed:
                return await self._governed_contract_refusal(
                    events,
                    failure_key=(
                        "target:incomplete_aggregate:" + ",".join(coverage.missing_claim_ids)
                    ),
                    guidance=(
                        "the target verifier set did not cover every mandatory claim "
                        "on one current execution generation."
                    ),
                )
            current_events = await self._loop._events()
            current_contract = _governed_verification_contract(current_events)
            current_handoff = _latest_deliverable_event(current_events)
            cast(Any, self._loop)._governed_host_pass_key = (
                current_contract.digest if current_contract is not None else "",
                current_handoff.id if current_handoff is not None else "",
                _last_verification_authority_seq(current_events),
            )
        return Disp.FALLTHROUGH


class _BrowserVerifyGateMixin(_FinishGateProto):
    async def _detect_preview_url(self) -> str | None:
        """P1-1 — detect the URL the live deliverable currently serves on, using the
        SAME backend-aware resolver the `verify_web_app` tool uses
        (`preview_target.resolve_preview_port`). Duck-typed over the executor's
        sandbox (`exec_shell`): core never imports `tools`, so the gate runs the
        shared in-sandbox ownership probe and feeds the result to the shared
        resolver.

        On a SHARED-host backend (process/local — `sandbox.workspace_path` is set)
        the resolver NEVER returns a reserved control port (8000 = the agent-server):
        it prefers a CONVERSATION-OWNED served port, else any non-reserved owned
        port, else None — so a stale verdict about `:8000` (the agent-server, Bug 7)
        can never bind the finish gate. On an ISOLATED backend (gVisor/Podman —
        `workspace_path` is None) `:8000` IS the app, so the legacy first-reachable
        socket probe is kept.

        Returns `http://127.0.0.1:<port>/` or None (no sandbox / no exec_shell /
        nothing detected), in which case binding is NOT enforced (see
        `_verdict_targets_preview`) and the gate drives a fresh verify against the
        real preview. Never raises (a detection failure must not wedge the gate)."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None or not hasattr(sbx, "exec_shell"):
            return None
        preview_ports = tuple(dict.fromkeys((*_PREVIEW_PORTS, *active_managed_preview_ports(sbx))))
        host_shared = backend_shares_host_network(sbx)
        if host_shared:
            # Process backend (shares host net): ownership-aware — never bind the
            # agent-server's 8000. Isolated containers fall to the branch below where
            # 8000 IS the app (the old `workspace_path` heuristic wrongly sent them here).
            try:
                res = await sbx.exec_shell(
                    port_ownership_probe_command(preview_ports), timeout_s=10
                )
            except Exception:  # noqa: BLE001 — detection failure → no binding (degrade safe)
                return None
            owned = parse_port_ownership(str(getattr(res, "stdout", "") or ""))
            port = resolve_preview_port(
                host_shared=True,
                owned=owned,
                conversation_id=str(getattr(sbx, "conversation_id", "") or ""),
                preview_ports=preview_ports,
            )
            return f"http://127.0.0.1:{port}/" if port is not None else None

        # Isolated backend: 8000 is the app inside the box — first reachable wins.
        import shlex

        ports = list(preview_ports)
        script = (
            "import socket,sys\n"
            f"for p in {ports!r}:\n"
            "    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
            "    s.settimeout(0.3)\n"
            "    try:\n"
            "        s.connect(('127.0.0.1',p))\n"
            "        print(p)\n"
            "        sys.exit(0)\n"
            "    except Exception:\n"
            "        pass\n"
            "    finally:\n"
            "        s.close()\n"
        )
        try:
            res = await sbx.exec_shell(f"python3 -c {shlex.quote(script)}", timeout_s=10)
        except Exception:  # noqa: BLE001 — detection failure → no binding (degrade safe)
            return None
        for line in str(getattr(res, "stdout", "") or "").splitlines():
            line = line.strip()
            if line.isdigit():
                return f"http://127.0.0.1:{int(line)}/"
        return None

    async def _drive_verify_web_app(
        self,
        target_url: str | None = None,
        tool_name: str = "verify_web_app",
        *,
        medium: str = "web",
    ) -> bool:
        """W-45 ACTIVE verify: DRIVE one structured verifier call when the agent
        declares done without a fresh verdict. Mirrors `_drive_finish_browser_probe`
        — the probe ActionEvent is tagged `verify_probe` so it never counts as
        agent work. Returns True iff the call produced a usable observation.

        When the gate has already resolved the real preview target (`target_url`,
        backend-aware — never the agent-server's 8000 on the process backend), it is
        passed EXPLICITLY so the tool verifies the build's actual served port instead
        of repeating its own auto-detect (Bug 7). None ⇒ `{}` ⇒ the tool auto-detects
        (which itself uses the same backend-aware resolver)."""
        arguments: dict[str, str] = {}
        if target_url:
            arguments["url"] = target_url
        if medium != "web" and tool_name == "verify_web_app":
            arguments["medium"] = medium
        call = ToolCall(tool_name=tool_name, arguments=arguments)
        action = ActionEvent(
            thought=f"Verifying the app: running {tool_name} on the running preview.",
            tool_call=call,
            meta={"verify_probe": True},
        )
        if signals.hard_deny_reason(action) is not None:
            return False
        action = cast(ActionEvent, await self._loop._emit(action))
        await self._loop._execute_and_observe(action)
        events_after = await self._loop._events()
        obs = next(
            (e for e in reversed(events_after) if getattr(e, "action_id", None) == action.id),
            None,
        )
        return isinstance(obs, ObservationEvent) and obs.tool_result.success

    async def _gate_verify_web_app(
        self,
        events: list[Event],
        tool_name: str = "verify_web_app",
        *,
        step: AgentStep | None = None,
        appkit_prepared: (tuple[VerifierStartedEvent, HostVerificationDeliverable] | None) = None,
    ) -> Disp:
        """W-45 — verdict-consuming finish gate replacing `_browser_verified()` for web
        builds with "the latest structured verifier verdict since the last productive
        edit". At finish it drives once: pass → finish; fail → surface the verdict's
        evidence and next action, then CONTINUE.

        LOOP BREAKER: the verdict is cached by `_last_productive_seq` — a browser/
        navigate/verify probe is NOT a productive edit, so re-verifying without an
        edit reads the SAME cached verdict (no real re-run). When the SAME
        `failure_fingerprint` recurs without a productive edit that changes the
        served output, the gate marks the run STUCK instead of reloading 25-40×.
        The 3-refusal release no longer
        silently converts repeated failed verification into 'done' — it finishes
        ONLY with an explicit blocked/incomplete summary."""
        contract = _governed_verification_contract(events)
        governed_appkit = bool(
            _strict_appkit_contract(contract) and _governed_verification_required(events)
        )
        since_seq = _last_verification_authority_seq(events)
        if governed_appkit and contract is not None:
            started = _latest_matching_appkit_start(events, contract)
            if started is not None and type(started.seq) is int:
                since_seq = max(since_seq, started.seq)
        # P1-1: bind the accepted verdict to the CURRENT preview target. Detect the
        # live preview (same _PREVIEW_PORTS detection the tool uses) and accept only
        # a verdict whose url matches it; a stale / foreign-port (or url-less) PASS
        # must NOT satisfy the gate — it drives a fresh verify against the real
        # preview instead. target_url=None (preview undetectable) disables binding.
        target_url = await self._detect_preview_url()
        medium = await _finish_verification_medium(self, step, events)
        verdict = _latest_verify_verdict(events, since_seq, target_url, tool_name)
        if verdict is None:
            # No fresh verdict bound to the current preview — the agent may have
            # overclaimed, or only a stale/foreign-url verdict exists. Drive ONE
            # against the resolved real preview (target_url is backend-aware — never
            # the agent-server's 8000 on the process backend, Bug 7).
            if await self._drive_verify_web_app(target_url, tool_name, medium=medium):
                events = await self._loop._events()
                # The freshly driven verify auto-detected + tested the CURRENT
                # preview, so its verdict IS bound by construction — read it
                # unconditionally (target_url=None) rather than re-binding against a
                # detection that could disagree with the tool's own auto-detect.
                verdict = _latest_verify_verdict(
                    events, since_seq, target_url=None, tool_name=tool_name
                )
        if verdict is None:
            # P1-2: the verifier could not produce a usable verdict (execution
            # error / empty / the driven verify failed). This must NOT fall through
            # to a clean finish — on the build/web surface a FINISH requires a real
            # PASS verdict (W-32: route completion THROUGH the gate). Refuse-and-
            # continue (bounded), then an EXPLICIT unverified release at the cap;
            # never a silent done, never a fall-back to the legacy browser gate.
            if governed_appkit:
                host_gate = cast(_HostVerifyGateMixin, self)
                typed_disp = await _appkit_typed_gate_disposition(
                    host_gate,
                    events=await self._loop._events(),
                    verdict=None,
                    tool_name=tool_name,
                    prepared=appkit_prepared,
                )
                if typed_disp is not None:
                    return typed_disp
                return await host_gate._governed_contract_refusal(
                    events,
                    failure_key=f"{tool_name}:unavailable",
                    guidance=(
                        f"{tool_name} did not return a usable strict target verdict. "
                        "Repair the target verifier/runtime; unavailable verification "
                        "cannot release this governed build."
                    ),
                )
            return await self._verifier_unavailable_disposition(tool_name)

        if governed_appkit:
            typed_disp = await _appkit_typed_gate_disposition(
                cast(_HostVerifyGateMixin, self),
                events=await self._loop._events(),
                verdict=verdict,
                tool_name=tool_name,
                prepared=appkit_prepared,
            )
            if typed_disp is not None:
                return typed_disp

        if verdict.get("passed") is True:
            self._loop._browser_verify_refusals = 0  # clean pass → reset the streak
            if _vision_mode():
                shot = str(verdict.get("screenshot_path") or "")
                if shot:
                    await self._loop._emit(
                        StatusEvent(
                            status=ConversationStatus.RUNNING,
                            detail=f"vision_artifact:{shot}",
                        )
                    )
            return Disp.FALLTHROUGH

        # Bug 6 — HONEST unverifiable finish for a delivered-but-unverifiable static
        # build. If the ONLY failure is "not serving" (server unreachable; the
        # browser never even ran ⇒ no console/network errors and no blank-render
        # judgement) AND this backend cannot run a headless browser AND the static
        # deliverable file exists on disk, then the build is UNVERIFIABLE (infra),
        # not BROKEN — finish honestly with an explicit marker instead of refusing →
        # STUCK. A REAL fail (console errors, network failures, blank render, or a
        # MISSING deliverable) never reaches here, so W-45 is not weakened. This runs
        # only after `gate_execution_nudge` (so an approved-but-unexecuted plan still
        # STUCKs there) and requires index.html on disk (so a zero-action run cannot
        # finish).
        honest = await self._maybe_honest_unverifiable_static_finish(verdict)
        if honest is not None:
            return honest

        # FAIL / DEGRADED. Build the concrete next-step payload from the verdict.
        fp, summary, next_action, screenshot, first_error = _verification_failure_details(
            verdict, tool_name
        )

        # CXT-7: record the failure to durable context (.disco/context/verifier_failures.json),
        # best-effort — NEVER alters the gate verdict/flow. CXT-4's assembler surfaces these
        # unresolved failures into the model's ContextPack on later turns/resume.
        await self._record_verifier_failure_to_context(
            message=(first_error or summary), rel_path=(screenshot or None)
        )

        prior_fp = _prior_verify_marker_fp(events, since_seq)
        if prior_fp is not None and prior_fp == fp:
            # LOOP BREAKER: the SAME failure verdict has recurred since the last
            # productive edit and the gate already nudged for it — no new
            # information. Halt STUCK (named) instead of re-loading forever.
            await self._loop._land_blocked(
                reason=f"{_VERIFY_MARKER_PREFIX}{fp}",
                guidance=(
                    f"{tool_name} kept returning the same failure with no "
                    f"progress since the last edit. {summary}"
                    + (f" Error: {first_error}" if first_error else "")
                    + (f" Next: {next_action}" if next_action else "")
                ),
                legacy_status=ConversationStatus.STUCK,
                legacy_detail=f"{_VERIFY_MARKER_PREFIX}{fp}",
            )
            return Disp.HALT

        if governed_appkit or self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            payload = (
                f"{tool_name} did not pass ({verdict.get('verdict')}). {summary}\n"
                + (f"first error: {first_error}\n" if first_error else "")
                + (f"screenshot: {screenshot}\n" if screenshot else "")
                + (f"next step: {next_action}" if next_action else "Fix the issue, then finish.")
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=payload),
                )
            )
            # Stamp the fingerprint so a repeat WITHOUT a productive edit trips the
            # loop breaker above on the next finish attempt.
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail=f"{_VERIFY_MARKER_PREFIX}{fp}",
                )
            )
            return Disp.CONTINUE

        # 3-refusal release — but NOT a silent 'done' (P1-3). The release emits a
        # DISTINCT terminal signal (StatusEvent detail="unverified_release") AND a
        # visible INCOMPLETE message BEFORE finalization, so a repeatedly-failing
        # web build can never present as a clean verified FINISHED — the UI/harness
        # keys on the marker, not on the agent's pre-gate summary text. Bounded:
        # the run still releases after the cap so it cannot hang forever.
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        warn = (
            f"⚠ Finished WITHOUT a passing {tool_name} verdict (3 attempts) — the "
            f"deliverable is INCOMPLETE. {summary}"
            + (f" Outstanding: {next_action}" if next_action else "")
            + " Note this clearly in your summary."
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=warn),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    def _browser_verification_unavailable(self) -> bool:
        """True when this backend cannot run browser-based verification (the process
        backend ships no `browser` tool). Mirrors `_drive_finish_browser_probe`'s
        availability check — the precise "the render/console check cannot run here"
        signal that distinguishes an UNVERIFIABLE delivery from a BROKEN app."""
        try:
            tool_names = {getattr(t, "name", None) for t in self._loop.executor.available_tools()}
        except Exception:  # noqa: BLE001 — introspection failure → assume available (cautious)
            return False
        return "browser" not in tool_names

    async def _static_deliverable_present(self) -> bool:
        """True when the static web deliverable (index.html) exists on disk in the
        sandbox workspace — the file-truth half of the honest unverifiable finish (a
        zero-action run wrote nothing, so this is False and it cannot finish)."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None or not hasattr(sbx, "file_exists"):
            return False
        try:
            return bool(await sbx.file_exists("index.html"))
        except Exception:  # noqa: BLE001 — existence probe failure → cannot confirm
            return False

    async def _record_verifier_failure_to_context(
        self, *, message: str, rel_path: str | None
    ) -> None:
        """CXT-7 — persist a verify_web_app failure to .disco/context/verifier_failures.json
        (best-effort; never alters the gate flow). Survives truncation/resume so the
        CXT-4 assembler can surface the unresolved failure to the model later."""
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return
        try:
            from ...context import ArtifactMemoryStore, Severity, VerifierFailureRef

            await ArtifactMemoryStore(sbx).record_verifier_failures(
                (
                    VerifierFailureRef(
                        kind="verify_web_app",
                        message=(message or "verify_web_app did not pass")[:500],
                        rel_path=rel_path or None,
                        severity=Severity.ERROR,
                    ),
                )
            )
        except Exception:
            _LOG.warning(
                "CXT-7 verifier-failure context write failed for %s",
                self._loop.conversation_id,
                exc_info=True,
            )

    async def _maybe_honest_unverifiable_static_finish(self, verdict: dict) -> Disp | None:
        """Bug 6 — when a web build's verify FAILS ONLY because nothing is serving
        (no console/network errors, no blank-render judgement: the browser never ran)
        AND this backend cannot run a headless browser AND index.html exists on disk,
        return FALLTHROUGH with an explicit honest-unverifiable marker so a delivered
        static build FINISHES instead of pausing/STUCKing. Returns None (let the
        normal refuse/loop-break path run) for every other case — a real fail
        (console/network/blank) or a missing deliverable is NEVER converted to a
        success (W-45 preserved)."""
        http_ok = 200 <= int(verdict.get("http_status") or 0) < 400
        not_serving = (
            str(verdict.get("verdict")) == "fail"
            and not (verdict.get("console_errors") or [])
            and not (verdict.get("network_failures") or [])
            and not http_ok
        )
        if not not_serving:
            return None
        if not self._browser_verification_unavailable():
            return None
        if not await self._static_deliverable_present():
            return None
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverifiable_static_finish",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "⚠ Finished WITHOUT a live browser verification — the static "
                        "deliverable (index.html) exists but no preview server is "
                        "reachable and this backend cannot run a headless browser, so "
                        "the render could not be checked here. The files are delivered; "
                        "note clearly in your summary that the build is UNVERIFIED."
                    ),
                ),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def maybe_honest_unverifiable_static_actionless_finish(self, events: list[Event]) -> bool:
        """Bug 6 — the ACTIONLESS-VALVE twin of `_maybe_honest_unverifiable_static_finish`.

        The finish-gate honest path only runs when the model REACHES the finish gate.
        On a browserless backend with a final browser-verify plan step the model never
        does — it churns on the unsatisfiable step and the actionless valve would PAUSE
        a substantively-complete build. This applies the SAME honest-finish concept at
        the valve: when (and ONLY when) the conservative conditions below all hold, emit
        the honest marker + a clean terminal FINISHED and return True; otherwise return
        False so the valve keeps its existing pause/stuck behavior.

        Conservative conditions (ALL required — any failure ⇒ False ⇒ no honest finish):
          1. a plan exists, is INCOMPLETE, and EVERY not-done step is verify-only;
          2. real productive work happened since approval (APPROVE_PLAN_NO_EXECUTION —
             a zero-action run can never honest-finish here);
          3. the static deliverable (index.html) exists on disk;
          4. a NON-browser validation PASSED after the last write/edit (a failed or
             absent validation blocks);
          5. browser verification is GENUINELY unavailable (no browser tool, OR a
             browser observation/error carried the unavailable signal);
          6. NO real web-failure evidence (console/network errors or a served-but-blank
             render) — W-45: a genuinely BROKEN app is never converted to a success.
        """
        if not _missing_steps_all_verify(events):
            return False
        if signals.productive_actions_since_approval(events) <= 0:
            return False
        if not await self._static_deliverable_present():
            return False
        if not _nonbrowser_static_validation_passed(events):
            return False
        if not (self._browser_verification_unavailable() or _browser_unavailable_observed(events)):
            return False
        if _real_web_failure_evidence(events):
            return False
        # All guards hold — finish honestly instead of pausing actionless. Same honest
        # marker as the finish-gate path, then a clean terminal FINISHED (NOT PAUSED).
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverifiable_static_finish",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "⚠ Finished WITHOUT a live browser verification — the static "
                        "deliverable (index.html) exists and a non-browser validation "
                        "passed, but this backend cannot run a headless browser and no "
                        "preview server is reachable, so the only remaining plan step "
                        "(browser verification) could not run here. The files are "
                        "delivered; note clearly in your summary that the render is "
                        "UNVERIFIED."
                    ),
                ),
            )
        )
        await self._loop._emit(StatusEvent(status=ConversationStatus.FINISHED))
        self._loop._browser_verify_refusals = 0
        return True

    async def _verifier_unavailable_disposition(self, tool_name: str = "verify_web_app") -> Disp:
        """P1-2 — disposition when the structured verifier is advertised but produced NO
        usable verdict (verifier execution error / empty / the driven verify
        failed). On the build/web surface a clean FINISH requires a real PASS
        verdict, so this must NOT fall through to finalization (the W-32 regression
        codex found). Refuse-and-continue with a "verification could not run"
        reminder while under the cap; at the cap, release EXPLICITLY as unverified
        (distinct status marker + visible message) rather than a silent clean
        finish. Bounded by the shared `_browser_verify_refusals` cap so a verifier
        that can never run still terminates."""
        if self._loop._browser_verify_refusals < 3:
            self._loop._browser_verify_refusals += 1
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            f"verification could not run: {tool_name} did not return a "
                            "usable verdict (the verifier failed to execute, or the preview "
                            "server is not reachable on its port). The build is NOT verified "
                            "— start/repair the dev server on the preview port, then finish "
                            "again and it will re-verify."
                        ),
                    ),
                )
            )
            return Disp.CONTINUE
        # Cap reached — bounded release, but EXPLICITLY unverified (never a clean
        # done): distinct terminal marker + visible message, mirroring the FAIL
        # release above.
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="unverified_release",
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        f"⚠ Finished WITHOUT a {tool_name} verdict — the verifier could "
                        "not run after 3 attempts, so the deliverable is UNVERIFIED and may "
                        "be INCOMPLETE. Note this clearly in your summary."
                    ),
                ),
            )
        )
        self._loop._browser_verify_refusals = 0
        return Disp.FALLTHROUGH

    async def _browser_verify_delegated_to_host(self, step: AgentStep, events: list[Event]) -> bool:
        contract = _governed_verification_contract(events)
        if _strict_appkit_contract(contract):
            return False
        governed = _governed_verification_required(events)
        if not (self._host_verify_authoritative() or governed):
            return False
        if getattr(self._loop, "_host_verifier", None) is None:
            return False
        deliverable = await self._host_verify_deliverable(step, events)
        if deliverable is None:
            return False
        if contract is not None:
            handoff = _latest_deliverable_event(events)
            expected_key = (
                contract.digest,
                handoff.id if handoff is not None else "",
                _last_verification_authority_seq(events),
            )
            return getattr(self._loop, "_governed_host_pass_key", None) == expected_key
        if deliverable.artifact_kind != "app":
            return False
        latest = _latest_host_verifier_event(events, _last_verification_authority_seq(events))
        if latest is None or latest.verification_result is None:
            return False
        result = latest.verification_result
        if (
            latest.artifact_path != deliverable.artifact_path
            or latest.artifact_kind != deliverable.artifact_kind
            or not result.is_current_for(deliverable, observed_url=result.observed_url)
        ):
            return False
        if latest.verdict in ("pass", "fail"):
            return True
        if latest.verdict == "unavailable":
            return any(
                isinstance(event, StatusEvent)
                and event.detail == "unverified_release"
                and (event.seq or 0) > (latest.seq or 0)
                for event in events
            )
        return False

    async def gate_browser_verify(
        self,
        step: AgentStep,
        events: list[Event],
        *,
        appkit_prepared: (tuple[VerifierStartedEvent, HostVerificationDeliverable] | None) = None,
    ) -> Disp:
        # BROWSER-VERIFY GATE — §BP-05. If web deliverable holds, refuse finish
        # until a clean browser observation (zero console errors) exists
        # since the last state-changing edit.
        verify_tool = self._active_verify_tool()
        # Strict AppKit mode is detected from the EXECUTOR (duck-typed phase
        # attribute), not only the advertised tool set: before a successful
        # app_create the phase allowlist hides verify_appkit_app, and a finish
        # in that window must still be gated (a zero-work appkit FINISH slipped
        # through here, live 2026-07-03).
        contract = _governed_verification_contract(events)
        is_appkit = (
            _strict_appkit_contract(contract)
            if contract is not None
            else verify_tool == "verify_appkit_app"
            or getattr(self._loop.executor, "appkit_phase", None) is not None
        )
        # WO-TC3: a build that installed trusted components must pass through this
        # gate even without a web-deliverable marker — the component integrity/deps/
        # probe checks ride the verify_web_app verdict. This is kept OUT of the
        # _planning_tools guard on purpose: verification is forced by the mere fact
        # that components were installed, so the security property does not depend
        # on the (currently true) invariant that the install tool only ever lives
        # in a plan-gated scope. add_trusted_component can only fire in the build
        # surface, so no non-build finish is affected in practice.
        tc_installed = _tc_components_installed(events)
        # Governed structured-browser target with an app handoff: mandatory target
        # claims (HTTP ready, rendered content, console/network clean, …) require a
        # structured browser runtime. The legacy ``_is_web_deliverable`` detector
        # recognizes index.html writes, preview_start pairs, and server_status
        # records — it does NOT recognize a governed ``app`` handoff for a non-
        # index entry (e.g. ``app.py`` after cancel/recovery). Without this
        # disjunct the inline gate never arms and a host-unavailable verdict falls
        # through to FINISHED with zero claim enforcement. This is claim-driven
        # (target-owned), not filename/framework-specific: any governed admission
        # whose mandatory claims require a browser runtime arms the gate when an
        # app handoff exists, independently of the legacy detector.
        governed_browser_target = (
            _governed_structured_browser_target(events)
            and not is_appkit
            and _latest_app_deliverable_event(events) is not None
        )
        if self._loop.mode != OperatingMode.PLANNING and (
            tc_installed
            or governed_browser_target
            or (self._loop._planning_tools and (is_appkit or _is_web_deliverable(events)))
        ):
            # W-45: when the structured `verify_web_app` tool is in the execution
            # set (the build surface), consume its VERDICT as the primary check —
            # the clean actionable signal that kills the reload loop. The legacy
            # raw-observation path below stays for browserless backends / tests
            # without the tool (non-web + assist paths are untouched: this whole
            # block is gated on _is_web_deliverable / strict AppKit mode).
            if await self._browser_verify_delegated_to_host(step, events):
                return Disp.FALLTHROUGH
            if verify_tool is not None:
                return await self._gate_verify_web_app(
                    events,
                    verify_tool,
                    step=step,
                    appkit_prepared=appkit_prepared,
                )
            since_seq = _last_verification_authority_seq(events)
            # The preview platform assigns a RANDOM port — there is NO fixed :8000
            # inside the sandbox (a curl there 404s). Resolve the live preview the
            # SAME backend-aware way the verify_web_app gate does and bind every
            # browser-observation check + the driven probe + the nudge text to it.
            # None ⇒ undetectable (sandbox-less / legacy / isolated where :8000 IS
            # the app) ⇒ the readers fall back to the historical :8000 acceptance.
            target_url = await self._detect_preview_url()
            target_key = _preview_key(target_url) if target_url else None
            ok, _ = _browser_verified(events, since_seq, target_key)
            if not ok:
                # ACTIVE verify (verification-overclaim fix): the AGENT has NOT
                # produced a clean preview browser observation since the last edit.
                # Rather than wait/trust it to browse (it may have overclaimed and
                # never looked), DRIVE the browse ourselves (against the resolved
                # preview, not a dead :8000) and judge the probe on ground truth —
                # zero console errors AND a non-blank render (a page can serve 200
                # with a clean console yet mount nothing). Degrades to the prior
                # passive nudge/release on browserless backends (the probe is a
                # no-op there). Bounded by the existing 3-refusal cap below.
                if await self._drive_finish_browser_probe(target_url):
                    events = await self._loop._events()
                    probe = _latest_browser_structured(events, target_key)
                    if probe is not None:
                        probe_console_clean = not any(
                            c.get("level") == "error" for c in probe.get("console", [])
                        )
                        ok = probe_console_clean and _browser_content_meaningful(probe)
            # Messaging reads the FULL history: a post-browse edit
            # invalidates the verification but not what was seen.
            first_error = _latest_browser_error(events, target_key)
            if ok:
                self._loop._browser_verify_refusals = 0  # reset on clean pass
                # W6 vision artifact: when the browser observation includes a
                # screenshot, emit a StatusEvent so the UI / post-run harness can
                # find it (satisfies "finish-gate captures a screenshot" in the
                # no-test UI finish-gate). Only emitted when vision mode is active.
                if _vision_mode():
                    shot = _latest_browser_screenshot(events, target_key)
                    if shot:
                        await self._loop._emit(
                            StatusEvent(
                                status=ConversationStatus.RUNNING,
                                detail=f"vision_artifact:{shot}",
                            )
                        )
            elif self._loop._browser_verify_refusals < 3:
                self._loop._browser_verify_refusals += 1
                # Point the agent at the RESOLVED preview (random platform port), not
                # a dead :8000; fall back to :8000 only when undetectable.
                preview_url = target_url or "http://127.0.0.1:8000/"
                if first_error:
                    # Variant (2): quote the error
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        f"Use the browser tool to navigate to {preview_url}, "
                        "read the CONSOLE output, and fix any errors you see. "
                        f"The last load had errors: {first_error}"
                    )
                else:
                    # Variant (1): verbatim from order
                    nudge = (
                        "Before finishing: verify your app the way a user would. "
                        f"Use the browser tool to navigate to {preview_url}, "
                        "read the CONSOLE output, and fix any errors you see. "
                        "Finish only after a clean load."
                    )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=nudge),
                    )
                )
                return Disp.CONTINUE
            else:
                # 3-refusal release valve (3): allow but warn visibly
                warn_msg = (
                    "⚠ finished WITHOUT a clean browser verification — "
                    f"last console errors: {first_error or 'none seen'}"
                )
                await self._loop._emit(
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(role="user", content=warn_msg),
                    )
                )
        return Disp.FALLTHROUGH


class _ExportRenderGateMixin(_FinishGateProto):
    async def _manifest_export_artifact_path(
        self, events: list[Event], *, require_shown: bool
    ) -> str | None:
        records = await self._artifact_manifest_records(events, consumer="export_render")
        if records is None:
            return None

        shown: list[str] = []
        unshown: list[str] = []
        seen: set[str] = set()
        for record in records:
            if _artifact_record_kind(record) == "app":
                continue
            raw_path = getattr(record, "path", None)
            if not isinstance(raw_path, str):
                continue
            path = _safe_deliverable_file_path(raw_path)
            if path is None or path in seen:
                continue
            if export_render_facts_for_path(events, path) is None:
                continue
            seen.add(path)
            if bool(getattr(record, "shown", False)):
                shown.append(path)
            else:
                unshown.append(path)

        if shown:
            return shown[-1]
        if require_shown:
            return None
        if len(unshown) == 1:
            return unshown[0]
        if len(unshown) > 1:
            _LOG.info(
                "artifact-manifest reader export path ambiguous for %s: %s",
                self._loop.conversation_id,
                unshown,
            )
        return None

    async def gate_export_render(self, step: AgentStep, events: list[Event]) -> Disp:
        """[P10] Refuse FINISHED when a rendered export (deck/document) is BLANK,
        TRUNCATED, or CORRUPT — the "looks done but the file is empty" false
        completeness. The real executor for the inert ``ExportContract.validate``
        stage.

        Reads the render facts the producer stamped from the ACTUAL output bytes
        (``latest_export_render_facts``), NOT the model's declared slide_count. A
        broken export re-enters the loop with a concrete steer; a good one (or none
        produced) falls through. Bounded by ``EXPORT_GATE_MAX_REFUSALS`` so a
        genuinely-broken renderer can't trap the run — it releases with a loud
        UNVERIFIED warning, exactly like the browser-verify valve. Decision/message
        logic is pure (``contract.export_render``); this method only emits."""
        # The latest deliverable AFTER the export decides which facts govern this
        # finish (only the latest counts: a newer files handoff means the broken deck
        # is current and must be gated).
        export_idx = latest_export_render_index(events)
        latest_post_export_deliverable: DeliverableEvent | None = None
        for i in range(len(events) - 1, export_idx, -1):
            ev = events[i]
            if isinstance(ev, DeliverableEvent):
                latest_post_export_deliverable = ev
                break
        # An app deliverable emitted AFTER the broken export means the app is the
        # current handoff and the deck is superseded — fall through (the app gates
        # own that path).
        if (
            latest_post_export_deliverable is not None
            and latest_post_export_deliverable.artifact_kind == "app"
        ):
            return Disp.FALLTHROUGH

        # A FILES handoff naming a specific stamped file is gated on THAT file's facts,
        # so delivering a known-bad export can't clear on a newer sibling's good facts.
        # Otherwise the latest stamped export governs.
        facts: ExportRenderFacts | None = None
        manifest_path = await self._manifest_export_artifact_path(
            events,
            require_shown=(
                latest_post_export_deliverable is not None
                and latest_post_export_deliverable.artifact_kind == "files"
            ),
        )
        if manifest_path is not None:
            facts = export_render_facts_for_path(events, manifest_path)
        if (
            facts is None
            and latest_post_export_deliverable is not None
            and latest_post_export_deliverable.artifact_kind == "files"
        ):
            facts = export_render_facts_for_path(events, latest_post_export_deliverable.path)
        if facts is None:
            facts = latest_export_render_facts(events)
        if facts is None or facts.ok:
            return Disp.FALLTHROUGH  # no export stamped, or it renders fine

        if count_export_gate_refusals(events, since=export_idx) >= EXPORT_GATE_MAX_REFUSALS:
            await self._loop._emit(
                StatusEvent(status=ConversationStatus.RUNNING, detail="unverified_export"),
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=export_gate_release_warning(facts)),
                )
            )
            return Disp.FALLTHROUGH

        await self._record_verifier_failure_to_context(message=facts.detail, rel_path=None)
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content=export_gate_refusal_reminder(facts)),
            )
        )
        return Disp.CONTINUE


class _RenderVerifyGateMixin(_FinishGateProto):
    async def _workflow_output_path_exists(self, path: str) -> tuple[bool, str]:
        safe = _safe_deliverable_file_path(path)
        if safe is None:
            return False, path

        sbx = getattr(self._loop.executor, "sandbox", None)
        file_exists = getattr(sbx, "file_exists", None) if sbx is not None else None
        if callable(file_exists):
            file_exists_fn = cast(Callable[[str], Awaitable[object]], file_exists)
            try:
                return bool(await file_exists_fn(safe)), safe
            except Exception as exc:  # noqa: BLE001 — cannot confirm output existence
                _LOG.warning(
                    "workflow output existence check failed for %s:%s: %s",
                    self._loop.conversation_id,
                    safe,
                    exc,
                )
                return False, safe

        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        if not workspace:
            return False, safe

        from pathlib import Path

        try:
            root = Path(workspace).resolve()
            candidate = (root / safe).resolve()
            root_s = str(root)
            cand_s = str(candidate)
            if not (cand_s == root_s or cand_s.startswith(root_s.rstrip("/") + "/")):
                return False, safe
            return candidate.is_file(), safe
        except OSError as exc:
            _LOG.warning(
                "workflow output existence check failed for %s:%s from workspace: %s",
                self._loop.conversation_id,
                safe,
                exc,
            )
            return False, safe

    async def gate_workflow_output_contract(
        self,
        step: AgentStep,
        events: list[Event],  # noqa: ARG002
    ) -> Disp:
        workflow_run = getattr(self._loop, "_workflow_run", None)
        if workflow_run is None:
            return Disp.FALLTHROUGH
        contract = getattr(getattr(workflow_run, "definition", None), "output_contract", None)
        if contract is None:
            return Disp.FALLTHROUGH

        params = getattr(workflow_run, "params", {})
        if not isinstance(params, dict):
            params = {}
        output_path = _render_workflow_output_path(contract.path_template, params)
        exists, checked_path = await self._workflow_output_path_exists(output_path)
        meta = {
            "workflow_run_id": str(getattr(workflow_run, "run_id", "")),
            "workflow_output_path": checked_path,
            "workflow_output_format": contract.format,
        }
        if exists:
            self._loop._workflow_output_contract_refusals = 0
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail="workflow_output_contract_passed",
                    meta={**meta, "verdict": "pass"},
                )
            )
            return Disp.FALLTHROUGH

        # RELEASE VALVE — an uncapped refusal is the sealed done-trap wearing a
        # new mask: a model that never lands the contract path would be refused
        # finish forever. After the cap, release with an HONEST warning instead.
        if self._loop._workflow_output_contract_refusals >= _FINISH_VERIFY_CAP:
            await self._loop._emit(
                StatusEvent(
                    status=ConversationStatus.RUNNING,
                    detail="workflow_output_contract_release",
                    meta={**meta, "verdict": "release"},
                )
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(
                        role="user",
                        content=(
                            "⚠ Finished despite the workflow output contract not being "
                            f"satisfied after {self._loop._workflow_output_contract_refusals} "
                            f"refusals. Expected `{checked_path}` ({contract.format}) in the "
                            "workspace. The workflow output is missing; note this clearly in "
                            "the summary."
                        ),
                    ),
                )
            )
            self._loop._workflow_output_contract_refusals = 0
            return Disp.FALLTHROUGH

        self._loop._workflow_output_contract_refusals += 1
        await self._loop._emit(
            StatusEvent(
                status=ConversationStatus.RUNNING,
                detail="workflow_output_contract_refused",
                meta={**meta, "verdict": "fail"},
            )
        )
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "finish refused: this workflow completes by writing "
                        f"{checked_path}. Write it (file_write), then call finish. "
                        "If the workflow should not produce output, use `skip` with "
                        "a reason instead of `finish`.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )
        return Disp.CONTINUE

    async def run_finish_verify_gates(self, step: AgentStep, events: list[Event]) -> Disp:
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
        render-verify gates clear)."""
        disp = await self.gate_workflow_output_contract(step, events)
        if disp is Disp.CONTINUE or disp is Disp.HALT:
            return disp
        events = await self._loop._events()

        contract = _governed_verification_contract(events)
        strict_appkit = _strict_appkit_contract(contract)
        host_gate = cast(_HostVerifyGateMixin, self)
        appkit_prepared: tuple[VerifierStartedEvent, HostVerificationDeliverable] | None = None
        if contract is not None and strict_appkit:
            compatibility_error = _strict_appkit_compatibility_error(contract)
            if compatibility_error is not None:
                return await host_gate._governed_contract_refusal(
                    events,
                    failure_key=f"appkit:unsupported_contract:{compatibility_error}",
                    guidance=compatibility_error,
                )
            if self._active_verify_tool() != "verify_appkit_app":
                return await host_gate._governed_contract_refusal(
                    events,
                    failure_key="appkit:strict_verifier_unavailable",
                    guidance=(
                        "the admitted AppKit target requires verify_appkit_app, but that "
                        "strict verifier is not available in the current execution scope."
                    ),
                )
            appkit_prepared = await _prepare_appkit_typed_authority(
                host_gate,
                step,
                events,
                contract,
            )
            if appkit_prepared is None:
                return await host_gate._governed_contract_refusal(
                    events,
                    failure_key="appkit:typed_preflight_unavailable",
                    guidance=(
                        "the strict AppKit target could not bind its canonical entry "
                        "and current managed runtime before verification."
                    ),
                )
            events = await self._loop._events()
        handoff = _latest_deliverable_event(events)
        missing_or_foreign_handoff = (
            contract is not None
            and _governed_verification_required(events)
            and not strict_appkit
            and (handoff is None or not _handoff_matches_verification_contract(handoff, contract))
        )
        legacy_missing_web_handoff = (
            contract is None
            and _governed_structured_browser_target(events)
            and not _appkit_scope_active(self._loop)
            and _latest_app_deliverable_event(events) is None
        )
        if missing_or_foreign_handoff:
            detail = _handoff_refusal_detail(handoff, contract)
            return await host_gate._governed_contract_refusal(
                events,
                failure_key="target:missing_or_foreign_handoff",
                guidance=(
                    f"{detail}. Hand off the target-owned entry with the adapter's "
                    "interactive-app or artifact-files shape. Serving the SAME entry "
                    "again will be ignored as a duplicate and will not clear this — "
                    "change the fact named above."
                ),
            )
        if legacy_missing_web_handoff:
            self._loop._invisible_steps += 1
            await self._loop._emit(
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
            return await self._loop._post_noop_valve()

        if strict_appkit:
            disp = await self.gate_browser_verify(
                step,
                events,
                appkit_prepared=appkit_prepared,
            )
            if disp is Disp.CONTINUE or disp is Disp.HALT:
                return disp
            events = await self._loop._events()
            appkit_result = (
                _recorded_appkit_typed_result(events, appkit_prepared[0])
                if appkit_prepared is not None
                else None
            )
            if appkit_prepared is None or appkit_result is None or not appkit_result.passed:
                return await host_gate._governed_contract_refusal(
                    events,
                    failure_key="appkit:typed_receipt_unavailable",
                    guidance=(
                        "the strict AppKit verifier passed, but its result could not "
                        "be bound to the current typed target authority."
                    ),
                )
            verified_appkit_deliverable = appkit_prepared[1].model_copy(
                update={
                    "artifact_identity": appkit_result.artifact_identity,
                    "observed_after_seq": appkit_result.observed_after_seq,
                }
            )
            disp = await host_gate.gate_host_verify(
                step,
                events,
                preverified=((verified_appkit_deliverable, appkit_result),),
            )
        elif self._host_verify_authoritative() or _governed_verification_required(events):
            disp = await self.gate_host_verify(step, events)
            if disp is Disp.CONTINUE or disp is Disp.HALT:
                return disp
            events = await self._loop._events()
            disp = await self.gate_browser_verify(step, events)
        else:
            disp = await self.gate_browser_verify(step, events)
            events = await self._loop._events()
            await self.gate_host_verify(step, events)  # shadow: advisory, records telemetry
        if disp is Disp.CONTINUE or disp is Disp.HALT:
            return disp
        if contract is not None and strict_appkit:
            events = await self._loop._events()
            handoff = _latest_deliverable_event(events)
            if handoff is None or not _handoff_matches_verification_contract(handoff, contract):
                return await host_gate._governed_contract_refusal(
                    events,
                    failure_key="appkit:foreign_materialized_handoff",
                    guidance=(
                        "the strict verifier did not produce an exact current handoff "
                        "for the admitted AppKit delivery contract."
                    ),
                )
            typed_status = (
                _recorded_appkit_typed_status(events, appkit_prepared[0])
                if appkit_prepared is not None
                else None
            )
            if typed_status is not VerificationClaimStatus.PASS:
                return await host_gate._governed_contract_refusal(
                    events,
                    failure_key="appkit:typed_receipt_unavailable",
                    guidance=(
                        "the strict AppKit verifier passed, but its result could not "
                        "be bound to the current typed target authority."
                    ),
                )
        # [P10] Export render-correctness gate — for a files-deliverable (deck/
        # document) the app/browser gates above fall through, so THIS is the check
        # that a blank/truncated/corrupt export can't report FINISHED.
        events = await self._loop._events()
        return await self.gate_export_render(step, events)

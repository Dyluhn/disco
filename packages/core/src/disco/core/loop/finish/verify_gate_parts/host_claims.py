"""Host-verification claim assembly and governance/handoff predicates.

Owns: folding the current explicit + dictated-content + contract-registered
claim set for host verification, the governed-contract predicates that decide
whether a structured browser/AppKit target is in force, handoff-to-contract
comparison, and the exact-sequence preview-selection fold. No mixin state is
read here — every function takes its inputs explicitly.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Any

from ....events import active_verification_requirements_event
from ....verification import (
    AdmittedVerificationContract,
    PreviewSelectionIdentity,
    VerificationCheckContract,
    VerificationExecutionIdentity,
    requires_structured_browser_runtime,
    validated_image_data_url,
)
from ..common import (
    ActionEvent,
    AgentErrorEvent,
    DeliverableEvent,
    DictatedContentCondition,
    Event,
    HostVerificationClaim,
    ObservationEvent,
    VerificationClaimKind,
    VerifierReferenceImage,
    VerifierStartedEvent,
    current_build_platform_admission,
    default_structured_web_claims,
    dictated_content_conditions_from_events,
)

_STARTUP_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|token|secret)\b"
    r"(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s;]+"
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
_HOST_VERDICT_SCREENSHOT_PATH_MAX_CHARS = 512

# The fallback cause this module authors when a raw verifier cause cannot be
# classified, extracted from the bare `return` inside `bounded_model_verifier_cause`
# so its test can DERIVE it rather than retype it (F62 / F58). Value verbatim.
_UNCLASSIFIED_MODEL_VERIFIER_CAUSE = "model verifier unavailable (unclassified structural failure)"


def bounded_model_verifier_cause(value: object) -> str:
    clean = "".join(char for char in str(value or "") if char in "\n\t" or ord(char) >= 32)
    clean = _STARTUP_DIAGNOSTIC_SECRET_RE.sub("<redacted>", clean)
    clean = clean[:256]
    if _SAFE_MODEL_VERIFIER_CAUSE_RE.fullmatch(clean):
        return clean
    return _UNCLASSIFIED_MODEL_VERIFIER_CAUSE


def bounded_host_verdict_screenshot_path(value: object) -> str | None:
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


def _contract_accepted_claim_kinds(
    check: VerificationCheckContract | None,
    admission: Any,
    accepted: frozenset[VerificationClaimKind],
) -> frozenset[VerificationClaimKind]:
    """The claim kinds the ADMITTED contract accepts, when ``check`` is unscoped."""

    if check is None and admission is not None and admission.verification_contract is not None:
        return frozenset(
            kind
            for contract_check in admission.verification_contract.checks
            for kind in contract_check.accepted_claim_kinds
        )
    return accepted


def _dictated_content_claims(
    conditions: list[DictatedContentCondition],
    accepted: frozenset[VerificationClaimKind],
    contract_accepted: frozenset[VerificationClaimKind],
    *,
    artifact_path: str | None,
) -> list[HostVerificationClaim]:
    """Bind quoted-user-literal conditions to host claims.

    `application.title` is SINGLE-VALUED: an app has exactly one name, so a
    later revision that dictates a new title SUPERSEDES the earlier one rather
    than adding a second. Accumulating both minted two mutually exclusive
    identity claims — the retitle satisfied one and permanently failed the
    other, so the build could never finish however correctly the model
    behaved (seed 406431: AppSpec held the requested 'AppKit Restart
    Recovered 406431' while the superseded 'AppKit Restart 406431' was still
    required). Visible-text claims are NOT slot-scoped and keep accumulating;
    only identity collapses.
    """

    claims: list[HostVerificationClaim] = []
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
        # A literal assigned to a different artifact (REPORT.md, about.html) is
        # not a claim about the page this verifier actually opened. Likewise,
        # source identifiers are not rendered copy. Demanding either in this DOM
        # makes the build unsatisfiable for an agent that obeys the instruction.
        # Epic-4 seed 460009: "Create REPORT.md headed exactly 'Ledger Audit 460009'
        # ... Update index.html to show 'Ledger Audited 460009'" required both
        # strings in the page; the agent oscillated (fix one, break the other) until
        # the no-progress detector gave up, and the runs that passed passed only
        # because they happened to put the report heading on the page too. Exactly
        # the mutually-exclusive-claims failure the application.title slot above
        # already guards (seed 406431), one slot over.
        if (
            VerificationClaimKind.VISIBLE_TEXT in accepted
            and condition.content_surface == "visible_text"
            and _condition_targets_artifact(condition, artifact_path)
        ):
            claims.append(
                HostVerificationClaim(
                    claim_id=f"web.visible_text:{digest}",
                    kind=VerificationClaimKind.VISIBLE_TEXT,
                    expected=condition.literal,
                    source_authority=f"user_event:{condition.source_event_id}",
                )
            )
    return claims


def _condition_targets_artifact(
    condition: DictatedContentCondition,
    artifact_path: str | None,
) -> bool:
    """Whether a visible-text condition belongs to the artifact being observed."""

    target = condition.document_artifact
    if target is None:
        return True
    if artifact_path is None:
        return False
    observed = posixpath.normpath(artifact_path.removeprefix("/workspace/"))
    if observed in {"", "."}:
        observed = "index.html"
    elif not posixpath.splitext(observed)[1]:
        observed = posixpath.join(observed, "index.html")
    expected = posixpath.normpath(target.removeprefix("/workspace/"))
    return observed == expected


def host_verification_claims(
    contract: dict[str, Any],
    events: list[Event],
    *,
    check: VerificationCheckContract | None = None,
    artifact_path: str | None = None,
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
    contract_accepted = _contract_accepted_claim_kinds(check, admission, accepted)
    conditions = dictated_content_conditions_from_events(events)
    claims.extend(
        _dictated_content_claims(
            conditions,
            accepted,
            contract_accepted,
            artifact_path=artifact_path,
        )
    )
    requested_claims, _ = user_verification_material(events)
    # Free-form user references and semantic wishes remain valuable evidence, but
    # they are not automatically target-owned acceptance authority. The ordinary
    # structured host has no generic way to prove visual similarity, arbitrary
    # interactions, contract semantics, or target-specific behavior. A target that
    # truly requires one must put the exact claim in ``check.claims``; that copy is
    # already present above and stays required.
    advisory_requested_kinds = {
        VerificationClaimKind.VISUAL_SEMANTIC,
        VerificationClaimKind.CONTRACT_SEMANTIC,
        VerificationClaimKind.INTERACTION,
        VerificationClaimKind.TARGET_SPECIFIC,
    }
    target_owned_ids = {claim.claim_id for claim in claims}
    claims.extend(
        claim.model_copy(update={"required": False})
        if claim.kind in advisory_requested_kinds
        else claim
        for claim in requested_claims
        if claim.kind in accepted and claim.claim_id not in target_owned_ids
    )
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


def governed_structured_browser_target(events: list[Event]) -> bool:
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


def governed_verification_contract(
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


def governed_verification_required(events: list[Event]) -> bool:
    contract = governed_verification_contract(events)
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


def strict_appkit_contract(
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


def strict_appkit_compatibility_error(
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


def handoff_clauses(
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


def handoff_matches_verification_contract(
    handoff: DeliverableEvent,
    contract: AdmittedVerificationContract,
) -> bool:
    return all(ok for _, ok in handoff_clauses(handoff, contract))


def handoff_refusal_detail(
    handoff: DeliverableEvent | None,
    contract: AdmittedVerificationContract | None,
) -> str:
    """Say WHY the current handoff cannot serve this target, naming the fact."""

    if handoff is None:
        return "there is no handoff for the current target at all"
    if contract is None:
        return "the latest handoff is not current for this target"
    for name, ok in handoff_clauses(handoff, contract):
        if not ok:
            return f"the latest handoff does not bind this target's {name}"
    return "the latest handoff is not current for this target"


def latest_appkit_verification_outcome(
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


def latest_matching_appkit_start(
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


def appkit_runtime_identity(raw: object) -> VerificationExecutionIdentity | None:
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


def user_verification_material(
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


def _fold_appkit_preview_event(
    event: ObservationEvent,
    actions: dict[str, ActionEvent],
    active: dict[str, tuple[int, PreviewSelectionIdentity]],
) -> None:
    result = event.tool_result
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
        return
    identity = PreviewSelectionIdentity.from_structured(
        action_id=action.id,
        action_seq=action.seq,
        observation_id=event.id,
        observation_seq=event.seq,
        structured=runtime,
    )
    if identity is None:
        active.clear()
        return
    active[identity.session_name] = (event.seq, identity)


def _fold_preview_start_event(
    event: ObservationEvent,
    actions: dict[str, ActionEvent],
    active: dict[str, tuple[int, PreviewSelectionIdentity]],
) -> None:
    result = event.tool_result
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
        return
    identity = PreviewSelectionIdentity.from_structured(
        action_id=action.id,
        action_seq=action.seq,
        observation_id=event.id,
        observation_seq=event.seq,
        structured=structured,
    )
    if identity is None:
        active.clear()
        return
    active[identity.session_name] = (event.seq, identity)


def _fold_preview_stop_event(
    event: ObservationEvent,
    active: dict[str, tuple[int, PreviewSelectionIdentity]],
) -> None:
    result = event.tool_result
    stopped = result.structured.get("stopped") if isinstance(result.structured, dict) else None
    if not isinstance(stopped, list) or not all(isinstance(name, str) and name for name in stopped):
        active.clear()
        return
    for name in stopped:
        active.pop(name, None)


def preview_selection_at(
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
            _fold_appkit_preview_event(event, actions, active)
            continue
        if result.tool_name == "preview_start" and result.success is True:
            _fold_preview_start_event(event, actions, active)
            continue
        if result.tool_name == "preview_stop" and result.success is True:
            _fold_preview_stop_event(event, active)
    return max(active.values(), key=lambda item: item[0])[1] if active else None

"""Pure event-sourced progress and recovery-lease reduction.

This module deliberately has no tool-name, framework, target, clock, database,
or model dependency. It answers only what host-authored events prove:

* which exact resource revisions/ranges are known;
* which resource revisions actually changed;
* which revision-bound verification claims exist;
* which authorized control state changed; and
* whether a one-use recovery capability lease is active, consumed, cleared, or
  exhausted.

K4 initially consumes this in shadow mode. Legacy gates remain authoritative
until mutation/verification/control producers emit complete typed receipts and
preserved traces demonstrate parity. Unprofiled historical outcomes are marked
unattributed and never converted into either progress or blame.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..effects import (
    ActionProfile,
    ControlReceipt,
    EffectCapability,
    EffectReceipt,
    MutationReceipt,
    ObservationReceipt,
    OpaqueEffectReceipt,
    RecoveryLeaseTransitionKind,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
    VerificationReceipt,
    validate_effect_receipts,
)
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
)
from .resource_context import merge_spans


class ProgressKind(str, Enum):
    EPISTEMIC = "epistemic"
    STATE = "state"
    VALIDATION = "validation"
    CONTROL = "control"


class ResourceState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resource: ResourceKey
    exists: bool
    revision: ResourceRevision | None = None


class ObservationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Capability is audit provenance, not knowledge identity. Delivering the
    # same exact bytes through a differently classified tool cannot manufacture
    # new epistemic progress.
    capabilities: frozenset[EffectCapability]
    revision: ResourceRevision
    coverage: ResourceCoverage


class MutationEvidence(BaseModel):
    """One host-observed, invocation-bound resource transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_event_seq: int = Field(ge=1)
    result_event_seq: int = Field(ge=1)
    resource: ResourceKey
    before: ResourceRevision | None = None
    after: ResourceRevision | None = None


class VerificationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verifier_id: str
    subject: ResourceRevision
    requirement_fingerprint: str
    passed: bool
    failure_fingerprint: str | None = None


class ControlEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: EffectCapability
    transition_kind: str
    prior_state: str
    new_state: str


class ControlState(BaseModel):
    """Latest accepted state for one typed control domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: EffectCapability
    transition_kind: str
    state: str
    last_event_seq: int = Field(ge=1)


class RecoveryCandidate(BaseModel):
    """Shadow-only recommendation derived from a comparable known-zero suffix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    blocked_capability: EffectCapability
    zero_progress_streak: int = Field(ge=1)
    threshold: int = Field(ge=1)
    trigger_event_seq: int = Field(ge=1)


class RecoveryLeasePhase(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    CLEARED_BY_PROGRESS = "cleared_by_progress"
    CLEARED_BY_USER = "cleared_by_user"
    EXHAUSTED = "exhausted"
    INDETERMINATE = "indeterminate"


class RecoveryLeaseState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: str
    issued_after_seq: int
    issue_event_seq: int
    baseline_evidence_fingerprint: str
    blocked_capabilities: tuple[EffectCapability, ...]
    granted_capabilities: tuple[EffectCapability, ...]
    phase: RecoveryLeasePhase
    call_id: str | None = None
    action_id: str | None = None
    consume_event_seq: int | None = None
    resolved_event_seq: int | None = None


class ProgressState(BaseModel):
    """Canonical replay result for one append-only conversation log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latest_event_seq: int = 0
    user_epoch_seq: int | None = None
    last_progress_seq: int | None = None
    progress_event_seqs: tuple[int, ...] = ()
    progress_kinds: frozenset[ProgressKind] = frozenset()
    current_resources: tuple[ResourceState, ...] = ()
    observations: tuple[ObservationEvidence, ...] = ()
    mutations: tuple[MutationEvidence, ...] = ()
    verifications: tuple[VerificationEvidence, ...] = ()
    controls: tuple[ControlEvidence, ...] = ()
    current_controls: tuple[ControlState, ...] = ()
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    executed_invocations: int = 0
    zero_progress_invocations: int = 0
    unattributed_invocations: int = 0
    invalid_event_pairs: int = 0
    invalid_receipt_groups: int = 0
    stale_receipts: int = 0
    recovery_lease: RecoveryLeaseState | None = None
    recovery_candidate: RecoveryCandidate | None = None
    recovery_comparable: bool = True
    invalid_lease_transitions: int = 0
    invalid_log_order: bool = False


@dataclass(frozen=True)
class _Invocation:
    action: ActionEvent
    action_event_seq: int
    event_seq: int
    call_id: str
    action_profile: ActionProfile | None
    receipts: tuple[EffectReceipt, ...]


_RECOVERY_ZERO_STREAK_THRESHOLD = 4


def _seq(event: Event, index: int) -> int:
    return event.seq if event.seq is not None else index + 1


def _resource_sort_key(resource: ResourceKey) -> tuple[str, str]:
    return (resource.namespace, resource.identifier)


def _revision_sort_key(revision: ResourceRevision) -> tuple[str, str, str]:
    return (*_resource_sort_key(revision.resource), revision.digest)


def _authenticated_invocations(
    events: tuple[Event, ...],
) -> tuple[dict[int, _Invocation], frozenset[int]]:
    """Bind unique environment outcomes to their exact preceding agent action."""

    actions_by_id: dict[str, list[tuple[int, ActionEvent]]] = defaultdict(list)
    action_ids_by_call: dict[str, set[str]] = defaultdict(set)
    terminal_count_by_action: Counter[str] = Counter()
    terminal_count_by_call: Counter[str] = Counter()

    for index, event in enumerate(events):
        if isinstance(event, ActionEvent) and event.source is EventSource.AGENT:
            actions_by_id[event.id].append((index, event))
            action_ids_by_call[event.tool_call.call_id].add(event.id)
        elif isinstance(event, ObservationEvent) and event.source is EventSource.ENVIRONMENT:
            terminal_count_by_action[event.action_id] += 1
            terminal_count_by_call[event.tool_result.call_id] += 1
        elif (
            isinstance(event, AgentErrorEvent)
            and event.source is EventSource.ENVIRONMENT
            and event.action_id
            and event.tool_call_id
        ):
            terminal_count_by_action[event.action_id] += 1
            terminal_count_by_call[event.tool_call_id] += 1

    pairs: dict[int, _Invocation] = {}
    invalid_indices: set[int] = set()
    for index, event in enumerate(events):
        action_id: str | None = None
        call_id: str | None = None
        profile: ActionProfile | None = None
        receipts: tuple[EffectReceipt, ...] = ()
        tool_name: str | None = None
        if isinstance(event, ObservationEvent) and event.source is EventSource.ENVIRONMENT:
            action_id = event.action_id
            call_id = event.tool_result.call_id
            profile = event.tool_result.action_profile
            receipts = event.tool_result.effect_receipts
            tool_name = event.tool_result.tool_name
        elif (
            isinstance(event, AgentErrorEvent)
            and event.source is EventSource.ENVIRONMENT
            and event.action_id
            and event.tool_call_id
        ):
            action_id = event.action_id
            call_id = event.tool_call_id
            profile = event.action_profile
            receipts = event.effect_receipts
        else:
            continue

        candidates = actions_by_id.get(action_id, [])
        action = candidates[0][1] if len(candidates) == 1 else None
        action_index = candidates[0][0] if len(candidates) == 1 else None
        valid = (
            action is not None
            and action_index is not None
            and action_index < index
            and action.tool_call.call_id == call_id
            and (tool_name is None or action.tool_call.tool_name == tool_name)
            and len(action_ids_by_call.get(call_id, ())) == 1
            and terminal_count_by_action[action_id] == 1
            and terminal_count_by_call[call_id] == 1
            and (action.seq is None or event.seq is None or action.seq < event.seq)
        )
        if not valid or action is None or action_index is None or call_id is None:
            invalid_indices.add(index)
            continue
        pairs[index] = _Invocation(
            action=action,
            action_event_seq=_seq(action, action_index),
            event_seq=_seq(event, index),
            call_id=call_id,
            action_profile=profile,
            receipts=receipts,
        )
    return pairs, frozenset(invalid_indices)


def recovery_lease_id(
    *,
    issued_after_seq: int,
    baseline_evidence_fingerprint: str,
    blocked_capabilities: Iterable[EffectCapability],
    granted_capabilities: Iterable[EffectCapability],
) -> str:
    """Derive the canonical id for one evidence-bound recovery lease."""

    payload = {
        "issued_after_seq": issued_after_seq,
        "baseline": baseline_evidence_fingerprint,
        "blocked": sorted(capability.value for capability in blocked_capabilities),
        "granted": sorted(capability.value for capability in granted_capabilities),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"lease_{digest[:32]}"


def _evidence_fingerprint(
    resources: dict[ResourceKey, ResourceState],
    observations: dict[tuple[Any, ...], ObservationEvidence],
    mutations: list[MutationEvidence],
    verifications: dict[tuple[Any, ...], VerificationEvidence],
    controls: dict[tuple[str, str, str, str], ControlEvidence],
    current_controls: dict[tuple[str, str], ControlState],
) -> str:
    payload = {
        "resources": [
            state.model_dump(mode="json")
            for state in sorted(
                resources.values(), key=lambda item: _resource_sort_key(item.resource)
            )
        ],
        "observations": [
            {
                "revision": evidence.revision.model_dump(mode="json"),
                "coverage": evidence.coverage.model_dump(mode="json"),
            }
            for evidence in sorted(
                observations.values(),
                key=lambda item: (
                    *_revision_sort_key(item.revision),
                    item.coverage.unit.value,
                    -1 if item.coverage.total is None else item.coverage.total,
                ),
            )
        ],
        "mutations": [evidence.model_dump(mode="json") for evidence in mutations],
        "verifications": [
            evidence.model_dump(mode="json")
            for evidence in sorted(
                verifications.values(),
                key=lambda item: (
                    item.verifier_id,
                    *_revision_sort_key(item.subject),
                    item.requirement_fingerprint,
                ),
            )
        ],
        "controls": [
            evidence.model_dump(mode="json")
            for evidence in sorted(
                controls.values(),
                key=lambda item: (
                    item.capability.value,
                    item.transition_kind,
                    item.prior_state,
                    item.new_state,
                ),
            )
        ],
        "current_controls": [
            state.model_dump(mode="json")
            for state in sorted(
                current_controls.values(),
                key=lambda item: (item.capability.value, item.transition_kind),
            )
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _observation_key(receipt: ObservationReceipt) -> tuple[Any, ...]:
    revision = receipt.revision
    return (
        revision.resource.namespace,
        revision.resource.identifier,
        revision.digest,
        receipt.coverage.unit.value,
    )


def _terminal_claims_consumed_lease(event: Event, lease: RecoveryLeaseState | None) -> bool:
    if lease is None or lease.phase is not RecoveryLeasePhase.CONSUMED:
        return False
    if isinstance(event, ObservationEvent):
        return event.action_id == lease.action_id or event.tool_result.call_id == lease.call_id
    if isinstance(event, AgentErrorEvent):
        return event.action_id == lease.action_id or event.tool_call_id == lease.call_id
    return False


def reduce_progress(events: Iterable[Event]) -> ProgressState:
    """Replay ``events`` into exact evidence, progress, and recovery state."""

    event_list = tuple(events)
    persisted_seqs = tuple(event.seq for event in event_list)
    if any(seq is not None for seq in persisted_seqs):
        present = [seq for seq in persisted_seqs if seq is not None]
        ordered = len(present) == len(persisted_seqs) and all(
            present[index - 1] < present[index] for index in range(1, len(present))
        )
        if not ordered:
            fingerprint = _evidence_fingerprint({}, {}, [], {}, {}, {})
            return ProgressState(
                latest_event_seq=max(present, default=0),
                evidence_fingerprint=fingerprint,
                recovery_comparable=False,
                invalid_log_order=True,
            )

    invocations, invalid_pair_indices = _authenticated_invocations(event_list)
    actions_by_call: dict[str, list[tuple[int, int, ActionEvent]]] = defaultdict(list)
    for action_index, candidate in enumerate(event_list):
        if isinstance(candidate, ActionEvent) and candidate.source is EventSource.AGENT:
            actions_by_call[candidate.tool_call.call_id].append(
                (_seq(candidate, action_index), action_index, candidate)
            )
    resources: dict[ResourceKey, ResourceState] = {}
    resource_action_seqs: dict[ResourceKey, int] = {}
    observations: dict[tuple[Any, ...], ObservationEvidence] = {}
    mutations: list[MutationEvidence] = []
    verifications: dict[tuple[Any, ...], VerificationEvidence] = {}
    controls: dict[tuple[str, str, str, str], ControlEvidence] = {}
    current_controls: dict[tuple[str, str], ControlState] = {}
    progress_seqs: list[int] = []
    progress_kinds: set[ProgressKind] = set()
    last_progress_seq: int | None = None
    user_epoch_seq: int | None = None
    executed = 0
    zero_progress = 0
    unattributed = 0
    invalid_receipts = 0
    stale_receipts = 0
    lease: RecoveryLeaseState | None = None
    seen_lease_ids: set[str] = set()
    invalid_lease_transitions = 0
    trailing_zero_capabilities: list[EffectCapability] = []
    recovery_comparable = True
    last_zero_event_seq: int | None = None
    latest_seq = 0

    for index, event in enumerate(event_list):
        seq = _seq(event, index)
        prior_latest_seq = latest_seq
        latest_seq = max(latest_seq, seq)

        if isinstance(event, MessageEvent) and event.source is EventSource.USER:
            user_epoch_seq = seq
            last_progress_seq = seq
            progress_seqs.append(seq)
            progress_kinds.add(ProgressKind.CONTROL)
            zero_progress = 0
            trailing_zero_capabilities.clear()
            recovery_comparable = True
            last_zero_event_seq = None
            if lease is not None and lease.phase in {
                RecoveryLeasePhase.ACTIVE,
                RecoveryLeasePhase.CONSUMED,
                RecoveryLeasePhase.EXHAUSTED,
                RecoveryLeasePhase.INDETERMINATE,
            }:
                lease = lease.model_copy(
                    update={
                        "phase": RecoveryLeasePhase.CLEARED_BY_USER,
                        "resolved_event_seq": seq,
                    }
                )
            continue

        if isinstance(event, StatusEvent) and event.recovery_lease_transition is not None:
            transition = event.recovery_lease_transition
            if event.source is not EventSource.SYSTEM:
                invalid_lease_transitions += 1
                continue
            current_fingerprint = _evidence_fingerprint(
                resources,
                observations,
                mutations,
                verifications,
                controls,
                current_controls,
            )
            canonical_id = recovery_lease_id(
                issued_after_seq=transition.issued_after_seq,
                baseline_evidence_fingerprint=transition.baseline_evidence_fingerprint,
                blocked_capabilities=transition.blocked_capabilities,
                granted_capabilities=transition.granted_capabilities,
            )
            identity_valid = (
                transition.lease_id == canonical_id and transition.issued_after_seq < seq
            )
            if transition.transition is RecoveryLeaseTransitionKind.ISSUE:
                issue_valid = (
                    identity_valid
                    and transition.baseline_evidence_fingerprint == current_fingerprint
                    and transition.issued_after_seq == prior_latest_seq
                    and transition.lease_id not in seen_lease_ids
                    and (
                        lease is None
                        or lease.phase
                        in {
                            RecoveryLeasePhase.CLEARED_BY_PROGRESS,
                            RecoveryLeasePhase.CLEARED_BY_USER,
                        }
                    )
                )
                if not issue_valid:
                    invalid_lease_transitions += 1
                    continue
                seen_lease_ids.add(transition.lease_id)
                lease = RecoveryLeaseState(
                    lease_id=transition.lease_id,
                    issued_after_seq=transition.issued_after_seq,
                    issue_event_seq=seq,
                    baseline_evidence_fingerprint=transition.baseline_evidence_fingerprint,
                    blocked_capabilities=transition.blocked_capabilities,
                    granted_capabilities=transition.granted_capabilities,
                    phase=RecoveryLeasePhase.ACTIVE,
                )
            else:
                call_actions = actions_by_call.get(transition.call_id or "", ())
                call_action = call_actions[0] if len(call_actions) == 1 else None
                consume_valid = (
                    identity_valid
                    and lease is not None
                    and lease.phase is RecoveryLeasePhase.ACTIVE
                    and transition.lease_id == lease.lease_id
                    and transition.issued_after_seq == lease.issued_after_seq
                    and transition.blocked_capabilities == lease.blocked_capabilities
                    and transition.granted_capabilities == lease.granted_capabilities
                    and transition.call_id is not None
                    and call_action is not None
                    and lease.issue_event_seq < call_action[0] < seq
                    and call_action[1] < index
                )
                if not consume_valid:
                    invalid_lease_transitions += 1
                    continue
                if lease is None or call_action is None:  # pragma: no cover - narrowed above
                    raise AssertionError("validated lease consumption has no active lease")
                lease = lease.model_copy(
                    update={
                        "phase": RecoveryLeasePhase.CONSUMED,
                        "call_id": transition.call_id,
                        "action_id": call_action[2].id,
                        "consume_event_seq": seq,
                    }
                )
            continue

        invocation = invocations.get(index)
        if invocation is None:
            if index in invalid_pair_indices:
                trailing_zero_capabilities.clear()
                recovery_comparable = False
                last_zero_event_seq = None
                if _terminal_claims_consumed_lease(event, lease) and lease is not None:
                    lease = lease.model_copy(
                        update={
                            "phase": RecoveryLeasePhase.INDETERMINATE,
                            "resolved_event_seq": seq,
                        }
                    )
            continue
        profile = invocation.action_profile
        if profile is None:
            unattributed += 1
            zero_progress = 0
            trailing_zero_capabilities.clear()
            recovery_comparable = False
            last_zero_event_seq = None
            if (
                lease is not None
                and lease.phase is RecoveryLeasePhase.CONSUMED
                and lease.call_id == invocation.call_id
            ):
                lease = lease.model_copy(
                    update={
                        "phase": RecoveryLeasePhase.INDETERMINATE,
                        "resolved_event_seq": seq,
                    }
                )
            continue

        executed += 1
        receipts = invocation.receipts
        try:
            receipts = validate_effect_receipts(profile, receipts)
        except ValueError:
            invalid_receipts += 1
            # Host/classifier telemetry defects are not evidence that the model
            # made no progress. Preserve the anomaly as unattributed instead of
            # charging the zero-progress counter.
            unattributed += 1
            zero_progress = 0
            trailing_zero_capabilities.clear()
            recovery_comparable = False
            last_zero_event_seq = None
            if (
                lease is not None
                and lease.phase is RecoveryLeasePhase.CONSUMED
                and lease.call_id == invocation.call_id
            ):
                lease = lease.model_copy(
                    update={
                        "phase": RecoveryLeasePhase.INDETERMINATE,
                        "resolved_event_seq": seq,
                    }
                )
            continue

        delta_kinds: set[ProgressKind] = set()
        receipt_capabilities = {receipt.capability for receipt in receipts}
        for receipt in receipts:
            if isinstance(receipt, ObservationReceipt):
                resource = receipt.revision.resource
                prior_state = resources.get(resource)
                prior_action_seq = resource_action_seqs.get(resource, -1)
                if invocation.action_event_seq < prior_action_seq:
                    # A delayed result is still useful historical read evidence,
                    # but cannot roll the current resource pointer backwards.
                    stale_receipts += 1
                elif (
                    prior_state is None
                    or not prior_state.exists
                    or prior_state.revision != receipt.revision
                ):
                    resources[resource] = ResourceState(
                        resource=resource,
                        exists=True,
                        revision=receipt.revision,
                    )
                    resource_action_seqs[resource] = invocation.action_event_seq
                    delta_kinds.add(ProgressKind.EPISTEMIC)
                key = _observation_key(receipt)
                prior = observations.get(key)
                if prior is not None and prior.coverage.total != receipt.coverage.total:
                    # One byte revision cannot truthfully have two totals in the
                    # same unit. Retain the established evidence and surface the
                    # anomaly; never turn the contradiction into progress.
                    stale_receipts += 1
                    continue
                merged = merge_spans(
                    (
                        *(prior.coverage.spans if prior is not None else ()),
                        *receipt.coverage.spans,
                    )
                )
                prior_capabilities = prior.capabilities if prior is not None else frozenset()
                capabilities = prior_capabilities | {receipt.capability}
                new_coverage = prior is None or merged != prior.coverage.spans
                if new_coverage or capabilities != prior_capabilities:
                    observations[key] = ObservationEvidence(
                        capabilities=capabilities,
                        revision=receipt.revision,
                        coverage=ResourceCoverage(
                            unit=receipt.coverage.unit,
                            spans=merged,
                            total=receipt.coverage.total,
                        ),
                    )
                if new_coverage:
                    delta_kinds.add(ProgressKind.EPISTEMIC)
            elif isinstance(receipt, MutationReceipt):
                resource = receipt.resource
                current = resources.get(resource)
                mutations.append(
                    MutationEvidence(
                        action_event_seq=invocation.action_event_seq,
                        result_event_seq=seq,
                        resource=resource,
                        before=receipt.before,
                        after=receipt.after,
                    )
                )
                delta_kinds.add(ProgressKind.STATE)
                if receipt.before is not None and current is not None:
                    if not current.exists or current.revision != receipt.before:
                        stale_receipts += 1
                        # The causal discontinuity remains visible, but the
                        # host-observed after revision is still the newest exact
                        # state at this event. Do not freeze truth at a stale
                        # prior revision merely because an intermediate event
                        # was absent from the log.
                after_state = ResourceState(
                    resource=resource,
                    exists=receipt.after is not None,
                    revision=receipt.after,
                )
                prior_action_seq = resource_action_seqs.get(resource, -1)
                if invocation.action_event_seq < prior_action_seq:
                    stale_receipts += 1
                elif current != after_state:
                    resources[resource] = after_state
                    resource_action_seqs[resource] = invocation.action_event_seq
            elif isinstance(receipt, VerificationReceipt):
                current = resources.get(receipt.subject.resource)
                if current is not None and (
                    not current.exists or current.revision != receipt.subject
                ):
                    stale_receipts += 1
                    continue
                key = (
                    receipt.verifier_id,
                    receipt.subject.resource.namespace,
                    receipt.subject.resource.identifier,
                    receipt.subject.digest,
                    receipt.requirement_fingerprint,
                    receipt.passed,
                    receipt.failure_fingerprint,
                )
                prior = verifications.get(key)
                claim = VerificationEvidence(
                    verifier_id=receipt.verifier_id,
                    subject=receipt.subject,
                    requirement_fingerprint=receipt.requirement_fingerprint,
                    passed=receipt.passed,
                    failure_fingerprint=receipt.failure_fingerprint,
                )
                if prior is None:
                    verifications[key] = claim
                    delta_kinds.add(ProgressKind.VALIDATION)
            elif isinstance(receipt, ControlReceipt):
                if receipt.prior_state == receipt.new_state:
                    continue
                history_key = (
                    receipt.capability.value,
                    receipt.transition_kind,
                    receipt.prior_state,
                    receipt.new_state,
                )
                if history_key not in controls:
                    controls[history_key] = ControlEvidence(
                        capability=receipt.capability,
                        transition_kind=receipt.transition_kind,
                        prior_state=receipt.prior_state,
                        new_state=receipt.new_state,
                    )
                state_key = (receipt.capability.value, receipt.transition_kind)
                current_control = current_controls.get(state_key)
                if current_control is not None and current_control.state != receipt.prior_state:
                    stale_receipts += 1
                if current_control is None or current_control.state != receipt.new_state:
                    current_controls[state_key] = ControlState(
                        capability=receipt.capability,
                        transition_kind=receipt.transition_kind,
                        state=receipt.new_state,
                        last_event_seq=seq,
                    )
                    delta_kinds.add(ProgressKind.CONTROL)
            elif isinstance(receipt, OpaqueEffectReceipt):
                continue

        if delta_kinds:
            last_progress_seq = seq
            progress_seqs.append(seq)
            progress_kinds.update(delta_kinds)
            zero_progress = 0
            trailing_zero_capabilities.clear()
            recovery_comparable = True
            last_zero_event_seq = None
            if (
                lease is not None
                and invocation.action_event_seq > lease.issue_event_seq
                and lease.phase
                in {
                    RecoveryLeasePhase.ACTIVE,
                    RecoveryLeasePhase.CONSUMED,
                    RecoveryLeasePhase.EXHAUSTED,
                    RecoveryLeasePhase.INDETERMINATE,
                }
            ):
                if (
                    lease.phase is RecoveryLeasePhase.CONSUMED
                    and lease.call_id == invocation.call_id
                ):
                    capabilities = set(profile.capabilities)
                    granted = set(lease.granted_capabilities)
                    blocked = set(lease.blocked_capabilities)
                    if not capabilities & granted or capabilities & blocked:
                        invalid_lease_transitions += 1
                lease = lease.model_copy(
                    update={
                        "phase": RecoveryLeasePhase.CLEARED_BY_PROGRESS,
                        "resolved_event_seq": seq,
                    }
                )
        else:
            zero_progress += 1
            zero_capability: EffectCapability | None = None
            if len(receipt_capabilities) == 1:
                zero_capability = next(iter(receipt_capabilities))
            elif not receipt_capabilities and len(profile.capabilities) == 1:
                zero_capability = next(iter(profile.capabilities))
            if zero_capability is None:
                trailing_zero_capabilities.clear()
                recovery_comparable = False
                last_zero_event_seq = None
            else:
                if (
                    not trailing_zero_capabilities
                    or trailing_zero_capabilities[-1] == zero_capability
                ):
                    trailing_zero_capabilities.append(zero_capability)
                else:
                    trailing_zero_capabilities = [zero_capability]
                recovery_comparable = True
                last_zero_event_seq = seq
            if (
                lease is not None
                and lease.phase is RecoveryLeasePhase.CONSUMED
                and lease.call_id == invocation.call_id
            ):
                capabilities = set(profile.capabilities)
                exercised = capabilities & set(lease.granted_capabilities)
                blocked = capabilities & set(lease.blocked_capabilities)
                if not exercised or blocked:
                    invalid_lease_transitions += 1
                    lease = lease.model_copy(
                        update={
                            "phase": RecoveryLeasePhase.INDETERMINATE,
                            "resolved_event_seq": seq,
                        }
                    )
                else:
                    lease = lease.model_copy(
                        update={
                            "phase": RecoveryLeasePhase.EXHAUSTED,
                            "resolved_event_seq": seq,
                        }
                    )

    fingerprint = _evidence_fingerprint(
        resources,
        observations,
        mutations,
        verifications,
        controls,
        current_controls,
    )
    recovery_candidate = None
    if (
        recovery_comparable
        and len(trailing_zero_capabilities) >= _RECOVERY_ZERO_STREAK_THRESHOLD
        and last_zero_event_seq is not None
    ):
        recovery_candidate = RecoveryCandidate(
            blocked_capability=trailing_zero_capabilities[-1],
            zero_progress_streak=len(trailing_zero_capabilities),
            threshold=_RECOVERY_ZERO_STREAK_THRESHOLD,
            trigger_event_seq=last_zero_event_seq,
        )
    return ProgressState(
        latest_event_seq=latest_seq,
        user_epoch_seq=user_epoch_seq,
        last_progress_seq=last_progress_seq,
        progress_event_seqs=tuple(progress_seqs),
        progress_kinds=frozenset(progress_kinds),
        current_resources=tuple(
            sorted(resources.values(), key=lambda item: _resource_sort_key(item.resource))
        ),
        observations=tuple(
            sorted(
                observations.values(),
                key=lambda item: (
                    *_revision_sort_key(item.revision),
                    item.coverage.unit.value,
                    -1 if item.coverage.total is None else item.coverage.total,
                ),
            )
        ),
        mutations=tuple(mutations),
        verifications=tuple(
            sorted(
                verifications.values(),
                key=lambda item: (
                    item.verifier_id,
                    *_revision_sort_key(item.subject),
                    item.requirement_fingerprint,
                    item.passed,
                    item.failure_fingerprint or "",
                ),
            )
        ),
        controls=tuple(
            sorted(
                controls.values(),
                key=lambda item: (
                    item.capability.value,
                    item.transition_kind,
                    item.prior_state,
                    item.new_state,
                ),
            )
        ),
        current_controls=tuple(
            sorted(
                current_controls.values(),
                key=lambda item: (item.capability.value, item.transition_kind),
            )
        ),
        evidence_fingerprint=fingerprint,
        executed_invocations=executed,
        zero_progress_invocations=zero_progress,
        unattributed_invocations=unattributed,
        invalid_event_pairs=len(invalid_pair_indices),
        invalid_receipt_groups=invalid_receipts,
        stale_receipts=stale_receipts,
        recovery_lease=lease,
        recovery_candidate=recovery_candidate,
        recovery_comparable=recovery_comparable,
        invalid_lease_transitions=invalid_lease_transitions,
    )


__all__ = [
    "ControlEvidence",
    "ControlState",
    "MutationEvidence",
    "ObservationEvidence",
    "ProgressKind",
    "ProgressState",
    "RecoveryCandidate",
    "RecoveryLeasePhase",
    "RecoveryLeaseState",
    "ResourceState",
    "VerificationEvidence",
    "recovery_lease_id",
    "reduce_progress",
]

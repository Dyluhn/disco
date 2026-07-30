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
from collections import Counter, defaultdict  # noqa: F401 — compatibility facade bindings
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..effects import (
    ActionProfile,
    ControlReceipt,  # noqa: F401 — compatibility facade binding
    EffectCapability,
    EffectReceipt,
    MutationReceipt,  # noqa: F401 — compatibility facade binding
    ObservationReceipt,
    OpaqueEffectReceipt,  # noqa: F401 — compatibility facade binding
    RecoveryLeaseTransitionKind,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
    VerificationReceipt,  # noqa: F401 — compatibility facade binding
    validate_effect_receipts,  # noqa: F401 — compatibility facade binding
)
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,  # noqa: F401 — compatibility facade binding
    ObservationEvent,
    StatusEvent,
)
from .resource_context import merge_spans  # noqa: F401 — compatibility facade binding


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


_RECOVERY_ZERO_STREAK_THRESHOLD = 4


def _seq(event: Event, index: int) -> int:
    return event.seq if event.seq is not None else index + 1


def _resource_sort_key(resource: ResourceKey) -> tuple[str, str]:
    return (resource.namespace, resource.identifier)


def _revision_sort_key(revision: ResourceRevision) -> tuple[str, str, str]:
    return (*_resource_sort_key(revision.resource), revision.digest)


def reduce_progress(events: Iterable[Event]) -> ProgressState:
    """Replay ``events`` into exact evidence, progress, and recovery state.

    Thin orchestrator over :mod:`progress_reducer`.  The per-event fold state
    and receipt-application policy live there; this function validates log
    order and delegates the fold.  Kept as the public compatibility surface.
    """
    from .progress_reducer import reduce_progress_events

    return reduce_progress_events(events)


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


_LEASE_CLEARABLE_PHASES = frozenset(
    {
        RecoveryLeasePhase.ACTIVE,
        RecoveryLeasePhase.CONSUMED,
        RecoveryLeasePhase.EXHAUSTED,
        RecoveryLeasePhase.INDETERMINATE,
    }
)
_LEASE_ISSUE_ALLOWED_PHASES = frozenset(
    {RecoveryLeasePhase.CLEARED_BY_PROGRESS, RecoveryLeasePhase.CLEARED_BY_USER}
)


def _clear_lease(
    lease: RecoveryLeaseState, phase: RecoveryLeasePhase, seq: int
) -> RecoveryLeaseState:
    return lease.model_copy(update={"phase": phase, "resolved_event_seq": seq})


@dataclass(frozen=True)
class _Invocation:
    action: ActionEvent
    action_event_seq: int
    event_seq: int
    call_id: str
    action_profile: ActionProfile | None
    receipts: tuple[EffectReceipt, ...]


def _authenticated_invocations(
    events: tuple[Event, ...],
) -> tuple[dict[int, _Invocation], frozenset[int]]:
    """Compatibility facade for the reducer-owned invocation binder."""
    from .progress_reducer import _authenticated_invocations as bind_invocations

    return bind_invocations(events)


class _LeaseTracker:
    """Owns recovery-lease transition validation and state."""

    def __init__(
        self,
        actions_by_call: dict[str, list[tuple[int, int, ActionEvent]]],
    ) -> None:
        self._actions_by_call = actions_by_call
        self.lease: RecoveryLeaseState | None = None
        self._seen_lease_ids: set[str] = set()
        self.invalid_transitions = 0

    def clear_by_user(self, seq: int) -> None:
        if self.lease is not None and self.lease.phase in _LEASE_CLEARABLE_PHASES:
            self.lease = _clear_lease(self.lease, RecoveryLeasePhase.CLEARED_BY_USER, seq)

    def handle_transition(
        self,
        event: StatusEvent,
        seq: int,
        prior_latest_seq: int,
        index: int,
        evidence_fingerprint: str,
    ) -> None:
        transition = event.recovery_lease_transition
        assert transition is not None
        if event.source is not EventSource.SYSTEM:
            self.invalid_transitions += 1
            return
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
            self._handle_issue(
                transition, identity_valid, evidence_fingerprint, prior_latest_seq, seq
            )
        else:
            self._handle_consume(transition, identity_valid, seq, index)

    def _handle_issue(
        self, transition: Any, identity_valid: bool, current_fingerprint: str,
        prior_latest_seq: int, seq: int,
    ) -> None:
        issue_valid = (
            identity_valid
            and transition.baseline_evidence_fingerprint == current_fingerprint
            and transition.issued_after_seq == prior_latest_seq
            and transition.lease_id not in self._seen_lease_ids
            and (self.lease is None or self.lease.phase in _LEASE_ISSUE_ALLOWED_PHASES)
        )
        if not issue_valid:
            self.invalid_transitions += 1
            return
        self._seen_lease_ids.add(transition.lease_id)
        self.lease = RecoveryLeaseState(
            lease_id=transition.lease_id,
            issued_after_seq=transition.issued_after_seq,
            issue_event_seq=seq,
            baseline_evidence_fingerprint=transition.baseline_evidence_fingerprint,
            blocked_capabilities=transition.blocked_capabilities,
            granted_capabilities=transition.granted_capabilities,
            phase=RecoveryLeasePhase.ACTIVE,
        )

    def _handle_consume(
        self, transition: Any, identity_valid: bool, seq: int, index: int
    ) -> None:
        call_actions = self._actions_by_call.get(transition.call_id or "", ())
        call_action = call_actions[0] if len(call_actions) == 1 else None
        lease = self.lease
        if not self._consume_is_valid(transition, identity_valid, lease, call_action, seq, index):
            self.invalid_transitions += 1
            return
        if lease is None or call_action is None:  # pragma: no cover - narrowed above
            raise AssertionError("validated lease consumption has no active lease")
        self.lease = lease.model_copy(
            update={
                "phase": RecoveryLeasePhase.CONSUMED,
                "call_id": transition.call_id,
                "action_id": call_action[2].id,
                "consume_event_seq": seq,
            }
        )

    @staticmethod
    def _consume_is_valid(
        transition: Any, identity_valid: bool, lease: RecoveryLeaseState | None,
        call_action: tuple[int, int, ActionEvent] | None, seq: int, index: int,
    ) -> bool:
        return (
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

    def on_progress(self, invocation: _Invocation, seq: int, profile: ActionProfile | None) -> None:
        lease = self.lease
        if (
            lease is not None
            and invocation.action_event_seq > lease.issue_event_seq
            and lease.phase in _LEASE_CLEARABLE_PHASES
        ):
            if lease.phase is RecoveryLeasePhase.CONSUMED and lease.call_id == invocation.call_id:
                capabilities = set(profile.capabilities if profile is not None else set())
                granted = set(lease.granted_capabilities)
                blocked = set(lease.blocked_capabilities)
                if not capabilities & granted or capabilities & blocked:
                    self.invalid_transitions += 1
            self.lease = _clear_lease(lease, RecoveryLeasePhase.CLEARED_BY_PROGRESS, seq)

    def on_zero_progress(
        self, invocation: _Invocation, seq: int, profile: ActionProfile | None
    ) -> None:
        lease = self.lease
        if lease is None or lease.phase is not RecoveryLeasePhase.CONSUMED:
            return
        if lease.call_id != invocation.call_id:
            return
        capabilities = set(profile.capabilities if profile is not None else set())
        exercised = capabilities & set(lease.granted_capabilities)
        blocked = capabilities & set(lease.blocked_capabilities)
        if not exercised or blocked:
            self.invalid_transitions += 1
            self.lease = _clear_lease(lease, RecoveryLeasePhase.INDETERMINATE, seq)
        else:
            self.lease = _clear_lease(lease, RecoveryLeasePhase.EXHAUSTED, seq)

    def on_invalid_pair(self, event: Event, seq: int) -> None:
        if _terminal_claims_consumed_lease(event, self.lease) and self.lease is not None:
            self.lease = _clear_lease(self.lease, RecoveryLeasePhase.INDETERMINATE, seq)

    def on_unattributed(self, invocation: _Invocation, seq: int) -> None:
        if (
            self.lease is not None
            and self.lease.phase is RecoveryLeasePhase.CONSUMED
            and self.lease.call_id == invocation.call_id
        ):
            self.lease = _clear_lease(self.lease, RecoveryLeasePhase.INDETERMINATE, seq)


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

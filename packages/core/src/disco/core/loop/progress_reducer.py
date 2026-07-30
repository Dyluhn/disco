"""Pure event-sourced progress reduction collaborators.

Extracted from ``progress.py``.  The :class:`_ProgressReducer` owns the
per-event fold state; :func:`reduce_progress` in ``progress.py`` remains the
single public entry point.  One implementation owner; no duplicated policy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from ..effects import (
    ActionProfile,
    ControlReceipt,
    EffectCapability,
    EffectReceipt,
    MutationReceipt,
    ObservationReceipt,
    OpaqueEffectReceipt,
    ResourceCoverage,
    ResourceKey,
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
from .progress import (
    _RECOVERY_ZERO_STREAK_THRESHOLD,
    ControlEvidence,
    ControlState,
    MutationEvidence,
    ObservationEvidence,
    ProgressKind,
    ProgressState,
    RecoveryCandidate,
    ResourceState,
    VerificationEvidence,
    _evidence_fingerprint,
    _Invocation,
    _LeaseTracker,
    _observation_key,
    _resource_sort_key,
    _revision_sort_key,
    _seq,
)
from .resource_context import merge_spans


def _collect_action_index(
    events: tuple[Event, ...],
) -> tuple[
    dict[str, list[tuple[int, ActionEvent]]],
    dict[str, set[str]],
    Counter[str],
    Counter[str],
]:
    """First pass: index agent actions and count terminal events per id/call."""
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
    return actions_by_id, action_ids_by_call, terminal_count_by_action, terminal_count_by_call


def _terminal_pair_fields(
    event: Event,
) -> tuple[str | None, str | None, ActionProfile | None, tuple[EffectReceipt, ...], str | None]:
    """Return (action_id, call_id, profile, receipts, tool_name) for a terminal event."""
    if isinstance(event, ObservationEvent) and event.source is EventSource.ENVIRONMENT:
        return (
            event.action_id,
            event.tool_result.call_id,
            event.tool_result.action_profile,
            event.tool_result.effect_receipts,
            event.tool_result.tool_name,
        )
    if (
        isinstance(event, AgentErrorEvent)
        and event.source is EventSource.ENVIRONMENT
        and event.action_id
        and event.tool_call_id
    ):
        return (
            event.action_id,
            event.tool_call_id,
            event.action_profile,
            event.effect_receipts,
            None,
        )
    return None, None, None, (), None


def _pair_is_valid(
    action: ActionEvent | None,
    action_index: int | None,
    index: int,
    call_id: str | None,
    tool_name: str | None,
    event: Event,
    action_ids_by_call: dict[str, set[str]],
    terminal_count_by_action: Counter[str],
    terminal_count_by_call: Counter[str],
) -> bool:
    """Validate that a terminal event binds to exactly one preceding agent action."""
    return (
        action is not None
        and action_index is not None
        and action_index < index
        and action.tool_call.call_id == call_id
        and (tool_name is None or action.tool_call.tool_name == tool_name)
        and len(action_ids_by_call.get(call_id or "", ())) == 1
        and terminal_count_by_action.get(action.id, 0) == 1
        and terminal_count_by_call.get(call_id or "", 0) == 1
        and (action.seq is None or event.seq is None or action.seq < event.seq)
    )


def _authenticated_invocations(
    events: tuple[Event, ...],
) -> tuple[dict[int, _Invocation], frozenset[int]]:
    """Bind unique environment outcomes to their exact preceding agent action."""
    actions_by_id, action_ids_by_call, terminal_count_by_action, terminal_count_by_call = (
        _collect_action_index(events)
    )
    pairs: dict[int, _Invocation] = {}
    invalid_indices: set[int] = set()
    for index, event in enumerate(events):
        action_id, call_id, profile, receipts, tool_name = _terminal_pair_fields(event)
        if action_id is None or call_id is None:
            continue
        candidates = actions_by_id.get(action_id, [])
        action = candidates[0][1] if len(candidates) == 1 else None
        action_index = candidates[0][0] if len(candidates) == 1 else None
        if not _pair_is_valid(
            action,
            action_index,
            index,
            call_id,
            tool_name,
            event,
            action_ids_by_call,
            terminal_count_by_action,
            terminal_count_by_call,
        ):
            invalid_indices.add(index)
            continue
        assert action is not None and action_index is not None and call_id is not None
        pairs[index] = _Invocation(
            action=action,
            action_event_seq=_seq(action, action_index),
            event_seq=_seq(event, index),
            call_id=call_id,
            action_profile=profile,
            receipts=receipts,
        )
    return pairs, frozenset(invalid_indices)


def _apply_observation_receipt(
    receipt: ObservationReceipt,
    invocation: _Invocation,
    resources: dict[ResourceKey, ResourceState],
    resource_action_seqs: dict[ResourceKey, int],
    observations: dict[tuple[Any, ...], ObservationEvidence],
) -> tuple[set[ProgressKind], int]:
    """Apply an observation receipt; return (delta_kinds, stale_delta)."""
    delta_kinds: set[ProgressKind] = set()
    stale = 0
    resource = receipt.revision.resource
    prior_state = resources.get(resource)
    prior_action_seq = resource_action_seqs.get(resource, -1)
    if invocation.action_event_seq < prior_action_seq:
        stale += 1
    elif (
        prior_state is None
        or not prior_state.exists
        or prior_state.revision != receipt.revision
    ):
        resources[resource] = ResourceState(
            resource=resource, exists=True, revision=receipt.revision
        )
        resource_action_seqs[resource] = invocation.action_event_seq
        delta_kinds.add(ProgressKind.EPISTEMIC)
    key = _observation_key(receipt)
    prior = observations.get(key)
    if prior is not None and prior.coverage.total != receipt.coverage.total:
        return delta_kinds, stale + 1
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
                unit=receipt.coverage.unit, spans=merged, total=receipt.coverage.total
            ),
        )
    if new_coverage:
        delta_kinds.add(ProgressKind.EPISTEMIC)
    return delta_kinds, stale


def _apply_mutation_receipt(
    receipt: MutationReceipt,
    invocation: _Invocation,
    seq: int,
    resources: dict[ResourceKey, ResourceState],
    resource_action_seqs: dict[ResourceKey, int],
    mutations: list[MutationEvidence],
) -> tuple[set[ProgressKind], int]:
    """Apply a mutation receipt; return (delta_kinds, stale_delta)."""
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
    stale = 0
    if receipt.before is not None and current is not None:
        if not current.exists or current.revision != receipt.before:
            stale += 1
    after_state = ResourceState(
        resource=resource, exists=receipt.after is not None, revision=receipt.after
    )
    prior_action_seq = resource_action_seqs.get(resource, -1)
    if invocation.action_event_seq < prior_action_seq:
        stale += 1
    elif current != after_state:
        resources[resource] = after_state
        resource_action_seqs[resource] = invocation.action_event_seq
    return {ProgressKind.STATE}, stale


def _apply_verification_receipt(
    receipt: VerificationReceipt,
    resources: dict[ResourceKey, ResourceState],
    verifications: dict[tuple[Any, ...], VerificationEvidence],
) -> tuple[set[ProgressKind], int]:
    """Apply a verification receipt; return (delta_kinds, stale_delta)."""
    current = resources.get(receipt.subject.resource)
    if current is not None and (not current.exists or current.revision != receipt.subject):
        return set(), 1
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
        return {ProgressKind.VALIDATION}, 0
    return set(), 0


def _apply_control_receipt(
    receipt: ControlReceipt,
    seq: int,
    controls: dict[tuple[str, str, str, str], ControlEvidence],
    current_controls: dict[tuple[str, str], ControlState],
) -> tuple[set[ProgressKind], int]:
    """Apply a control receipt; return (delta_kinds, stale_delta)."""
    if receipt.prior_state == receipt.new_state:
        return set(), 0
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
    stale = 0
    if current_control is not None and current_control.state != receipt.prior_state:
        stale += 1
    if current_control is None or current_control.state != receipt.new_state:
        current_controls[state_key] = ControlState(
            capability=receipt.capability,
            transition_kind=receipt.transition_kind,
            state=receipt.new_state,
            last_event_seq=seq,
        )
        return {ProgressKind.CONTROL}, stale
    return set(), stale


class _ReceiptApplier:
    """Owns the per-receipt application policy."""

    def __init__(self) -> None:
        self.resources: dict[ResourceKey, ResourceState] = {}
        self.resource_action_seqs: dict[ResourceKey, int] = {}
        self.observations: dict[tuple[Any, ...], ObservationEvidence] = {}
        self.mutations: list[MutationEvidence] = []
        self.verifications: dict[tuple[Any, ...], VerificationEvidence] = {}
        self.controls: dict[tuple[str, str, str, str], ControlEvidence] = {}
        self.current_controls: dict[tuple[str, str], ControlState] = {}
        self.stale_receipts = 0

    def apply(
        self, invocation: _Invocation, receipts: tuple[EffectReceipt, ...], seq: int
    ) -> tuple[set[ProgressKind], set[EffectCapability]]:
        delta_kinds: set[ProgressKind] = set()
        receipt_capabilities = {receipt.capability for receipt in receipts}
        for receipt in receipts:
            if isinstance(receipt, ObservationReceipt):
                kinds, stale = _apply_observation_receipt(
                    receipt, invocation, self.resources,
                    self.resource_action_seqs, self.observations,
                )
                delta_kinds |= kinds
                self.stale_receipts += stale
            elif isinstance(receipt, MutationReceipt):
                kinds, stale = _apply_mutation_receipt(
                    receipt, invocation, seq, self.resources,
                    self.resource_action_seqs, self.mutations,
                )
                delta_kinds |= kinds
                self.stale_receipts += stale
            elif isinstance(receipt, VerificationReceipt):
                kinds, stale = _apply_verification_receipt(
                    receipt, self.resources, self.verifications,
                )
                delta_kinds |= kinds
                self.stale_receipts += stale
            elif isinstance(receipt, ControlReceipt):
                kinds, stale = _apply_control_receipt(
                    receipt, seq, self.controls, self.current_controls,
                )
                delta_kinds |= kinds
                self.stale_receipts += stale
            elif isinstance(receipt, OpaqueEffectReceipt):
                continue
        return delta_kinds, receipt_capabilities


def _sorted_resources(
    resources: dict[ResourceKey, ResourceState],
) -> tuple[ResourceState, ...]:
    return tuple(
        sorted(resources.values(), key=lambda item: _resource_sort_key(item.resource))
    )


def _sorted_observations(
    observations: dict[tuple[Any, ...], ObservationEvidence],
) -> tuple[ObservationEvidence, ...]:
    return tuple(
        sorted(
            observations.values(),
            key=lambda item: (
                *_revision_sort_key(item.revision),
                item.coverage.unit.value,
                -1 if item.coverage.total is None else item.coverage.total,
            ),
        )
    )


def _sorted_verifications(
    verifications: dict[tuple[Any, ...], VerificationEvidence],
) -> tuple[VerificationEvidence, ...]:
    return tuple(
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
    )


def _sorted_controls(
    controls: dict[tuple[str, str, str, str], ControlEvidence],
) -> tuple[ControlEvidence, ...]:
    return tuple(
        sorted(
            controls.values(),
            key=lambda item: (
                item.capability.value,
                item.transition_kind,
                item.prior_state,
                item.new_state,
            ),
        )
    )


def _sorted_current_controls(
    current_controls: dict[tuple[str, str], ControlState],
) -> tuple[ControlState, ...]:
    return tuple(
        sorted(
            current_controls.values(),
            key=lambda item: (item.capability.value, item.transition_kind),
        )
    )


class _ProgressReducer:
    """Owns the per-event fold loop and delegates to bounded collaborators."""

    def __init__(self, event_list: tuple[Event, ...]) -> None:
        self._event_list = event_list
        self._invocations, self._invalid_pair_indices = _authenticated_invocations(event_list)
        self._actions_by_call: dict[str, list[tuple[int, int, ActionEvent]]] = defaultdict(list)
        for action_index, candidate in enumerate(event_list):
            if isinstance(candidate, ActionEvent) and candidate.source is EventSource.AGENT:
                self._actions_by_call[candidate.tool_call.call_id].append(
                    (_seq(candidate, action_index), action_index, candidate)
                )
        self._receipts = _ReceiptApplier()
        self._lease_tracker = _LeaseTracker(self._actions_by_call)
        self._progress_seqs: list[int] = []
        self._progress_kinds: set[ProgressKind] = set()
        self._last_progress_seq: int | None = None
        self._user_epoch_seq: int | None = None
        self._executed = 0
        self._zero_progress = 0
        self._unattributed = 0
        self._invalid_receipts = 0
        self._trailing_zero_capabilities: list[EffectCapability] = []
        self._recovery_comparable = True
        self._last_zero_event_seq: int | None = None
        self._latest_seq = 0

    @property
    def invalid_pair_count(self) -> int:
        return len(self._invalid_pair_indices)

    def _reset_zero_streak(self) -> None:
        self._trailing_zero_capabilities.clear()
        self._recovery_comparable = False
        self._last_zero_event_seq = None

    def _handle_user_message(self, event: MessageEvent, seq: int) -> None:
        self._user_epoch_seq = seq
        self._last_progress_seq = seq
        self._progress_seqs.append(seq)
        self._progress_kinds.add(ProgressKind.CONTROL)
        self._zero_progress = 0
        self._trailing_zero_capabilities.clear()
        self._recovery_comparable = True
        self._last_zero_event_seq = None
        self._lease_tracker.clear_by_user(seq)

    def _handle_lease_transition(
        self, event: StatusEvent, seq: int, prior_latest_seq: int, index: int
    ) -> None:
        fingerprint = _evidence_fingerprint(
            self._receipts.resources,
            self._receipts.observations,
            self._receipts.mutations,
            self._receipts.verifications,
            self._receipts.controls,
            self._receipts.current_controls,
        )
        self._lease_tracker.handle_transition(event, seq, prior_latest_seq, index, fingerprint)

    def _handle_unattributed(self, invocation: _Invocation, seq: int) -> None:
        self._unattributed += 1
        self._zero_progress = 0
        self._reset_zero_streak()
        self._lease_tracker.on_unattributed(invocation, seq)

    def _handle_invalid_receipts(self, invocation: _Invocation, seq: int) -> None:
        self._invalid_receipts += 1
        self._unattributed += 1
        self._zero_progress = 0
        self._reset_zero_streak()
        self._lease_tracker.on_unattributed(invocation, seq)

    def _handle_progress(
        self, invocation: _Invocation, delta_kinds: set[ProgressKind], seq: int,
        profile: ActionProfile | None,
    ) -> None:
        self._last_progress_seq = seq
        self._progress_seqs.append(seq)
        self._progress_kinds.update(delta_kinds)
        self._zero_progress = 0
        self._trailing_zero_capabilities.clear()
        self._recovery_comparable = True
        self._last_zero_event_seq = None
        self._lease_tracker.on_progress(invocation, seq, profile)

    def _handle_zero_progress(
        self, invocation: _Invocation, receipt_capabilities: set[EffectCapability],
        seq: int, profile: ActionProfile | None,
    ) -> None:
        self._zero_progress += 1
        zero_capability = self._select_zero_capability(receipt_capabilities, profile)
        if zero_capability is None:
            self._reset_zero_streak()
        else:
            self._track_zero_capability(zero_capability, seq)
        self._lease_tracker.on_zero_progress(invocation, seq, profile)

    def _select_zero_capability(
        self, receipt_capabilities: set[EffectCapability], profile: ActionProfile | None
    ) -> EffectCapability | None:
        if len(receipt_capabilities) == 1:
            return next(iter(receipt_capabilities))
        if not receipt_capabilities and profile is not None and len(profile.capabilities) == 1:
            return next(iter(profile.capabilities))
        return None

    def _track_zero_capability(self, capability: EffectCapability, seq: int) -> None:
        if (
            not self._trailing_zero_capabilities
            or self._trailing_zero_capabilities[-1] == capability
        ):
            self._trailing_zero_capabilities.append(capability)
        else:
            self._trailing_zero_capabilities = [capability]
        self._recovery_comparable = True
        self._last_zero_event_seq = seq

    def _handle_invocation(self, invocation: _Invocation, index: int, event: Event) -> None:
        seq = _seq(event, index)
        profile = invocation.action_profile
        if profile is None:
            self._handle_unattributed(invocation, seq)
            return
        self._executed += 1
        receipts = invocation.receipts
        try:
            receipts = validate_effect_receipts(profile, receipts)
        except ValueError:
            self._handle_invalid_receipts(invocation, seq)
            return
        delta_kinds, receipt_capabilities = self._receipts.apply(invocation, receipts, seq)
        if delta_kinds:
            self._handle_progress(invocation, delta_kinds, seq, profile)
        else:
            self._handle_zero_progress(invocation, receipt_capabilities, seq, profile)

    def reduce(self) -> ProgressState:
        """Feed all events to the reducer and return the final state."""
        for index, event in enumerate(self._event_list):
            seq = _seq(event, index)
            prior_latest_seq = self._latest_seq
            self._latest_seq = max(self._latest_seq, seq)

            if isinstance(event, MessageEvent) and event.source is EventSource.USER:
                self._handle_user_message(event, seq)
                continue

            if isinstance(event, StatusEvent) and event.recovery_lease_transition is not None:
                self._handle_lease_transition(event, seq, prior_latest_seq, index)
                continue

            invocation = self._invocations.get(index)
            if invocation is None:
                if index in self._invalid_pair_indices:
                    self._reset_zero_streak()
                    self._lease_tracker.on_invalid_pair(event, seq)
                continue
            self._handle_invocation(invocation, index, event)

        return self._build_state()

    def _build_state(self) -> ProgressState:
        fingerprint = _evidence_fingerprint(
            self._receipts.resources,
            self._receipts.observations,
            self._receipts.mutations,
            self._receipts.verifications,
            self._receipts.controls,
            self._receipts.current_controls,
        )
        recovery_candidate = None
        if (
            self._recovery_comparable
            and len(self._trailing_zero_capabilities) >= _RECOVERY_ZERO_STREAK_THRESHOLD
            and self._last_zero_event_seq is not None
        ):
            recovery_candidate = RecoveryCandidate(
                blocked_capability=self._trailing_zero_capabilities[-1],
                zero_progress_streak=len(self._trailing_zero_capabilities),
                threshold=_RECOVERY_ZERO_STREAK_THRESHOLD,
                trigger_event_seq=self._last_zero_event_seq,
            )
        return ProgressState(
            latest_event_seq=self._latest_seq,
            user_epoch_seq=self._user_epoch_seq,
            last_progress_seq=self._last_progress_seq,
            progress_event_seqs=tuple(self._progress_seqs),
            progress_kinds=frozenset(self._progress_kinds),
            current_resources=_sorted_resources(self._receipts.resources),
            observations=_sorted_observations(self._receipts.observations),
            mutations=tuple(self._receipts.mutations),
            verifications=_sorted_verifications(self._receipts.verifications),
            controls=_sorted_controls(self._receipts.controls),
            current_controls=_sorted_current_controls(self._receipts.current_controls),
            evidence_fingerprint=fingerprint,
            executed_invocations=self._executed,
            zero_progress_invocations=self._zero_progress,
            unattributed_invocations=self._unattributed,
            invalid_event_pairs=self.invalid_pair_count,
            invalid_receipt_groups=self._invalid_receipts,
            stale_receipts=self._receipts.stale_receipts,
            recovery_lease=self._lease_tracker.lease,
            recovery_candidate=recovery_candidate,
            recovery_comparable=self._recovery_comparable,
            invalid_lease_transitions=self._lease_tracker.invalid_transitions,
        )


def reduce_progress_events(events: Iterable[Event]) -> ProgressState:
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
    reducer = _ProgressReducer(event_list)
    return reducer.reduce()

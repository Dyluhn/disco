from __future__ import annotations

from collections.abc import Iterable

import pytest
from disco.core import (
    ActionEvent,
    ActionProfile,
    AgentErrorEvent,
    ControlReceipt,
    ConversationStatus,
    EffectCapability,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    MutationReceipt,
    ObservationEvent,
    ObservationReceipt,
    OpaqueEffectReceipt,
    RecoveryLeaseTransition,
    RecoveryLeaseTransitionKind,
    ResourceCoverage,
    ResourceKey,
    ResourceRevision,
    StatusEvent,
    ToolCall,
    ToolResult,
    VerificationReceipt,
    event_from_json_dict,
    event_to_json_dict,
)
from disco.core.effects import CoverageSpan, CoverageUnit, EffectReceipt
from disco.core.inspect import record_progress_shadow, registry
from disco.core.loop.progress import (
    ProgressKind,
    RecoveryLeasePhase,
    recovery_lease_id,
    reduce_progress,
)
from pydantic import ValidationError

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64


def _sequenced(events: Iterable[Event]) -> tuple[Event, ...]:
    return tuple(event.model_copy(update={"seq": index}) for index, event in enumerate(events, 1))


def _resource(identifier: str = "src/app.py") -> ResourceKey:
    return ResourceKey(namespace="workspace", identifier=identifier)


def _revision(digest: str = _A, identifier: str = "src/app.py") -> ResourceRevision:
    return ResourceRevision(resource=_resource(identifier), digest=digest)


def _profile(*capabilities: EffectCapability) -> ActionProfile:
    return ActionProfile(capabilities=frozenset(capabilities))


def _action(
    call_id: str,
    *,
    tool_name: str = "probe",
    event_id: str | None = None,
) -> ActionEvent:
    values: dict[str, object] = {
        "thought": "inspect exact state",
        "tool_call": ToolCall(tool_name=tool_name, arguments={}, call_id=call_id),
    }
    if event_id is not None:
        values["id"] = event_id
    return ActionEvent.model_validate(values)


def _result(
    action: ActionEvent,
    profile: ActionProfile | None,
    receipts: tuple[EffectReceipt, ...] = (),
    *,
    tool_name: str | None = None,
) -> ObservationEvent:
    return ObservationEvent(
        action_id=action.id,
        tool_result=ToolResult(
            call_id=action.tool_call.call_id,
            tool_name=tool_name or action.tool_call.tool_name,
            success=True,
            content="host result",
            action_profile=profile,
            effect_receipts=receipts,
        ),
    )


def _observation(
    *,
    digest: str = _A,
    spans: tuple[tuple[int, int], ...] = ((0, 5),),
    total: int = 10,
    capability: EffectCapability = EffectCapability.WORKSPACE_CONTENT_READ,
    identifier: str = "src/app.py",
) -> ObservationReceipt:
    return ObservationReceipt(
        capability=capability,
        revision=_revision(digest, identifier),
        coverage=ResourceCoverage(
            unit=CoverageUnit.BYTES,
            spans=tuple(CoverageSpan(start=start, end=end) for start, end in spans),
            total=total,
        ),
    )


def _pair(
    call_id: str,
    profile: ActionProfile,
    *receipts: EffectReceipt,
    tool_name: str = "probe",
) -> tuple[ActionEvent, ObservationEvent]:
    action = _action(call_id, tool_name=tool_name)
    return action, _result(action, profile, tuple(receipts))


def _user(text: str = "build it") -> MessageEvent:
    return MessageEvent(
        source=EventSource.USER,
        message=LLMMessage(role="user", content=text),
    )


def test_observation_coverage_counts_only_new_exact_ranges() -> None:
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    events = _sequenced(
        (
            *_pair("call_head", profile, _observation(spans=((0, 5),))),
            *_pair("call_overlap", profile, _observation(spans=((3, 8),))),
            *_pair("call_subset", profile, _observation(spans=((1, 7),))),
        )
    )

    state = reduce_progress(events)

    assert state.progress_event_seqs == (2, 4)
    assert state.zero_progress_invocations == 1
    assert state.progress_kinds == frozenset({ProgressKind.EPISTEMIC})
    assert len(state.observations) == 1
    assert state.observations[0].coverage.spans == (CoverageSpan(start=0, end=8),)


def test_conflicting_coverage_total_is_an_anomaly_not_progress() -> None:
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    events = _sequenced(
        (
            *_pair("call_first", profile, _observation(spans=((0, 5),), total=10)),
            *_pair("call_conflict", profile, _observation(spans=((5, 8),), total=11)),
        )
    )

    state = reduce_progress(events)

    assert state.progress_event_seqs == (2,)
    assert state.zero_progress_invocations == 1
    assert state.stale_receipts == 1
    assert state.observations[0].coverage.total == 10


def test_capability_change_cannot_launder_identical_bytes_into_progress() -> None:
    content_profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    external_profile = _profile(EffectCapability.EXTERNAL_OBSERVE)
    first = _sequenced(_pair("call_content", content_profile, _observation()))
    external_receipt = _observation(capability=EffectCapability.EXTERNAL_OBSERVE)
    full = _sequenced(
        (
            *first,
            *_pair("call_external", external_profile, external_receipt),
        )
    )

    first_state = reduce_progress(first)
    state = reduce_progress(full)

    assert state.progress_event_seqs == (2,)
    assert state.zero_progress_invocations == 1
    assert len(state.observations) == 1
    assert state.observations[0].capabilities == frozenset(
        {
            EffectCapability.WORKSPACE_CONTENT_READ,
            EffectCapability.EXTERNAL_OBSERVE,
        }
    )
    assert state.evidence_fingerprint == first_state.evidence_fingerprint


def test_mutations_preserve_each_real_transition_and_latest_state() -> None:
    profile = _profile(EffectCapability.WORKSPACE_MUTATE)
    resource = _resource()
    events = _sequenced(
        (
            *_pair(
                "call_create",
                profile,
                MutationReceipt(resource=resource, after=_revision(_A), after_size_bytes=10),
            ),
            *_pair(
                "call_discontinuous",
                profile,
                MutationReceipt(
                    resource=resource,
                    before=_revision(_C),
                    after=_revision(_B),
                    after_size_bytes=11,
                ),
            ),
            *_pair(
                "call_revert",
                profile,
                MutationReceipt(
                    resource=resource,
                    before=_revision(_B),
                    after=_revision(_A),
                    after_size_bytes=10,
                ),
            ),
        )
    )

    state = reduce_progress(events)

    assert state.progress_event_seqs == (2, 4, 6)
    assert len(state.mutations) == 3
    assert state.stale_receipts == 1
    assert state.current_resources[0].revision == _revision(_A)


def test_delayed_old_observation_cannot_roll_current_resource_back() -> None:
    old_action = _action("call_old")
    new_action = _action("call_new")
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    events = _sequenced(
        (
            old_action,
            new_action,
            _result(new_action, profile, (_observation(digest=_B),)),
            _result(old_action, profile, (_observation(digest=_A),)),
        )
    )

    state = reduce_progress(events)

    assert state.current_resources[0].revision == _revision(_B)
    assert {item.revision for item in state.observations} == {_revision(_A), _revision(_B)}
    assert state.stale_receipts == 1


def test_verification_identity_includes_subject_revision_and_outcome() -> None:
    mutate = _profile(EffectCapability.WORKSPACE_MUTATE)
    verify = _profile(EffectCapability.ARTIFACT_VERIFY)
    resource = _resource()
    requirement = _B
    failed = VerificationReceipt(
        verifier_id="web-ready",
        subject=_revision(_A),
        requirement_fingerprint=requirement,
        passed=False,
        failure_fingerprint=_C,
    )
    events = _sequenced(
        (
            *_pair(
                "call_create",
                mutate,
                MutationReceipt(resource=resource, after=_revision(_A), after_size_bytes=10),
            ),
            *_pair("call_fail", verify, failed),
            *_pair("call_same_fail", verify, failed),
            *_pair(
                "call_new_fail",
                verify,
                failed.model_copy(update={"failure_fingerprint": _D}),
            ),
            *_pair(
                "call_pass",
                verify,
                VerificationReceipt(
                    verifier_id="web-ready",
                    subject=_revision(_A),
                    requirement_fingerprint=requirement,
                    passed=True,
                ),
            ),
            *_pair(
                "call_stale_subject",
                verify,
                VerificationReceipt(
                    verifier_id="web-ready",
                    subject=_revision(_B),
                    requirement_fingerprint=requirement,
                    passed=True,
                ),
            ),
        )
    )

    state = reduce_progress(events)

    assert len(state.verifications) == 3
    assert state.progress_event_seqs == (2, 4, 8, 10)
    assert state.zero_progress_invocations == 1
    assert state.stale_receipts == 1


def test_control_progress_requires_a_new_exact_transition() -> None:
    profile = _profile(EffectCapability.PLAN_CONTROL)
    forward = ControlReceipt(
        capability=EffectCapability.PLAN_CONTROL,
        transition_kind="plan-42",
        prior_state="pending",
        new_state="running",
    )
    reverse = forward.model_copy(update={"prior_state": "running", "new_state": "pending"})
    events = _sequenced(
        (
            *_pair("call_forward", profile, forward),
            *_pair("call_duplicate", profile, forward),
            *_pair("call_reverse", profile, reverse),
        )
    )

    state = reduce_progress(events)

    assert state.progress_event_seqs == (2, 6)
    assert len(state.controls) == 2
    assert state.zero_progress_invocations == 0


def test_control_fold_preserves_opposite_terminal_states() -> None:
    profile = _profile(EffectCapability.RUN_CONTROL)
    forward = ControlReceipt(
        capability=EffectCapability.RUN_CONTROL,
        transition_kind="execution",
        prior_state="paused",
        new_state="running",
    )
    reverse = forward.model_copy(update={"prior_state": "running", "new_state": "paused"})
    ends_paused = reduce_progress(
        _sequenced(
            (
                *_pair("call_forward", profile, forward),
                *_pair("call_reverse", profile, reverse),
            )
        )
    )
    ends_running = reduce_progress(
        _sequenced(
            (
                *_pair("call_reverse", profile, reverse),
                *_pair("call_forward", profile, forward),
            )
        )
    )

    assert ends_paused.current_controls[0].state == "paused"
    assert ends_running.current_controls[0].state == "running"
    assert ends_paused.evidence_fingerprint != ends_running.evidence_fingerprint


def test_opaque_and_hollow_results_are_known_zero_but_legacy_is_unknown() -> None:
    opaque_profile = _profile(EffectCapability.OPAQUE_EXECUTE)
    opaque = OpaqueEffectReceipt(
        capability=EffectCapability.OPAQUE_EXECUTE,
        reason="host cannot attribute exact resource effects",
    )
    hollow_action, hollow_result = _pair("call_hollow", opaque_profile)
    legacy_action = _action("call_legacy")
    events = _sequenced(
        (
            *_pair("call_opaque", opaque_profile, opaque),
            hollow_action,
            hollow_result,
            legacy_action,
            _result(legacy_action, None),
        )
    )

    state = reduce_progress(events)

    assert state.executed_invocations == 2
    # Both classified calls are known zero, but the trailing legacy outcome
    # breaks the actionable streak rather than bridging it into model blame.
    assert reduce_progress(events[:4]).zero_progress_invocations == 2
    assert state.zero_progress_invocations == 0
    assert state.unattributed_invocations == 1
    assert state.progress_event_seqs == ()


def test_invalid_host_receipts_are_unattributed_not_charged_to_the_agent() -> None:
    action = _action("call_invalid")
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    incompatible = MutationReceipt(
        resource=_resource(),
        after=_revision(_A),
        after_size_bytes=10,
    )
    events = _sequenced((action, _result(action, profile, (incompatible,))))

    state = reduce_progress(events)

    assert state.invalid_receipt_groups == 1
    assert state.unattributed_invocations == 1
    assert state.zero_progress_invocations == 0
    assert state.current_resources == ()


def test_failed_invocation_evidence_is_reduced_exactly_like_success() -> None:
    action = _action("call_failed_write", tool_name="file_write")
    profile = _profile(EffectCapability.WORKSPACE_MUTATE)
    receipt = MutationReceipt(
        resource=_resource(),
        after=_revision(_A),
        after_size_bytes=10,
    )
    failure = AgentErrorEvent(
        error="post-write verifier failed",
        action_id=action.id,
        tool_call_id=action.tool_call.call_id,
        action_profile=profile,
        effect_receipts=(receipt,),
    )

    state = reduce_progress(_sequenced((action, failure)))

    assert state.executed_invocations == 1
    assert state.progress_event_seqs == (2,)
    assert state.current_resources[0].revision == _revision(_A)


def test_only_one_exact_action_terminal_pair_is_authenticated() -> None:
    action = _action("call_duplicate")
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    result = _result(action, profile, (_observation(),))
    events = _sequenced((action, result, result.model_copy(update={"id": "evt_duplicate"})))

    state = reduce_progress(events)

    assert state.invalid_event_pairs == 2
    assert state.executed_invocations == 0
    assert state.observations == ()


def test_out_of_order_persisted_log_is_inert_and_incomparable() -> None:
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    action, result = _pair("call_read", profile, _observation())
    malformed = (
        action.model_copy(update={"seq": 10}),
        result.model_copy(update={"seq": 3}),
    )

    state = reduce_progress(malformed)

    assert state.invalid_log_order is True
    assert state.recovery_comparable is False
    assert state.progress_event_seqs == ()
    assert state.current_resources == ()


def test_tool_rename_does_not_change_semantic_progress() -> None:
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    receipt = _observation()
    old_action = _action("call_same", tool_name="read_file", event_id="evt_action")
    new_action = _action("call_same", tool_name="resource_fetch", event_id="evt_action")
    old = reduce_progress(_sequenced((old_action, _result(old_action, profile, (receipt,)))))
    new = reduce_progress(_sequenced((new_action, _result(new_action, profile, (receipt,)))))

    assert old == new


def _lease_transition(
    prefix: tuple[Event, ...],
    *,
    transition: RecoveryLeaseTransitionKind,
    call_id: str | None = None,
) -> RecoveryLeaseTransition:
    state = reduce_progress(prefix)
    blocked = (EffectCapability.WORKSPACE_CONTENT_READ,)
    granted = (EffectCapability.WORKSPACE_INVENTORY_READ,)
    issued_after_seq = prefix[-1].seq
    assert issued_after_seq is not None
    lease_id = recovery_lease_id(
        issued_after_seq=issued_after_seq,
        baseline_evidence_fingerprint=state.evidence_fingerprint,
        blocked_capabilities=blocked,
        granted_capabilities=granted,
    )
    return RecoveryLeaseTransition(
        transition=transition,
        lease_id=lease_id,
        issued_after_seq=issued_after_seq,
        baseline_evidence_fingerprint=state.evidence_fingerprint,
        blocked_capabilities=blocked,
        granted_capabilities=granted,
        call_id=call_id,
    )


def test_recovery_lease_is_one_use_and_clears_only_on_exact_progress() -> None:
    prefix = _sequenced((_user(),))
    issue_transition = _lease_transition(prefix, transition=RecoveryLeaseTransitionKind.ISSUE)
    issue = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=issue_transition,
    )
    action = _action("call_inventory", tool_name="workspace_inventory")
    consume_transition = issue_transition.model_copy(
        update={
            "transition": RecoveryLeaseTransitionKind.CONSUME,
            "call_id": action.tool_call.call_id,
        }
    )
    consume = StatusEvent(
        status=ConversationStatus.RUNNING,
        recovery_lease_transition=consume_transition,
    )
    profile = _profile(EffectCapability.WORKSPACE_INVENTORY_READ)
    receipt = _observation(
        capability=EffectCapability.WORKSPACE_INVENTORY_READ,
        identifier=".",
    )
    events = _sequenced((*prefix, issue, action, consume, _result(action, profile, (receipt,))))

    state = reduce_progress(events)

    assert state.recovery_lease is not None
    assert state.recovery_lease.phase is RecoveryLeasePhase.CLEARED_BY_PROGRESS
    assert state.recovery_lease.call_id == "call_inventory"
    assert state.invalid_lease_transitions == 0

    hollow_events = _sequenced((*prefix, issue, action, consume, _result(action, profile)))
    hollow = reduce_progress(hollow_events)
    assert hollow.recovery_lease is not None
    assert hollow.recovery_lease.phase is RecoveryLeasePhase.EXHAUSTED


def test_recovery_candidate_requires_four_comparable_same_capability_zeros() -> None:
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    events: list[Event] = [*_pair("call_initial", profile, _observation())]
    for index in range(4):
        events.extend(_pair(f"call_repeat_{index}", profile, _observation()))

    state = reduce_progress(_sequenced(events))

    assert state.recovery_comparable is True
    assert state.recovery_candidate is not None
    assert state.recovery_candidate.blocked_capability is (EffectCapability.WORKSPACE_CONTENT_READ)
    assert state.recovery_candidate.zero_progress_streak == 4
    assert state.recovery_candidate.threshold == 4


def test_unknown_consumed_lease_outcomes_are_indeterminate_not_exhausted() -> None:
    prefix = _sequenced((_user(),))
    issue_transition = _lease_transition(prefix, transition=RecoveryLeaseTransitionKind.ISSUE)
    issue = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=issue_transition,
    )
    action = _action("call_unknown", tool_name="workspace_inventory")
    consume = StatusEvent(
        status=ConversationStatus.RUNNING,
        recovery_lease_transition=issue_transition.model_copy(
            update={
                "transition": RecoveryLeaseTransitionKind.CONSUME,
                "call_id": action.tool_call.call_id,
            }
        ),
    )
    result = _result(action, None)

    state = reduce_progress(_sequenced((*prefix, issue, action, consume, result)))

    assert state.recovery_lease is not None
    assert state.recovery_lease.phase is RecoveryLeasePhase.INDETERMINATE
    assert state.zero_progress_invocations == 0
    assert state.unattributed_invocations == 1


def test_invalid_receipts_on_consumed_lease_are_indeterminate_not_model_zero() -> None:
    prefix = _sequenced((_user(),))
    issue_transition = _lease_transition(
        prefix, transition=RecoveryLeaseTransitionKind.ISSUE
    )
    issue = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=issue_transition,
    )
    action = _action("call_bad_telemetry", tool_name="workspace_inventory")
    consume = StatusEvent(
        status=ConversationStatus.RUNNING,
        recovery_lease_transition=issue_transition.model_copy(
            update={
                "transition": RecoveryLeaseTransitionKind.CONSUME,
                "call_id": action.tool_call.call_id,
            }
        ),
    )
    inventory_profile = _profile(EffectCapability.WORKSPACE_INVENTORY_READ)
    incompatible = MutationReceipt(
        resource=_resource(),
        after=_revision(),
        after_size_bytes=10,
    )
    result = _result(action, inventory_profile, (incompatible,))

    state = reduce_progress(_sequenced((*prefix, issue, action, consume, result)))

    assert state.recovery_lease is not None
    assert state.recovery_lease.phase is RecoveryLeasePhase.INDETERMINATE
    assert state.invalid_receipt_groups == 1
    assert state.zero_progress_invocations == 0


def test_unauthenticated_consumed_lease_terminal_resolves_indeterminate() -> None:
    prefix = _sequenced((_user(),))
    issue_transition = _lease_transition(prefix, transition=RecoveryLeaseTransitionKind.ISSUE)
    issue = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=issue_transition,
    )
    action = _action("call_duplicate", tool_name="workspace_inventory")
    consume = StatusEvent(
        status=ConversationStatus.RUNNING,
        recovery_lease_transition=issue_transition.model_copy(
            update={
                "transition": RecoveryLeaseTransitionKind.CONSUME,
                "call_id": action.tool_call.call_id,
            }
        ),
    )
    profile = _profile(EffectCapability.WORKSPACE_INVENTORY_READ)
    result = _result(action, profile)
    duplicate = result.model_copy(update={"id": "evt_duplicate_terminal"})

    state = reduce_progress(_sequenced((*prefix, issue, action, consume, result, duplicate)))

    assert state.invalid_event_pairs == 2
    assert state.recovery_lease is not None
    assert state.recovery_lease.phase is RecoveryLeasePhase.INDETERMINATE
    assert state.zero_progress_invocations == 0


def test_recovery_lease_schema_is_canonical_and_round_trips() -> None:
    prefix = _sequenced((_user(),))
    transition = _lease_transition(prefix, transition=RecoveryLeaseTransitionKind.ISSUE)
    status = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=transition,
    )

    restored = event_from_json_dict(event_to_json_dict(status))
    assert isinstance(restored, StatusEvent)
    assert restored.recovery_lease_transition == transition

    payload = transition.model_dump()
    payload["granted_capabilities"] = payload["blocked_capabilities"]
    with pytest.raises(ValidationError, match="must be disjoint"):
        RecoveryLeaseTransition.model_validate(payload)

    payload = transition.model_dump()
    payload["call_id"] = "call_not_allowed_on_issue"
    with pytest.raises(ValidationError, match="issue cannot carry"):
        RecoveryLeaseTransition.model_validate(payload)


def test_forged_or_unbound_lease_transitions_are_inert() -> None:
    prefix = _sequenced((_user(),))
    transition = _lease_transition(prefix, transition=RecoveryLeaseTransitionKind.ISSUE)
    forged_source = StatusEvent(
        source=EventSource.AGENT,
        status=ConversationStatus.STUCK,
        recovery_lease_transition=transition,
    )
    forged = reduce_progress(_sequenced((*prefix, forged_source)))
    assert forged.recovery_lease is None
    assert forged.invalid_lease_transitions == 1

    issue = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=transition,
    )
    nonexistent_consume = StatusEvent(
        status=ConversationStatus.RUNNING,
        recovery_lease_transition=transition.model_copy(
            update={
                "transition": RecoveryLeaseTransitionKind.CONSUME,
                "call_id": "call_missing",
            }
        ),
    )
    unbound = reduce_progress(_sequenced((*prefix, issue, nonexistent_consume)))
    assert unbound.recovery_lease is not None
    assert unbound.recovery_lease.phase is RecoveryLeasePhase.ACTIVE
    assert unbound.invalid_lease_transitions == 1


def test_delayed_prelease_result_cannot_clear_or_poison_the_lease() -> None:
    user = _user()
    delayed_action = _action("call_delayed")
    prefix = _sequenced((user, delayed_action))
    issue_transition = _lease_transition(prefix, transition=RecoveryLeaseTransitionKind.ISSUE)
    issue = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=issue_transition,
    )
    delayed_result = _result(
        delayed_action,
        _profile(EffectCapability.WORKSPACE_CONTENT_READ),
        (_observation(),),
    )
    interim = _sequenced((*prefix, issue, delayed_result))

    interim_state = reduce_progress(interim)
    assert interim_state.recovery_lease is not None
    assert interim_state.recovery_lease.phase is RecoveryLeasePhase.ACTIVE

    recovery_action = _action("call_recovery", tool_name="workspace_inventory")
    consume = StatusEvent(
        status=ConversationStatus.RUNNING,
        recovery_lease_transition=issue_transition.model_copy(
            update={
                "transition": RecoveryLeaseTransitionKind.CONSUME,
                "call_id": recovery_action.tool_call.call_id,
            }
        ),
    )
    recovery_result = _result(
        recovery_action,
        _profile(EffectCapability.WORKSPACE_INVENTORY_READ),
        (
            _observation(
                capability=EffectCapability.WORKSPACE_INVENTORY_READ,
                identifier=".",
            ),
        ),
    )
    final = reduce_progress(
        _sequenced((*prefix, issue, delayed_result, recovery_action, consume, recovery_result))
    )

    assert final.recovery_lease is not None
    assert final.recovery_lease.phase is RecoveryLeasePhase.CLEARED_BY_PROGRESS
    assert final.invalid_lease_transitions == 0


def test_user_instruction_clears_lease_but_pause_and_restart_status_do_not() -> None:
    prefix = _sequenced((_user(),))
    issue_transition = _lease_transition(prefix, transition=RecoveryLeaseTransitionKind.ISSUE)
    issue = StatusEvent(
        status=ConversationStatus.STUCK,
        recovery_lease_transition=issue_transition,
    )
    paused = reduce_progress(
        _sequenced(
            (
                *prefix,
                issue,
                StatusEvent(status=ConversationStatus.PAUSED),
                StatusEvent(status=ConversationStatus.RUNNING),
            )
        )
    )
    assert paused.recovery_lease is not None
    assert paused.recovery_lease.phase is RecoveryLeasePhase.ACTIVE

    steered = reduce_progress(_sequenced((*prefix, issue, _user("try a different layout"))))
    assert steered.recovery_lease is not None
    assert steered.recovery_lease.phase is RecoveryLeasePhase.CLEARED_BY_USER
    assert steered.evidence_fingerprint == reduce_progress(prefix).evidence_fingerprint


def test_reducer_is_byte_replay_stable_through_event_serialization() -> None:
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    events = _sequenced((*_pair("call_read", profile, _observation()), _user("continue")))
    restored = tuple(event_from_json_dict(event_to_json_dict(event)) for event in events)

    assert reduce_progress(events) == reduce_progress(restored)


def test_progress_shadow_is_bounded_non_authoritative_inspect_telemetry(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    profile = _profile(EffectCapability.WORKSPACE_CONTENT_READ)
    state = reduce_progress(_sequenced(_pair("call_read", profile, _observation())))

    record_progress_shadow(
        "conv-progress",
        latest_event_seq=state.latest_event_seq,
        last_progress_seq=state.last_progress_seq,
        evidence_fingerprint=state.evidence_fingerprint,
        progress_kinds=sorted(kind.value for kind in state.progress_kinds),
        current_resource_count=len(state.current_resources),
        observation_count=len(state.observations),
        mutation_count=len(state.mutations),
        verification_count=len(state.verifications),
        executed_invocations=state.executed_invocations,
        zero_progress_invocations=state.zero_progress_invocations,
        unattributed_invocations=state.unattributed_invocations,
        invalid_event_pairs=state.invalid_event_pairs,
        invalid_receipt_groups=state.invalid_receipt_groups,
        stale_receipts=state.stale_receipts,
        recovery_lease_phase=None,
        recovery_candidate_capability=None,
        recovery_candidate_streak=0,
        recovery_comparable=state.recovery_comparable,
        invalid_log_order=state.invalid_log_order,
        legacy_escape_active=False,
    )

    snapshot = registry().snapshot("conv-progress")
    assert snapshot is not None
    assert len(snapshot["progress_shadows"]) == 1
    shadow = snapshot["progress_shadows"][0]
    assert shadow["authoritative"] is False
    assert shadow["evidence_fingerprint"] == state.evidence_fingerprint
    assert shadow["observation_count"] == 1
    assert shadow["agreement"] is True
    assert "src/app.py" not in str(shadow)
    registry().clear()

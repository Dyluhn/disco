from __future__ import annotations

import pytest
from disco.core import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    APPKIT_EJECTION_SOURCE_TRIGGER,
    APPKIT_EJECTION_TARGET_TRIGGER,
    ActionEvent,
    AppKitEjectionEvent,
    BuildPlatformAdmissionEvent,
    ContextSummaryEvent,
    ConversationStatus,
    Event,
    StatusEvent,
    ToolCall,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
    current_appkit_ejection,
    current_build_platform_admission,
    event_from_json_dict,
    event_to_json_dict,
)

_DIGEST = "sha256:" + "a" * 64
_RUN_ID = "run:sha256:" + "b" * 64


def _intent(seq: int = 1) -> WorkspaceMutationEvent:
    return WorkspaceMutationEvent(
        id="intent",
        seq=seq,
        operation="agent.run-intent.message",
        run_protocol_version=1,
    )


def _platform(seq: int = 2) -> BuildPlatformAdmissionEvent:
    return BuildPlatformAdmissionEvent(
        seq=seq,
        route="platform",
        profile_id="disco.freeform_web@1",
        run_intent_id="intent",
        composition_authority="build_platform_core",
        composition_digest=_DIGEST,
        run_identity=_RUN_ID,
    )


def _ejection_lifecycle() -> list[Event]:
    intent = _intent()
    initial = BuildPlatformAdmissionEvent(
        id="appkit-admission",
        seq=2,
        route="legacy",
        profile_id="disco.appkit_web@1",
        run_intent_id=intent.id,
        composition_authority="legacy",
    )
    view = WorkspaceMutationEvent(
        seq=3,
        operation="agent.view-admitted",
        run_protocol_version=1,
        run_intent_id=intent.id,
        agent_view_id="view-1",
    )
    action = ActionEvent(
        id="action-1",
        seq=4,
        agent_view_id="view-1",
        thought="confirmed profile transition",
        tool_call=ToolCall(
            call_id="call-1",
            tool_name="request_custom_build",
            arguments={"reason": "custom code", "needed_capabilities": ["shell"]},
        ),
    )
    waiting = StatusEvent(
        seq=5,
        status=ConversationStatus.WAITING_FOR_CONFIRMATION,
        detail=action.id,
    )
    resumed = StatusEvent(
        seq=6,
        status=ConversationStatus.RUNNING,
        agent_view_id="view-1",
    )
    source = WorkspaceVersionEvent(
        seq=7,
        version_seq=2,
        tree_digest="a" * 64,
        trigger=APPKIT_EJECTION_SOURCE_TRIGGER,
    )
    target = WorkspaceVersionEvent(
        seq=8,
        version_seq=3,
        tree_digest="b" * 64,
        trigger=APPKIT_EJECTION_TARGET_TRIGGER,
    )
    ejection = AppKitEjectionEvent(
        seq=9,
        agent_view_id="view-1",
        action_id=action.id,
        tool_call_id=action.tool_call.call_id,
        source_version_seq=source.version_seq,
        source_tree_digest=source.tree_digest,
        ejected_version_seq=target.version_seq,
        ejected_tree_digest=target.tree_digest,
        lost_guarantees=APPKIT_EJECTION_LOST_GUARANTEES,
    )
    return [intent, initial, view, action, waiting, resumed, source, target, ejection]


def test_platform_admission_round_trips_and_survives_parked_compaction_history() -> None:
    admission = _platform()
    round_tripped = event_from_json_dict(event_to_json_dict(admission))
    assert round_tripped == admission
    events = [
        _intent(),
        round_tripped,
        StatusEvent(seq=3, status=ConversationStatus.PAUSED),
        ContextSummaryEvent(
            seq=4,
            range_id="range-1",
            rel_path=".disco/context/range-1.md",
            summary="completed work remains durable",
        ),
        WorkspaceMutationEvent(
            id="resume-intent",
            seq=5,
            operation="agent.run-intent.resume",
            run_protocol_version=1,
        ),
    ]
    assert current_build_platform_admission(events) == admission


@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.FINISHED,
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
        ConversationStatus.IDLE,
    ],
)
def test_terminal_conclusion_releases_route_for_a_later_run(
    status: ConversationStatus,
) -> None:
    events = [
        _intent(),
        _platform(),
        StatusEvent(seq=3, status=status),
        WorkspaceMutationEvent(
            id="later",
            seq=4,
            operation="agent.run-intent.message",
            run_protocol_version=1,
        ),
    ]
    assert current_build_platform_admission(events) is None


def test_legacy_and_platform_identity_shapes_fail_closed() -> None:
    legacy = BuildPlatformAdmissionEvent(
        route="legacy",
        profile_id="disco.freeform_web@1",
        run_intent_id="intent",
        composition_authority="legacy",
    )
    assert legacy.composition_digest is None and legacy.run_identity is None
    with pytest.raises(ValueError, match="requires composition"):
        BuildPlatformAdmissionEvent(
            route="platform",
            profile_id="disco.freeform_web@1",
            run_intent_id="intent",
            composition_authority="build_platform_core",
        )
    with pytest.raises(ValueError, match="cannot claim"):
        BuildPlatformAdmissionEvent(
            route="legacy",
            profile_id="disco.freeform_web@1",
            run_intent_id="intent",
            composition_authority="legacy",
            composition_digest=_DIGEST,
            run_identity=_RUN_ID,
        )


def test_appkit_ejection_and_superseding_freeform_admission_round_trip() -> None:
    lifecycle = _ejection_lifecycle()
    ejection = lifecycle[-1]
    assert isinstance(ejection, AppKitEjectionEvent)
    restored = event_from_json_dict(event_to_json_dict(ejection))
    assert restored == ejection
    assert current_appkit_ejection([*lifecycle[:-1], restored]) == ejection

    replacement = BuildPlatformAdmissionEvent(
        seq=10,
        route="platform",
        profile_id="disco.freeform_web@1",
        run_intent_id="intent",
        composition_authority="build_platform_core",
        composition_digest=_DIGEST,
        run_identity=_RUN_ID,
        transition="appkit_ejection",
        supersedes_admission_id="appkit-admission",
    )
    assert current_build_platform_admission([*lifecycle, replacement]) == replacement


def test_appkit_ejection_authority_requires_complete_guarantees_and_causal_evidence() -> None:
    with pytest.raises(ValueError, match="complete lost-guarantee"):
        AppKitEjectionEvent(
            agent_view_id="view-1",
            action_id="action-1",
            tool_call_id="call-1",
            source_version_seq=2,
            source_tree_digest="a" * 64,
            ejected_version_seq=3,
            ejected_tree_digest="b" * 64,
            lost_guarantees=("one omitted subset",),
        )

    lifecycle = _ejection_lifecycle()
    without_resume = [
        event
        for event in lifecycle
        if not (isinstance(event, StatusEvent) and event.status is ConversationStatus.RUNNING)
    ]
    assert current_appkit_ejection(without_resume) is None
    newer_intent = WorkspaceMutationEvent(
        seq=9,
        operation="agent.run-intent.user-message",
        run_protocol_version=1,
    )
    replayed = lifecycle[-1].model_copy(update={"seq": 10})
    assert current_appkit_ejection([*lifecycle[:-1], newer_intent, replayed]) is None


def test_appkit_ejection_admission_cannot_hide_the_transition_boundary() -> None:
    with pytest.raises(ValueError, match="must supersede"):
        BuildPlatformAdmissionEvent(
            route="legacy",
            profile_id="disco.freeform_web@1",
            run_intent_id="intent",
            composition_authority="legacy",
            transition="appkit_ejection",
        )
    with pytest.raises(ValueError, match="cannot supersede"):
        BuildPlatformAdmissionEvent(
            route="legacy",
            profile_id="disco.freeform_web@1",
            run_intent_id="intent",
            composition_authority="legacy",
            supersedes_admission_id="unexpected",
        )

    initial = BuildPlatformAdmissionEvent(
        id="appkit-admission",
        seq=2,
        route="legacy",
        profile_id="disco.appkit_web@1",
        run_intent_id="intent",
        composition_authority="legacy",
    )
    unbound = BuildPlatformAdmissionEvent(
        seq=3,
        route="legacy",
        profile_id="disco.freeform_web@1",
        run_intent_id="intent",
        composition_authority="legacy",
        transition="appkit_ejection",
        supersedes_admission_id=initial.id,
    )
    assert current_build_platform_admission([_intent(), initial, unbound]) == initial

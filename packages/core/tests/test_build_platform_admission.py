from __future__ import annotations

import pytest
from disco.core import (
    BuildPlatformAdmissionEvent,
    ContextSummaryEvent,
    ConversationStatus,
    StatusEvent,
    WorkspaceMutationEvent,
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

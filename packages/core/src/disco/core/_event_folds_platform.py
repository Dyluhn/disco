"""Platform folds — build-platform admission and AppKit ejection projections.

Pure folds over the ordered event log that derive the current route pin and
the current confirmed AppKit ejection.  These import concrete private event
modules, not ``events.py``, so there is exactly one fold implementation.
"""

from __future__ import annotations

from collections.abc import Iterable

from ._event_control import (
    AppKitEjectionEvent,
    BuildPlatformAdmissionEvent,
    StatusEvent,
    WorkspaceMutationEvent,
    WorkspaceVersionEvent,
)
from ._event_interaction import ActionEvent, MessageEvent
from ._event_types import (
    APPKIT_EJECTION_SOURCE_TRIGGER,
    APPKIT_EJECTION_TARGET_TRIGGER,
    BaseEvent,
    ConversationStatus,
    EventSource,
)

# The discriminated serialization union remains solely in events.py. Folds need
# only the common event envelope while retaining their historical annotations.
Event = BaseEvent


def active_verification_requirements_event(
    events: Iterable[Event],
) -> MessageEvent | None:
    """Fold the latest causally valid complete user/scenario requirement snapshot."""

    active: MessageEvent | None = None
    for event in events:
        if (
            not isinstance(event, MessageEvent)
            or event.source is not EventSource.USER
            or event.verification_requirements is None
        ):
            continue
        expected = active.id if active is not None else None
        if event.verification_requirements.supersedes_event_id == expected:
            active = event
    return active


def _admissions_for_intent(
    admissions: list[BuildPlatformAdmissionEvent],
    candidate: BuildPlatformAdmissionEvent,
) -> list[BuildPlatformAdmissionEvent]:
    """Prior admissions sharing the candidate's run intent, before its seq."""
    assert isinstance(candidate.seq, int)
    return [
        event
        for event in admissions
        if isinstance(event.seq, int)
        and event.seq < candidate.seq
        and event.run_intent_id == candidate.run_intent_id
    ]


def _ejection_intent(
    materialized: list[Event],
    ejection: AppKitEjectionEvent,
) -> WorkspaceMutationEvent | None:
    """The newest run-intent mutation preceding an ejection."""
    assert isinstance(ejection.seq, int)
    return max(
        (
            event
            for event in materialized
            if isinstance(event, WorkspaceMutationEvent)
            and isinstance(event.seq, int)
            and event.seq < ejection.seq
            and event.operation.startswith("agent.run-intent.")
        ),
        key=lambda event: event.seq or -1,
        default=None,
    )


def _ejection_action(
    materialized: list[Event],
    ejection: AppKitEjectionEvent,
) -> ActionEvent | None:
    """The request_custom_build action that triggered an ejection."""
    assert isinstance(ejection.seq, int)
    return next(
        (
            event
            for event in materialized
            if isinstance(event, ActionEvent)
            and isinstance(event.seq, int)
            and event.seq < ejection.seq
            and event.id == ejection.action_id
            and event.agent_view_id == ejection.agent_view_id
            and event.tool_call.call_id == ejection.tool_call_id
            and event.tool_call.tool_name == "request_custom_build"
        ),
        None,
    )


def _ejection_admission(
    materialized: list[Event],
    intent: WorkspaceMutationEvent | None,
    action: ActionEvent,
) -> WorkspaceMutationEvent | None:
    """The view-admitted mutation between the intent and the triggering action."""
    assert isinstance(action.seq, int)
    if intent is None or not isinstance(intent.seq, int):
        return None
    return next(
        (
            event
            for event in materialized
            if isinstance(event, WorkspaceMutationEvent)
            and isinstance(event.seq, int)
            and intent.seq < event.seq < action.seq
            and event.operation == "agent.view-admitted"
            and event.run_intent_id == intent.id
            and event.agent_view_id == action.agent_view_id
        ),
        None,
    )


def _ejection_waiting_status(
    materialized: list[Event],
    action: ActionEvent,
    ejection: AppKitEjectionEvent,
) -> StatusEvent | None:
    """The WAITING_FOR_CONFIRMATION status gating the ejection's action."""
    assert isinstance(action.seq, int)
    assert isinstance(ejection.seq, int)
    return next(
        (
            event
            for event in materialized
            if isinstance(event, StatusEvent)
            and isinstance(event.seq, int)
            and action.seq < event.seq < ejection.seq
            and event.status is ConversationStatus.WAITING_FOR_CONFIRMATION
            and event.detail == action.id
        ),
        None,
    )


def _ejection_resumed_status(
    materialized: list[Event],
    waiting: StatusEvent,
    action: ActionEvent,
    ejection: AppKitEjectionEvent,
) -> StatusEvent | None:
    """The RUNNING status that resumed after the confirmation gate."""
    assert isinstance(waiting.seq, int)
    assert isinstance(ejection.seq, int)
    return next(
        (
            event
            for event in materialized
            if isinstance(event, StatusEvent)
            and isinstance(event.seq, int)
            and waiting.seq < event.seq < ejection.seq
            and event.status is ConversationStatus.RUNNING
            and event.agent_view_id == action.agent_view_id
        ),
        None,
    )


def _ejection_source_version(
    materialized: list[Event],
    resumed: StatusEvent,
    ejection: AppKitEjectionEvent,
) -> WorkspaceVersionEvent | None:
    """The source workspace version event for the ejection."""
    assert isinstance(resumed.seq, int)
    assert isinstance(ejection.seq, int)
    return next(
        (
            event
            for event in materialized
            if isinstance(event, WorkspaceVersionEvent)
            and isinstance(event.seq, int)
            and resumed.seq < event.seq < ejection.seq
            and event.trigger == APPKIT_EJECTION_SOURCE_TRIGGER
            and event.version_seq == ejection.source_version_seq
            and event.tree_digest == ejection.source_tree_digest
        ),
        None,
    )


def _ejection_target_version(
    materialized: list[Event],
    source: WorkspaceVersionEvent,
    ejection: AppKitEjectionEvent,
) -> WorkspaceVersionEvent | None:
    """The target workspace version event for the ejection."""
    assert isinstance(source.seq, int)
    assert isinstance(ejection.seq, int)
    return next(
        (
            event
            for event in materialized
            if isinstance(event, WorkspaceVersionEvent)
            and isinstance(event.seq, int)
            and source.seq < event.seq < ejection.seq
            and event.trigger == APPKIT_EJECTION_TARGET_TRIGGER
            and event.version_seq == ejection.ejected_version_seq
            and event.tree_digest == ejection.ejected_tree_digest
        ),
        None,
    )


def _ejection_is_confirmed(
    materialized: list[Event],
    ejection: AppKitEjectionEvent,
) -> bool:
    """Whether an ejection has the full confirmed chain of preceding events."""
    action = _ejection_action(materialized, ejection)
    if action is None or not isinstance(action.seq, int):
        return False
    intent = _ejection_intent(materialized, ejection)
    admission = _ejection_admission(materialized, intent, action)
    if admission is None:
        return False
    waiting = _ejection_waiting_status(materialized, action, ejection)
    if waiting is None:
        return False
    resumed = _ejection_resumed_status(materialized, waiting, action, ejection)
    if resumed is None:
        return False
    source = _ejection_source_version(materialized, resumed, ejection)
    if source is None:
        return False
    target = _ejection_target_version(materialized, source, ejection)
    return target is not None


def current_appkit_ejection(events: Iterable[Event]) -> AppKitEjectionEvent | None:
    """Fold a fully confirmed and revision-bound AppKit ejection, if any."""

    materialized = list(events)
    candidates = sorted(
        (
            event
            for event in materialized
            if isinstance(event, AppKitEjectionEvent) and type(event.seq) is int
        ),
        key=lambda event: event.seq or -1,
        reverse=True,
    )
    for ejection in candidates:
        assert isinstance(ejection.seq, int)
        if _ejection_is_confirmed(materialized, ejection):
            return ejection
    return None


def _resolve_appkit_ejection_admission(
    materialized: list[Event],
    candidate: BuildPlatformAdmissionEvent,
    prior_for_intent: list[BuildPlatformAdmissionEvent],
) -> BuildPlatformAdmissionEvent | None:
    """Resolve an appkit_ejection transition admission against its superseded prior."""
    superseded = _superseded_appkit_admission(candidate, prior_for_intent)
    ejection = current_appkit_ejection(materialized)
    action = _appkit_ejection_action(materialized, ejection)
    intent = _run_intent_before_action(materialized, action)
    if _ejection_admission_chain_matches(candidate, superseded, ejection, intent):
        return candidate
    return None


def _resolve_delivery_selection_admission(
    candidate: BuildPlatformAdmissionEvent,
    superseded: BuildPlatformAdmissionEvent | None,
) -> BuildPlatformAdmissionEvent | None:
    """Accept only a host-owned Freeform target replacement in the same run."""

    freeform_profiles = {"disco.freeform_web@1", "disco.freeform_artifact@1"}
    if (
        superseded is not None
        and superseded.id == candidate.supersedes_admission_id
        and superseded.run_intent_id == candidate.run_intent_id
        and isinstance(superseded.seq, int)
        and isinstance(candidate.seq, int)
        and superseded.seq < candidate.seq
        and superseded.route == candidate.route == "platform"
        and superseded.profile_id in freeform_profiles
        and candidate.profile_id in freeform_profiles
    ):
        return candidate
    return None


def _superseded_appkit_admission(
    candidate: BuildPlatformAdmissionEvent,
    prior_for_intent: list[BuildPlatformAdmissionEvent],
) -> BuildPlatformAdmissionEvent | None:
    return next(
        (
            event
            for event in prior_for_intent
            if event.id == candidate.supersedes_admission_id
            and event.profile_id == "disco.appkit_web@1"
        ),
        None,
    )


def _appkit_ejection_action(
    materialized: list[Event],
    ejection: AppKitEjectionEvent | None,
) -> ActionEvent | None:
    return next(
        (
            event
            for event in materialized
            if ejection is not None
            and isinstance(event, ActionEvent)
            and event.id == ejection.action_id
        ),
        None,
    )


def _run_intent_before_action(
    materialized: list[Event],
    action: ActionEvent | None,
) -> WorkspaceMutationEvent | None:
    return max(
        (
            event
            for event in materialized
            if action is not None
            and isinstance(action.seq, int)
            and isinstance(event, WorkspaceMutationEvent)
            and isinstance(event.seq, int)
            and event.seq < action.seq
            and event.operation.startswith("agent.run-intent.")
        ),
        key=lambda event: event.seq or -1,
        default=None,
    )


def _ejection_admission_chain_matches(
    candidate: BuildPlatformAdmissionEvent,
    superseded: BuildPlatformAdmissionEvent | None,
    ejection: AppKitEjectionEvent | None,
    intent: WorkspaceMutationEvent | None,
) -> bool:
    if (
        superseded is not None
        and isinstance(superseded.seq, int)
        and ejection is not None
        and isinstance(ejection.seq, int)
        and isinstance(candidate.seq, int)
        and intent is not None
        and intent.id == candidate.run_intent_id
        and superseded.seq < ejection.seq < candidate.seq
    ):
        return True
    return False


def _latest_valid_platform_admission(
    materialized: list[Event],
    admissions: list[BuildPlatformAdmissionEvent],
) -> BuildPlatformAdmissionEvent | None:
    latest: BuildPlatformAdmissionEvent | None = None
    for candidate in admissions:
        prior_for_intent = _admissions_for_intent(admissions, candidate)
        if candidate.transition == "initial":
            if not prior_for_intent:
                latest = candidate
            continue
        resolved = (
            _resolve_delivery_selection_admission(candidate, latest)
            if candidate.transition == "delivery_selection"
            else _resolve_appkit_ejection_admission(
                materialized,
                candidate,
                prior_for_intent,
            )
        )
        if resolved is not None:
            latest = resolved
    return latest


def _terminal_after_admission(
    materialized: list[Event],
    admission_seq: int,
) -> bool:
    terminal = {
        ConversationStatus.FINISHED,
        ConversationStatus.ERROR,
        ConversationStatus.STUCK,
        ConversationStatus.IDLE,
    }
    return any(
        isinstance(event, StatusEvent)
        and type(event.seq) is int
        and event.seq > admission_seq
        and event.status in terminal
        for event in materialized
    )


def current_build_platform_admission(
    events: Iterable[Event],
) -> BuildPlatformAdmissionEvent | None:
    """Fold the route pin for the current non-terminal Build segment.

    PAUSED and user/approval gates retain the pin. A genuine terminal status
    releases it, so a later run may apply the current rollout switch.
    """

    materialized = list(events)
    admissions = sorted(
        (
            event
            for event in materialized
            if isinstance(event, BuildPlatformAdmissionEvent) and type(event.seq) is int
        ),
        key=lambda event: event.seq or -1,
    )
    latest = _latest_valid_platform_admission(materialized, admissions)
    if latest is None or type(latest.seq) is not int:
        return None
    if _terminal_after_admission(materialized, latest.seq):
        return None
    return latest


__all__ = [
    "active_verification_requirements_event",
    "current_appkit_ejection",
    "current_build_platform_admission",
]

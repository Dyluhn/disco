"""Planning-related signal functions — split from ``loop/signals.py``.

Pure, stateless log-derived signal functions for plan approval, verifier
repair, revision planning, and plan-step lag.  Every function is a pure
projection of an event list (or a View) to a verdict, with no instance state,
no emission, and no I/O.
"""

from __future__ import annotations

from ..dod import predicate_fingerprints
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)
from ..view import effective_plan_progress

# Re-export the planning-segment and prose-plan functions from plan_validation.
from .plan_validation import (  # noqa: F401 — re-exported
    _approved_plan_at,
    _done_condition_identities,
    _latest_approval_status,
    _plan_approval_seqs,
    _plan_by_legacy_approval,
    _plan_by_transition,
    _step_is_finish_intent,
    current_blocked_question_landing,
    current_planning_segment_start_seq,
    current_revision_instruction,
    effective_mode,
    harvest_prose_plan_steps,
    in_planning_for_revision,
    latest_agent_prose_message,
    latest_prose_plan_message,
    latest_prose_plan_summary,
    latest_user_answers_blocked_question,
    latest_user_text,
    next_plan_revision,
    pending_revision_steer,
    plan_nudges_since_current_planning,
    plan_submitted_since_current_planning,
    prose_plan_force_submit,
    prose_plan_harvested,
    prose_plan_steps_from_text,
    revision_force_submit,
    revision_planning_has_prior_approval,
    strip_element_mention,
)
from .plan_validation import (
    _harvested_revision_plan_from_user as plan_validation_harvested_revision_plan_from_user,
)

# Re-exported constants used by the planning signals.
# _FINISH_INTENT_RE, _ACTIONABLE_BUILD_VERB_RE, and _step_is_finish_intent
# are now in plan_validation.py.

APPROVED_PLAN_VERIFIER_REPAIR = "approved_plan_verifier_repair"

# Shared constants and helpers — defined here to avoid a circular import.
# signals.py re-exports these for backward compatibility.
_BOOKKEEPING_TOOLS = frozenset(
    {
        "submit_plan",
        "propose_plan_update",
        "plan_step",
        "update_plan_progress",
        "finish",
        "think",
    }
)

_NON_PRODUCTIVE_TOOLS = frozenset(
    {
        "submit_plan",
        "think",
        "plan_step",
        "update_plan_progress",
        "ask_user",
        "questions_v2",
        "propose_plan_update",
        "notify_user",
        "finish",
        "remember",
        "serve",
        "file_read",
        "file_list",
        "search",
        "extract",
        "server_status",
        "preview_start",
        "preview_status",
        "preview_logs",
        "preview_stop",
        "shell_view",
        "shell_wait",
        "browser",
        "verify_web_app",
        "verify_appkit_app",
        "design_lint",
        "app_snapshot_version",
    }
)

_SYNTHETIC_FINISH_RESET_STATUSES = frozenset(
    {
        ConversationStatus.IDLE,
        ConversationStatus.FINISHED,
        ConversationStatus.STUCK,
        ConversationStatus.ERROR,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.AWAITING_USER_QUESTION,
    }
)


def _event_seq(event: Event, fallback: int) -> int:
    return event.seq if event.seq is not None else fallback


def _successful_action_ids(events: list[Event]) -> set[str]:
    """Action ids whose paired result is a successful ObservationEvent."""
    succeeded: dict[str, bool] = {}
    for e in events:
        if isinstance(e, ObservationEvent):
            action_id = e.action_id
            if isinstance(action_id, str):
                succeeded.setdefault(action_id, bool(e.tool_result.success))
        elif isinstance(e, AgentErrorEvent) and e.action_id is not None:
            succeeded.setdefault(e.action_id, False)
    return {action_id for action_id, ok in succeeded.items() if ok}


def _paired_action_ids(events: list[Event]) -> set[str]:
    """Action ids with any paired ObservationEvent or AgentErrorEvent."""
    paired: set[str] = set()
    for event in events:
        if isinstance(event, ObservationEvent):
            action_id = event.action_id
            if isinstance(action_id, str):
                paired.add(action_id)
        elif isinstance(event, AgentErrorEvent) and event.action_id is not None:
            paired.add(event.action_id)
    return paired


def _is_successful_productive_action(event: Event, successful_actions: set[str]) -> bool:
    if not isinstance(event, ActionEvent) or event.tool_call is None:
        return False
    if event.meta.get("verify_probe"):
        return False
    return event.id in successful_actions and event.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS


def successful_action_ids(events: list[Event]) -> set[str]:
    """Public alias for the paired-success scan (runtime ladder guards use it)."""
    return _successful_action_ids(events)


def is_successful_productive_action(event: Event, successful: set[str]) -> bool:
    """Public alias — True iff `event` is a successful PRODUCTIVE action."""
    return _is_successful_productive_action(event, successful)


# _plan_approval_seqs and _latest_approval_status are now in plan_validation.py.


# _plan_by_transition and _plan_by_legacy_approval are now in plan_validation.py.


def latest_approved_plan(events: list[Event]) -> PlanEvent | None:
    """Return the PlanEvent selected by the latest durable approval marker.

    New approvals bind an exact event id in ``plan_verification_transition``.
    Legacy approvals are reconstructed as the latest PlanEvent preceding the
    marker, preserving restart compatibility without treating a newer,
    unapproved proposal as current authority state.
    """
    approval = _latest_approval_status(events)
    if approval is None:
        return None
    transition = approval.plan_verification_transition
    if transition is not None:
        return _plan_by_transition(events, approval)
    return _plan_by_legacy_approval(events, approval)


def _verifier_repair_failure_filter(
    events: list[Event],
    old_plan: PlanEvent,
    plan_seq: int,
    approval_seq: int,
) -> list[StatusEvent]:
    """Collect plan-owned verifier failures between approval and the new plan."""
    old_fingerprints = predicate_fingerprints(
        [step.done_condition for step in old_plan.steps if step.done_condition is not None]
    )
    return [
        event
        for event in events
        if isinstance(event, StatusEvent)
        and event.plan_verifier_failure is not None
        and approval_seq < (event.seq or 0) < plan_seq
        and event.plan_verifier_failure.plan_event_id == old_plan.id
        and event.plan_verifier_failure.plan_revision == old_plan.revision
        and event.plan_verifier_failure.predicate_fingerprints == old_fingerprints
    ]


def _verifier_repair_user_check(
    events: list[Event], failure_seq: int, plan_seq: int
) -> bool:
    """True if a USER message arrived between the failure and the new plan."""
    return any(
        isinstance(event, MessageEvent)
        and event.source == EventSource.USER
        and failure_seq < (event.seq or 0) < plan_seq
        for event in events
    )


def _verifier_repair_planning_check(
    events: list[Event], failure_seq: int, plan_seq: int
) -> bool:
    """True if a ``planning`` marker exists between the failure and the new plan."""
    return any(
        isinstance(event, StatusEvent)
        and event.detail == "planning"
        and failure_seq < (event.seq or 0) < plan_seq
        for event in events
    )


def plan_is_verifier_repair(events: list[Event], plan: PlanEvent) -> bool:
    """Whether ``plan`` is a correction of the current plan-owned verifier only."""
    old_plan = latest_approved_plan(events)
    if old_plan is None:
        return False
    persisted_plan = _find_persisted_plan(events, plan)
    if persisted_plan is None or (persisted_plan.seq or 0) <= 0:
        return False
    if not _plan_fingerprints_match(persisted_plan, plan):
        return False
    plan_seq = persisted_plan.seq or 0
    approval = _latest_approval_before(events, plan_seq)
    if approval is None:
        return False
    failure_event = _find_latest_failure(events, old_plan, plan_seq, approval.seq or 0)
    if failure_event is None:
        return False
    failure = failure_event.plan_verifier_failure
    assert failure is not None
    if not _verifier_repair_causal_checks(events, failure, failure_event.seq or 0, plan_seq, plan):
        return False
    return _has_productive_work_between(events, approval.seq or 0, failure_event.seq or 0)


def _has_productive_work_between(
    events: list[Event], approval_seq: int, failure_seq: int
) -> bool:
    """True if successful productive work happened between approval and failure."""
    successful = _successful_action_ids(events)
    return any(
        approval_seq < (event.seq or 0) < failure_seq
        and _is_successful_productive_action(event, successful)
        for event in events
    )


def _find_latest_failure(
    events: list[Event], old_plan: PlanEvent, plan_seq: int, approval_seq: int
) -> StatusEvent | None:
    """Find the latest qualifying verifier failure between approval and plan."""
    failures = _verifier_repair_failure_filter(events, old_plan, plan_seq, approval_seq)
    if not failures:
        return None
    return max(failures, key=lambda event: event.seq or 0)


def _verifier_repair_causal_checks(
    events: list[Event],
    failure: object,
    failure_seq: int,
    plan_seq: int,
    plan: PlanEvent,
) -> bool:
    """Run the causal checks for verifier repair classification."""
    if not _verifier_repair_failure_valid(failure):
        return False
    if not _verifier_repair_planning_check(events, failure_seq, plan_seq):
        return False
    if _verifier_repair_user_check(events, failure_seq, plan_seq):
        return False
    if _plan_fingerprints_unchanged(plan, failure.predicate_fingerprints):  # type: ignore[attr-defined]
        return False
    return True


def _find_persisted_plan(events: list[Event], plan: PlanEvent) -> PlanEvent | None:
    """Find the persisted copy of ``plan`` in the event log."""
    return next(
        (
            event
            for event in reversed(events)
            if isinstance(event, PlanEvent)
            and event.id == plan.id
            and event.revision == plan.revision
        ),
        None,
    )


def _plan_fingerprints_match(persisted: PlanEvent, plan: PlanEvent) -> bool:
    """Whether the persisted plan and the candidate have matching fingerprints."""
    return predicate_fingerprints(
        [step.done_condition for step in persisted.steps if step.done_condition is not None]
    ) == predicate_fingerprints(
        [step.done_condition for step in plan.steps if step.done_condition is not None]
    )


def _latest_approval_before(events: list[Event], plan_seq: int) -> StatusEvent | None:
    """The latest ``plan_approved`` marker before ``plan_seq``."""
    return max(
        (
            event
            for event in events
            if isinstance(event, StatusEvent)
            and event.detail == "plan_approved"
            and (event.seq or 0) < plan_seq
        ),
        key=lambda event: event.seq or 0,
        default=None,
    )


def _verifier_repair_failure_valid(failure: object) -> bool:
    """Whether a plan verifier failure qualifies for repair."""
    return failure.attempt_for_approved_plan >= 2 and failure.replan_allowed  # type: ignore[attr-defined]


def _plan_fingerprints_unchanged(plan: PlanEvent, failure_fingerprints: object) -> bool:
    """Whether the new plan's fingerprints match the failed verifier's."""
    new_fingerprints = predicate_fingerprints(
        [step.done_condition for step in plan.steps if step.done_condition is not None]
    )
    return new_fingerprints == failure_fingerprints


def approved_plan_verifier_repair_event(events: list[Event]) -> StatusEvent | None:
    """Return the latest exact, independently reconstructable repair approval."""
    approval = _latest_approval_status(events)
    if approval is None or approval.plan_verification_transition is None:
        return None
    if approval.plan_verification_transition.reason != APPROVED_PLAN_VERIFIER_REPAIR:
        return None
    plan = latest_approved_plan(events)
    if plan is None:
        return None
    prefix = [event for event in events if (event.seq or 0) < (approval.seq or 0)]
    return approval if plan_is_verifier_repair(prefix, plan) else None


def superseded_plan_owned_failure(events: list[Event]) -> frozenset[str]:
    """Message ids for plan-owned verifier directives causally replaced by approval."""
    retired: set[str] = set()
    failure_messages = {"plan_verifier_failed", "plan_verifier_replan_required"}
    for message_index, message in enumerate(events):
        if not isinstance(message, MessageEvent) or not _is_failure_message(
            message, failure_messages
        ):
            continue
        _process_failure_message(events, message_index, message, retired)
    return frozenset(retired)


def _is_failure_message(message: Event, failure_messages: set[str]) -> bool:
    """Whether ``message`` is an ENVIRONMENT failure directive."""
    return (
        isinstance(message, MessageEvent)
        and message.source == EventSource.ENVIRONMENT
        and message.meta.get("blocking") in failure_messages
    )


def _process_failure_message(
    events: list[Event],
    message_index: int,
    message: MessageEvent,
    retired: set[str],
) -> None:
    """Process one failure message for potential approval replacement."""
    expected_failure_fingerprint = message.meta.get("failure_fingerprint")
    failure_event = _find_matching_failure(events, message_index, expected_failure_fingerprint)
    if failure_event is None or failure_event.plan_verifier_failure is None:
        return
    failure = failure_event.plan_verifier_failure
    failure_fingerprints = set(failure.failed_predicate_fingerprints)
    if not failure_fingerprints:
        return
    failed_plan = _find_failed_plan(events, message_index, failure, failure_event)
    if failed_plan is None or not _failed_plan_fingerprints_match(failed_plan, failure):
        return
    _check_approval_replacement(events, message_index, failure, failure_fingerprints, retired)


def _find_matching_failure(
    events: list[Event], message_index: int, expected_fingerprint: object
) -> StatusEvent | None:
    """Find the StatusEvent with a plan_verifier_failure matching the message."""
    return next(
        (
            event
            for event in reversed(events[:message_index])
            if isinstance(event, StatusEvent)
            and event.plan_verifier_failure is not None
            and (
                expected_fingerprint is None
                or event.plan_verifier_failure.failure_fingerprint == expected_fingerprint
            )
        ),
        None,
    )


def _find_failed_plan(
    events: list[Event], message_index: int, failure: object, failure_event: StatusEvent
) -> PlanEvent | None:
    """Find the PlanEvent that the failure refers to."""
    return next(
        (
            event
            for event in events[:message_index]
            if isinstance(event, PlanEvent)
            and event.id == failure.plan_event_id  # type: ignore[attr-defined]
            and event.revision == failure.plan_revision  # type: ignore[attr-defined]
            and (event.seq or 0) < (failure_event.seq or 0)
        ),
        None,
    )


def _failed_plan_fingerprints_match(failed_plan: PlanEvent, failure: object) -> bool:
    """Whether the failed plan's fingerprints match the failure's."""
    return predicate_fingerprints(
        [step.done_condition for step in failed_plan.steps if step.done_condition is not None]
    ) == list(failure.predicate_fingerprints)  # type: ignore[attr-defined]


def _check_approval_replacement(
    events: list[Event],
    message_index: int,
    failure: object,
    failure_fingerprints: set[str],
    retired: set[str],
) -> None:
    """Check whether a later approval replaces the failed plan's directives."""
    for approval in events[message_index + 1 :]:
        if not isinstance(approval, StatusEvent) or not _is_approval_with_transition(approval):
            continue
        transition = approval.plan_verification_transition
        if transition is None:
            continue
        if not _transition_matches_failure(transition, failure, failure_fingerprints):
            continue
        replacement = _find_replacement_plan(events, transition, approval)
        if replacement is None or not _replacement_fingerprints_match(replacement, transition):
            continue
        retired.add(events[message_index].id)
        break


def _is_approval_with_transition(event: Event) -> bool:
    """Whether ``event`` is a ``plan_approved`` StatusEvent with a transition."""
    return (
        isinstance(event, StatusEvent)
        and event.detail == "plan_approved"
        and event.plan_verification_transition is not None
    )


def _transition_matches_failure(
    transition: object, failure: object, failure_fingerprints: set[str]
) -> bool:
    """Whether a transition's old/new fingerprints match the failure."""
    if (
        transition.old_plan_event_id != failure.plan_event_id  # type: ignore[attr-defined]
        or transition.old_plan_revision != failure.plan_revision  # type: ignore[attr-defined]
        or list(transition.old_predicate_fingerprints)  # type: ignore[attr-defined]
        != list(failure.predicate_fingerprints)  # type: ignore[attr-defined]
        or not failure_fingerprints.issubset(set(transition.old_predicate_fingerprints))  # type: ignore[attr-defined]
        or not failure_fingerprints.isdisjoint(set(transition.new_predicate_fingerprints))  # type: ignore[attr-defined]
    ):
        return False
    return True


def _find_replacement_plan(
    events: list[Event], transition: object, approval: StatusEvent
) -> PlanEvent | None:
    """Find the replacement PlanEvent bound by the transition."""
    return next(
        (
            event
            for event in events
            if isinstance(event, PlanEvent)
            and event.id == transition.new_plan_event_id  # type: ignore[attr-defined]
            and event.revision == transition.new_plan_revision  # type: ignore[attr-defined]
            and (event.seq or 0) < (approval.seq or 0)
        ),
        None,
    )


def _replacement_fingerprints_match(replacement: PlanEvent, transition: object) -> bool:
    """Whether the replacement plan's fingerprints match the transition's new set."""
    return predicate_fingerprints(
        [
            step.done_condition
            for step in replacement.steps
            if step.done_condition is not None
        ]
    ) == list(transition.new_predicate_fingerprints)  # type: ignore[attr-defined]


def verifier_repair_planning_active(events: list[Event]) -> bool:
    """True while a twice-failed plan verifier has forced a no-user replan."""
    old_plan = latest_approved_plan(events)
    if old_plan is None:
        return False
    old_fingerprints = predicate_fingerprints(
        [step.done_condition for step in old_plan.steps if step.done_condition is not None]
    )
    failure_event = _find_repairable_failure(events, old_plan, old_fingerprints)
    if failure_event is None:
        return False
    failure_seq = failure_event.seq or 0
    planning_seq = _latest_planning_after(events, failure_seq)
    if planning_seq <= failure_seq:
        return False
    if _has_user_message_after(events, failure_seq):
        return False
    return not _has_approval_after(events, planning_seq)


def _find_repairable_failure(
    events: list[Event], old_plan: PlanEvent, old_fingerprints: list[str]
) -> StatusEvent | None:
    """Find the latest twice-failed, replan-allowed verifier failure for old_plan."""
    return next(
        (
            event
            for event in reversed(events)
            if isinstance(event, StatusEvent)
            and event.plan_verifier_failure is not None
            and event.plan_verifier_failure.plan_event_id == old_plan.id
            and event.plan_verifier_failure.plan_revision == old_plan.revision
            and event.plan_verifier_failure.predicate_fingerprints == old_fingerprints
            and event.plan_verifier_failure.attempt_for_approved_plan >= 2
            and event.plan_verifier_failure.replan_allowed
        ),
        None,
    )


def _latest_planning_after(events: list[Event], after_seq: int) -> int:
    """Seq of the latest ``planning`` marker after ``after_seq``, or 0."""
    return max(
        (
            event.seq or 0
            for event in events
            if isinstance(event, StatusEvent)
            and event.detail == "planning"
            and (event.seq or 0) > after_seq
        ),
        default=0,
    )


def _has_user_message_after(events: list[Event], after_seq: int) -> bool:
    """True if a USER message exists after ``after_seq``."""
    return any(
        isinstance(event, MessageEvent)
        and event.source == EventSource.USER
        and (event.seq or 0) > after_seq
        for event in events
    )


def _has_approval_after(events: list[Event], after_seq: int) -> bool:
    """True if a ``plan_approved`` marker exists after ``after_seq``."""
    return any(
        isinstance(event, StatusEvent)
        and event.detail == "plan_approved"
        and (event.seq or 0) > after_seq
        for event in events
    )


def verifier_repair_execution_active(events: list[Event]) -> bool:
    """Keep verifier-only approval truth live until work/user/terminal supersedes it."""
    approval = approved_plan_verifier_repair_event(events)
    if approval is None:
        return False
    approval_seq = approval.seq or 0
    successful = _successful_action_ids(events)
    for event in events:
        if (event.seq or 0) <= approval_seq:
            continue
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            return False
        if isinstance(event, StatusEvent) and event.status in {
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
        }:
            return False
        if _is_successful_productive_action(event, successful):
            return False
    return True


# _step_is_finish_intent is now in plan_validation.py.


# _done_condition_identities and _approved_plan_at are now in plan_validation.py.


def finish_intent_replan_after_prior_productive_work(events: list[Event]) -> bool:
    """True for a re-plan that only tells the model to finish after earlier work."""
    approvals = _plan_approval_seqs(events)
    if len(approvals) < 2:
        return False
    previous_approval_seq = approvals[-2]
    latest_approval_seq = approvals[-1]

    if not _has_prior_productive_work(events, previous_approval_seq, latest_approval_seq):
        return False

    plan, states = effective_plan_progress(events)
    if plan is None or not plan.steps:
        return False
    plan_seq = plan.seq or 0
    if plan_seq > latest_approval_seq or plan_seq <= previous_approval_seq:
        return False

    remaining = [
        step for index, step in enumerate(plan.steps, start=1) if states.get(index) != "done"
    ]
    if bool(remaining) and all(_step_is_finish_intent(step) for step in remaining):
        return True

    return _finish_intent_replacement_route(events, plan, previous_approval_seq)


def _has_prior_productive_work(
    events: list[Event], previous_seq: int, latest_seq: int
) -> bool:
    """True if productive work happened between two approval markers."""
    successful_actions = _successful_action_ids(events)
    for event in events:
        seq = event.seq or 0
        if seq <= previous_seq or seq >= latest_seq:
            continue
        if _is_successful_productive_action(event, successful_actions):
            return True
    return False


def _finish_intent_replacement_route(
    events: list[Event], plan: PlanEvent, previous_approval_seq: int
) -> bool:
    """Second route: a replan that adds no new completion obligation."""
    previous_plan = _approved_plan_at(events, previous_approval_seq)
    if previous_plan is None:
        return False
    known = _done_condition_identities(previous_plan)
    introduced = _done_condition_identities(plan) - known
    return bool(known) and not introduced


def planning_turns_since_replan(events: list[Event]) -> int:
    """BW-01 follow-up 2 — count tool-less PLANNING turns spent since the build
    most recently (re-)entered planning.

    Returns 0 when NOT spinning in planning: no `planning` marker at all, or a
    later PlanEvent / plan_approved (the model DID submit/land a plan since
    re-entry — the auto-release case).
    """
    from .engine import _PLAN_NUDGE

    planning_seq: int | None = None
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "planning":
            planning_seq = e.seq or 0
            break
    if planning_seq is None:
        return 0
    return _count_planning_nudges(events, planning_seq, _PLAN_NUDGE)


def _is_plan_nudge_event(event: Event, plan_nudge: str) -> bool:
    """True for a planning nudge, by LABEL first and legacy content second.

    Constraint 4 made the nudge text repetition-aware, so byte-equality with the
    first firing can no longer be the identity test. Emissions carry a stable
    `diagnostic` label; the content comparison is retained so events already
    durable in logs written before 2026-08-07b (no label) still count exactly as
    they did. Router-phase nudges carry a DIFFERENT label and have never been
    counted here — that stays true.
    """
    from .engine_contracts import _PLAN_NUDGE_DIAGNOSTIC

    if not isinstance(event, MessageEvent) or event.source != EventSource.ENVIRONMENT:
        return False
    if event.meta.get("diagnostic") == _PLAN_NUDGE_DIAGNOSTIC:
        return True
    return event.message is not None and event.message.content == plan_nudge


def _count_planning_nudges(events: list[Event], planning_seq: int, plan_nudge: str) -> int:
    """Count ENVIRONMENT plan-nudge messages after ``planning_seq``."""
    count = 0
    for e in events:
        if (e.seq or 0) <= planning_seq:
            continue
        if isinstance(e, PlanEvent):
            return 0
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            return 0
        if _is_plan_nudge_event(e, plan_nudge):
            count += 1
    return count


def plan_step_lag_signal(events: list[Event]) -> bool:
    """The 'auditor' for the soft plan-step nudge. Returns True when the
    agent has done substantial productive work but the capstone tracker is
    clearly lagging — the classic 'did the work, forgot to check it off'
    failure.
    """
    plan: PlanEvent | None = None
    approval_seq: int | None = None
    for e in events:
        if isinstance(e, PlanEvent):
            if plan is None or e.revision >= plan.revision:
                plan = e
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            approval_seq = e.seq
    if plan is None or not plan.steps or approval_seq is None:
        return False
    total = len(plan.steps)

    productive, last_tracker_seq, last_lag_nudge_seq = _scan_lag_events(
        events, approval_seq, total
    )
    if productive < total:
        return False
    _, states = effective_plan_progress(events)
    done_count = sum(1 for i in range(1, total + 1) if states.get(i) == "done")
    if done_count * 2 >= total:
        return False
    if last_lag_nudge_seq > last_tracker_seq:
        return False
    return True


def _scan_lag_events(
    events: list[Event], approval_seq: int | None, total: int
) -> tuple[int, int, int]:
    """Scan events for productive work, tracker actions, and lag nudges."""
    productive = 0
    last_tracker_seq = -1
    last_lag_nudge_seq = -1
    for e in events:
        seq = e.seq or 0
        if approval_seq is not None and seq <= approval_seq:
            continue
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            name = e.tool_call.tool_name
            if name in ("plan_step", "update_plan_progress"):
                last_tracker_seq = seq
            elif name not in _NON_PRODUCTIVE_TOOLS:
                productive += 1
        elif (
            isinstance(e, MessageEvent)
            and e.source == EventSource.ENVIRONMENT
            and e.message is not None
            and "plan-step tracker" in e.message.content
        ):
            last_lag_nudge_seq = seq
    return productive, last_tracker_seq, last_lag_nudge_seq


# Planning-mode, revision-steer, and user-message functions are now in
# plan_validation.py and re-exported above.


def harvested_revision_plan_from_user(events: list[Event]) -> PlanEvent | None:
    """Build the one-step forced revision plan from the triggering user text.

    Compatibility wrapper that calls the parameterized form in plan_validation.
    """
    return plan_validation_harvested_revision_plan_from_user(
        events,
        in_planning=in_planning_for_revision(events),
        has_prior_approval=revision_planning_has_prior_approval(events),
        instruction=current_revision_instruction(events) or latest_user_text(events) or "",
        revision=next_plan_revision(events),
    )

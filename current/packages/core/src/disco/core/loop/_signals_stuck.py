"""Stuck-escape and actionless signal functions — split from ``loop/signals.py``.

Pure, stateless log-derived signal functions for stuck-escape quarantine,
actionless pause counting, consecutive noop counting, and fresh-read autoground
targeting.  Every function is a pure projection of an event list to a verdict,
with no instance state, no emission, and no I/O.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ConversationStatus,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
)

if TYPE_CHECKING:
    pass

# Re-exported constants used by the stuck signals.
from ._signals_planning import (  # noqa: F401 — re-exported
    _BOOKKEEPING_TOOLS,
    _NON_PRODUCTIVE_TOOLS,
    _SYNTHETIC_FINISH_RESET_STATUSES,
    _event_seq,
    _is_successful_productive_action,
    _successful_action_ids,
)

SYNTHETIC_FINISH_ATTEMPT_DETAIL = "synthetic_finish_attempted"
PROSE_NOOP_REPAIR_DIAGNOSTIC = "prose_noop_repair"
STUCK_ESCAPE_BLOCK_DETAIL_PREFIX = "stuck_escape_block:"
STUCK_ESCAPE_REFUSAL_ERROR_PREFIX = "stuck_escape_tool_quarantine:"
_STUCK_ESCAPE_BLOCKABLE_TOOLS = frozenset({"file_read"})

_AUTOGROUND_ERROR_CODES = frozenset({"FRESH_READ_REQUIRED", "bad_range", "bad_line"})

# Recovery boundaries end the current stuck-escape recovery episode exactly like
# a fresh user message.
_ESCAPE_RECOVERY_BOUNDARY_DETAILS = frozenset(
    {"plan_approved", "harvested_revision_plan", "alternative_picked:manual"}
)


def _ends_stuck_escape_episode(event: Event) -> bool:
    """Whether *event* closes the current stuck-escape recovery episode."""
    if isinstance(event, MessageEvent) and event.source == EventSource.USER:
        return True
    if (
        isinstance(event, StatusEvent)
        and event.source == EventSource.SYSTEM
        and (event.detail or "") in _ESCAPE_RECOVERY_BOUNDARY_DETAILS
    ):
        return True
    blocking = event.meta.get("blocking") if isinstance(event, MessageEvent) else None
    return (
        isinstance(event, MessageEvent)
        and event.source == EventSource.ENVIRONMENT
        and isinstance(blocking, str)
        and bool(blocking)
    )


def stuck_escape_seq(events: list[Event]) -> int | None:
    """The seq of the most recent `stuck_escape` marker in the CURRENT recovery
    episode, else None."""
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "stuck_escape":
            return e.seq
        if _ends_stuck_escape_episode(e):
            return None
    return None


def stuck_escape_blocked_tools(events: list[Event]) -> frozenset[str]:
    """Return host-typed tools quarantined for the current escape turn."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if _ends_stuck_escape_episode(event):
            return frozenset()
        if not (
            isinstance(event, StatusEvent)
            and event.source == EventSource.SYSTEM
            and event.status == ConversationStatus.RUNNING
            and event.detail == "stuck_escape"
        ):
            continue
        return _collect_blocked_tools(events, index)
    return frozenset()


def _collect_blocked_tools(events: list[Event], index: int) -> frozenset[str]:
    """Collect the blocked tool names immediately preceding the escape marker."""
    blocked: set[str] = set()
    cursor = index - 1
    while cursor >= 0:
        marker = events[cursor]
        if not (
            isinstance(marker, StatusEvent)
            and marker.source == EventSource.SYSTEM
            and marker.status == ConversationStatus.RUNNING
            and isinstance(marker.detail, str)
            and marker.detail.startswith(STUCK_ESCAPE_BLOCK_DETAIL_PREFIX)
        ):
            break
        tool_name = marker.detail.removeprefix(STUCK_ESCAPE_BLOCK_DETAIL_PREFIX)
        if (
            re.fullmatch(r"[a-z][a-z0-9_]*", tool_name) is None
            or tool_name not in _STUCK_ESCAPE_BLOCKABLE_TOOLS
        ):
            return frozenset()
        blocked.add(tool_name)
        cursor -= 1
    return frozenset(blocked)


def stuck_escape_attempt_count(events: list[Event]) -> int:
    """C7 — total number of `stuck_escape` markers emitted so far."""
    n = 0
    for e in events:
        if isinstance(e, StatusEvent) and e.detail == "stuck_escape":
            n += 1
    return n


def actionless_pause_count_current_execution_segment(events: list[Event]) -> int:
    """REL-RC-P: consecutive actionless PAUSE landings in the current execution
    segment.
    """
    indexed = [(idx, event) for idx, event in enumerate(events, start=1)]
    approval_seq: int | None = None
    for idx, event in indexed:
        if isinstance(event, StatusEvent) and event.detail == "plan_approved":
            approval_seq = _event_seq(event, idx)
    if approval_seq is None:
        return 0

    successful_actions = _successful_action_ids(events)
    count = 0
    for idx, event in reversed(indexed):
        seq = _event_seq(event, idx)
        if seq <= approval_seq:
            break
        if _is_successful_productive_action(event, successful_actions):
            break
        if not isinstance(event, StatusEvent):
            continue
        result = _actionless_status_step(event)
        if result == "count":
            count += 1
        elif result == "continue":
            continue
        else:
            break
    return count


def _actionless_status_step(event: StatusEvent) -> str:
    """Classify one StatusEvent in the actionless backward walk."""
    if event.status == ConversationStatus.PAUSED and event.detail == "actionless":
        return "count"
    if event.status == ConversationStatus.RUNNING:
        return "continue"
    if event.status in _SYNTHETIC_FINISH_RESET_STATUSES:
        if (getattr(event, "meta", None) or {}).get("blocked_landing"):
            return "continue"
        return "break"
    if event.status == ConversationStatus.PAUSED:
        return "break"
    return "continue"


def _latest_actionless_pause_seq(events: list[Event]) -> int | None:
    latest: int | None = None
    for idx, event in enumerate(events, start=1):
        if (
            isinstance(event, StatusEvent)
            and event.status == ConversationStatus.PAUSED
            and event.detail == "actionless"
        ):
            latest = _event_seq(event, idx)
    return latest


def synthetic_finish_attempted_for_current_pause(events: list[Event]) -> bool:
    """True iff REL-RC-P already attempted finish after the latest actionless
    pause."""
    latest_pause = _latest_actionless_pause_seq(events)
    if latest_pause is None:
        return False
    for idx, event in enumerate(events, start=1):
        if _event_seq(event, idx) <= latest_pause:
            continue
        if isinstance(event, StatusEvent) and event.detail == SYNTHETIC_FINISH_ATTEMPT_DETAIL:
            return True
    return False


def productive_action_since_approval(events: list[Event]) -> bool:
    """Has the agent done any state-changing or information-gathering work since the
    most recent plan approval? Used to gate the execution-mode FINISHED transition:
    if False, the loop refuses to finish (the model would be declaring done without
    having acted). If there is no plan_approved marker (plan-mode never entered, or
    not yet approved), the gate is inert — return True so the regular finish path
    runs untouched."""
    approval_seq: int | None = None
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            approval_seq = e.seq
            break
    if approval_seq is None:
        return True
    successful_actions = _successful_action_ids(events)
    for e in events:
        if (e.seq or 0) <= approval_seq:
            continue
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.id in successful_actions and e.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
                return True
    return False


def should_synthesize_finish_after_actionless_pauses(events: list[Event]) -> bool:
    """REL-RC-P decision predicate. The engine separately checks it is not in
    planning mode; this pure helper owns the replayable event-derived guards."""
    return (
        actionless_pause_count_current_execution_segment(events) >= 2
        and productive_action_since_approval(events)
        and not synthetic_finish_attempted_for_current_pause(events)
    )


def consecutive_noops(events: list[Event]) -> int:
    """Count trailing agent steps consumed WITHOUT a real executed action."""
    from ..events import DeliverableEvent

    count = 0
    for e in reversed(events):
        if isinstance(e, ObservationEvent):
            continue
        if isinstance(e, ActionEvent):
            result = _noop_action_step(e)
            if result == "count":
                count += 1
                continue
            if result == "continue":
                continue
            break
        if isinstance(e, StatusEvent) and _noop_status_resets(e):
            break
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, MessageEvent) and e.source == EventSource.AGENT:
            count += 1
            continue
        if isinstance(e, DeliverableEvent):
            count += 1
            continue
    return count


def _noop_action_step(e: ActionEvent) -> str:
    """Classify an ActionEvent in the noop backward walk."""
    tool = e.tool_call.tool_name if e.tool_call is not None else None
    if tool == "remember":
        return "count"
    if tool == "submit_plan":
        return "break"
    if tool in _BOOKKEEPING_TOOLS:
        return "continue"
    return "break"


def _noop_status_resets(e: StatusEvent) -> bool:
    """Whether a StatusEvent resets the noop streak."""
    return (
        e.status == ConversationStatus.PAUSED
        or (e.status == ConversationStatus.RUNNING and e.detail == "resumed")
    )


def fresh_read_autoground_target(events: list[Event]) -> str | None:
    """[REL-RC-B/REL-RC-E] The workspace path the build is LOOPING on at an edit gate."""
    path_by_action = _build_path_by_action(events)
    target, count = _scan_autoground_streak(events, path_by_action)
    if target is None or count < 2:
        return None
    return _check_autoground_marker(events, target)


def _build_path_by_action(events: list[Event]) -> dict[str, str]:
    """Map action ids to their ``path`` argument."""
    path_by_action: dict[str, str] = {}
    for e in events:
        if isinstance(e, ActionEvent) and e.tool_call:
            p = e.tool_call.arguments.get("path")
            if isinstance(p, str) and p:
                path_by_action[e.id] = p
    return path_by_action


def _scan_autoground_streak(
    events: list[Event], path_by_action: dict[str, str]
) -> tuple[str | None, int]:
    """Scan backward for same-path FRESH_READ_REQUIRED errors."""
    target: str | None = None
    count = 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, ObservationEvent) and e.tool_result.success:
            break
        if isinstance(e, AgentErrorEvent) and e.error in _AUTOGROUND_ERROR_CODES:
            p = path_by_action.get(e.action_id or "")
            if p:
                if target is None:
                    target = p
                if p == target:
                    count += 1
    return target, count


def _check_autoground_marker(events: list[Event], target: str) -> str | None:
    """Return None if an auto_ground_read marker already exists for target."""
    marker = f"auto_ground_read:{target}"
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, StatusEvent) and e.detail == marker:
            return None
    return target


def stuck_escape_refusal_count(
    events: list[Event],
    tool_name: str,
    *,
    action_path: str | None = None,
    after_seq: int | None = None,
) -> int:
    """Count canonical paired refusals in the current authenticated escape."""
    from .signals import normalized_workspace_read_path

    escape_seq = stuck_escape_seq(events)
    if escape_seq is None:
        return 0
    floor = escape_seq if after_seq is None else max(escape_seq, after_seq)
    actions = _escape_refusal_actions(
        events,
        tool_name,
        action_path,
        floor,
        normalized_workspace_read_path,
    )
    expected_error = f"{STUCK_ESCAPE_REFUSAL_ERROR_PREFIX}{tool_name}"
    return _count_paired_refusals(events, actions, expected_error, floor)


def _escape_refusal_actions(
    events: list[Event],
    tool_name: str,
    action_path: str | None,
    floor: int,
    normalize_fn: object,
) -> dict[str, ActionEvent]:
    """Build the action map for refused tool calls after the escape floor."""
    return {
        event.id: event
        for event in events
        if isinstance(event, ActionEvent)
        and event.seq is not None
        and event.seq > floor
        and event.tool_call is not None
        and event.tool_call.tool_name == tool_name
        and (
            action_path is None
            or normalize_fn(event.tool_call.arguments.get("path")) == action_path  # type: ignore[operator]
        )
    }


def _count_paired_refusals(
    events: list[Event],
    actions: dict[str, ActionEvent],
    expected_error: str,
    floor: int,
) -> int:
    """Count AgentErrorEvents matching the expected refusal for paired actions."""
    return sum(
        1
        for event in events
        if isinstance(event, AgentErrorEvent)
        and event.seq is not None
        and event.seq > floor
        and event.error == expected_error
        and event.action_id in actions
        and event.tool_call_id == actions[event.action_id].tool_call.call_id
    )


def actions_since_last_resume(events: list[Event]) -> int:
    """Count ActionEvents (excluding meta/bookkeeping tools and the
    verify-on-finish probe) since the last resume boundary, or since start."""
    count = 0
    for e in reversed(events):
        if isinstance(e, StatusEvent) and (
            e.status == ConversationStatus.PAUSED
            or (e.status == ConversationStatus.RUNNING and e.detail == "resumed")
        ):
            break
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.meta.get("verify_probe"):
                continue
            if e.tool_call.tool_name not in _BOOKKEEPING_TOOLS:
                count += 1
    return count


def consecutive_actionless_pauses(events: list[Event]) -> int:
    """BW-02 escalation signal — the trailing run of
    `StatusEvent(PAUSED, detail="actionless")` landings."""
    count = 0
    for e in reversed(events):
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.tool_call.tool_name in _BOOKKEEPING_TOOLS or e.meta.get("verify_probe"):
                continue
            break
        if isinstance(e, StatusEvent):
            if e.status == ConversationStatus.PAUSED and e.detail == "actionless":
                count += 1
                continue
            if e.status == ConversationStatus.RUNNING:
                continue
            break
    return count


def productive_actions_since_approval(events: list[Event]) -> int:
    """Count PRODUCTIVE (state-changing) actions since the latest plan approval."""
    approval_seq: int | None = None
    for e in events:
        if isinstance(e, StatusEvent) and e.detail == "plan_approved":
            approval_seq = e.seq
    successful_actions = _successful_action_ids(events)
    count = 0
    for e in events:
        if not isinstance(e, ActionEvent) or e.tool_call is None:
            continue
        if approval_seq is not None and (e.seq or 0) <= approval_seq:
            continue
        if e.meta.get("verify_probe"):
            continue
        if e.id in successful_actions and e.tool_call.tool_name not in _NON_PRODUCTIVE_TOOLS:
            count += 1
    return count


def bookkeeping_streak_len(events: list[Event]) -> int:
    """Trailing count of consecutive BOOKKEEPING-only actions."""
    plan_bk = _BOOKKEEPING_TOOLS - {"finish"}
    streak = 0
    for e in reversed(events):
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            break
        if isinstance(e, StatusEvent) and e.detail in ("resumed", "plan_approved"):
            break
        if isinstance(e, ActionEvent) and e.tool_call is not None:
            if e.tool_call.tool_name in plan_bk:
                streak += 1
            else:
                break
    return streak


def active_plan_step_count(events: list[Event]) -> int:
    """Step count of the latest PlanEvent, or 0 if no plan has been approved yet."""
    from ..events import PlanEvent

    plan = None
    for e in events:
        if isinstance(e, PlanEvent):
            if plan is None or e.revision >= plan.revision:
                plan = e
    if plan is None or not plan.steps:
        return 0
    return len(plan.steps)

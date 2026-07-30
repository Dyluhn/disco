"""Read-churn signal functions — split from ``loop/signals.py``.

Pure, stateless log-derived signal functions for detecting tiny repeated
``file_read`` streaks (read churn) and computing the associated nudge state.
Every function is a pure projection of an event list to a verdict, with no
instance state, no emission, and no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
)

READ_CHURN_NUDGE_DIAGNOSTIC = "read_churn_nudge"

_READ_CHURN_SMALL_LIMIT = 25
_READ_CHURN_WARNING_COUNTS = frozenset({5, 10, 15})
_READ_CHURN_LADDER_AT = 20
_READ_LINES_HEADER_RE = re.compile(r"\[lines\s+(\d+)-(\d+)\s+of\s+(\d+)")
_READ_CHURN_RESET_TOOLS = frozenset(
    {
        "file_write",
        "file_edit",
        "file_append",
        "shell",
        "shell_exec",
        "run_project_script",
        "browser",
        "design_lint",
        "update_plan_progress",
        "submit_plan",
        "finish",
    }
)


@dataclass(frozen=True)
class ReadChurnState:
    path: str
    count: int
    warning_count: int | None
    invisible_noops: int


def _read_churn_path(action: ActionEvent) -> str | None:
    if action.tool_call is None or action.tool_call.tool_name != "file_read":
        return None
    path = action.tool_call.arguments.get("path")
    return path if isinstance(path, str) and path else None


def _read_churn_limit(action: ActionEvent) -> int | None:
    if action.tool_call is None:
        return None
    value = action.tool_call.arguments.get("limit")
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _read_churn_successful_observations(events: list[Event]) -> dict[str, ObservationEvent]:
    return {
        event.action_id: event
        for event in events
        if isinstance(event, ObservationEvent) and event.tool_result.success
    }


def _file_read_covers_remainder(observation: ObservationEvent | None) -> bool:
    if observation is None:
        return False
    match = _READ_LINES_HEADER_RE.search(observation.tool_result.content or "")
    if match is None:
        return False
    _start, end, total = (int(group) for group in match.groups())
    return end >= total


def _is_whole_file_read(action: ActionEvent, observation: ObservationEvent | None) -> bool:
    if action.tool_call is None or action.tool_call.tool_name != "file_read":
        return False
    return _read_churn_limit(action) is None or _file_read_covers_remainder(observation)


def _is_small_file_read(action: ActionEvent, observation: ObservationEvent | None) -> bool:
    if observation is None or action.tool_call is None:
        return False
    if action.tool_call.tool_name != "file_read":
        return False
    limit = _read_churn_limit(action)
    return limit is not None and limit <= _READ_CHURN_SMALL_LIMIT


def _is_read_churn_reset_action(action: ActionEvent) -> bool:
    if action.tool_call is None:
        return False
    tool = action.tool_call.tool_name
    return (
        tool in _READ_CHURN_RESET_TOOLS
        or tool.startswith("preview_")
        or (tool.startswith("file_") and tool.endswith("_lines"))
    )


def read_churn_state(events: list[Event]) -> ReadChurnState | None:
    """Current same-target tiny ``file_read`` streak in execution."""
    successful = _read_churn_successful_observations(events)
    diagnostics_seen: set[tuple[str, int]] = set()
    target, count = _scan_read_churn_streak(events, successful, diagnostics_seen)
    if target is None or count == 0:
        return None
    return _build_read_churn_state(target, count, diagnostics_seen)


def _scan_read_churn_streak(
    events: list[Event],
    successful: dict[str, ObservationEvent],
    diagnostics_seen: set[tuple[str, int]],
) -> tuple[str | None, int]:
    """Walk events backward to find the current tiny-read streak."""
    target: str | None = None
    count = 0
    for event in reversed(events):
        if isinstance(event, MessageEvent):
            if _churn_message_breaks(event, diagnostics_seen):
                break
            continue
        if isinstance(event, StatusEvent):
            if event.detail in ("plan_approved", "planning"):
                break
            continue
        if isinstance(event, PlanEvent):
            break
        if isinstance(event, ObservationEvent | AgentErrorEvent):
            continue
        if not isinstance(event, ActionEvent):
            continue
        observation = successful.get(event.id)
        path = _read_churn_path(event)
        if target is None:
            new_target, should_break = _churn_first_read(event, observation, path)
            if should_break:
                break
            if new_target is not None:
                target = new_target
                count = 1
            continue
        result = _churn_subsequent_read(event, observation, path, target)
        if result == "break":
            break
        if result == "count":
            count += 1
    return target, count


def _churn_message_breaks(event: MessageEvent, diagnostics_seen: set[tuple[str, int]]) -> bool:
    """Whether a MessageEvent breaks the churn streak."""
    if event.source == EventSource.USER:
        return True
    if event.source == EventSource.AGENT:
        return True
    if (
        event.source == EventSource.ENVIRONMENT
        and event.meta.get("diagnostic") == READ_CHURN_NUDGE_DIAGNOSTIC
    ):
        path = event.meta.get("path")
        n = event.meta.get("count", event.meta.get("streak"))
        if isinstance(path, str) and isinstance(n, int):
            diagnostics_seen.add((path, n))
    return False


def _churn_first_read(
    action: ActionEvent, observation: ObservationEvent | None, path: str | None
) -> tuple[str | None, bool]:
    """Handle the first read in a potential churn streak."""
    if path is None or not _is_small_file_read(action, observation):
        if _is_read_churn_reset_action(action):
            return None, True
        return None, False
    return path, False


def _churn_subsequent_read(
    action: ActionEvent,
    observation: ObservationEvent | None,
    path: str | None,
    target: str,
) -> str:
    """Handle a subsequent read in a churn streak."""
    if path is not None:
        if path != target:
            return "break"
        if _is_whole_file_read(action, observation):
            return "break"
        if _is_small_file_read(action, observation):
            return "count"
        return "break"
    if _is_read_churn_reset_action(action):
        return "break"
    return "continue"


def _build_read_churn_state(
    target: str, count: int, diagnostics_seen: set[tuple[str, int]]
) -> ReadChurnState:
    """Build the final ReadChurnState from the streak scan."""
    warning_count = (
        count
        if count in _READ_CHURN_WARNING_COUNTS and (target, count) not in diagnostics_seen
        else None
    )
    invisible_noops = max(0, count - (_READ_CHURN_LADDER_AT - 1))
    return ReadChurnState(
        path=target,
        count=count,
        warning_count=warning_count,
        invisible_noops=invisible_noops,
    )

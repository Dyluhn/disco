"""Pure redundant-read and per-file rewrite detectors."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from ..equality import event_content_eq
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
    ToolResult,
)
from ..workspace_paths import strip_redundant_workspace_prefix
from .dedup import _F9_POINTER_SENTINEL
from .no_progress_detector import F6_FILE_MUTATING_TOOLS
from .signals import _NON_PRODUCTIVE_TOOLS, READ_CHURN_NUDGE_DIAGNOSTIC

_FILE_READ_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\t(.*)$", re.MULTILINE)
_FILE_READ_HEADER_RE = re.compile(r"^\[lines\s+\d+-\d+\s+of\s+(\d+)(?:[; (\]\u2014])")
_FILE_READ_RANGE_HEADER_RE = re.compile(
    r"\[lines\s+(\d+)-(\d+)\s+of\s+(\d+)"
    r"(?:(?:; read more with offset=(\d+))|( \u2014 offset past end of file))?\]"
)


def _consecutive_pairs(
    events: list[Event],
    first_type: type,
    second_type: type,
) -> list[tuple[Event, Event]]:
    pairs: list[tuple[Event, Event]] = []
    index = 0
    while index < len(events) - 1:
        first, second = events[index], events[index + 1]
        if isinstance(first, first_type) and isinstance(second, second_type):
            pairs.append((first, second))
            index += 2
        else:
            index += 1
    return pairs


def _normalized_read_path(raw: object) -> str | None:
    path = str(raw or "").strip()
    if not path:
        return None
    normalized = posixpath.normpath(strip_redundant_workspace_prefix(path))
    return normalized if normalized not in ("", ".") else None


def _bounded_decimal(raw: str) -> int | None:
    if len(raw) > 18:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _numbered_read_lines(content: str) -> dict[int, str] | None:
    lines: dict[int, str] = {}
    for match in _FILE_READ_NUMBERED_LINE_RE.finditer(content):
        number = _bounded_decimal(match.group(1))
        if number is None:
            return None
        lines[number] = match.group(2)
    return lines


def _read_total(content: str) -> int | None:
    match = _FILE_READ_HEADER_RE.match(content)
    return _bounded_decimal(match.group(1)) if match is not None else None


def _valid_more_range(
    more_raw: str | None,
    more_offset: int | None,
    end: int,
    total: int,
) -> bool:
    if more_raw is None:
        return True
    return more_offset is not None and end < total and more_offset == end + 1


def _valid_past_eof(
    past_eof: bool,
    start: int,
    end: int,
    total: int,
) -> bool:
    return not past_eof or (total > 0 and start > total and end == total)


def _read_range(content: str) -> tuple[int, int, int] | None:
    header = content.splitlines()[0] if content else ""
    match = _FILE_READ_RANGE_HEADER_RE.fullmatch(header)
    if match is None:
        return None
    start = _bounded_decimal(match.group(1))
    end = _bounded_decimal(match.group(2))
    total = _bounded_decimal(match.group(3))
    if start is None or end is None or total is None:
        return None
    more_raw = match.group(4)
    more_offset = _bounded_decimal(more_raw) if more_raw is not None else None
    if not _valid_more_range(more_raw, more_offset, end, total):
        return None
    if not _valid_past_eof(match.group(5) is not None, start, end, total):
        return None
    return start, end, total


def _trusted_read_churn_nudge_path(event: Event) -> str | None:
    if not isinstance(event, MessageEvent):
        return None
    if event.source != EventSource.ENVIRONMENT:
        return None
    if event.meta.get("diagnostic") != READ_CHURN_NUDGE_DIAGNOSTIC:
        return None
    if type(event.meta.get("count")) is not int or int(event.meta["count"]) < 5:
        return None
    return _normalized_read_path(event.meta.get("path"))


def _numbered_body(content: str) -> tuple[bool, list[int]]:
    matches = [_FILE_READ_NUMBERED_LINE_RE.fullmatch(line) for line in content.splitlines()[1:]]
    parsed = [_bounded_decimal(match.group(1)) for match in matches if match is not None]
    valid = all(match is not None for match in matches) and all(
        number is not None for number in parsed
    )
    return valid, [number for number in parsed if number is not None]


def _contiguous_numbered_range(
    numbers: list[int],
    start: int,
    end: int,
) -> bool:
    expected_count = end - start + 1
    return (
        expected_count >= 0
        and len(numbers) == expected_count
        and all(number == start + index for index, number in enumerate(numbers))
    )


def _paired_whole_read(
    action: ActionEvent,
    observation: ObservationEvent,
    expected_path: str,
) -> bool:
    call = action.tool_call
    result = observation.tool_result
    return bool(
        action.source == EventSource.AGENT
        and observation.source == EventSource.ENVIRONMENT
        and observation.action_id == action.id
        and call is not None
        and call.tool_name == "file_read"
        and result.tool_name == "file_read"
        and result.success
        and _normalized_read_path(call.arguments.get("path")) == expected_path
        and "offset" not in call.arguments
        and "limit" not in call.arguments
    )


def _is_whole_read(
    start: int,
    end: int,
    total: int,
    body_valid: bool,
    numbers: list[int],
) -> bool:
    if total == 0:
        return start == 1 and end == 0 and body_valid and not numbers
    return (
        start == 1 and end == total and body_valid and _contiguous_numbered_range(numbers, 1, total)
    )


def _permitted_whole_read_baseline(
    action: ActionEvent,
    observation: ObservationEvent,
    expected_path: str,
) -> tuple[dict[int, str], int] | None:
    if not _paired_whole_read(action, observation, expected_path):
        return None
    lines = _numbered_read_lines(observation.tool_result.content)
    read_range = _read_range(observation.tool_result.content)
    if lines is None or read_range is None:
        return None
    start, end, total = read_range
    body_valid, numbers = _numbered_body(observation.tool_result.content)
    return (dict(lines), total) if _is_whole_read(start, end, total, body_valid, numbers) else None


def _after_stuck_escape(events: list[Event]) -> list[Event]:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if isinstance(event, StatusEvent) and event.detail == "stuck_escape":
            return events[index + 1 :]
    return events


def _valid_file_read_pair(
    action: Event,
    observation: Event,
) -> tuple[ActionEvent, ObservationEvent] | None:
    if not isinstance(action, ActionEvent):
        return None
    if not isinstance(observation, ObservationEvent):
        return None
    call = action.tool_call
    result = observation.tool_result
    if call is None or call.tool_name != "file_read":
        return None
    if not result.success or result.tool_name != "file_read":
        return None
    return action, observation


def _unchanged_read_pair(
    pair: tuple[Event, Event],
    first_action: ActionEvent,
    first_observation: ObservationEvent,
) -> bool:
    valid = _valid_file_read_pair(*pair)
    if valid is None:
        return False
    action, observation = valid
    if not event_content_eq(action, first_action, ignore_thought=True):
        return False
    same_result = event_content_eq(
        observation,
        first_observation,
        ignore_volatile_content=True,
    )
    host_dedup = observation.tool_result.content.startswith(_F9_POINTER_SENTINEL)
    return same_result or host_dedup


def repeated_unchanged_file_read(events: list[Event], threshold: int) -> bool:
    if threshold <= 0:
        return False
    pairs = _consecutive_pairs(
        _after_stuck_escape(events),
        ActionEvent,
        ObservationEvent,
    )
    if len(pairs) < threshold:
        return False
    last = pairs[-threshold:]
    first = _valid_file_read_pair(*last[0])
    if first is None:
        return False
    return all(_unchanged_read_pair(pair, *first) for pair in last[1:])


@dataclass
class _ReadEvidence:
    lines: dict[int, str]
    start: int
    end: int
    total: int
    body_valid: bool
    numbers: list[int]


def _read_evidence(result: ToolResult) -> _ReadEvidence | None:
    lines = _numbered_read_lines(result.content)
    read_range = _read_range(result.content)
    if lines is None or read_range is None:
        return None
    start, end, total = read_range
    body_valid, numbers = _numbered_body(result.content)
    return _ReadEvidence(lines, start, end, total, body_valid, numbers)


def _structurally_valid_read(evidence: _ReadEvidence) -> bool:
    if evidence.total == 0:
        return (
            evidence.start >= 1
            and 0 <= evidence.end < evidence.start
            and evidence.body_valid
            and not evidence.numbers
        )
    if evidence.total < 0 or not evidence.body_valid:
        return False
    ordinary = 1 <= evidence.start <= evidence.end <= evidence.total and _contiguous_numbered_range(
        evidence.numbers,
        evidence.start,
        evidence.end,
    )
    empty_in_range = (
        1 <= evidence.start <= evidence.total
        and evidence.end == evidence.start - 1
        and not evidence.numbers
    )
    past_end = (
        evidence.start > evidence.total and evidence.end == evidence.total and not evidence.numbers
    )
    return ordinary or empty_in_range or past_end


@dataclass
class _NudgeState:
    actions: dict[str, ActionEvent] = field(default_factory=dict)
    path: str | None = None
    baseline_lines: dict[int, str] | None = None
    baseline_total: int | None = None
    violated: bool = False

    def reset(self, path: str | None = None) -> None:
        self.path = path
        self.baseline_lines = None
        self.baseline_total = None
        self.violated = False


def _paired_nudge_action(
    state: _NudgeState,
    observation: ObservationEvent,
) -> ActionEvent | None:
    action = state.actions.get(observation.action_id or "")
    if action is None or action.tool_call is None:
        return None
    return (
        action
        if action.source == EventSource.AGENT
        and action.tool_call.tool_name == observation.tool_result.tool_name
        else None
    )


def _consume_nudge_read(
    state: _NudgeState,
    action: ActionEvent,
    observation: ObservationEvent,
) -> None:
    assert action.tool_call is not None
    if _normalized_read_path(action.tool_call.arguments.get("path")) != state.path:
        return
    evidence = _read_evidence(observation.tool_result)
    if evidence is None:
        return
    if state.baseline_lines is None:
        baseline = _permitted_whole_read_baseline(action, observation, state.path or "")
        if baseline is not None:
            state.baseline_lines, state.baseline_total = baseline
        elif "offset" in action.tool_call.arguments or "limit" in action.tool_call.arguments:
            state.violated = True
        return
    if not _structurally_valid_read(evidence):
        return
    changed = evidence.total != state.baseline_total or any(
        state.baseline_lines.get(number) != text for number, text in evidence.lines.items()
    )
    if changed:
        state.reset()
    else:
        state.violated = True


def _consume_nudge_observation(
    state: _NudgeState,
    event: ObservationEvent,
) -> None:
    if event.source != EventSource.ENVIRONMENT or state.path is None:
        return
    result = event.tool_result
    action = _paired_nudge_action(state, event)
    if action is not None and result.success and result.tool_name not in _NON_PRODUCTIVE_TOOLS:
        state.reset()
        return
    if not result.success or result.tool_name != "file_read" or action is None:
        return
    _consume_nudge_read(state, action, event)


def redundant_read_after_churn_nudge(events: list[Event]) -> bool:
    state = _NudgeState()
    for event in events:
        if isinstance(event, ActionEvent) and event.tool_call is not None:
            state.actions[event.id] = event
            continue
        path = _trusted_read_churn_nudge_path(event)
        if path is not None:
            state.actions.clear()
            state.reset(path)
            continue
        if isinstance(event, StatusEvent) and event.detail == "stuck_escape":
            state.violated = False
            continue
        if isinstance(event, ObservationEvent):
            _consume_nudge_observation(state, event)
    return state.violated


@dataclass
class _CoverageState:
    actions: dict[str, ActionEvent] = field(default_factory=dict)
    known: dict[str, dict[int, str]] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    line_hits: dict[str, dict[int, int]] = field(default_factory=dict)
    header_hits: dict[str, int] = field(default_factory=dict)
    nudge_path: str | None = None
    post_nudge_actions: set[str] = field(default_factory=set)

    def clear_hits(self) -> None:
        self.line_hits.clear()
        self.header_hits.clear()

    def clear_nudge(self) -> None:
        self.nudge_path = None
        self.post_nudge_actions.clear()

    def reset_for_progress(self) -> None:
        self.known.clear()
        self.totals.clear()
        self.clear_hits()
        self.clear_nudge()


def _coverage_action(state: _CoverageState, event: ActionEvent) -> None:
    if event.tool_call is None:
        return
    state.actions[event.id] = event
    if state.nudge_path is not None:
        state.post_nudge_actions.add(event.id)


def _coverage_nudge(state: _CoverageState, path: str) -> None:
    state.line_hits.pop(path, None)
    state.header_hits.pop(path, None)
    state.nudge_path = path
    state.post_nudge_actions.clear()


def _record_redundancy(
    state: _CoverageState,
    path: str,
    lines: dict[int, str],
) -> None:
    if lines:
        hits = state.line_hits.setdefault(path, {})
        for number in lines:
            hits[number] = hits.get(number, 0) + 1
    else:
        state.header_hits[path] = state.header_hits.get(path, 0) + 1


def _consume_coverage_read(
    state: _CoverageState,
    action: ActionEvent,
    event: ObservationEvent,
    path: str,
    lines: dict[int, str],
    total: int,
) -> None:
    permitted = (
        state.nudge_path == path
        and action.id in state.post_nudge_actions
        and _permitted_whole_read_baseline(action, event, path) is not None
    )
    prior_total = state.totals.get(path)
    if prior_total is None or prior_total != total:
        state.totals[path] = total
        state.known[path] = dict(lines)
        state.clear_hits()
        if permitted:
            state.clear_nudge()
        return
    known = state.known.setdefault(path, {})
    redundant = not lines or (
        bool(known) and all(known.get(number) == text for number, text in lines.items())
    )
    if permitted:
        if not redundant:
            state.clear_hits()
            known.update(lines)
        state.clear_nudge()
    elif redundant:
        _record_redundancy(state, path, lines)
    else:
        state.clear_hits()
        known.update(lines)


def _consume_coverage_observation(
    state: _CoverageState,
    event: ObservationEvent,
) -> None:
    result = event.tool_result
    if result.success and result.tool_name not in _NON_PRODUCTIVE_TOOLS:
        state.reset_for_progress()
        return
    if not result.success:
        return
    action = state.actions.get(event.action_id or "")
    if action is None or action.tool_call is None:
        return
    if action.tool_call.tool_name != "file_read" or result.tool_name != "file_read":
        return
    path = _normalized_read_path(action.tool_call.arguments.get("path"))
    lines = _numbered_read_lines(result.content)
    total = _read_total(result.content)
    if path is None or lines is None or total is None:
        return
    _consume_coverage_read(state, action, event, path, lines, total)


def redundant_read_coverage(events: list[Event], threshold: int) -> bool:
    if threshold <= 0:
        return False
    state = _CoverageState()
    for event in events:
        if isinstance(event, ActionEvent):
            _coverage_action(state, event)
            continue
        path = _trusted_read_churn_nudge_path(event)
        if path is not None:
            _coverage_nudge(state, path)
            continue
        if isinstance(event, StatusEvent) and event.detail == "stuck_escape":
            state.clear_hits()
            continue
        if isinstance(event, ObservationEvent):
            _consume_coverage_observation(state, event)
    line_triggered = any(
        count >= threshold for hits in state.line_hits.values() for count in hits.values()
    )
    return line_triggered or any(count >= threshold for count in state.header_hits.values())


def _mutating_actions(
    events: list[Event],
) -> dict[str, str | None]:
    actions: dict[str, str | None] = {}
    for event in events:
        if not isinstance(event, ActionEvent) or event.tool_call is None:
            continue
        if event.tool_call.tool_name not in F6_FILE_MUTATING_TOOLS:
            continue
        path = event.tool_call.arguments.get("path")
        actions[event.id] = path if isinstance(path, str) and path else None
    return actions


def _rewrite_counts(
    events: list[Event],
    actions: dict[str, str | None],
) -> tuple[dict[str, int], dict[str, int]]:
    attempts: dict[str, int] = {}
    failures: dict[str, int] = {}
    for event in events:
        if isinstance(event, AgentErrorEvent):
            path = actions.get(event.action_id or "")
            failed = True
        elif isinstance(event, ObservationEvent):
            path = actions.get(event.action_id or "")
            failed = not event.tool_result.success
        else:
            continue
        if path is None:
            continue
        attempts[path] = attempts.get(path, 0) + 1
        if failed:
            failures[path] = failures.get(path, 0) + 1
    return attempts, failures


def per_file_rewrite_counts(
    events: list[Event],
    failure_threshold: int,
    attempt_threshold: int,
) -> tuple[str, int, int] | None:
    if failure_threshold <= 0 or attempt_threshold <= 0:
        return None
    actions = _mutating_actions(events)
    if not actions:
        return None
    attempts, failures = _rewrite_counts(events, actions)
    for path, attempt_count in attempts.items():
        failure_count = failures.get(path, 0)
        if failure_count >= failure_threshold and attempt_count >= attempt_threshold:
            return path, failure_count, attempt_count
    return None

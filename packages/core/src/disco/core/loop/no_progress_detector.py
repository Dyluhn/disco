"""Semantic verifier and barren-turn no-progress detection."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import cast

from ..equality import event_content_eq
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    MessageEvent,
    ObservationEvent,
    StatusEvent,
)

F6_FILE_MUTATING_TOOLS = frozenset(
    {
        "file_write",
        "file_edit",
        "file_append",
        "file_replace_lines",
        "file_insert_lines",
    }
)
_BARREN_NO_EFFECT_MUTATING_TOOLS = F6_FILE_MUTATING_TOOLS | frozenset(
    {
        "exact_replace",
        "file_str_replace",
        "run_project_script",
        "safe_write_file",
    }
)
_BARREN_NO_EFFECT_ERROR_CODES = frozenset({"FRESH_READ_REQUIRED", "no_op_edit", "no_op_write"})
_NO_PROGRESS_PROBE_TOOLS = frozenset(
    {"browser", "server_status", "preview_status", "verify_web_app"}
)
NO_PROGRESS_DISTINCT_EDITS = 4
BARREN_STREAK_TURNS = 8
BARREN_STREAK_IDENTICAL_FAILURES = 3
_BARREN_READ_ONLY_TOOLS = frozenset({"think", "file_read", "file_list", "search", "extract"})
_FAILURE_PREFIX_CHARS = 160
_STRUCTURED_VERIFIER_TOOLS = frozenset({"verify_web_app", "verify_appkit_app"})
FAILED_VERIFIER_REPEAT_LIMIT = 2
_VERIFIER_FILE_RECEIPT_TOOLS = F6_FILE_MUTATING_TOOLS
_VERIFIER_APPKIT_RECEIPT_TOOLS = frozenset(
    {
        "app_create",
        "app_add_section",
        "app_update_content",
        "app_set_design",
        "app_add_primitive",
    }
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_RECOVERY_BOUNDARY_DETAILS = frozenset(
    {"plan_approved", "harvested_revision_plan", "alternative_picked:manual"}
)


@dataclass(frozen=True)
class VerifierFailureNoProgress:
    """Stable failed-verifier streak after the latest effective mutation."""

    tool_name: str
    failure_fingerprint: str
    failure_fingerprint_sha256: str
    repeats: int
    streak_start_seq: int
    latest_verdict_seq: int
    summary: str
    next_action: str

    @property
    def marker_detail(self) -> str:
        return f"verifier_no_progress:{self.tool_name}:{self.failure_fingerprint_sha256[:16]}"


@dataclass(frozen=True)
class VerifierEvidenceInvalid:
    """A successful verifier execution emitted unusable structured evidence."""

    tool_name: str
    verdict_seq: int
    reason: str


@dataclass
class _VerifierStreak:
    fingerprint: str
    fingerprint_sha256: str
    repeats: int
    streak_start_seq: int
    latest_verdict_seq: int
    summary: str
    next_action: str


def _after_last_user_message(events: list[Event]) -> list[Event]:
    """Slice after the latest user instruction or typed recovery boundary."""
    last = -1
    for index, event in enumerate(events):
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            last = index
        elif isinstance(event, StatusEvent) and (event.detail or "") in _RECOVERY_BOUNDARY_DETAILS:
            last = index
    return events[last + 1 :]


def _verifier_fingerprint(structured: dict) -> tuple[str, str] | None:
    raw = structured.get("failure_fingerprint")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return (raw if len(raw) <= 256 else f"sha256:{digest}", digest)


def successful_mutation_with_receipt(
    event: ObservationEvent,
    action: ActionEvent | None,
) -> bool:
    """Trust only successful mutations carrying concrete changed-state evidence."""
    result = event.tool_result
    if not result.success or action is None or action.tool_call is None:
        return False
    tool_name = action.tool_call.tool_name
    if result.tool_name != tool_name or not isinstance(result.structured, dict):
        return False
    structured = result.structured
    if structured.get("state_changed") is True:
        return True
    if tool_name == "run_project_script":
        applied = structured.get("applied")
        return isinstance(applied, list) and bool(applied)
    if tool_name in _VERIFIER_APPKIT_RECEIPT_TOOLS:
        written = structured.get("files_written")
        return isinstance(written, list) and bool(written)
    if tool_name not in _VERIFIER_FILE_RECEIPT_TOOLS:
        return False
    path = structured.get("path")
    sha256 = structured.get("sha256")
    return (
        isinstance(path, str)
        and bool(path.strip())
        and isinstance(sha256, str)
        and _SHA256_HEX.fullmatch(sha256) is not None
    )


def _invalid_verdict(
    tool_name: str,
    seq: int,
    structured: object,
) -> VerifierEvidenceInvalid | None:
    if not isinstance(structured, dict):
        return VerifierEvidenceInvalid(tool_name, seq, "missing_structured_verdict")
    if not isinstance(structured.get("passed"), bool):
        return VerifierEvidenceInvalid(tool_name, seq, "missing_or_malformed_passed")
    if structured["passed"]:
        return None
    fingerprint = structured.get("failure_fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return None
    reason = (
        "missing_failure_fingerprint" if fingerprint is None else "malformed_failure_fingerprint"
    )
    return VerifierEvidenceInvalid(tool_name, seq, reason)


def _next_streak(
    structured: dict,
    prior: _VerifierStreak | None,
    seq: int,
) -> _VerifierStreak:
    normalized, digest = cast(
        tuple[str, str],
        _verifier_fingerprint(structured),
    )
    if prior is None:
        repeats = 1
        streak_start_seq = seq
    elif prior.fingerprint == normalized:
        repeats = prior.repeats + 1
        streak_start_seq = prior.streak_start_seq
    else:
        repeats = 1
        streak_start_seq = seq
    return _VerifierStreak(
        fingerprint=normalized,
        fingerprint_sha256=digest,
        repeats=repeats,
        streak_start_seq=streak_start_seq,
        latest_verdict_seq=seq,
        summary=str(structured.get("summary") or "")[:500],
        next_action=str(structured.get("next_action") or "")[:500],
    )


def _consume_verifier_event(
    event: Event,
    seq: int,
    action_by_id: dict[str, ActionEvent],
    streaks: dict[str, _VerifierStreak],
) -> VerifierEvidenceInvalid | None:
    if not isinstance(event, ObservationEvent):
        return None
    action = action_by_id.get(event.action_id or "")
    if successful_mutation_with_receipt(event, action):
        streaks.clear()
        return None
    tool_name = event.tool_result.tool_name
    if tool_name not in _STRUCTURED_VERIFIER_TOOLS or not event.tool_result.success:
        return None
    structured = event.tool_result.structured
    invalid = _invalid_verdict(tool_name, seq, structured)
    if invalid is not None:
        return invalid
    assert isinstance(structured, dict)
    if structured["passed"]:
        streaks.pop(tool_name, None)
        return None
    streaks[tool_name] = _next_streak(structured, streaks.get(tool_name), seq)
    return None


def repeated_failed_verifier_no_progress(
    events: list[Event],
    *,
    repeats: int = FAILED_VERIFIER_REPEAT_LIMIT,
) -> VerifierFailureNoProgress | VerifierEvidenceInvalid | None:
    """Return the earliest trailing unchanged failed-verifier streak."""
    if repeats <= 0:
        return None
    window = _after_last_user_message(events)
    action_by_id = {
        event.id: event
        for event in window
        if isinstance(event, ActionEvent) and event.tool_call is not None
    }
    streaks: dict[str, _VerifierStreak] = {}
    for index, event in enumerate(window, start=1):
        seq = event.seq if event.seq is not None else index
        invalid = _consume_verifier_event(event, seq, action_by_id, streaks)
        if invalid is not None:
            return invalid
    candidates = [item for item in streaks.items() if item[1].repeats >= repeats]
    if not candidates:
        return None
    tool_name, current = min(
        candidates,
        key=lambda item: (item[1].streak_start_seq, item[0]),
    )
    return VerifierFailureNoProgress(
        tool_name=tool_name,
        failure_fingerprint=current.fingerprint,
        failure_fingerprint_sha256=current.fingerprint_sha256,
        repeats=current.repeats,
        streak_start_seq=current.streak_start_seq,
        latest_verdict_seq=current.latest_verdict_seq,
        summary=current.summary,
        next_action=current.next_action,
    )


def _edit_probe_trail(
    window: list[Event],
    probe_tools: frozenset[str],
    edit_tools: frozenset[str],
) -> list[tuple[str, object]]:
    tool_by_id = {
        event.id: event.tool_call.tool_name
        for event in window
        if isinstance(event, ActionEvent) and event.tool_call is not None
    }
    trail: list[tuple[str, object]] = []
    for event in window:
        if (
            isinstance(event, ActionEvent)
            and event.tool_call is not None
            and event.tool_call.tool_name in edit_tools
        ):
            trail.append(("edit", event))
        elif (
            isinstance(event, ObservationEvent)
            and tool_by_id.get(event.action_id or "") in probe_tools
        ):
            trail.append(
                (
                    "probe",
                    (event.tool_result.success, event.tool_result.content),
                )
            )
    return trail


def _trailing_probe_progress(
    trail: list[tuple[str, object]],
    last_key: object,
) -> tuple[int, int]:
    probe_count = 0
    distinct: list[ActionEvent] = []
    for kind, payload in reversed(trail):
        if kind == "probe":
            if payload != last_key:
                break
            probe_count += 1
            continue
        action = cast(ActionEvent, payload)
        if not any(event_content_eq(action, prior, ignore_thought=True) for prior in distinct):
            distinct.append(action)
    return probe_count, len(distinct)


def repeated_verify_no_progress(
    events: list[Event],
    *,
    distinct_edits: int = NO_PROGRESS_DISTINCT_EDITS,
    probe_tools: frozenset[str] = _NO_PROGRESS_PROBE_TOOLS,
    edit_tools: frozenset[str] = F6_FILE_MUTATING_TOOLS,
) -> bool:
    """Detect varied edits followed by the same repeated probe outcome."""
    if distinct_edits <= 0:
        return False
    trail = _edit_probe_trail(
        _after_last_user_message(events),
        probe_tools,
        edit_tools,
    )
    last_key = next(
        (payload for kind, payload in reversed(trail) if kind == "probe"),
        None,
    )
    if last_key is None:
        return False
    probes, edits = _trailing_probe_progress(trail, last_key)
    return probes >= 2 and edits >= distinct_edits


def _failure_prefix(text: str) -> str:
    return " ".join((text or "").strip().split())[:_FAILURE_PREFIX_CHARS]


def _no_effect_refusal_code(texts: tuple[str, ...]) -> str | None:
    for text in texts:
        if text in _BARREN_NO_EFFECT_ERROR_CODES:
            return text
    combined = "\n".join(texts)
    for code in _BARREN_NO_EFFECT_ERROR_CODES:
        if code in combined:
            return code
    lowered = combined.lower()
    return next(
        (code for code in ("no_op_edit", "no_op_write") if code in lowered),
        None,
    )


def _no_effect_refusal_code_from_error(event: AgentErrorEvent) -> str | None:
    return _no_effect_refusal_code((event.error, event.detail or ""))


def _no_effect_refusal_code_from_observation(
    event: ObservationEvent,
) -> str | None:
    result = event.tool_result
    structured_kind = (
        str(result.structured.get("kind") or "") if isinstance(result.structured, dict) else ""
    )
    return _no_effect_refusal_code((result.error or "", result.content, structured_kind))


def _barren_failures(
    window: list[Event],
    recent_action_ids: set[str],
) -> tuple[dict[str, str], list[str]]:
    no_effect: dict[str, str] = {}
    prefixes: list[str] = []
    for event in window:
        if isinstance(event, AgentErrorEvent):
            action_id = event.action_id or ""
            code = _no_effect_refusal_code_from_error(event)
            prefix = _failure_prefix(event.detail or event.error)
        elif isinstance(event, ObservationEvent) and not event.tool_result.success:
            action_id = event.action_id or ""
            code = _no_effect_refusal_code_from_observation(event)
            prefix = _failure_prefix(event.tool_result.error or event.tool_result.content)
        else:
            continue
        if action_id not in recent_action_ids:
            continue
        if code is not None:
            no_effect[action_id] = code
        if prefix:
            prefixes.append(prefix)
    return no_effect, prefixes


def _actions_are_barren(
    actions: list[ActionEvent],
    no_effect: dict[str, str],
    prefixes: list[str],
) -> bool:
    for action in actions:
        assert action.tool_call is not None
        tool_name = action.tool_call.tool_name
        if tool_name in _BARREN_READ_ONLY_TOOLS:
            continue
        if tool_name not in _BARREN_NO_EFFECT_MUTATING_TOOLS:
            return False
        if action.id not in no_effect:
            return False
        prefixes.append(no_effect[action.id])
    return True


def barren_streak_no_progress(
    events: list[Event],
    *,
    turns: int = BARREN_STREAK_TURNS,
    identical_failures: int = BARREN_STREAK_IDENTICAL_FAILURES,
) -> bool:
    """Detect a trailing read/reasoning-only streak with repeated failures."""
    if turns <= 0 or identical_failures <= 0:
        return False
    window = _after_last_user_message(events)
    actions = [
        event for event in window if isinstance(event, ActionEvent) and event.tool_call is not None
    ]
    if len(actions) < turns:
        return False
    recent_actions = actions[-turns:]
    no_effect, prefixes = _barren_failures(
        window,
        {action.id for action in recent_actions},
    )
    if not _actions_are_barren(recent_actions, no_effect, prefixes):
        return False
    if len(prefixes) < identical_failures:
        return False
    return any(count >= identical_failures for count in Counter(prefixes).values())


def no_progress_detected(events: list[Event]) -> bool:
    return repeated_verify_no_progress(events) or barren_streak_no_progress(events)

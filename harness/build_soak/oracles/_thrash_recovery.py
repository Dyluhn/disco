"""Blocked-recovery, mutation-receipt, and plan-authority helpers."""

from __future__ import annotations

import re
from typing import Any

from ..events import (
    KIND_MESSAGE,
    KIND_OBSERVATION,
    KIND_STATUS,
    SRC_USER,
    kind_of,
    seq_of,
)

_FILE_MUTATION_TOOLS = frozenset(
    {
        "exact_replace",
        "file_append",
        "file_edit",
        "file_insert_lines",
        "file_replace_lines",
        "file_str_replace",
        "file_write",
        "safe_write_file",
        "write_file",
    }
)
_RECEIPT_FILE_TOOLS = _FILE_MUTATION_TOOLS
_RECEIPT_APPLIED_TOOLS = frozenset({"run_project_script"})


def _validate_blocked_meta(meta: Any, detail: str) -> bool:
    return isinstance(meta, dict) and all(
        (
            meta.get("blocked_landing") is True,
            meta.get("superseded_by_landing") is True,
            meta.get("blocked_reason") == detail,
            meta.get("legacy_detail") == detail,
            meta.get("legacy_status") == "STUCK",
        )
    )


def _validate_landing(landing: dict[str, Any], detail: str) -> bool:
    meta = landing.get("meta")
    return (
        landing.get("status") == "AWAITING_USER_QUESTION"
        and isinstance(meta, dict)
        and all(
            (
                meta.get("blocked_landing") is True,
                meta.get("blocked_reason") == detail,
                meta.get("legacy_detail") == detail,
                meta.get("legacy_status") == "STUCK",
            )
        )
    )


def _subsequent_statuses(
    marker: dict[str, Any], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    marker_seq = seq_of(marker)
    return [
        event for event in events if seq_of(event) > marker_seq and kind_of(event) == KIND_STATUS
    ]


def _first_user_seq_after(landing: dict[str, Any], events: list[dict[str, Any]]) -> int | None:
    landing_seq = seq_of(landing)
    return next(
        (
            seq_of(event)
            for event in events
            if seq_of(event) > landing_seq
            and kind_of(event) == KIND_MESSAGE
            and event.get("source") == SRC_USER
        ),
        None,
    )


def _finished_after(user_seq: int, events: list[dict[str, Any]]) -> bool:
    return any(
        seq_of(event) > user_seq
        and kind_of(event) == KIND_STATUS
        and event.get("status") in {"FINISHED", "VERIFIED"}
        for event in events
    )


def recovered_blocked_marker(marker: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    """Whether a historical STUCK marker was explicitly superseded and recovered."""
    detail = str(marker.get("detail") or "")
    if not _validate_blocked_meta(marker.get("meta"), detail):
        return False
    statuses = _subsequent_statuses(marker, events)
    if not statuses or not _validate_landing(statuses[0], detail):
        return False
    user_seq = _first_user_seq_after(statuses[0], events)
    return user_seq is not None and _finished_after(user_seq, events)


def _applied_paths_are_exact(structured: dict[str, Any]) -> bool:
    applied = structured.get("applied")
    return (
        isinstance(applied, list)
        and bool(applied)
        and all(isinstance(item, str) and bool(item) for item in applied)
    )


def _file_receipt_is_exact(structured: dict[str, Any]) -> bool:
    path = structured.get("path")
    sha256 = structured.get("sha256")
    return (
        isinstance(path, str)
        and bool(path.strip())
        and isinstance(sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", sha256) is not None
    )


def trusted_mutation_receipt_outcome(
    event: dict[str, Any], *, action_ids: frozenset[str] | None = None
) -> bool:
    """Whether one observation event carries a trusted changed-state receipt."""
    if kind_of(event) != KIND_OBSERVATION:
        return False
    action_id = event.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return False
    if action_ids is not None and action_id not in action_ids:
        return False
    result = event.get("tool_result")
    if not isinstance(result, dict) or result.get("success") is not True:
        return False
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return False
    if structured.get("state_changed") is True:
        return True
    tool_name = result.get("tool_name")
    if tool_name in _RECEIPT_APPLIED_TOOLS:
        return _applied_paths_are_exact(structured)
    if tool_name in _RECEIPT_FILE_TOOLS:
        return _file_receipt_is_exact(structured)
    return False


def _valid_plan_transition(transition: dict[str, Any]) -> bool:
    revision = transition.get("new_plan_revision")
    plan_event_id = transition.get("new_plan_event_id")
    return all(
        (
            transition.get("new_authority") == "plan",
            transition.get("reason") in {"approved_initial_plan", "approved_plan_revision"},
            isinstance(revision, int),
            not isinstance(revision, bool),
            isinstance(revision, int) and revision >= 1,
            isinstance(plan_event_id, str),
            isinstance(plan_event_id, str) and bool(plan_event_id),
        )
    )


def _predicate_fingerprints(transition: dict[str, Any]) -> tuple[str, ...] | None:
    fingerprints = transition.get("new_predicate_fingerprints")
    if not isinstance(fingerprints, list):
        return None
    if any(not isinstance(item, str) or not item for item in fingerprints):
        return None
    return tuple(sorted(fingerprints))


def approved_plan_predicate_scope(
    event: dict[str, Any],
) -> tuple[str, ...] | None:
    """Return one trusted approved plan-verifier authority scope."""
    if (
        kind_of(event) != KIND_STATUS
        or event.get("source") != "system"
        or event.get("detail") != "plan_approved"
    ):
        return None
    transition = event.get("plan_verification_transition")
    if not isinstance(transition, dict) or not _valid_plan_transition(transition):
        return None
    return _predicate_fingerprints(transition)

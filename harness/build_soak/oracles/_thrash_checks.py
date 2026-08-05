"""Thrash check stages and error-signature helpers.

Extracted from ``_thrash_helpers.py`` to keep that module within the
architecture budget. These functions implement the individual thrash
check stages (markers, actionless pauses, repair thrash, tool errors,
streak/semantic/background repeats) and the error-signature helpers.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from disco.core.tool_fingerprint import tool_call_fingerprint

from .. import failure_codes as fc
from ..events import (
    KIND_ACTION,
    KIND_AGENT_ERROR,
    KIND_OBSERVATION,
    KIND_STATUS,
    action_id_of,
    kind_of,
    seq_of,
    tool_name_of,
)
from . import _thrash_shapes as shapes
from ._thrash_helpers import (
    approved_plan_predicate_scope,
    largest_background_script_restart_group,
    largest_semantic_shell_repeat_group,
    recovered_blocked_marker,
    trusted_mutation_receipt_outcome,
)
from ._thrash_shell import _SHELL_TOOLS
from .schema import OracleResult, failing

_ORACLE = "ThrashOracle"
_UUID = re.compile(r"\b(?:[0-9a-f]{8}-?){2,}[0-9a-f]*\b", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")
_THRASH_STUCK_DETAILS = {
    "repeated_action_observation",
    "repeated_noop",
    "verify_no_progress",
    "verifier_no_progress",
    "identical_plan_streak",
}


def _fingerprint(event: dict[str, Any]) -> str:
    """This event's tool-call identity.

    Delegates to `disco.core.tool_fingerprint`, the SINGLE owner of "is this the
    same call?" (A6.3 §3.1, a binding design constraint). The loop's freshness
    memo keys on the same function, so the memo can never fire on a different
    equivalence class than the oracle that grades the run. It is not copied here
    — a second copy would drift, and the drift would stay invisible until a
    canary fired on a class the memo had never seen.
    """
    call = event.get("tool_call") or {}
    return tool_call_fingerprint(tool_name_of(event), call.get("arguments"))


def _normalized_error_text(text: str) -> str:
    normalized = _UUID.sub("<id>", text.lower())
    normalized = _NUMBER.sub("<n>", normalized)
    return _SPACE.sub(" ", normalized).strip()


def _error_signature(text: str) -> str:
    return _normalized_error_text(text)[:240]


def _detail_signature(error: str, detail: str) -> str:
    normalized = _normalized_error_text(detail)
    if not normalized or normalized == _normalized_error_text(error):
        return ""
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()


def _failed_action_signature(action: dict[str, Any], error_signature: str) -> str:
    if tool_name_of(action) not in _SHELL_TOOLS or error_signature != "command exited <n>":
        return ""
    args = (action.get("tool_call") or {}).get("arguments") or {}
    command = args.get("command", args.get("cmd"))
    if not isinstance(command, str):
        return ""
    return "sha256:" + hashlib.sha256(command.encode("utf-8", "surrogatepass")).hexdigest()


def progress_epoch_boundary(
    event: dict[str, Any], *, action_ids: frozenset[str] | None = None
) -> bool:
    if approved_plan_predicate_scope(event) is not None:
        return True
    if trusted_mutation_receipt_outcome(event, action_ids=action_ids):
        return True
    if kind_of(event) != "message":
        return False
    if event.get("source") == "user":
        return True
    meta = event.get("meta")
    blocking = meta.get("blocking") if isinstance(meta, dict) else None
    return event.get("source") == "environment" and isinstance(blocking, str) and bool(blocking)


def action_outcomes(events: list[dict[str, Any]]) -> dict[str, tuple[bool, str, str]]:
    outcomes: dict[str, tuple[bool, str, str]] = {}
    for event in events:
        kind = kind_of(event)
        action_id = str(event.get("action_id") or "")
        if not action_id:
            continue
        if kind == KIND_OBSERVATION:
            result = event.get("tool_result") or {}
            success = result.get("success") is True
            content = str(result.get("content") or "")
            error = str(result.get("error") or ("" if success else content))
            outcomes[action_id] = (
                success,
                _error_signature(error),
                "" if success else _detail_signature(error, content),
            )
        elif kind == KIND_AGENT_ERROR:
            error = str(event.get("error") or "")
            outcomes[action_id] = (
                False,
                _error_signature(error),
                _detail_signature(error, str(event.get("detail") or "")),
            )
    return outcomes


def check_thrash_markers(
    events: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> OracleResult | None:
    thrash_markers = [
        event
        for event in events
        if kind_of(event) == KIND_STATUS
        and str(event.get("status")) == "STUCK"
        and str(event.get("detail") or "") in _THRASH_STUCK_DETAILS
    ]
    recovered_marker_seqs = [
        seq_of(event) for event in thrash_markers if recovered_blocked_marker(event, events)
    ]
    terminal_markers = [
        {"seq": seq_of(event), "detail": str(event.get("detail") or "")}
        for event in thrash_markers
        if seq_of(event) not in recovered_marker_seqs
    ]
    if terminal_markers:
        return shapes.shaped_red(
            shapes.SHAPE_STUCK_VALVE,
            facts={"terminal_markers": terminal_markers, "action_count": len(actions)},
        )
    return None


def check_actionless_pauses(
    events: list[dict[str, Any]], limits: dict[str, int]
) -> OracleResult | None:
    actionless = [
        seq_of(event)
        for event in events
        if kind_of(event) == KIND_STATUS
        and str(event.get("status")) == "PAUSED"
        and str(event.get("detail") or "") == "actionless"
    ]
    if len(actionless) > limits["max_actionless_pauses"]:
        return failing(
            _ORACLE,
            fc.ACTIONLESS_THRASH,
            first_broken_link="model_turns -> actionless_pause",
            facts={
                "count": len(actionless),
                "allowed": limits["max_actionless_pauses"],
                "seqs": actionless,
            },
        )
    return None


def check_repair_thrash(
    repairs: list[dict[str, Any]], limits: dict[str, int]
) -> OracleResult | None:
    repair_counts = Counter(str(span["repair_kind"]) for span in repairs)
    deepest: dict[str, int] = {}
    unattributed: Counter[str] = Counter()
    for span in repairs:
        kind = str(span["repair_kind"])
        raw = span.get("attempt")
        depth = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1 else 0
        if depth:
            deepest[kind] = max(deepest.get(kind, 0), depth)
        else:
            unattributed[kind] += 1
    escalation = {
        kind: max(deepest.get(kind, 0), unattributed[kind])
        for kind in set(deepest) | set(unattributed)
    }
    if escalation:
        repair_kind, repair_count = max(escalation.items(), key=lambda item: item[1])
        if repair_count > limits["max_same_model_repair_repeats"]:
            return failing(
                _ORACLE,
                fc.MODEL_REPAIR_THRASH,
                first_broken_link="model_request -> repeated_hidden_repair",
                facts={
                    "repair_kind": repair_kind,
                    "count": repair_count,
                    "occurrences": repair_counts[repair_kind],
                    "allowed": limits["max_same_model_repair_repeats"],
                    "repairs": repairs,
                },
            )
    if len(repairs) > limits["max_total_model_repairs"]:
        return failing(
            _ORACLE,
            fc.MODEL_REPAIR_THRASH,
            first_broken_link="model_request -> excessive_hidden_repairs",
            facts={
                "count": len(repairs),
                "allowed": limits["max_total_model_repairs"],
                "repair_counts": dict(repair_counts),
                "repairs": repairs,
            },
        )
    return None


def check_tool_error_thrash(
    events: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
    limits: dict[str, int],
    progress_epochs_cls: type,
) -> tuple[OracleResult | None, int]:
    epoch = 0
    epoch_by_index: dict[int, int] = {}
    known_action_ids = frozenset(
        str(action_id_of(event))
        for event in events
        if kind_of(event) == KIND_ACTION and action_id_of(event)
    )
    epochs = progress_epochs_cls()
    for index, event in enumerate(events):
        if epochs.crosses(event, action_ids=known_action_ids):
            epoch += 1
        epoch_by_index[index] = epoch
    action_epochs = {
        str(action_id_of(event)): epoch_by_index[index]
        for index, event in enumerate(events)
        if kind_of(event) == KIND_ACTION
    }
    failed_signatures: Counter[tuple[str, str, str, str, int]] = Counter()
    failure_seqs: dict[tuple[str, str, str, str, int], list[int]] = {}
    for action in actions:
        action_id = action_id_of(action)
        success, signature, detail_sig = outcomes.get(
            str(action_id), (False, "missing outcome", "")
        )
        if success:
            continue
        key = (
            tool_name_of(action) or "?",
            signature,
            detail_sig,
            _failed_action_signature(action, signature),
            action_epochs.get(str(action_id), 0),
        )
        failed_signatures[key] += 1
        failure_seqs.setdefault(key, []).append(seq_of(action))
    if failed_signatures:
        (tool, signature, detail_sig, action_sig, failure_epoch), count = (
            failed_signatures.most_common(1)[0]
        )
        if count > limits["max_same_tool_error_repeats"]:
            return (
                failing(
                    _ORACLE,
                    fc.TOOL_ERROR_THRASH,
                    first_broken_link="tool_error -> repeated_same_tool_error",
                    facts={
                        "tool": tool,
                        "error_signature": signature,
                        "detail_signature": detail_sig or None,
                        "action_signature": action_sig or None,
                        "count": count,
                        "allowed": limits["max_same_tool_error_repeats"],
                        "progress_epoch": failure_epoch,
                        "action_seqs": failure_seqs[
                            (tool, signature, detail_sig, action_sig, failure_epoch)
                        ],
                    },
                ),
                max(failed_signatures.values(), default=0),
            )
    return None, max(failed_signatures.values(), default=0)


def check_streak_thrash(
    events: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
    actions: list[dict[str, Any]],
    limits: dict[str, int],
    longest_streak_fn: Any,
) -> tuple[OracleResult | None, int, int, int, int]:
    known_action_ids = frozenset(
        str(action_id_of(event))
        for event in events
        if kind_of(event) == KIND_ACTION and action_id_of(event)
    )
    streak, fingerprint, streak_seqs = longest_streak_fn(events, action_ids=known_action_ids)
    if streak > limits["max_identical_action_repeats"]:
        return (
            shapes.repetition_red(
                events,
                shape=shapes.SHAPE_IDENTICAL_STREAK,
                fingerprint=fingerprint,
                count=streak,
                allowed=limits["max_identical_action_repeats"],
                occurrence_seqs=streak_seqs,
            ),
            streak,
            0,
            0,
            0,
        )
    semantic_count, semantic_fingerprint, semantic_seqs = largest_semantic_shell_repeat_group(
        events, outcomes
    )
    if semantic_count > limits["max_identical_action_repeats"]:
        return (
            shapes.repetition_red(
                events,
                shape=shapes.SHAPE_SEMANTIC_SHELL,
                fingerprint=semantic_fingerprint,
                count=semantic_count,
                allowed=limits["max_identical_action_repeats"],
                occurrence_seqs=semantic_seqs,
            ),
            streak,
            semantic_count,
            0,
            0,
        )
    background_count, background_fingerprint, background_seqs, cleanup_seqs, cleanup_credit = (
        largest_background_script_restart_group(actions, outcomes)
    )
    background_allowed = limits["max_identical_action_repeats"] + cleanup_credit
    if background_count > background_allowed:
        return (
            shapes.repetition_red(
                events,
                shape=shapes.SHAPE_BACKGROUND_RESTART,
                fingerprint=background_fingerprint,
                count=background_count,
                allowed=background_allowed,
                occurrence_seqs=background_seqs,
                extra={
                    "base_allowed": limits["max_identical_action_repeats"],
                    "cleanup_credit": cleanup_credit,
                    "cleanup_seqs": cleanup_seqs,
                },
            ),
            streak,
            semantic_count,
            background_count,
            cleanup_credit,
        )
    return None, streak, semantic_count, background_count, cleanup_credit


def build_passing_facts(
    events: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    repairs: list[dict[str, Any]],
    limits: dict[str, int],
    streak: int,
    semantic_count: int,
    background_count: int,
    cleanup_credit: int,
    max_error_group: int,
) -> dict[str, Any]:
    actionless = [
        seq_of(event)
        for event in events
        if kind_of(event) == KIND_STATUS
        and str(event.get("status")) == "PAUSED"
        and str(event.get("detail") or "") == "actionless"
    ]
    recovered_seqs = [
        seq_of(event)
        for event in events
        if kind_of(event) == KIND_STATUS
        and str(event.get("status")) == "STUCK"
        and str(event.get("detail") or "") in _THRASH_STUCK_DETAILS
        and recovered_blocked_marker(event, events)
    ]
    return {
        "action_count": len(actions),
        "actionless_pauses": len(actionless),
        "longest_identical_action_streak": streak,
        "largest_semantic_shell_repeat_group": semantic_count,
        "largest_background_script_restart_group": background_count,
        "background_script_cleanup_credit": cleanup_credit,
        "largest_same_tool_error_group": max_error_group,
        "model_repair_count": len(repairs),
        "model_repair_counts": dict(Counter(str(span["repair_kind"]) for span in repairs)),
        "recovered_blocked_marker_seqs": recovered_seqs,
        "limits": limits,
    }

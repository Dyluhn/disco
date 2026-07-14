"""Deterministic model-thrashing oracle over Disco's durable event stream.

The product already contains bounded no-progress valves.  Those valves protect
cost; they do not make a run healthy.  A build that eventually finishes after
repeatedly guessing tool syntax, repeating the same call, or actionless-pausing
is precisely the friction a reliability soak must surface.

Thresholds are scenario-owned under ``assertions.thrash`` so migrations are
explicit and evidence-locked.  Without that block the oracle SKIPs, preserving
classification of older frozen dossiers.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

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
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "ThrashOracle"
_UUID = re.compile(r"\b(?:[0-9a-f]{8}-?){2,}[0-9a-f]*\b", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")
_THRASH_STUCK_DETAILS = {
    "repeated_action_observation",
    "repeated_noop",
    "verify_no_progress",
    "identical_plan_streak",
}


def _limits(scenario: dict[str, Any] | None) -> dict[str, int] | None:
    block = (((scenario or {}).get("assertions") or {}).get("thrash"))
    if not isinstance(block, dict):
        return None
    defaults = {
        "max_identical_action_repeats": 2,
        "max_same_tool_error_repeats": 2,
        "max_actionless_pauses": 0,
        "max_same_model_repair_repeats": 1,
        "max_total_model_repairs": 3,
    }
    out: dict[str, int] = {}
    for key, default in defaults.items():
        raw = block.get(key, default)
        if not isinstance(raw, int) or raw < 0:
            raise ValueError(f"assertions.thrash.{key} must be a non-negative integer")
        out[key] = raw
    return out


def _fingerprint(event: dict[str, Any]) -> str:
    call = event.get("tool_call") or {}
    args = call.get("arguments") or {}
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return f"{tool_name_of(event) or '?'}:{encoded}"


def _error_signature(text: str) -> str:
    normalized = _UUID.sub("<id>", text.lower())
    normalized = _NUMBER.sub("<n>", normalized)
    return _SPACE.sub(" ", normalized).strip()[:240]


def _action_outcomes(events: list[dict[str, Any]]) -> dict[str, tuple[bool, str]]:
    outcomes: dict[str, tuple[bool, str]] = {}
    for event in events:
        kind = kind_of(event)
        action_id = str(event.get("action_id") or "")
        if not action_id:
            continue
        if kind == KIND_OBSERVATION:
            result = event.get("tool_result") or {}
            success = result.get("success") is True
            error = str(result.get("error") or ("" if success else result.get("content") or ""))
            outcomes[action_id] = (success, _error_signature(error))
        elif kind == KIND_AGENT_ERROR:
            outcomes[action_id] = (False, _error_signature(str(event.get("error") or "")))
    return outcomes


def _longest_identical_streak(actions: list[dict[str, Any]]) -> tuple[int, str, list[int]]:
    best_count, best_fp, best_seqs = 0, "", []
    current_fp, current_seqs = "", []
    for action in actions:
        fp = _fingerprint(action)
        if fp == current_fp:
            current_seqs.append(seq_of(action))
        else:
            current_fp, current_seqs = fp, [seq_of(action)]
        if len(current_seqs) > best_count:
            best_count, best_fp, best_seqs = len(current_seqs), fp, list(current_seqs)
    return best_count, best_fp, best_seqs


class ThrashOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        inspect_trace: dict[str, Any] | None = None,
    ) -> list[OracleResult]:
        try:
            limits = _limits(scenario)
        except ValueError as exc:
            return [
                failing(
                    _ORACLE,
                    fc.SCENARIO_CONTRACT_UNSATISFIABLE,
                    first_broken_link="scenario_contract -> thrash_thresholds",
                    facts={"reason": str(exc)},
                )
            ]
        if limits is None:
            return [skipping(_ORACLE, reason="scenario has no assertions.thrash policy")]

        actions = [event for event in events if kind_of(event) == KIND_ACTION]
        outcomes = _action_outcomes(events)
        terminal_markers = [
            {"seq": seq_of(event), "detail": str(event.get("detail") or "")}
            for event in events
            if kind_of(event) == KIND_STATUS
            and str(event.get("status")) == "STUCK"
            and str(event.get("detail") or "") in _THRASH_STUCK_DETAILS
        ]
        if terminal_markers:
            return [
                failing(
                    _ORACLE,
                    fc.TOOL_CALL_THRASH,
                    first_broken_link="model_turns -> bounded_stuck_valve",
                    facts={"terminal_markers": terminal_markers, "action_count": len(actions)},
                )
            ]

        actionless = [
            seq_of(event)
            for event in events
            if kind_of(event) == KIND_STATUS
            and str(event.get("status")) == "PAUSED"
            and str(event.get("detail") or "") == "actionless"
        ]
        if len(actionless) > limits["max_actionless_pauses"]:
            return [
                failing(
                    _ORACLE,
                    fc.ACTIONLESS_THRASH,
                    first_broken_link="model_turns -> actionless_pause",
                    facts={
                        "count": len(actionless),
                        "allowed": limits["max_actionless_pauses"],
                        "seqs": actionless,
                    },
                )
            ]

        # Repairs happen before an ActionEvent is committed and were historically
        # invisible to the durable event log. DISCO_INSPECT now records a bounded,
        # redacted `agent.repair` point span for unknown-tool guesses, degenerate
        # turns, tool-history repairs, and provider request rejections.
        spans = (inspect_trace or {}).get("spans")
        repairs = [
            span
            for span in spans or []
            if isinstance(span, dict)
            and span.get("span") == "agent.repair"
            and span.get("event") == "point"
            and str(span.get("repair_kind") or "")
        ] if isinstance(spans, list) else []
        repair_counts = Counter(str(span["repair_kind"]) for span in repairs)
        if repair_counts:
            repair_kind, repair_count = repair_counts.most_common(1)[0]
            if repair_count > limits["max_same_model_repair_repeats"]:
                return [
                    failing(
                        _ORACLE,
                        fc.MODEL_REPAIR_THRASH,
                        first_broken_link="model_request -> repeated_hidden_repair",
                        facts={
                            "repair_kind": repair_kind,
                            "count": repair_count,
                            "allowed": limits["max_same_model_repair_repeats"],
                            "repairs": repairs,
                        },
                    )
                ]
        if len(repairs) > limits["max_total_model_repairs"]:
            return [
                failing(
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
            ]

        failed_signatures: Counter[tuple[str, str]] = Counter()
        failure_seqs: dict[tuple[str, str], list[int]] = {}
        for action in actions:
            action_id = action_id_of(action)
            success, signature = outcomes.get(str(action_id), (False, "missing outcome"))
            if success:
                continue
            key = (tool_name_of(action) or "?", signature)
            failed_signatures[key] += 1
            failure_seqs.setdefault(key, []).append(seq_of(action))
        if failed_signatures:
            (tool, signature), count = failed_signatures.most_common(1)[0]
            if count > limits["max_same_tool_error_repeats"]:
                return [
                    failing(
                        _ORACLE,
                        fc.TOOL_ERROR_THRASH,
                        first_broken_link="tool_error -> repeated_same_tool_error",
                        facts={
                            "tool": tool,
                            "error_signature": signature,
                            "count": count,
                            "allowed": limits["max_same_tool_error_repeats"],
                            "action_seqs": failure_seqs[(tool, signature)],
                        },
                    )
                ]

        streak, fingerprint, streak_seqs = _longest_identical_streak(actions)
        if streak > limits["max_identical_action_repeats"]:
            return [
                failing(
                    _ORACLE,
                    fc.TOOL_CALL_THRASH,
                    first_broken_link="tool_call -> identical_tool_call_streak",
                    facts={
                        "fingerprint": fingerprint,
                        "count": streak,
                        "allowed": limits["max_identical_action_repeats"],
                        "action_seqs": streak_seqs,
                    },
                )
            ]

        return [
            passing(
                _ORACLE,
                facts={
                    "action_count": len(actions),
                    "actionless_pauses": len(actionless),
                    "longest_identical_action_streak": streak,
                    "largest_same_tool_error_group": max(failed_signatures.values(), default=0),
                    "model_repair_count": len(repairs),
                    "model_repair_counts": dict(repair_counts),
                    "limits": limits,
                },
            )
        ]

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

import re
from typing import Any

from .. import failure_codes as fc
from ..events import KIND_ACTION, KIND_OBSERVATION, kind_of, seq_of
from ._thrash_checks import (
    action_outcomes,
    build_passing_facts,
    check_actionless_pauses,
    check_repair_thrash,
    check_streak_thrash,
    check_thrash_markers,
    check_tool_error_thrash,
    progress_epoch_boundary,
)
from ._thrash_helpers import approved_plan_predicate_scope
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "ThrashOracle"
_PREVIEW_GENERATION_RE = re.compile(r"^pv_[0-9a-f]{32}$")


def _limits(scenario: dict[str, Any] | None) -> dict[str, int] | None:
    block = ((scenario or {}).get("assertions") or {}).get("thrash")
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


def repair_spans(spans: Any) -> list[dict[str, Any]]:
    """The model-repair point spans in an inspect trace."""
    if not isinstance(spans, list):
        return []
    return [
        span
        for span in spans
        if isinstance(span, dict)
        and span.get("span") == "agent.repair"
        and span.get("event") == "point"
        and str(span.get("repair_kind") or "")
    ]


def preview_generation_of(event: dict[str, Any]) -> str | None:
    """The preview generation a successful, typed preview receipt establishes."""
    if kind_of(event) != KIND_OBSERVATION:
        return None
    result = event.get("tool_result") or {}
    if result.get("success") is not True:
        return None
    structured = result.get("structured")
    if not isinstance(structured, dict):
        return None
    generation = structured.get("generation")
    if not isinstance(generation, str) or _PREVIEW_GENERATION_RE.fullmatch(generation) is None:
        return None
    if structured.get("projection_id") != generation:
        return None
    return generation


class ProgressEpochs:
    """Stateful progress-epoch boundaries: typed events AND authority replacement."""

    def __init__(self) -> None:
        self._preview_generation: str | None = None

    def crosses(self, event: dict[str, Any], *, action_ids: frozenset[str] | None = None) -> bool:
        generation = preview_generation_of(event)
        if generation is not None:
            previous, self._preview_generation = self._preview_generation, generation
            if previous is not None and previous != generation:
                return True
        return progress_epoch_boundary(event, action_ids=action_ids)


def _longest_identical_streak(
    events: list[dict[str, Any]], *, action_ids: frozenset[str]
) -> tuple[int, str, list[int]]:
    """Return the longest exact-action streak within one trusted progress epoch."""
    from ._thrash_checks import _fingerprint

    best_count, best_fp, best_seqs = 0, "", []
    current_fp, current_seqs = "", []
    approved_scope: tuple[str, ...] | None = None
    epochs = ProgressEpochs()
    for event in events:
        next_scope = approved_plan_predicate_scope(event)
        if next_scope is not None:
            if approved_scope != next_scope:
                current_fp, current_seqs = "", []
                approved_scope = next_scope
            continue
        if epochs.crosses(event, action_ids=action_ids):
            current_fp, current_seqs = "", []
            continue
        if kind_of(event) != KIND_ACTION:
            continue
        fp = _fingerprint(event)
        if fp == current_fp:
            current_seqs.append(seq_of(event))
        else:
            current_fp, current_seqs = fp, [seq_of(event)]
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
        outcomes = action_outcomes(events)

        marker_error = check_thrash_markers(events, actions)
        if marker_error is not None:
            return [marker_error]

        actionless_error = check_actionless_pauses(events, limits)
        if actionless_error is not None:
            return [actionless_error]

        repairs = repair_spans((inspect_trace or {}).get("spans"))
        repair_error = check_repair_thrash(repairs, limits)
        if repair_error is not None:
            return [repair_error]

        error_thrash, max_error_group = check_tool_error_thrash(
            events, actions, outcomes, limits, ProgressEpochs
        )
        if error_thrash is not None:
            return [error_thrash]

        streak_error, streak, semantic_count, background_count, cleanup_credit = (
            check_streak_thrash(events, outcomes, actions, limits, _longest_identical_streak)
        )
        if streak_error is not None:
            return [streak_error]

        facts = build_passing_facts(
            events,
            actions,
            repairs,
            limits,
            streak,
            semantic_count,
            background_count,
            cleanup_credit,
            max_error_group,
        )
        return [passing(_ORACLE, facts=facts)]

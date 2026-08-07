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

from typing import Any

from disco.core.receipt_currency import (
    CurrencyBoundaries,
)
from disco.core.receipt_currency import (
    preview_generation_of as preview_generation_of,
)

from .. import failure_codes as fc
from ..events import KIND_ACTION, kind_of, seq_of
from ._thrash_checks import (
    action_outcomes,
    build_passing_facts,
    check_actionless_pauses,
    check_repair_thrash,
    check_streak_thrash,
    check_thrash_markers,
    check_tool_error_thrash,
)
from ._thrash_checks import (
    progress_epoch_boundary as progress_epoch_boundary,  # re-export: tests + callers name it here
)
from ._thrash_helpers import approved_plan_predicate_scope
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "ThrashOracle"


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


# `preview_generation_of` is the re-export at the top of this module; its body
# moved to `disco.core.receipt_currency` with the rest of the currency rules
# (2026-08-06z, GROUNDED FEEDBACK constraint 2). Kept importable from here
# because `tests/test_preview_generation_epoch.py` and frozen artifacts name it.


class ProgressEpochs:
    """Stateful progress-epoch boundaries: typed events AND authority replacement.

    Progress is not currency. This answers "has the run moved forward since that
    FAILED call?" for `check_tool_error_thrash`, which skips successes outright,
    so its boundary set is `PROGRESS_BOUNDARY_KINDS` — every currency rule EXCEPT
    a bare workspace mutation. `test_receiptless_success_is_not_a_progress_
    boundary` pins that exclusion: a hollow edit must not launder a repeated tool
    error. Byte-identical in behaviour to the pre-2026-08-06z implementation; the
    rules are no longer implemented here, they are the shared owner's.
    """

    def __init__(self) -> None:
        self._boundaries = CurrencyBoundaries(include_workspace_mutation=False)

    def crosses(self, event: dict[str, Any], *, action_ids: frozenset[str] | None = None) -> bool:
        return self._boundaries.crossing(event, action_ids=action_ids) is not None


def _longest_identical_streak(
    events: list[dict[str, Any]],
    *,
    action_ids: frozenset[str],
    failed_action_ids: frozenset[str] | None = None,
) -> tuple[int, str, list[int]]:
    """Return the longest exact-action streak that is still CURRENT.

    2026-08-06z, GROUNDED FEEDBACK constraint 2 ("one currency predicate, two
    consumers"): the window is the shared owner's
    (`disco.core.receipt_currency`), the same one the loop's W-39 echo uses to
    decide whether to hand an earlier answer back. This is the ONE permitted
    oracle-adjudication change, and it is exactly the owner's clause *"the cap
    must not count a re-verification made legitimate by intervening workspace
    change"*: a workspace mutation now breaks the streak, where before only a
    TRUSTED typed receipt did.

    The direction matters and is stated as a refusal, not a requirement — a
    mutation ends currency UNLESS its failure is on the record. A write proven to
    have failed changed nothing and must not launder a repeat; a write whose
    outcome is not yet known must not be read as having changed nothing either.

    This is the exact-repeat class ONLY. The semantic-shell class keeps its
    narrower per-language generation counter — three standing guard tests show a
    documentation write must not launder a repeated script verification there,
    and that class groups three different spellings of one script, so its notion
    of relevance is genuinely finer than "the workspace changed".

    Replay-proven over the preserved wave-1 and 06v corpora (38 cells):
    ZERO classification flips, with all five reds reproduced live — including the
    97601 negative control, which stays red because nothing in fact changed
    between its three repeats.
    """
    from ._thrash_checks import _fingerprint

    best_count, best_fp, best_seqs = 0, "", []
    current_fp, current_seqs = "", []
    approved_scope: tuple[str, ...] | None = None
    boundaries = CurrencyBoundaries()
    for event in events:
        next_scope = approved_plan_predicate_scope(event)
        if next_scope is not None:
            if approved_scope != next_scope:
                current_fp, current_seqs = "", []
                approved_scope = next_scope
            continue
        if (
            boundaries.crossing(
                event,
                action_ids=action_ids,
                failed_action_ids=failed_action_ids,
            )
            is not None
        ):
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

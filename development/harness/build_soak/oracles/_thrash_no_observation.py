"""F59 no-observation streak helper — extracted to keep _thrash_checks within budget."""

from __future__ import annotations

from typing import Any

from ..events import KIND_ACTION, action_id_of, kind_of, seq_of


def is_no_observation_streak(
    events: list[dict[str, Any]],
    outcomes: dict[str, tuple[bool, str, str]],
    streak_seqs: list[int],
) -> bool:
    if not streak_seqs:
        return False
    first = next(
        (e for e in events if kind_of(e) == KIND_ACTION and seq_of(e) == streak_seqs[0]),
        None,
    )
    if first is None:
        return False
    return str(action_id_of(first) or "") not in outcomes

"""Fuzzy tool-name matching — the Rung-7 "did you mean?" recovery (F2 / T9).

Extracted from `engine.py` (god-file decomposition, Wave 1): these are pure
string helpers with no loop state, used only to nudge a hallucinated tool name
toward the nearest real one when the weak-model assist gate is on.
"""

from __future__ import annotations


def _levenshtein(a: str, b: str) -> int:
    """Small inline Levenshtein distance (edit cost 1 per ins/del/sub).

    O(len(a) * len(b)) time, O(len(b)) space. Used only to nudge a hallucinated
    tool name toward a real one in the Rung-7 hint when the assist gate is on
    (F2 / T9). Tool names are short (a few dozen chars at most), so no
    micro-optimisation is warranted; readability over cleverness.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Make `b` the shorter string — the inner loop is the memory hot spot.
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _nearest_tool_name(target: str, candidates: set[str]) -> str | None:
    """Return the candidate with the smallest Levenshtein distance to `target`.

    None if `candidates` is empty. The candidate set is whatever pool the
    caller wants to suggest from — for the Rung-7 hint, that's the set the
    model was *just* shown (`offered_names`); suggesting a tool the model
    can't see would be a worse recovery than no suggestion.
    """
    if not candidates:
        return None
    best_name: str | None = None
    best_dist = -1
    for name in candidates:
        d = _levenshtein(target, name)
        if best_dist < 0 or d < best_dist:
            best_dist = d
            best_name = name
    return best_name

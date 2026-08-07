"""English ordinals for agent-facing repetition clauses.

F56 (2026-08-07k): the repetition-aware feedback surfaces this campaign built
rendered their counts with a naive ``f"{n}th"``, which is correct only for 4-20.
A live agent in the 07j sealed corpus was told "This is the **2th** time the plan
verification conditions have failed in this run".

The defect is invisible to the tests those surfaces already have, and that is
the interesting part: the clause exists ONLY on the repeat path, so every
first-firing test renders the empty string instead, and every byte-identity
check compares two firings to EACH OTHER rather than to English — so two
correctly-differing bodies both containing "2th" pass every gate the campaign
had. It took reading the raw bytes to see it.

One implementation owner, because the idiom had already been copy-pasted to a
second seam (`turn_control_support._serve_handoff_guidance`) before anyone read
either one out loud. A third copy is how this comes back.
"""

from __future__ import annotations

_TEENS = {11, 12, 13}
_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def ordinal_suffix(n: int) -> str:
    """The English ordinal suffix for ``n``.

    11/12/13 take "th" despite ending in 1/2/3 — the trap a naive ``% 10`` rule
    falls into in the opposite direction from a naive ``"th"``. Both traps are
    in the test table.
    """
    if abs(n) % 100 in _TEENS:
        return "th"
    return _SUFFIXES.get(abs(n) % 10, "th")


def ordinal(n: int) -> str:
    """``n`` with its English ordinal suffix: 1st, 2nd, 3rd, 4th, 11th, 21st."""
    return f"{n}{ordinal_suffix(n)}"

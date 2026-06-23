"""CW-2 + CW-6 — capability-gated, context-window-derived context caps.

ROOT (the read-loop): the snapshot + read + observation-snip caps were a SINGLE
hardcoded calibration tuned for the weak (assist) tier. On a capable model with a
large context window that calibration is far too small — a file >6KB can't be
pinned in full, >7KB can't be read in one shot, and a large read observation is
render-snipped to a corrupted head/tail — so the model "never fully sees" a file
and re-reads it forever (live-proven: an 11KB board.js re-read; an 8KB windows.js
re-read 20×).

FIX: derive every cap from `(assist, context_window)`. When assist is ON the caps
are the byte-identical pre-existing calibration; when assist is OFF they scale with
the live driver context window (CW-1's `_driver_context_window()`), BOUNDED so even
a small-window capable model gets a sane (never unbounded) cap.

This module is the ONE place the derivation lives so the snapshot pin cap
(view_render), the file_read page budget (files.py via ToolContext), and the
observation-snip cap (events.py) can never drift out of the consistency invariant:

    obs_snip_chars >= read_char_budget >= per_file pin cap

so a file that fits the pin can also be read in one shot AND survive the
observation snip intact. Leaf module (no intra-loop imports) → safe to import from
core (events/view_render) and from the agent-server runtime alike.
"""

from __future__ import annotations

from dataclasses import dataclass

# Rough chars-per-token for budgeting prompt text against a token window. English
# code/prose averages ~3.5-4 chars/token; 3.5 keeps the derived char budgets
# CONSERVATIVE (we under-claim the window rather than overrun it).
CHARS_PER_TOKEN = 3.5

# Assist-ON baseline caps — today's weak-model calibration. The derive function
# returns EXACTLY these when assist is ON (or the window is unknown), and the
# call sites fall back to their own identical module constants when a cap is not
# threaded, so the assist-ON / unthreaded path stays byte-identical.
ASSIST_MAX_FILES = 8
ASSIST_PER_FILE_CHARS = 6_000
ASSIST_TOTAL_CHARS = 16_000
ASSIST_READ_CHAR_BUDGET = 7_000
ASSIST_OBS_SNIP_CHARS = 8_000

# assist-OFF derivation bounds.
_SNAPSHOT_WINDOW_FRACTION = 0.50  # ~50% of the window (in chars) for the pinned set
_PER_FILE_CEILING = 48_000  # a big source file fits in full, but never unbounded
_MAX_FILES_OFF = 20  # raised breadth for the larger working set (16-24 range)


@dataclass(frozen=True)
class ContextCaps:
    """The resolved cap bundle for a turn. Every consumer reads from ONE of these
    so the consistency invariant (obs >= read >= per_file) holds by construction."""

    max_files: int
    per_file_chars: int
    total_chars: int
    read_char_budget: int
    obs_snip_chars: int


_ASSIST_BASELINE = ContextCaps(
    max_files=ASSIST_MAX_FILES,
    per_file_chars=ASSIST_PER_FILE_CHARS,
    total_chars=ASSIST_TOTAL_CHARS,
    read_char_budget=ASSIST_READ_CHAR_BUDGET,
    obs_snip_chars=ASSIST_OBS_SNIP_CHARS,
)


def derive_context_caps(*, assist: bool, context_window: int | None) -> ContextCaps:
    """Resolve the per-turn context caps from the capability gate + live window.

    assist ON (or unknown/non-positive window) → the assist baseline, byte-identical
    to the pre-existing hardcoded calibration. assist OFF + a known window → caps
    scaled to that window, every value BOUNDED (per-file ceiling + half-total guard
    + a window-fraction total) so even a small-window capable model gets a sane,
    never-unbounded cap.
    """
    if assist or not context_window or context_window <= 0:
        return _ASSIST_BASELINE
    window_chars = int(context_window * CHARS_PER_TOKEN)
    # Total pinned-snapshot budget: ~50% of the window, floored at the assist
    # baseline so a tiny window is never WORSE than the assist tier.
    total_chars = max(ASSIST_TOTAL_CHARS, int(window_chars * _SNAPSHOT_WINDOW_FRACTION))
    # Per-file pin: large enough for a real source file (ceiling 48k), but never
    # more than half the total (so multiple files still fit), never below baseline.
    per_file_chars = max(ASSIST_PER_FILE_CHARS, min(_PER_FILE_CEILING, total_chars // 2))
    # CW-6 consistency: the read budget must be >= the per-file pin (a file that
    # fits the pin reads in one shot) and the obs snip must be >= the read budget
    # (the large read observation survives the render snip intact, not corrupted).
    read_char_budget = max(ASSIST_READ_CHAR_BUDGET, per_file_chars)
    obs_snip_chars = max(ASSIST_OBS_SNIP_CHARS, read_char_budget)
    return ContextCaps(
        max_files=_MAX_FILES_OFF,
        per_file_chars=per_file_chars,
        total_chars=total_chars,
        read_char_budget=read_char_budget,
        obs_snip_chars=obs_snip_chars,
    )

# CW-6 — the observation-snip override (the cap raised in tandem with the read
# budget for assist-OFF) lives in events.py as `obs_snip_override`. That module is
# a foundational leaf (it defines LLMMessage); importing the `loop` package from it
# would cycle, so the ContextVar is defined there and ViewBuilder.build sets it from
# the derived `ContextCaps.obs_snip_chars`.

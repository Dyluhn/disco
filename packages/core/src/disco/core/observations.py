"""CXT-5 — recoverable observation excerpts + the destructive-elision scan engine.

The campaign rule: never show incomplete source-like content with placeholder text
that could be copied into files. Truncation is fine IF it is RECOVERABLE — it must
name how to fetch the omitted part (a path + range, a re-run hint, an offset). This
module provides the canonical recoverable-excerpt shape and the scan that flags
DESTRUCTIVE (no-recover) elision. The P1 Product Harness OutputTruthOracle imports
DESTRUCTIVE_ELISION_MARKERS + scan_for_destructive_elision to fail builds whose
final deliverables contain unrecoverable elision (rule set v1).
"""

from __future__ import annotations

import hashlib
from typing import Any

# Exact phrases (case-insensitive) that signal content was thrown away. These are
# the forbidden markers for source-like content; the bare "…"/"..." ellipsis is
# intentionally NOT here (too common as a head/tail separator).
DESTRUCTIVE_ELISION_MARKERS: tuple[str, ...] = (
    "(elided)",
    "[trimmed]",
    "content omitted",
    "[content omitted]",
    "truncated for brevity",
)

# Tokens that make a truncation RECOVERABLE — if any appears on the same line as a
# marker, the truncation is fine (it tells the model how to get the rest).
RECOVER_CUES: tuple[str, ...] = (
    "file_read",
    "file_edit",
    "re-run",
    "rerun",
    "offset=",
    "grep",
    "read the full",
    "full output at",
    "full diagnostics at",
    "read the manifest",
    "read more",
)


def scan_for_destructive_elision(text: str) -> list[str]:
    """Return the offending lines: those containing a DESTRUCTIVE_ELISION_MARKER
    with NO RECOVER_CUE on the same line. Empty list = clean. Already-recoverable
    markers in-tree (snip_content, shell spill, file_read header, …) all carry a
    cue, so they are never flagged."""
    out: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(m in low for m in DESTRUCTIVE_ELISION_MARKERS) and not any(
            c in low for c in RECOVER_CUES
        ):
            out.append(line.strip())
    return out


def recoverable_excerpt(
    full_content: str,
    *,
    path: str,
    shown_start: int,
    shown_end: int,
    recover_tool: str = "file_read",
    recover_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical recoverable-excerpt record for a source-like read.

    Line numbers are 1-based inclusive. ``complete`` is True only when the shown
    range covers the whole content. ``recover`` tells the model exactly how to
    fetch an omitted range; ``sha256`` lets a consumer detect drift.
    """
    lines = full_content.splitlines()
    total = len(lines)
    shown_start = max(1, shown_start)
    shown_end = min(total, shown_end) if total else 0
    complete = shown_start <= 1 and shown_end >= total

    omitted: list[list[int]] = []
    if shown_start > 1:
        omitted.append([1, shown_start - 1])
    if shown_end < total:
        omitted.append([shown_end + 1, total])

    if recover_args is None:
        # default: fetch the first omitted region (or the whole file if all shown)
        if omitted:
            lo, _hi = omitted[0]
            recover_args = {"path": path, "offset": lo, "limit": 240}
        else:
            recover_args = {"path": path}

    return {
        "kind": "file_excerpt",
        "path": path,
        "complete": complete,
        "shown_ranges": [[shown_start, shown_end]],
        "omitted_ranges": omitted,
        "sha256": hashlib.sha256(full_content.encode("utf-8")).hexdigest(),
        "recover": {"tool": recover_tool, "args": recover_args},
    }

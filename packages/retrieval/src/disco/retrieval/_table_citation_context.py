"""Bound report table evidence to its immediately preceding introduction."""

from __future__ import annotations

import re
from collections.abc import Callable


def _caption_ids(lines: list[str], header: int, cite: re.Pattern[str]) -> tuple[str, ...]:
    index = header - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index < 0 or (index > 0 and lines[index - 1].strip()):
        return ()
    caption = lines[index].strip()
    if re.match(r"(?:[#>|*+\-`~]|\d+[.)]\s)", caption):
        return ()
    if not cite.sub("", caption).rstrip().endswith(":"):
        return ()
    return tuple(dict.fromkeys(cite.findall(caption)))


def _table_caption_citations(
    text: str,
    row_starts: set[int],
    cite: re.Pattern[str],
    is_divider: Callable[[str], bool],
) -> dict[int, tuple[str, ...]]:
    """Return candidates for recognized contiguous table rows, not direct citations.

    Row recognition remains the grounding parser's responsibility. This only
    attaches a single-line colon introduction; it cannot establish support.
    """
    lines = text.splitlines(keepends=True)
    result: dict[int, tuple[str, ...]] = {}
    offset = 0
    fence = ""
    prior_row = False
    candidates: tuple[str, ...] = ()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            fence = "" if fence == marker else fence or marker
        row = not fence and offset in row_starts
        if row:
            if not prior_row:
                candidates = (
                    _caption_ids(lines, index - 2, cite)
                    if index >= 2 and is_divider(lines[index - 1])
                    else ()
                )
            if candidates:
                result[offset] = candidates
        else:
            candidates = ()
        prior_row = bool(row)
        offset += len(line)
    return result

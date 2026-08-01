"""Best-effort line-span matching for stale/anchored edit anchors.

``_best_fuzzy_old_match_lines`` is decomposed into a target-preparation step
and a sliding-window scoring step so each stays independently readable and
under the per-callable complexity budget — the two steps were previously one
function that both normalized the anchor AND scanned every candidate window.
"""

from __future__ import annotations

from ._text_norm import _norm_ws, _strip_line_numbers


def _matched_old_lines(text: str, old: str) -> tuple[int, int] | None:
    """Best-effort line span for the file_edit old text under the same forgiving match family."""
    candidates: list[str] = []
    for candidate in (old, _strip_line_numbers(old)):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        idx = text.find(candidate)
        if idx >= 0:
            line = text.count("\n", 0, idx) + 1
            return (line, line + candidate.count("\n"))

    target = _norm_ws(_strip_line_numbers(old))
    if not target:
        return None
    doc = text.splitlines(keepends=True)
    norm = [x.strip() for x in doc]
    tgt = target.split("\n")
    for i in range(0, len(norm) - len(tgt) + 1):
        if norm[i : i + len(tgt)] == tgt:
            return (i + 1, i + len(tgt))
    return None


def _find_text_lines(text: str, needle: str) -> tuple[int, int] | None:
    """Return the 1-based line span where ``needle`` first appears, if it is non-empty."""
    if not needle.strip():
        return None
    idx = text.find(needle)
    if idx < 0:
        return None
    start = text.count("\n", 0, idx) + 1
    line_count = max(1, len(needle.splitlines()))
    return (start, start + line_count - 1)


def _prepare_fuzzy_target(old: str, text: str) -> tuple[str, list[str], list[str]] | None:
    """Normalize `old` into a match target + the candidate `lines` to scan.

    Returns (target, old_lines, lines), or None if either side is empty after
    normalization (nothing plausible to match against)."""
    old = _strip_line_numbers(old).strip("\n")
    if not old.strip():
        return None

    lines = text.splitlines()
    if not lines:
        return None

    old_lines = old.splitlines() or [old]
    target = _norm_ws(old)
    target_nonblank = "\n".join(ln.strip() for ln in old_lines if ln.strip())
    if target_nonblank:
        target = target_nonblank
    if not target:
        return None
    return target, old_lines, lines


def _best_fuzzy_window(
    lines: list[str], target: str, old_lines: list[str]
) -> tuple[float, int, int] | None:
    """Slide window sizes near `old_lines`'s length over `lines`, scoring each
    candidate against `target`; return the best (score, start_line, end_line)."""
    from difflib import SequenceMatcher

    target_line_count = max(1, len([ln for ln in old_lines if ln.strip()]) or len(old_lines))
    candidate_sizes = sorted(
        {
            max(1, target_line_count - 1),
            target_line_count,
            target_line_count + 1,
        }
    )
    best: tuple[float, int, int] | None = None
    for size in candidate_sizes:
        if size > len(lines):
            continue
        for idx in range(0, len(lines) - size + 1):
            candidate = "\n".join(ln.strip() for ln in lines[idx : idx + size] if ln.strip())
            if not candidate:
                continue
            score = SequenceMatcher(None, target, candidate).ratio()
            if best is None or score > best[0]:
                best = (score, idx + 1, idx + size)
    return best


def _best_fuzzy_old_match_lines(text: str, old: str) -> tuple[int, int] | None:
    """Find a plausible current line span for a stale anchored edit."""
    prepared = _prepare_fuzzy_target(old, text)
    if prepared is None:
        return None
    target, old_lines, lines = prepared

    match = _best_fuzzy_window(lines, target, old_lines)
    if match is None or match[0] < 0.55:
        return None
    return (match[1], match[2])

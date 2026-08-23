"""Markdown table repair + continuation-join mechanics for the report writer.

Originally extracted from the v1 per-section `synthesis.py` (removed by the
v2 agentic rework, PKG-35); the writer (`writer.py` / `_writer_parts.py`)
still uses these rendering-safety repairs. Each piece owns exactly one
decision; none of it edits prose content — pipe normalization and delimiter
injection only.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# BW-07 table repair — split so each piece owns one decision: gathering a
# candidate run, judging whether it's really a table, and rebuilding a
# confirmed one.
# ---------------------------------------------------------------------------

_DELIM_CELL_RE = re.compile(r"^\s*:?-{1,}:?\s*$")


def _is_delimiter_row(line: str) -> bool:
    """A GFM table delimiter row — every cell is ``-``/``:--``/``--:``/``:-:``."""
    s = line.strip()
    if "|" not in s or "-" not in s:
        return False
    inner = s.strip("|")
    cells = inner.split("|")
    return bool(cells) and all(_DELIM_CELL_RE.match(c) for c in cells)


def _normalize_table_row(line: str, ncols: int) -> str:
    """Re-emit a pipe row with leading/trailing pipes and exactly ``ncols``
    cells (pad short, clip long) so remark-gfm parses it."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = [c.strip() for c in inner.split("|")]
    if len(cells) < ncols:
        cells += [""] * (ncols - len(cells))
    elif len(cells) > ncols:
        cells = cells[:ncols]
    return "| " + " | ".join(cells) + " |"


def _gather_pipe_run(lines: list[str], start: int, n: int) -> list[str]:
    """Collect a run of consecutive pipe-bearing, non-fence, non-blank lines
    starting at ``start``."""
    run: list[str] = []
    j = start
    while j < n:
        lj = lines[j]
        if lj.lstrip().startswith("```") or "|" not in lj or not lj.strip():
            break
        run.append(lj)
        j += 1
    return run


def _header_cell_count(row: str) -> int:
    return len([c for c in row.strip().strip("|").split("|")])


def _looks_like_table(run: list[str], header_cells: int) -> bool:
    """A run qualifies as a real table if it already carries a delimiter row,
    or has >= 2 rows with a >= 2-cell header. A lone single-pipe line (a
    truncated fragment, or prose that happens to contain a ``|``) does not."""
    has_delim = any(_is_delimiter_row(r) for r in run)
    return has_delim or (len(run) >= 2 and header_cells >= 2)


def _rebuild_table(run: list[str], ncols: int) -> list[str]:
    """Normalize a confirmed table run: header + injected/kept delimiter row +
    body, each re-emitted to exactly ``ncols`` cells."""
    rebuilt: list[str] = [_normalize_table_row(run[0], ncols)]
    body = run[1:]
    if body and _is_delimiter_row(body[0]):
        body = body[1:]
    rebuilt.append("| " + " | ".join(["---"] * ncols) + " |")
    for r in body:
        rebuilt.append(_normalize_table_row(r, ncols))
    return rebuilt


def repair_tables(markdown: str) -> str:
    """BW-07 — repair malformed GFM tables so remark-gfm parses them instead
    of falling back to a literal-pipe paragraph."""
    lines = markdown.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence or "|" not in line:
            out.append(line)
            i += 1
            continue

        run = _gather_pipe_run(lines, i, n)
        j = i + len(run)
        header_cells = _header_cell_count(run[0])
        if not _looks_like_table(run, header_cells):
            out.extend(run)
            i = j
            continue

        out.extend(_rebuild_table(run, max(2, header_cells)))
        i = j
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Continuation join for the truncation guard (used by `_writer_parts`).
# ---------------------------------------------------------------------------


def _join_continuation_text(markdown: str, stripped_extra: str) -> str:
    """Join a continuation fragment onto ``markdown`` per the three BW-07
    cases: a clean line/word boundary (join on a fresh line), a table row
    split right after its closing pipe but before the newline (also a fresh
    line — a direct glue would fuse two rows via a tell-tale ``||``), or a cut
    mid-token/mid-cell (glue with no separator so the fragment completes)."""
    if markdown[-1:].isspace():
        return markdown.rstrip() + "\n" + stripped_extra
    if markdown[-1:] == "|" and stripped_extra[:1] == "|":
        return markdown + "\n" + stripped_extra
    return markdown + stripped_extra

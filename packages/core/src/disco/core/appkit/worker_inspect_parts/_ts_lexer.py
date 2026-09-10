"""Tiny, pure lexical primitives extracted from ``worker_inspect``'s comment
stripper and call-position scanner: blanking a single `//` or `/* */` comment,
and skipping run-of-whitespace. Each owns exactly one mechanical concern and
takes/returns plain string + index values — no back-reference into
``worker_inspect`` is needed here (none of this is a name the parent module's
tests reach through).
"""

from __future__ import annotations


def _blank_line_comment(src: str, i: int) -> tuple[str, int]:
    """Blank a `//...` comment starting at `src[i]` (which must be the first `/`)
    up to (not including) the trailing newline or EOF. Returns (blanked_text,
    index just past the comment)."""
    n = len(src)
    start = i
    while i < n and src[i] != "\n":
        i += 1
    return " " * (i - start), i


def _blank_block_comment(src: str, i: int) -> tuple[str, int]:
    """Blank a `/* ... */` comment starting at `src[i]` (which must be the `/` of
    the opening `/*`), preserving newlines so line numbers stay stable. Returns
    (blanked_text, index just past the closing `*/`)."""
    n = len(src)
    out = ["  "]
    i += 2
    while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
        out.append("\n" if src[i] == "\n" else " ")
        i += 1
    out.append("  ")
    i += 2
    return "".join(out), i


def _skip_ws(text: str, i: int) -> int:
    """Index of the first non-`" \\t\\r\\n"` character at/after `i` (or `len(text)`)."""
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i

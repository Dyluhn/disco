"""Line-number rendering/stripping and whitespace-normalization helpers shared by
the file-edit tools and their fresh-edit / fuzzy-match guards."""

from __future__ import annotations

from ._constants import _LINENO_PREFIX


def _number_lines(text: str, start: int = 1) -> str:
    """Render text with right-aligned 1-based line numbers + a tab, so the model
    can target precise ranges with file_replace_lines / file_insert_lines — the
    robust way to edit a large file without reproducing its exact bytes."""
    lines = text.splitlines()
    if not lines:
        return ""
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{start + i:>{width}}\t{ln}" for i, ln in enumerate(lines))


def _strip_line_numbers(s: str) -> str:
    """Remove accidental `N\\t` line-number prefixes the model copied from a read."""
    return _LINENO_PREFIX.sub("", s)


def _norm_ws(s: str) -> str:
    """Whitespace-normalized form for forgiving matching: strip BOTH ends of each
    line + drop blank leading/trailing lines. Tolerates the #1 cause of failed exact
    matches — indentation and trailing-space drift — which small models get wrong
    constantly. The replacement still uses the caller's `new` verbatim, so the edit's
    own indentation is whatever the model intended."""
    return "\n".join(ln.strip() for ln in s.strip("\n").splitlines())

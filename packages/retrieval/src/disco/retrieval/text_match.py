"""Locate a phrase in text a machine extracted from a page.

Extraction damages text in ways the reader of the page never saw. It puts a
space inside a word ("stormwater ou tflow"), leaves a soft hyphen where a line
broke ("Rec­ommendations"), spells a dash with whichever code point the
converter preferred, and wraps a unit in the markdown emphasis the table cell
had ("207 _kWh/year_"). Anyone quoting that page — a reader model taking
notes, a reviewer searching for a figure — quotes what the page SAYS, so a
matcher that compares bytes reports the text as absent and a true quote is
thrown away or a present table is recorded as missing. Both happened, and both
cost a report its answer.

So the comparison drops what extraction added and keeps everything that can
change a meaning. Whitespace, soft hyphens, zero-width characters and
typographic dash variants go. The plain hyphen-minus STAYS: in a source that
writes "10-6 cm/s" for ten to the minus six, letting a hyphen vanish would make
"106 cm/s" match it. Underscores and asterisks go only where they sit at the
edge of a word, which is where emphasis puts them and where an operator or an
identifier does not: "3*4" is not "34" and "snake_case" is not "snakecase".

Every match maps back to offsets in the ORIGINAL text, so what a caller reports
is the span the page really holds, artefacts and all.
"""

from __future__ import annotations

#: Invisible characters extraction leaves behind. None of them is readable, so
#: none of them can be the difference between two readings of a phrase.
_INVISIBLE = frozenset("­​‌‍﻿")

#: Dash variants a page and a quoter spell differently for the same dash. The
#: plain hyphen-minus is deliberately absent: see the module docstring.
_DASHES = dict.fromkeys("‐‑‒–—―⁃−", "-")

#: Markdown emphasis characters, removable only at a word edge.
_EMPHASIS = frozenset("_*")


def _is_emphasis(text: str, index: int) -> bool:
    """Whether this `_` or `*` is emphasis rather than part of the token.

    Emphasis sits at the edge of a word. An operator between two digits and an
    underscore inside an identifier sit between two alphanumerics, and those
    are content.
    """
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return not (before.isalnum() and after.isalnum())


def _comparable(text: str, index: int) -> str:
    """One character as the matcher sees it, or "" when extraction added it."""
    character = text[index]
    if character.isspace() or character in _INVISIBLE:
        return ""
    if character in _EMPHASIS and _is_emphasis(text, index):
        return ""
    return _DASHES.get(character, character)


def _folded(character: str) -> str:
    """Lowercase one character without changing its length.

    A whole-string `lower()` can lengthen text (Turkish dotted capital I among
    others) and every added character would shift the offset map, so folding is
    per character and refuses any expansion.
    """
    lowered = character.lower()
    return lowered if len(lowered) == 1 else character


def normalized_index(text: str, *, fold: bool = False) -> tuple[str, list[int]]:
    """The comparable form of `text`, and each kept character's original offset.

    `fold=True` compares case-insensitively, for a caller locating a topic
    rather than verifying a quote.
    """
    characters: list[str] = []
    offsets: list[int] = []
    for index in range(len(text)):
        comparable = _comparable(text, index)
        if comparable:
            characters.append(_folded(comparable) if fold else comparable)
            offsets.append(index)
    return "".join(characters), offsets


def normalized_form(text: str, *, fold: bool = False) -> str:
    """What the matcher actually compares — quotable in a "not found" report."""
    return normalized_index(text, fold=fold)[0]


def locate(
    needle: str, index: tuple[str, list[int]], start: int = 0, *, fold: bool = False
) -> tuple[int, int] | None:
    """The `(start, end)` the needle occupies in the original text, or None.

    `start` is an offset in the ORIGINAL text; the search begins at or after it.
    `fold` must match how `index` was built.
    """
    normalized, offsets = index
    wanted = normalized_form(needle, fold=fold)
    if not wanted:
        return None
    first = next((position for position, offset in enumerate(offsets) if offset >= start), None)
    if first is None:
        return None
    position = normalized.find(wanted, first)
    if position < 0:
        return None
    return offsets[position], offsets[position + len(wanted) - 1] + 1


def locate_all(
    needle: str, index: tuple[str, list[int]], *, limit: int, fold: bool = False
) -> list[tuple[int, int]]:
    """Up to `limit` non-overlapping spans the needle occupies, in order."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    while len(spans) < limit:
        span = locate(needle, index, cursor, fold=fold)
        if span is None:
            break
        spans.append(span)
        cursor = max(span[0] + 1, span[1])
    return spans


__all__ = ["locate", "locate_all", "normalized_form", "normalized_index"]

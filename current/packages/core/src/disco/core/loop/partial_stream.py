"""Partial JSON string-field extraction — split from ``stream_extract.py``.

The watch-it-write UX needs to show a ``file_write``'s ``content`` growing while
the model is still emitting it.  At that point the accumulated ``arguments``
string is not valid JSON, so ``json.loads`` is useless.  This decoder walks the
raw string, locates the field's opening quote, and decodes JSON string escapes
until either the closing quote arrives or the stream tail is reached.

It is deliberately forgiving: a ``None`` return just means "the field hasn't
started yet", and the loop simply shows nothing for that frame.
"""

from __future__ import annotations

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
}

_WS = " \t\r\n"


def _decode_unicode_escape(raw: str, i: int, n: int, out: list[str]) -> int:
    """Decode a ``\\uXXXX`` escape at ``raw[i]``.  Returns the new index.

    Handles astral surrogate pairs.  Stops cleanly on a truncated or malformed
    pair so the caller never emits a broken character.
    """
    if i + 6 > n:
        return n  # truncated \uXXXX — caller stops
    try:
        cp = int(raw[i + 2 : i + 6], 16)
    except ValueError:
        out.append("\\u" + raw[i + 2 : i + 6])
        return i + 6
    if 0xD800 <= cp <= 0xDBFF:
        return _decode_surrogate_pair(raw, i, n, cp, out)
    out.append(chr(cp))
    return i + 6


def _decode_surrogate_pair(
    raw: str, i: int, n: int, high: int, out: list[str]
) -> int:
    """Combine a UTF-16 surrogate pair.  Returns the new index or ``n`` on failure."""
    if i + 12 > n or raw[i + 6 : i + 8] != "\\u":
        return n  # low surrogate not present yet — stop cleanly
    try:
        low = int(raw[i + 8 : i + 12], 16)
    except ValueError:
        return n
    if 0xDC00 <= low <= 0xDFFF:
        out.append(chr(0x10000 + (high - 0xD800) * 0x400 + (low - 0xDC00)))
        return i + 12
    return n  # malformed pair — don't emit a lone surrogate


def _decode_escape(raw: str, i: int, n: int, out: list[str]) -> int:
    """Decode one ``\\`` escape at ``raw[i]``.  Returns the new index."""
    if i + 1 >= n:
        return n  # incomplete escape at the tail
    e = raw[i + 1]
    if e == "u":
        return _decode_unicode_escape(raw, i, n, out)
    out.append(_ESCAPES.get(e, e))
    return i + 2


def extract_partial_string_field(
    raw: str, field: str, *, require_complete: bool = False
) -> str | None:
    """Decode the value of string ``field`` from a possibly-incomplete JSON object
    string. Returns the value decoded *so far*, or None if the field's opening
    quote has not been emitted yet.

    With `require_complete=True`, returns None until the value's *closing* quote
    has arrived — use this for fields you want whole (e.g. a `path`), so a frame
    never shows a half-typed filename. Without it, returns the growing value —
    use this for the streamed body (`content`).

    Robust to: the closing quote not having arrived; an incomplete escape at the
    very tail (`\\`); a truncated unicode escape (`\\u00`). In those tail cases it
    returns everything decodable up to the break.
    """
    key = f'"{field}"'
    ki = raw.find(key)
    if ki == -1:
        return None
    i = ki + len(key)
    n = len(raw)
    # consume  <ws> : <ws> "
    while i < n and raw[i] in _WS:
        i += 1
    if i >= n or raw[i] != ":":
        return None
    i += 1
    while i < n and raw[i] in _WS:
        i += 1
    if i >= n or raw[i] != '"':
        return None
    i += 1  # past the opening quote

    out: list[str] = []
    closed = False
    while i < n:
        c = raw[i]
        if c == '"':
            closed = True
            break
        if c == "\\":
            i = _decode_escape(raw, i, n, out)
            continue
        out.append(c)
        i += 1
    if require_complete and not closed:
        return None
    return "".join(out)
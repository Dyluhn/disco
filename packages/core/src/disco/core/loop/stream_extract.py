"""Best-effort extraction of a string field's value out of a *partial* JSON
object — a tool call's `arguments` as it streams in.

The watch-it-write UX needs to show a `file_write`'s `content` growing while the
model is still emitting it. At that point the accumulated `arguments` string is
not valid JSON (it has no closing brace, and may even end mid-escape), so
`json.loads` is useless. This decoder walks the raw string, locates the field's
opening quote, and decodes JSON string escapes until either the closing quote
arrives or the stream tail is reached — stopping *cleanly* at an incomplete
trailing escape (a lone `\\` or a truncated `\\uXXXX`) so we never emit a broken
character or raise.

It is deliberately forgiving: a `None` return just means "the field hasn't
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


def extract_partial_string_field(
    raw: str, field: str, *, require_complete: bool = False
) -> str | None:
    """Decode the value of string `field` from a possibly-incomplete JSON object
    string. Returns the value decoded *so far*, or None if the field's opening
    quote has not been emitted yet.

    With `require_complete=True`, returns None until the value's *closing* quote
    has arrived — use this for fields you want whole (e.g. a `path`), so a frame
    never shows a half-typed filename. Without it, returns the growing value —
    use this for the streamed body (`content`).

    Robust to: the closing quote not having arrived; an incomplete escape at the
    very tail (`...\\`); a truncated unicode escape (`...\\u00`). In those tail
    cases it returns everything decodable up to the break.
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
            closed = True  # closing quote — value is complete
            break
        if c == "\\":
            if i + 1 >= n:
                break  # incomplete escape at the tail — stop cleanly
            e = raw[i + 1]
            if e == "u":
                if i + 6 > n:
                    break  # truncated \uXXXX at the tail
                try:
                    cp = int(raw[i + 2 : i + 6], 16)
                except ValueError:
                    out.append("\\u" + raw[i + 2 : i + 6])
                    i += 6
                    continue
                # Astral characters (emoji, etc.) arrive as a UTF-16 SURROGATE PAIR
                # \uD800-\uDBFF \uDC00-\uDFFF — combine them into one code point.
                # If the low half hasn't streamed yet, stop cleanly (a lone surrogate
                # would corrupt the value, breaking the streaming prefix invariant).
                if 0xD800 <= cp <= 0xDBFF:
                    if i + 12 > n or raw[i + 6 : i + 8] != "\\u":
                        break  # low surrogate not present yet — wait for more
                    try:
                        low = int(raw[i + 8 : i + 12], 16)
                    except ValueError:
                        break
                    if 0xDC00 <= low <= 0xDFFF:
                        out.append(chr(0x10000 + (cp - 0xD800) * 0x400 + (low - 0xDC00)))
                        i += 12
                        continue
                    break  # malformed pair — don't emit a lone surrogate
                out.append(chr(cp))
                i += 6
                continue
            out.append(_ESCAPES.get(e, e))
            i += 2
            continue
        out.append(c)
        i += 1
    if require_complete and not closed:
        return None
    return "".join(out)

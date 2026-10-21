"""Remove presentation delimiters without rewriting technical evidence."""

from __future__ import annotations

import re

_LITERAL = re.compile(
    r"\\[\\`*_{}\[\]()#+.!>~-]|(?P<ticks>`+)(?P<code>.*?)(?P=ticks)"
    r"|https?://[^\s<>`]+",
    re.DOTALL,
)
_EMPHASIS = re.compile(
    r"(?<![\w_])(?P<under>_{1,3})(?=\S)(?P<u>.+?)(?<=\S)(?P=under)(?![\w_])"
    r"|(?<!\*)(?P<star>\*{1,3})(?=\S)(?P<s>.+?)(?<=\S)(?P=star)(?!\*)"
)
_BLOCK_PREFIX = re.compile(r"^ {0,3}(?:#{1,6}[ \t]+|>[ \t]?)", re.MULTILINE)


def _plain_text(text: str) -> str:
    """Keep code spans, escaped punctuation and URLs literal during cleanup.

    Unpaired punctuation is evidence, not a formatting delimiter. Temporary
    markers cannot collide with the input and are removed before returning.
    Claim offsets remain owned by the original, unmodified report text.
    """
    marker = "\x00"
    while marker in text:
        marker += "\x00"
    literals: list[str] = []

    def protect(match: re.Match[str]) -> str:
        raw = match.group()
        value = match.group("code") if match.group("ticks") else raw
        if raw.startswith("\\"):
            value = raw[1:]
        literals.append(value)
        return f"{marker}{len(literals) - 1}{marker}"

    text = _LITERAL.sub(protect, text)
    text = _BLOCK_PREFIX.sub("", text)
    while True:
        plain = _EMPHASIS.sub(lambda match: match.group("u") or match.group("s"), text)
        if plain == text:
            break
        text = plain
    for index, literal in enumerate(literals):
        text = text.replace(f"{marker}{index}{marker}", literal)
    return text.strip()

"""Complete missing final container delimiters without changing any JSON value."""

from __future__ import annotations

import json
import re


def close_json_containers(text: str) -> str | None:
    """Return a closing-only candidate, or None when completion would need a guess.

    The caller must require a normally stopped provider response and strictly
    decode the result. This never closes strings, inserts values, fixes commas,
    deletes content, or accepts mismatched delimiters.
    """
    candidate = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if fence is not None:
        candidate = fence.group(1).strip()
    if not candidate.startswith("{"):
        return None
    pending = []
    decoder = json.JSONDecoder()
    position = 0
    while position < len(candidate):
        char = candidate[position]
        if char == '"':
            try:
                _, position = decoder.raw_decode(candidate, position)
            except json.JSONDecodeError:
                return None
            continue
        if char in "{[":
            pending.append("}" if char == "{" else "]")
        elif char in "}]" and (not pending or pending.pop() != char):
            return None
        position += 1
    return candidate + "".join(reversed(pending)) if pending else None

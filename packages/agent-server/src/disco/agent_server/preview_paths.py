"""Shared structural path validation for every Preview transport."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_ENCODED_PREVIEW_STRUCTURAL = re.compile(r"%([0-9a-fA-F]{2})")


def safe_preview_path(raw: str, *, allow_leading_slash: bool) -> str | None:
    """Normalize one URL/workspace path without ever decoding it a second time.

    The ASGI server has already percent-decoded ``scope['path']``. A second
    unquote would turn a harmless literal ``%2e%2e`` filename into traversal.
    Empty and redundant slash components are allowed; dot-dot, backslashes,
    NULs, and an absolute manifest entry fail closed.
    """

    value = raw.strip()
    if "\x00" in value or "\\" in value:
        return None
    if not allow_leading_slash and value.startswith("/"):
        return None
    parts = [part for part in PurePosixPath(value.strip("/")).parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def safe_capability_target_path(raw: str) -> str | None:
    """Reject encoded structure a browser or upstream may reinterpret later."""

    matched_escapes = set(_ENCODED_PREVIEW_STRUCTURAL.finditer(raw))
    for match in matched_escapes:
        if chr(int(match.group(1), 16)) in {".", "/", "\\", "\x00", "%"}:
            return None
    # A stray '%' is browser-dependent and cannot be signed/proxied canonically.
    without_escapes = _ENCODED_PREVIEW_STRUCTURAL.sub("", raw)
    if "%" in without_escapes:
        return None
    return safe_preview_path(raw, allow_leading_slash=True)

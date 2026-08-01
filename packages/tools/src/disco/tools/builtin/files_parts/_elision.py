"""Elision-placeholder detection shared by every mutating file tool."""

from __future__ import annotations

from ._constants import _EDIT_ELISION_RE


def _has_elision_marker(*texts: str | None) -> bool:
    return any(t is not None and _EDIT_ELISION_RE.search(t) is not None for t in texts)

"""Preview capability scalar payload helpers.

Extracted from ``auth.py`` under the same rule as the other parts here: nothing
in this module is public API — ``auth.py`` re-imports both names unchanged.
"""

from __future__ import annotations

from typing import Any


def _cap_scalar_fields(
    owner: object,
    cid: object,
    prefix: object,
    port: object,
    exp: object,
    allow_websocket: object,
) -> tuple[str, str, str, int, int, bool] | None:
    """The capability's scalar payload fields, or None if any is the wrong type.

    Returns the values rather than a bool so the caller keeps the narrowing the
    inline isinstance chain used to give it. A bool-returning predicate erases
    it, and PreviewCapability's constructor then receives ``Any | None``.
    """
    if (
        isinstance(owner, str)
        and isinstance(cid, str)
        and isinstance(prefix, str)
        and isinstance(port, int)
        and isinstance(exp, int)
        and isinstance(allow_websocket, bool)
    ):
        return owner, cid, prefix, port, exp, allow_websocket
    return None


def _partitioned_cookies_from_payload(payload: dict[str, Any]) -> bool | None:
    """Legacy absence stays False; present must be a real bool."""
    if "partitioned_cookies" not in payload:
        return False
    value = payload["partitioned_cookies"]
    if not isinstance(value, bool):
        return None
    return value

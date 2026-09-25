"""Scalar and authority type checks for signed Preview payloads."""

from __future__ import annotations


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


def _cap_authority_invalid(authority_id: object) -> bool:
    return authority_id is not None and (
        not isinstance(authority_id, str) or not authority_id or len(authority_id) > 256
    )

"""Raw Cookie-header parsing shared by session and Preview capability verification.

Extracted verbatim from ``auth.py`` to reduce module size; the parent module
re-imports both names here unchanged (see ``auth_parts/__init__.py``).
"""

from __future__ import annotations

from typing import Any


def cookie_header_values(cookie_header: str | None, name: str) -> tuple[str, ...]:
    """Return every raw Cookie value for ``name`` without collapsing duplicates.

    Generated child domains can set a Domain cookie whose name matches a
    HostOnly signed cookie on the parent. Framework cookie mappings retain only
    one duplicate, so authentication must validate every raw candidate.
    Disco's signed values use an unquoted URL-safe alphabet.
    """

    if not cookie_header or not name:
        return ()
    values: list[str] = []
    for part in cookie_header.split(";"):
        raw_name, separator, value = part.strip().partition("=")
        if separator and raw_name == name:
            values.append(value)
    return tuple(values)


def cookie_header_from_headers(headers: Any) -> str | None:
    """Preserve every ASGI Cookie field for duplicate-candidate validation.

    HTTP/2 permits a user agent or intermediary to split Cookie across repeated
    fields. Starlette's mapping-style ``get`` returns only one field, which can
    hide a valid HostOnly signed value behind an attacker-controlled duplicate.
    ``getlist`` is intentionally duck-typed so core remains framework-free.
    """

    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        listed: Any = getlist("cookie")
        if isinstance(listed, (str, bytes)):
            listed = (listed,)
        values = [str(value) for value in listed if value]
        if values:
            return "; ".join(values)
    get = getattr(headers, "get", None)
    if callable(get):
        value = get("cookie")
        return str(value) if value else None
    return None

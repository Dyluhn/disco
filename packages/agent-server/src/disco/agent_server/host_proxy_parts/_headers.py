"""Cookie and response-header hygiene for the host preview proxy.

Keeps the reserved preview capability cookie (and, on the local gateway, the
whole disco session/preview cookie family) out of everything forwarded to a
generated app, and strips hop-by-hop / proxy headers from what comes back.
"""

from __future__ import annotations

from disco.core.auth import (
    LOCAL_PREVIEW_COOKIE_PREFIX,
    PREVIEW_COOKIE,
    SESSION_COOKIE,
)
from starlette.types import Scope


def _cookie_values(scope: Scope, name: str) -> tuple[str, ...]:
    needle = name + "="
    found: list[str] = []
    for header_name, value in scope.get("headers", []):
        if header_name.lower() != b"cookie":
            continue
        for part in value.decode("latin1").split(";"):
            item = part.strip()
            if item.startswith(needle):
                found.append(item[len(needle) :])
    return tuple(found)


def _local_reserved_cookie_name(name: str) -> bool:
    return bool(
        name in {PREVIEW_COOKIE, SESSION_COOKIE}
        or name.startswith("disco_path_preview_")
        or name.startswith(LOCAL_PREVIEW_COOKIE_PREFIX)
    )


def _strip_reserved_preview_cookie(value: str, *, local_gateway: bool = False) -> str | None:
    """Remove proxy credentials before forwarding a generated-app request."""

    kept: list[str] = []
    for raw_part in value.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        raw_name, separator, _cookie_value_text = part.partition("=")
        name = raw_name.strip()
        if separator and (
            name == PREVIEW_COOKIE or (local_gateway and _local_reserved_cookie_name(name))
        ):
            continue
        kept.append(part)
    return "; ".join(kept) or None


def _sets_reserved_preview_cookie(value: str, *, local_gateway: bool = False) -> bool:
    first_pair = value.split(";", 1)[0]
    raw_name, separator, _cookie_value_text = first_pair.partition("=")
    name = raw_name.strip()
    return bool(
        separator
        and (name == PREVIEW_COOKIE or (local_gateway and _local_reserved_cookie_name(name)))
    )


def _forwardable_response_header(
    name: str,
    value: str,
    hop_by_hop: set[str],
    *,
    local_gateway: bool = False,
) -> bool:
    name_lower = name.lower()
    if name_lower in hop_by_hop or name_lower.startswith("proxy-"):
        return False
    return not (
        name_lower == "set-cookie"
        and _sets_reserved_preview_cookie(value, local_gateway=local_gateway)
    )

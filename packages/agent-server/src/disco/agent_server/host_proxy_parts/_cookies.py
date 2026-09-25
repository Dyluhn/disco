"""Application cookie policy for verified cross-site canonical Preview embeds."""

from __future__ import annotations

import re

from disco.core.auth import PreviewCapability
from starlette.types import Scope

_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_EMBEDDED_ATTRIBUTES = {"samesite", "secure", "partitioned"}


def _embedded_cookie_policy(scope: Scope | None) -> bool:
    if scope is None:
        return False
    capability = (scope.get("state") or {}).get("canonical_preview_capability")
    return (
        isinstance(capability, PreviewCapability)
        and capability.authority_id is not None
        and capability.partitioned_cookies is True
    )


def _rewrite_embedded_app_cookie(value: str, scope: Scope | None) -> str:
    """Adapt only an authenticated embedded Preview; standalone stays byte-exact.

    Callers first filter reserved Disco cookie names. Retain the app's value,
    HttpOnly, Path, Domain, expiry, and other attributes (including prefix
    restrictions); replace only transport attributes. Deletion takes the same
    partitioned path, so logout cannot leave the embedded session cookie behind.
    """
    if not _embedded_cookie_policy(scope):
        return value
    pair, *attributes = value.split(";")
    name, separator, _cookie_value = pair.partition("=")
    if not separator or not _COOKIE_NAME.fullmatch(name.strip()):
        return value
    retained = [
        attribute.strip()
        for attribute in attributes
        if attribute.strip()
        and attribute.split("=", 1)[0].strip().lower() not in _EMBEDDED_ATTRIBUTES
    ]
    return "; ".join([pair, *retained, "Secure", "SameSite=None", "Partitioned"])

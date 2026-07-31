"""Canonical-upstream ``Location`` header rewriting for the local gateway.

Extracted from ``host_proxy._rewrite_canonical_upstream_location``
(PKG-10-SANDBOX), which exceeded the per-callable McCabe budget as one flat
function. Split into "does this redirect target the exact upstream?" and
"what request scheme/host do we rewrite onto?" so each half stays trivially
under budget; the orchestrating function is now a straight-line sequence of
two guard checks.
"""

from __future__ import annotations

import urllib.parse

from starlette.types import Scope


def _upstream_location_match(value: str, upstream: str) -> urllib.parse.SplitResult | None:
    """Return the parsed ``value`` iff it is an absolute redirect to ``upstream``."""
    try:
        location = urllib.parse.urlsplit(value)
        source = urllib.parse.urlsplit(upstream)
        location_port = location.port or (443 if location.scheme.lower() == "https" else 80)
        source_port = source.port or (443 if source.scheme.lower() == "https" else 80)
        if (
            not location.scheme
            or not location.netloc
            or location.username is not None
            or location.password is not None
            or location.scheme.lower() != source.scheme.lower()
            or (location.hostname or "").lower() != (source.hostname or "").lower()
            or location_port != source_port
        ):
            return None
    except (ValueError, UnicodeError):
        return None
    return location


def _request_scheme_and_host(scope: Scope) -> tuple[str, str] | None:
    """Return the (scheme, host) the client actually connected through."""
    request_host = ""
    forwarded_proto = ""
    for name, raw_value in scope.get("headers", []):
        if name.lower() == b"host":
            request_host = raw_value.decode("latin1")
        elif name.lower() == b"x-forwarded-proto":
            forwarded_proto = raw_value.decode("latin1").split(",", 1)[0].strip().lower()
    if not request_host:
        return None
    request_scheme = str(scope.get("scheme") or "http").lower()
    if forwarded_proto in {"http", "https"}:
        request_scheme = forwarded_proto
    if request_scheme not in {"http", "https"}:
        return None
    return request_scheme, request_host


def _rewrite_canonical_upstream_location(
    value: str,
    *,
    upstream: str,
    scope: Scope,
) -> str:
    """Keep exact-upstream absolute redirects on the canonical Preview origin.

    Relative and genuinely external redirects retain application semantics. An
    absolute redirect back to the managed runtime address would bypass the
    capability/generation boundary, so translate only that exact origin.
    """
    location = _upstream_location_match(value, upstream)
    if location is None:
        return value
    resolved = _request_scheme_and_host(scope)
    if resolved is None:
        return value
    request_scheme, request_host = resolved
    return urllib.parse.urlunsplit(
        (request_scheme, request_host, location.path, location.query, location.fragment)
    )

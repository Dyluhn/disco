"""Shared URL/domain matching for every discovery provider.

Provider filters are host policies, not substring searches: ``example.com``
matches that host and its subdomains, but never ``notexample.com`` or
``example.com.evil``.
"""

from __future__ import annotations

import datetime
import re
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse

_TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "msclkid", "msockid"})


def parse_source_date(value: object) -> datetime.date | None:
    """Parse exact provider dates without guessing from arbitrary prose."""

    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(raw).date()
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_domain(value: str) -> str:
    """Return a lower-case hostname from a hostname, URL, or ``site:`` value."""
    token = value.strip().lower().removeprefix("site:")
    if "://" in token:
        token = urlparse(token).hostname or ""
    else:
        token = token.split("/", 1)[0].split(":", 1)[0]
    return token.strip(".")


def host_matches_domain(host: str, domain: str) -> bool:
    """Whether ``host`` is ``domain`` itself or one of its subdomains."""
    normalized_host = normalize_domain(host)
    normalized_domain = normalize_domain(domain)
    return bool(normalized_host and normalized_domain) and (
        normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")
    )


def url_allowed(
    url: str,
    allow: frozenset[str] | set[str] | None,
    deny: frozenset[str] | set[str] | None,
) -> bool:
    """Apply exact-host/subdomain allow and deny policies to a URL."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    if any(host_matches_domain(host, domain) for domain in deny or ()):
        return False
    return allow is None or any(host_matches_domain(host, domain) for domain in allow)


def source_url_key(url: str) -> str:
    """A conservative source identity key for discovery/report deduplication."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return url.strip()
    try:
        port = parsed.port
    except ValueError:
        return url.strip()
    authority = f"{host}:{port}" if port and port not in {80, 443} else host
    path = parsed.path.rstrip("/") or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    suffix = f"?{urlencode(sorted(query))}" if query else ""
    return f"{authority}{path}{suffix}"


def query_domain_scopes(query: str) -> tuple[frozenset[str] | None, frozenset[str]]:
    """Recognize explicit site operators without treating quoted prose as policy.

    Restrict hosts, allowing canonical path changes such as /Headers to
    /Reference/Headers. Multiple positive sites form the usual search union;
    separately configured host policies are still applied by the provider.
    """
    allow, deny = set(), set()
    pattern = r'"[^"\n]*"|(?:^|[\s(])(-?)site:(?:"([^"\n]+)"|([^\s)]+))'
    for match in re.finditer(pattern, query, re.IGNORECASE):
        sign, quoted, plain = match.groups()
        if sign is None:
            continue
        value = quoted or plain
        domain = normalize_domain(value)
        if domain:
            (deny if sign else allow).add(domain)
    return frozenset(allow) if allow else None, frozenset(deny)

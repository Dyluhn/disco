"""Small stateless protocol/URL-classification helpers, split out of
``_browser_daemon`` to keep the module under its size budget.

None of these carry a monkeypatch dependency (only ever called, never
reassigned, by any test) — ``_browser_daemon.py`` re-exports them with a
redundant alias so ``daemon_mod._page_kind`` etc. keep resolving exactly as
before.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def is_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def positive_epoch(value: object) -> bool:
    return type(value) is int and value > 0


def page_kind(url: object) -> str:
    value = str(url or "")
    if not value or value == "about:blank":
        return "uninitialized"
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        return "external"
    # Reload authority is intentionally narrower than URL reachability: only a
    # literal HTTP(S) loopback origin can represent the local preview. A
    # loopback-looking file/custom/javascript URL is still external.
    if parsed.scheme.lower() not in {"http", "https"} or host is None:
        return "external"
    if host.lower() == "localhost":
        return "local_preview"
    try:
        return "local_preview" if ipaddress.ip_address(host).is_loopback else "external"
    except ValueError:
        # Literal host parsing only. Never use DNS to enlarge the local trust class.
        return "external"

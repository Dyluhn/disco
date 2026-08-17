"""Console / network classification for the web-app probe evidence.

Pure functions that turn a raw browser structured payload into deterministic
diagnostics. These are EVIDENCE classifiers, not verdict authorities: they
never produce or upgrade a typed ``HostVerificationResult``; their output is
immutable evidence bound for later host verification.
"""

from __future__ import annotations

import re
from typing import Any

# Network failures that are NEVER load-bearing for "does the app work" — a missing
# favicon or a blocked analytics beacon must not fail an otherwise-good build.
_IGNORABLE_NETWORK = (
    "favicon",
    "/analytics",
    "google-analytics",
    "googletagmanager",
    "gtag/js",
    "/__vite_ping",
    "hot-update.json",
    "/sockjs-node",
)

# Advisory visual self-review checklist. Seeded into ``verdict["vision"]["notes"]``
# ONLY when a screenshot_b64 is present (vision active); the model applies it to the
# rendered screenshot. Advisory — it never gates the finish decision.
_VISION_REVIEW_CHECKLIST = (
    "Visual self-review (advisory — does not gate finish). Looking at the rendered\n"
    "screenshot, check ONLY for real problems and fix what is clearly off:\n"
    "- padding / alignment / spacing consistency (no cramped or colliding elements)\n"
    "- text contrast against its background (no low-contrast or invisible text)\n"
    "- visual hierarchy (headings, sections, and CTAs are distinguishable)\n"
    "- no overflow, overlap, or broken layout; footer and nav render correctly\n"
    "Only flag what is incorrect or off. Do not invent issues or restyle a page that\n"
    "already looks correct."
)


def _source_of(entry: dict[str, Any]) -> str:
    """Render a console entry's location dict into a ``url:line:col`` string."""
    loc = entry.get("location") or {}
    url = loc.get("url")
    if not url:
        return ""
    line = loc.get("lineNumber")
    if line is None:
        return str(url)
    col = loc.get("columnNumber")
    return f"{url}:{line}:{col}" if col is not None else f"{url}:{line}"


def _console_errors(console: list[dict[str, Any]]) -> list[dict[str, str]]:
    """error-level / uncaught console entries only (warnings handled separately)."""
    out: list[dict[str, str]] = []
    for c in console:
        if c.get("level") == "error":
            source = _source_of(c)
            text = str(c.get("text", ""))
            # Chromium mirrors failed resource requests into the console. Apply
            # the same narrow non-load-bearing resource policy used by the
            # network classifier, but only to the browser's generic load-error
            # message. A real exception remains an error even if its source URL
            # happens to contain an allowlisted token.
            if "failed to load resource" in text.lower() and _is_ignorable_network({"url": source}):
                continue
            out.append(
                {
                    "text": text,
                    "source": source,
                    "stack": str(c.get("stack", "") or ""),
                }
            )
    return out


def _console_warnings(console: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for c in console:
        if c.get("level") == "warning":
            out.append({"text": str(c.get("text", "")), "source": _source_of(c)})
    return out


def _is_ignorable_network(entry: dict[str, Any]) -> bool:
    url = str(entry.get("url", "")).lower()
    return any(token in url for token in _IGNORABLE_NETWORK)


def _critical_network_failures(network: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """4xx/5xx responses + hard request failures, minus the favicon/analytics
    allowlist. These are the network failures that mean the app is broken."""
    out: list[dict[str, Any]] = []
    for n in network:
        if _is_ignorable_network(n):
            continue
        status = n.get("status")
        failure = n.get("failure")
        is_http_error = isinstance(status, int) and not isinstance(status, bool) and status >= 400
        if failure or is_http_error:
            out.append(
                {
                    "method": str(n.get("method", "GET")),
                    "url": str(n.get("url", "")),
                    "status": status if isinstance(status, int) else None,
                    "failure": str(failure) if failure else None,
                }
            )
    return out


def _normalize_error_text(text: str) -> str:
    """Stabilize an error signature so the SAME bug fingerprints identically across
    reloads: lowercase, collapse whitespace, and strip volatile numerics (line/col
    offsets, hex addresses, hashed asset names) that differ run-to-run."""
    t = text.strip().lower()
    t = re.sub(r"0x[0-9a-f]+", "0x", t)  # hex addresses
    t = re.sub(r"[0-9a-f]{8,}", "", t)  # content hashes (asset fingerprints)
    t = re.sub(r"\d+", "", t)  # line/col numbers, counts
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _path_of(url: str) -> str:
    """Path component of a URL (drop scheme/host/query) for a stable network sig."""
    u = re.sub(r"^[a-z]+://[^/]+", "", str(url))
    return u.split("?", 1)[0].split("#", 1)[0]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
"""Stable failure fingerprint for the web-app probe evidence.

The fingerprint is the loop-breaker key: identical failures hash identically so
the finish gate can mark a re-verification STUCK instead of reloading forever.
This is EVIDENCE (a stable hash of observed diagnostics), not a verdict: it
never produces or upgrades a typed ``HostVerificationResult``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ._classification import _normalize_error_text, _path_of


def _failure_fingerprint(
    console_errors: list[dict[str, str]], network_failures: list[dict[str, Any]]
) -> str:
    """Stable 16-hex hash of the top console-error signatures + critical network
    failures. Identical failures → identical fingerprint (the loop-breaker key);
    a different bug → different fingerprint. Empty inputs → a fixed 'clean' hash
    (never collides with a real failure set)."""
    sigs: list[str] = []
    for e in console_errors[:5]:
        sigs.append("E:" + _normalize_error_text(e["text"]))
    for n in network_failures[:5]:
        marker = str(n.get("status") or n.get("failure") or "fail")
        sigs.append(f"N:{marker}:{_path_of(n.get('url', ''))}")
    raw = "|".join(sorted(sigs)) if sigs else "CLEAN"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
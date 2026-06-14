"""Disco environment-variable access with backward-compatible PMX_ fallback.

The project's env-var prefix was renamed `PMX_` → `DISCO_` (the 2026-06-13
full rebrand). To avoid breaking existing deployments — and, critically, so
that ciphertext encrypted under the old `PMX_SECRET_KEY` still decrypts — every
env read goes through :func:`disco_env`, which prefers the new `DISCO_<suffix>`
name and falls back to the legacy `PMX_<suffix>` name (emitting a one-time
deprecation log per legacy var).

Reads are intentionally NOT cached: callers (and tests via
`monkeypatch.setenv`) mutate the environment at runtime, and a startup-time
shim would miss those. The function re-reads `os.environ` on every call.

Usage: pass the SUFFIX (without the prefix):
    disco_env("SECRET_KEY")            # DISCO_SECRET_KEY or PMX_SECRET_KEY
    disco_env("PORT", "8000")          # with a default
    disco_env("HOST") or "127.0.0.1"   # None if neither set + no default
"""

from __future__ import annotations

import logging
import os

_LOG = logging.getLogger("disco.env")

# Legacy suffixes already logged once, so a hot path doesn't spam the log.
_DEPRECATION_LOGGED: set[str] = set()

_NEW_PREFIX = "DISCO_"
_OLD_PREFIX = "PMX_"


def disco_env(suffix: str, default: str | None = None) -> str | None:
    """Return the env var ``DISCO_<suffix>``, falling back to the legacy
    ``PMX_<suffix>`` (with a one-time deprecation log), else ``default``.

    ``suffix`` is the bare name WITHOUT the prefix (e.g. ``"SECRET_KEY"``,
    not ``"DISCO_SECRET_KEY"``). The DISCO_ name always wins when both are
    set, so an operator can migrate by setting the new var without unsetting
    the old one.
    """
    new = os.environ.get(_NEW_PREFIX + suffix)
    if new is not None:
        return new
    old = os.environ.get(_OLD_PREFIX + suffix)
    if old is not None:
        if suffix not in _DEPRECATION_LOGGED:
            _DEPRECATION_LOGGED.add(suffix)
            _LOG.warning(
                "%s%s is deprecated; set %s%s instead (the legacy name is "
                "still honored for now).",
                _OLD_PREFIX,
                suffix,
                _NEW_PREFIX,
                suffix,
            )
        return old
    return default


def disco_env_set(suffix: str) -> bool:
    """True iff either ``DISCO_<suffix>`` or the legacy ``PMX_<suffix>`` is set
    (regardless of value). For callers that branch on presence, not value."""
    return (_NEW_PREFIX + suffix) in os.environ or (_OLD_PREFIX + suffix) in os.environ

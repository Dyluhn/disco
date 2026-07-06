"""Internal pure helpers extracted from `dod_evaluator.py` — god-file
decomposition (pure move, zero behavior change).

These three helpers have no `DoDEvaluator` state, no I/O, no async — they
are pure string/network-policy helpers. The functions that USE them
(`_default_command_runner`, `_default_http_probe`, the per-predicate
dispatch in `DoDEvaluator._check_command_exit`) stay in `dod_evaluator.py`
and import the names from here. The public test re-export
(`from disco.core.dod_evaluator import tail`) is preserved by a re-import
inside `dod_evaluator.py`.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse


def _hard_deny_reason(command: str) -> str | None:
    """The engine's destructive-command gate (mirrors
    `loop.engine._hard_deny_reason`). Used by the default `command_runner` to
    refuse to run a denied command rather than silently no-op. The engine
    also gates the agent's own verify-on-finish through this same function —
    parity keeps the evaluator's safety posture identical to the loop's.

    Imported lazily so a future extraction of the security analyzers doesn't
    drag the evaluator into a cycle; the function is pure + deterministic so
    the call site is cheap."""
    try:
        from .security.analyzers import hard_deny_reason
    except Exception:  # pragma: no cover - defensive; analyzers is a stable leaf
        return None
    return hard_deny_reason(command)


def _egress_allowed(url: str, allow_hosts: frozenset[str]) -> tuple[bool, str]:
    """True if the URL shape is allowed before the guarded wire probe.

    Private, loopback, and link-local IP literals are never allowed for class-1
    probes. Hostnames must either be explicitly allowlisted or, when the
    allowlist is empty, are left for the connect-time guarded fetch to resolve
    and validate.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return False, f"malformed URL: {exc}"
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed (http/https only)"
    host = parsed.hostname or ""
    if not host:
        return False, "URL has no host"
    try:
        addr = ip_address(host)
    except ValueError:
        if not allow_hosts or host in allow_hosts:
            return True, ""
        return False, f"host {host!r} not in egress allow-list"
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return False, f"host {host!r} resolves to a denied private range"
    if not allow_hosts or host in allow_hosts:
        return True, ""
    return False, f"host {host!r} not in egress allow-list"


def tail(text: str, n: int) -> str:
    """The trailing `n` chars of `text`. Used to keep `CommandResult` /
    `DoDPredicateResult.details` small (a noisy build log would otherwise
    bloat the verdict; the audit only needs the tail to see WHY the
    command failed)."""
    if n <= 0 or not text:
        return ""
    if len(text) <= n:
        return text
    return "…" + text[-n:]

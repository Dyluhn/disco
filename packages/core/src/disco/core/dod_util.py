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
    """True if the URL is on the egress allow-list. Loopback + RFC1918 by
    default — see `_DEFAULT_HTTP_ALLOW_HOSTS` for the rationale. Rejects
    non-http(s) schemes (no `file://`, no `gopher://`)."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return False, f"malformed URL: {exc}"
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed (http/https only)"
    host = parsed.hostname or ""
    if not host:
        return False, "URL has no host"
    if host in allow_hosts:
        return True, ""
    # RFC1918 private network — accepted by default. A real prod probe to a
    # public host must be opted in via a custom `http_probe` argument.
    try:
        addr = ip_address(host)
    except ValueError:
        # Hostname that didn't resolve to an IP literal (DNS resolution is
        # the probe's job, not the gate's). Refuse by default — the loopback
        # / private-IP shape is the in-cluster probe we actually need.
        return False, f"host {host!r} not in egress allow-list"
    if addr.is_loopback or addr.is_private:
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

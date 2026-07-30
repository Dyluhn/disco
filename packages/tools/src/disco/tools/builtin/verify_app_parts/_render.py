"""Stable, agent-facing text rendering for the ``verify_web_app`` verdict.

DELIBERATELY excludes the screenshot path and any per-call sequence number so an
identical failure renders byte-identically — the semantic no-progress breaker keys
on (success, content). This is presentation of EVIDENCE, not a verdict authority.
"""

from __future__ import annotations

from typing import Any


def _render(verdict: dict[str, Any]) -> str:
    """Stable, agent-facing text. DELIBERATELY excludes the screenshot path and any
    per-call sequence number so an identical failure renders byte-identically — the
    semantic no-progress breaker keys on (success, content)."""
    state = "pass" if verdict["passed"] else "not passing"
    lines = [
        f"VERIFY_WEB_APP: {verdict['verdict'].upper()} ({state})",
        f"url: {verdict['url']}  http_status: {verdict['http_status']}",
        f"fingerprint: {verdict['failure_fingerprint']}",
        f"summary: {verdict['summary']}",
    ]
    if verdict["console_errors"]:
        lines.append(f"console_errors ({len(verdict['console_errors'])}):")
        for e in verdict["console_errors"][:5]:
            where = f"  @ {e['source']}" if e["source"] else ""
            lines.append(f"  - {e['text']}{where}")
    if verdict["network_failures"]:
        lines.append(f"network_failures ({len(verdict['network_failures'])}):")
        for n in verdict["network_failures"][:5]:
            marker = n.get("status") or n.get("failure") or "failed"
            lines.append(f"  - {n['method']} {n['url']} -> {marker}")
    if verdict["next_action"]:
        lines.append(f"next_action: {verdict['next_action']}")
    return "\n".join(lines)
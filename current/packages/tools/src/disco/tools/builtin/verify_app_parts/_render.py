"""Stable, agent-facing text rendering for the ``verify_web_app`` verdict.

DELIBERATELY excludes the screenshot path and any per-call sequence number so an
identical failure renders byte-identically — the semantic no-progress breaker keys
on (success, content). This is presentation of EVIDENCE, not a verdict authority.
"""

from __future__ import annotations

from typing import Any


def _runtime_label(verdict: dict[str, Any]) -> str:
    if verdict["passed"]:
        return "PASS"
    if verdict["verdict"] == "degraded":
        return "INCOMPLETE"
    return "FAIL"


def _visual_scope(verdict: dict[str, Any]) -> str:
    vision = verdict.get("vision")
    pixels_supplied = isinstance(vision, dict) and vision.get("used") is True
    if pixels_supplied:
        return (
            "visual_evidence: screenshot pixels were supplied to the agent for optional "
            "inspection; this runtime check did not judge layout or aesthetics"
        )
    return (
        "visual_evidence: NOT CHECKED — no screenshot pixels were supplied to the agent; "
        "layout, aesthetics, and visual correctness remain unverified"
    )


def _checked_scope(verdict: dict[str, Any]) -> str:
    status = verdict.get("http_status")
    if not isinstance(status, int) or not 200 <= status < 400:
        return "checked: server reachability and HTTP status"
    return (
        "checked: HTTP reachability/status; rendered DOM presence "
        f"({verdict['visible_text_chars']} visible text chars, "
        f"{verdict['elements_count']} interactive elements, "
        f"{verdict['canvas_count']} canvases); console errors; critical network failures"
    )


def _render(verdict: dict[str, Any]) -> str:
    """Stable, agent-facing text. DELIBERATELY excludes the screenshot path and any
    per-call sequence number so an identical failure renders byte-identically — the
    semantic no-progress breaker keys on (success, content)."""
    lines = [
        f"VERIFY_WEB_APP: RUNTIME CHECK {_runtime_label(verdict)}",
        f"url: {verdict['url']}  http_status: {verdict['http_status']}",
        f"fingerprint: {verdict['failure_fingerprint']}",
        f"summary: {verdict['summary']}",
        _checked_scope(verdict),
        _visual_scope(verdict),
        (
            "not_checked: task requirements, semantic/behavioral completeness, plan "
            "completion, and end-to-end user correctness"
        ),
    ]
    scope = str(verdict.get("verification_scope") or "")
    if scope:
        lines.append(f"scope: {scope} (not semantic completeness)")
    interaction = verdict.get("game_interaction")
    if isinstance(interaction, dict) and interaction:
        lines.append(
            "game_input_smoke: "
            f"{interaction.get('status', 'unverified')} "
            "(only input dispatch was checked; this does not certify gameplay)"
        )
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

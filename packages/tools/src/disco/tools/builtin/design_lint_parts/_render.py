"""Agent-visible rendering for the structured design-lint verdict."""

from __future__ import annotations

from typing import Any

_RENDER_FINDING_LIMIT = 20


def render_verdict(verdict: dict[str, Any]) -> str:
    lines = [f"DESIGN_LINT: {'PASS' if verdict['ok'] else 'FINDINGS'}", verdict["summary"]]
    if not verdict["design_spec_present"]:
        lines.append("note: no .disco/designspec.json — off-default values can't be justified.")
    elif not verdict["design_spec_valid"]:
        lines.append("note: .disco/designspec.json is invalid — justifications ignored.")
    findings = verdict["findings"]
    for finding in findings[:_RENDER_FINDING_LIMIT]:
        lines.append(
            f"  [{finding['severity']}] {finding['rule_id']} ({finding['choice_key']}) "
            f"{finding['path']}:{finding['line']} — {finding['evidence']}"
        )
    omitted = len(findings) - _RENDER_FINDING_LIMIT
    if omitted > 0:
        lines.append(f"…and {omitted} more findings — fix the above first, then re-run.")
    if findings:
        lines.append(
            "next_action: Findings are advisory. If you edit a scanned artifact and "
            "intend to claim they are fixed, re-run design_lint once after the final "
            "relevant edit; this result is stale after mutation. Otherwise proceed "
            "without claiming a clean lint result."
        )
    else:
        lines.append(
            "next_action: This clean result covers the current artifact bytes; do not "
            "re-run unless a relevant file changes."
        )
    return "\n".join(lines)

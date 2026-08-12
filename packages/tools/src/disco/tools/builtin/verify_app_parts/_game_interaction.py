"""Game-medium interaction evidence capture for ``verify_web_app``.

Drives the headless browser through click/keyboard probes and records the
interaction steps + screenshots. This is EVIDENCE (a structured dict), not a
verdict: it never manufactures or upgrades a typed ``HostVerificationResult``.
"""

from __future__ import annotations

from typing import Any

from ...anatomy import ToolContext
from ..browser import BrowserArgs, BrowserTool


async def _capture_game_interaction(
    ctx: ToolContext, before: dict[str, Any]
) -> dict[str, Any]:
    interaction: dict[str, Any] = {
        "before_screenshot_path": str(before.get("screenshot_path") or ""),
        "scope": "bounded_input_smoke",
        "certifies_gameplay": False,
        "steps": [],
    }
    latest = before
    probes = [
        BrowserArgs(action="click", selector="canvas"),
        BrowserArgs(action="press", key="Space"),
        BrowserArgs(action="press", key="ArrowRight"),
        BrowserArgs(action="screenshot"),
    ]
    for probe in probes:
        outcome = await BrowserTool().run(probe, ctx)
        step: dict[str, Any] = {"action": probe.action, "success": outcome.success}
        if probe.action == "press":
            step["key"] = probe.key
        if outcome.success and outcome.structured:
            latest = outcome.structured
            step["screenshot_path"] = str(latest.get("screenshot_path") or "")
        else:
            diagnostic = outcome.structured or {}
            if diagnostic.get("error_class"):
                step["error_class"] = str(diagnostic["error_class"])
            if diagnostic.get("error_reason"):
                step["error_reason"] = str(diagnostic["error_reason"])
            detail = outcome.error or outcome.content
            if detail:
                step["error"] = detail[:300]
        interaction["steps"].append(step)
    interaction["status"] = (
        "smoke_passed"
        if all(bool(step.get("success")) for step in interaction["steps"])
        else "smoke_failed"
    )
    interaction["after_screenshot_path"] = str(latest.get("screenshot_path") or "")
    return {**latest, "game_interaction": interaction}

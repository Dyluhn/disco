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
        elif outcome.error:
            step["error"] = outcome.error[:300]
        interaction["steps"].append(step)
    interaction["after_screenshot_path"] = str(latest.get("screenshot_path") or "")
    return {**latest, "game_interaction": interaction}
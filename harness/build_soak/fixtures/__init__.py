"""Fake-model / fake-tool simulator fixtures (guidelines §15.5, PR S6).

These let the §20 contract + S6 simulator tests DRIVE THE REAL agent loop with a
deterministically scripted model and an in-memory tool world — no live model spend,
no sandbox — so the loop emits a REAL events.jsonl the deterministic classifier then
consumes. The fixtures are disco-free: they take the loop's own builders
(`action_step`, `finish_step`, `AgentStep`, `FakeExecutor`, ...) by INJECTION so this
package imports nothing from the product (keeps the layering contract clean).
"""

from __future__ import annotations

from .fake_model import (
    FakeModelScript,
    malformed_plan,
    no_replan_followup,
    plan_then_build,
    wrong_tool_in_planning,
)
from .fake_tools import FakeToolWorld, build_recording_executor

__all__ = [
    "FakeModelScript",
    "FakeToolWorld",
    "build_recording_executor",
    "malformed_plan",
    "no_replan_followup",
    "plan_then_build",
    "wrong_tool_in_planning",
]

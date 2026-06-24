"""Declarative fake-model scripts (guidelines §15.5).

A `FakeModelScript` is a machine-readable list of model moves. Each move is one of:

    {"call_tool": "<name>", "args": {...}, "thought": "..."}   # propose a tool call
    {"finish": true | "<summary>"}                              # affirmative finish
    {"text": "<prose>"}                                          # a tool-less prose turn

`agent_steps(...)` materializes the moves into the loop's `AgentStep` objects via
INJECTED builders (the loop's own `action_step` / `finish_step` helpers from
`loop_fakes`, and the `AgentStep` class for a prose turn). The script holds no
product import — the test that has `loop_fakes` on its path passes the builders in.

The canned scripts below cover the §15.5 / S6 deterministic failure shapes:
wrong-tool-in-planning, a clean plan->build, a malformed (stepless) plan, and a
no-replan-after-followup free-build.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FakeModelScript:
    """An ordered list of fake-model moves. `name` is for evidence/manifest only."""

    moves: list[dict[str, Any]]
    name: str = "fake_model_script"

    def agent_steps(
        self,
        *,
        action_step: Callable[..., Any],
        finish_step: Callable[..., Any],
        prose_step: Callable[[str], Any],
    ) -> list[Any]:
        """Materialize the moves into loop `AgentStep`s.

        `action_step(tool, args, thought)` and `finish_step(thought)` are the
        `loop_fakes` helpers; `prose_step(text)` builds an `AgentStep(thought=text,
        tool_call=None, finished=False)` (a tool-less turn — the planning gate
        treats it as an acknowledgement + nudge)."""
        out: list[Any] = []
        for move in self.moves:
            if "call_tool" in move:
                out.append(
                    action_step(
                        move["call_tool"],
                        move.get("args") or {},
                        move.get("thought", "do it"),
                    )
                )
            elif "finish" in move:
                fin = move["finish"]
                out.append(finish_step(fin if isinstance(fin, str) else "done"))
            elif "text" in move:
                out.append(prose_step(str(move["text"])))
            else:  # pragma: no cover - guards a malformed script
                raise ValueError(f"unrecognized fake-model move: {move!r}")
        return out


# ---- canned scripts ---------------------------------------------------------


def wrong_tool_in_planning(
    *, write_tool: str = "file_write", path: str = "index.html"
) -> FakeModelScript:
    """§15.5 — the planner calls a WRITE tool first (a disallowed mutation in
    PLANNING), then submits a real plan. The product SHOULD reject the write and
    keep the loop alive; the classifier's ToolScopeOracle codes the attempt as
    WRITE_TOOL_ATTEMPTED_IN_PLANNING from the event log."""
    return FakeModelScript(
        name="wrong_tool_in_planning",
        moves=[
            {"call_tool": write_tool, "args": {"path": path, "content": "bad"}},
            {
                "call_tool": "submit_plan",
                "args": {
                    "summary": "Plan after rejection",
                    "steps": [{"title": "Create the file"}],
                },
            },
        ],
    )


def plan_then_build(*, build_tool: str = "shell") -> FakeModelScript:
    """A clean run: submit a plan (halts for approval), then — after approval — one
    real build action, then finish."""
    return FakeModelScript(
        name="plan_then_build",
        moves=[
            {
                "call_tool": "submit_plan",
                "args": {"summary": "Build it", "steps": [{"title": "Create the page"}]},
            },
            {"call_tool": build_tool, "args": {"cmd": "echo hi"}},
            {"finish": "built the page"},
        ],
    )


def malformed_plan() -> FakeModelScript:
    """A degenerate plan: a summary but NO concrete steps (the loop keeps steps
    empty rather than fabricating a fake one)."""
    return FakeModelScript(
        name="malformed_plan",
        moves=[{"call_tool": "submit_plan", "args": {"summary": "vague", "steps": []}}],
    )


def no_replan_followup(*, write_tool: str = "shell") -> FakeModelScript:
    """The execution-phase script for a no-replan-after-followup run: after a
    follow-up user turn the agent free-builds (a mutating action) WITHOUT a new
    plan, then finishes — the stale-plan failure the RevisionOracle codes as
    NO_REPLAN_AFTER_REVISION."""
    return FakeModelScript(
        name="no_replan_followup",
        moves=[
            {"call_tool": write_tool, "args": {"cmd": "echo changed"}},
            {"finish": "applied the change"},
        ],
    )

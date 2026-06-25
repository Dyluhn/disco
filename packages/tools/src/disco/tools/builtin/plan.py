"""Plan-mode meta tools (Build) — the structured-plan signal + capstone progress.

These are pure CONTROL signals, not workspace actions: they run `in_process` (no
sandbox) and carry no side effects, so they are LOW risk and never gate a build.

- `submit_plan` is how the agent proposes its plan while in PLANNING mode. The agent
  loop INTERCEPTS this call (it is never executed as a tool) and turns it into a
  `PlanEvent` that halts for human approval. The `run()` here is a defensive no-op
  for the (filtered-out) case where it is somehow invoked during execution.
- `plan_step` lets the executing agent report which capstone it is on, so the UI can
  check steps off honestly (agent-driven; an unreported step simply stays pending).

C18 — `PlanStepInput.done_condition` is an OPTIONAL machine-checkable predicate the
agent attaches to a step. When the step is later marked done via `plan_step(idx,
'done')`, the engine evaluates the predicate and emits a visible pass/fail note in
the trace. ADVISORY ONLY: failure does not block the run, does not nudge the agent,
and does not duplicate the C1c finish gate. The predicate REUSES the
`disco.core.dod.DoDPredicate` shape (file_exists / command / http_ok) so the
planner speaks the same vocabulary as the DoD spec — but this check is local and
advisory, not the fresh-context DoD evaluation C1b runs.
"""

from __future__ import annotations

from typing import Literal

from disco.core.dod import DoDPredicate
from pydantic import BaseModel, Field, field_validator

from ..anatomy import SecurityRisk, ToolContext, ToolDef, ToolOutcome


class PlanStepInput(BaseModel):
    title: str = Field(description="Short, plain-language capstone (one line).")
    detail: str | None = Field(default=None, description="Optional elaboration.")
    # C18 — optional advisory done-condition. The engine evaluates this when the
    # step is marked done via plan_step(idx, 'done') and surfaces a visible
    # pass/fail note in the trace. The shape is the same discriminated union as
    # `disco.core.dod.DoDPredicate` (file_exists / command / http_ok) so the
    # planner reuses the same vocabulary it would for a full DoD spec. Steps
    # WITHOUT a predicate behave exactly as today (back-compat).
    done_condition: DoDPredicate | None = Field(
        default=None,
        description=(
            "Optional machine-checkable predicate. When the agent marks this "
            "step done via plan_step(idx, 'done'), the engine evaluates this "
            "advisory predicate and emits a visible pass/fail note in the trace. "
            "ADVISORY ONLY: a failure does NOT block the run, does NOT nudge the "
            "agent, and does NOT duplicate the C1c finish gate. Use the same "
            "shape as `disco.core.dod.DoDPredicate` "
            "(`{'kind': 'file_exists', 'path': ...}` | "
            "`{'kind': 'command', 'cmd': ..., 'expect_exit': 0}` | "
            "`{'kind': 'http_ok', 'url': ..., 'expect_status': 200}`)."
        ),
    )


class SubmitPlanArgs(BaseModel):
    summary: str = Field(description="One or two sentences: what this plan delivers.")
    steps: list[PlanStepInput] = Field(
        description="Ordered capstones to carry out once the plan is approved."
    )
    context: str = Field(
        default="",
        description=(
            "Optional markdown: WHY this plan, what you learned from exploring the "
            "workspace (file_read/file_list) and the web (search/extract), and any "
            "trade-offs the human should know before approving. Shown to the user above "
            "the steps."
        ),
    )


class SubmitPlanTool:
    definition = ToolDef(
        name="submit_plan",
        description=(
            "Propose a plan for approval. EXPLORE FIRST: use file_list/file_read to "
            "understand the workspace and search/extract for any web context, THEN call "
            "submit_plan with a short summary, ordered concrete steps, and a markdown "
            "`context` block explaining what you found and why this plan. Do not take any "
            "state-changing action until the plan is approved."
        ),
        args_model=SubmitPlanArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,  # emits a plan for approval — no environment mutation
    )

    async def run(self, args: SubmitPlanArgs, ctx: ToolContext) -> ToolOutcome:
        # The loop intercepts submit_plan in PLANNING mode and never calls this. If it
        # is ever reached (e.g. invoked outside planning), it is an inert acknowledgement.
        return ToolOutcome(success=True, content="plan received")


class PlanStepArgs(BaseModel):
    index: int = Field(description="1-based index of the plan step you are reporting on.")
    state: Literal["active", "done"] = Field(
        description="'active' when you START a step, 'done' when you COMPLETE it."
    )


class PlanStepTool:
    definition = ToolDef(
        name="plan_step",
        description=(
            "Report progress on the approved plan: mark a step 'active' when you start it "
            "and 'done' when you finish it. Purely informational — it advances the UI's "
            "capstone tracker and takes no action in the workspace."
        ),
        args_model=PlanStepArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,  # informational progress signal — no environment mutation
    )

    async def run(self, args: PlanStepArgs, ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome(
            success=True,
            content=f"step {args.index} → {args.state}",
            structured={"index": args.index, "state": args.state},
        )


# runthru-v2 (#3) — the DECLARATIVE progress tool for capable models (assist OFF).
# Replaces the per-step incremental `plan_step` delta with a full-state snapshot the
# model rewrites every time. This is the Claude Code TodoWrite / Cursor design: because
# each call carries the COMPLETE state of every step, a dropped/garbled/skipped update
# self-corrects on the next call (idempotent), rather than leaving a step wrong forever.
# Research (codex/gpt-5.5 + web survey) + Dylan: incremental deltas are fragile across
# ALL model tiers; declarative full-rewrite is what frontier models maintain reliably.
# Small models (assist ON) are NOT offered this — they get an honest no-bookkeeping
# plan (NL + done-at-finish), so the burden never falls on a model that can't carry it.
# Like plan_step: a pure CONTROL signal — read_only, in_process, never gates finish.
class PlanProgressItem(BaseModel):
    index: int = Field(description="1-based index of the plan step.")
    state: Literal["pending", "active", "done"] = Field(
        description="This step's CURRENT state: 'pending' (not started), 'active' "
        "(in progress now), or 'done' (completed)."
    )


class UpdatePlanProgressArgs(BaseModel):
    steps: list[PlanProgressItem] = Field(
        description=(
            "The FULL current state of EVERY plan step, in order — rewrite the WHOLE "
            "list each time (a declarative snapshot, NOT a delta). Include every step "
            "with its current state. Because each call is the complete picture, a "
            "missed update self-corrects the next time you call this."
        )
    )

    @field_validator("steps", mode="before")
    @classmethod
    def _tolerate_truncated_steps(cls, v: object) -> object:
        # ROBUSTNESS (bake-off issue #1, harness-owns-robustness): a streamed tool-call's
        # args can arrive TRUNCATED (e.g. `{"steps": [""]}` when an arg-delta is dropped
        # mid-stream). Hard-failing validation makes the model retry the identical bad call
        # → repeated_action_error → STUCK build. This tool is non-critical declarative
        # bookkeeping (each call is the FULL snapshot, so a dropped one self-corrects next
        # call), so DROP un-parseable items instead of failing the whole call: a partial or
        # empty snapshot is a harmless no-op, never a wedge. NOTE: masks the symptom, not the
        # root streaming-truncation bug (tracked separately).
        if isinstance(v, list):
            return [s for s in v if isinstance(s, dict | PlanProgressItem)]
        return v


class UpdatePlanProgressTool:
    definition = ToolDef(
        name="update_plan_progress",
        description=(
            "Report progress on the approved plan by rewriting the FULL list of step "
            "states (declarative snapshot). Pass EVERY step with its current state "
            "('pending' | 'active' | 'done') — mark the step you're working on 'active' "
            "and completed ones 'done'. Purely informational: it advances the UI's plan "
            "tracker and takes NO action in the workspace. Call it as you make progress; "
            "since each call is the complete picture, an occasional miss self-corrects."
        ),
        args_model=UpdatePlanProgressArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,  # informational progress snapshot — no environment mutation
    )

    async def run(self, args: UpdatePlanProgressArgs, ctx: ToolContext) -> ToolOutcome:
        done = sum(1 for s in args.steps if s.state == "done")
        return ToolOutcome(
            success=True,
            content=f"plan progress: {done}/{len(args.steps)} done",
            structured={"steps": [s.model_dump() for s in args.steps]},
        )

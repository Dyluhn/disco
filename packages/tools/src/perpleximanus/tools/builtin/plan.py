"""Plan-mode meta tools (Build) — the structured-plan signal + capstone progress.

These are pure CONTROL signals, not workspace actions: they run `in_process` (no
sandbox) and carry no side effects, so they are LOW risk and never gate a build.

- `submit_plan` is how the agent proposes its plan while in PLANNING mode. The agent
  loop INTERCEPTS this call (it is never executed as a tool) and turns it into a
  `PlanEvent` that halts for human approval. The `run()` here is a defensive no-op
  for the (filtered-out) case where it is somehow invoked during execution.
- `plan_step` lets the executing agent report which capstone it is on, so the UI can
  check steps off honestly (agent-driven; an unreported step simply stays pending).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..anatomy import SecurityRisk, ToolContext, ToolDef, ToolOutcome


class PlanStepInput(BaseModel):
    title: str = Field(description="Short, plain-language capstone (one line).")
    detail: str | None = Field(default=None, description="Optional elaboration.")


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

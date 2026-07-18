"""Workflow control tools.

These tools are registered for workflow scopes so the model sees typed schemas.
The agent loop intercepts their calls and performs the durable event/status
side effects; direct execution is a defensive no-op acknowledgement.
"""

from __future__ import annotations

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import ToolContext, ToolDef, ToolOutcome
from ..behavior import declares


class WorkflowSkipArgs(BaseModel):
    reason: str = Field(
        description="Why this workflow run is being skipped without producing output."
    )


class WorkflowNeedsInputArgs(BaseModel):
    reason: str = Field(description="Why the workflow cannot continue.")
    required_action: str = Field(
        description="The concrete action or information needed from the user or operator."
    )


class WorkflowSkipTool:
    definition = ToolDef(
        name="skip",
        description=(
            "End this workflow run without producing the contracted output. Use only when "
            "the workflow honestly should be skipped; include the reason."
        ),
        args_model=WorkflowSkipArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
        behavior=declares(EffectCapability.RUN_FINALIZE, planner_safe=True),
    )

    async def run(self, args: WorkflowSkipArgs, ctx: ToolContext) -> ToolOutcome:  # noqa: ARG002
        return ToolOutcome(
            success=True,
            content=f"workflow skip requested: {args.reason}",
            structured={"reason": args.reason},
        )


class WorkflowNeedsInputTool:
    definition = ToolDef(
        name="needs_input",
        description=(
            "Declare that this workflow run cannot continue until the user or operator "
            "provides a specific required action or missing input."
        ),
        args_model=WorkflowNeedsInputArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
        behavior=declares(EffectCapability.RUN_CONTROL, planner_safe=True),
    )

    async def run(
        self,
        args: WorkflowNeedsInputArgs,
        ctx: ToolContext,  # noqa: ARG002
    ) -> ToolOutcome:
        return ToolOutcome(
            success=True,
            content=(
                f"workflow needs input: {args.reason}\nrequired action: {args.required_action}"
            ),
            structured={
                "reason": args.reason,
                "required_action": args.required_action,
            },
        )


__all__ = [
    "WorkflowNeedsInputArgs",
    "WorkflowNeedsInputTool",
    "WorkflowSkipArgs",
    "WorkflowSkipTool",
]

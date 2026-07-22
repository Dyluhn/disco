"""AppKit EPIC F — `request_custom_build`, the gated escape hatch.

Strict AppKit mode bars raw file/shell/code/browser tools. This in-process tool
is only a confirmed control request: the host must retain the governed revision,
create and label a distinct Freeform revision, and persist the lost-guarantee
transition before the AppKit executor may widen to the normal Build scope.
"""

from __future__ import annotations

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import ToolContext, ToolDef, ToolOutcome
from ..behavior import declares


class RequestCustomBuildArgs(BaseModel):
    reason: str = Field(
        description=(
            "Why the validated AppKit mutators are insufficient and a full custom "
            "build with raw tool access is required."
        )
    )
    needed_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "The raw capabilities needed, such as 'shell', 'file_write', 'code_exec', or 'browser'."
        ),
    )


class RequestCustomBuildTool:
    """Escalate a strict AppKit build to the normal Build toolset."""

    definition = ToolDef(
        name="request_custom_build",
        description=(
            "Escalate from the strict AppKit toolset to a full custom build with "
            "raw file/shell/code access. This permanently ejects the new revision "
            "from AppKit: semantic-only mutation boundaries, deterministic regeneration, "
            "writable-zone protection, AppKit verification status, and governed deployment "
            "guarantees are lost. The prior governed revision remains available for preview. "
            "Use only when AppKit mutators cannot express the change. Requires human "
            "confirmation and is unavailable in autonomous runs."
        ),
        args_model=RequestCustomBuildArgs,
        base_risk=SecurityRisk.HIGH,
        runs_in="in_process",
        read_only=False,
        behavior=declares(
            EffectCapability.RUN_CONTROL,
            EffectCapability.WORKSPACE_MUTATE,
            planner_safe=False,
        ),
    )

    async def run(self, args: RequestCustomBuildArgs, ctx: ToolContext) -> ToolOutcome:
        caps = ", ".join(args.needed_capabilities) if args.needed_capabilities else "(unspecified)"
        return ToolOutcome(
            success=True,
            content=(
                "AppKit ejection requested; the host must commit the revision boundary "
                "before scope can widen. "
                f"Requested capabilities: {caps}. Reason: {args.reason}"
            ),
            structured={
                "reason": args.reason,
                "needed_capabilities": list(args.needed_capabilities),
                "requested_transition": "appkit_to_freeform",
            },
        )


__all__ = ["RequestCustomBuildArgs", "RequestCustomBuildTool"]

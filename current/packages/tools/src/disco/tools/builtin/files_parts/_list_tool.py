"""``file_list`` — one non-recursive directory-level listing."""

from __future__ import annotations

from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ._constants import _FS


class FileListArgs(BaseModel):
    path: str = Field(default=".", description="Workspace-relative directory to list.")


class FileListTool:
    definition = ToolDef(
        name="file_list",
        description=(
            "List the entries of ONE directory level in the workspace (not recursive) — "
            "names + kind; descend by listing subdirectories."
        ),
        args_model=FileListArgs,
        needs=_FS,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner
        behavior=declares(EffectCapability.WORKSPACE_INVENTORY_READ, planner_safe=True),
    )

    async def run(self, args: FileListArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        entries = await ctx.sandbox.list_dir(args.path)
        return ToolOutcome(
            success=True,
            content="\n".join(entries) if entries else "(empty)",
            structured={"path": args.path, "entries": entries},
        )

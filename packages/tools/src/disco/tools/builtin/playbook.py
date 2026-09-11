"""`build_playbook` — read a bundled integration recipe before wiring a capability.

The playbooks (disco.core.playbooks) are the resource layer for full-stack builds:
auth and roles, realtime, chat, AI assistants and RAG, dictation, email,
payments, uploads, market data, booking/inventory, multiplayer, and how to
prove each one inside the sandbox. The catalog travels in the tool description
(the scaffold_starter pattern) so the model picks by fit; the playbook text comes
back as the tool result. Pure read: no sandbox, no workspace writes.
"""

from __future__ import annotations

from disco.core import SecurityRisk
from disco.core.playbooks import PlaybookRegistry
from pydantic import BaseModel, Field

from ..anatomy import ToolContext, ToolDef, ToolOutcome
from ..behavior import declares


class BuildPlaybookArgs(BaseModel):
    playbook: str | None = Field(
        default=None,
        description=(
            "Playbook id from the catalog in the tool description. Omit to get the "
            "catalog (ids with one-line summaries)."
        ),
    )


class BuildPlaybookTool:
    definition = ToolDef(
        name="build_playbook",
        description=(
            "Read a bundled playbook — a concrete recipe (stack, pinned "
            "dependencies, env var names, code shape, verification steps, security "
            "notes) for one capability of a full-stack app. Read the matching pack "
            "BEFORE implementing the capability, then adapt it to the project. Playbooks:\n"
            + PlaybookRegistry.default().index()
            + "\nCall with no arguments to list the catalog again."
        ),
        args_model=BuildPlaybookArgs,
        base_risk=SecurityRisk.LOW,
        runs_in="in_process",
        read_only=True,
        behavior=declares(planner_safe=True),
    )

    async def run(self, args: BuildPlaybookArgs, ctx: ToolContext) -> ToolOutcome:
        registry = PlaybookRegistry.default()
        if args.playbook is None:
            return ToolOutcome(success=True, content=registry.index())
        pack = registry.get(args.playbook)
        if pack is None:
            return ToolOutcome(
                success=False,
                content="",
                error=f"unknown playbook {args.playbook!r}; available: "
                + ", ".join(registry.ids()),
            )
        return ToolOutcome(
            success=True,
            content=pack.raw,
            structured={"playbook": pack.pack_id, "title": pack.title},
        )

"""scaffold_starter (P7) — materialize this build's host-owned starter frame.

A contract declares a starter_kit; the prompt pack tells the model to "scaffold from the
<name> starter". This tool makes that real: it writes the ACTIVE contract's starter
files (from ctx.starter_kit, threaded by the runtime — so it's active-contract-bound,
not a free-for-all) into the workspace, so the model edits a real frame instead of
hand-drawing common chrome. Never clobbers existing work (skips files that already
exist).
"""

from __future__ import annotations

from disco.core import SecurityRisk
from disco.core.kits import StarterKitRegistry
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome


class ScaffoldStarterArgs(BaseModel):
    title: str = Field(description="The artifact/site title the starter frame should carry (page <title> and header).")


class ScaffoldStarterTool:
    definition = ToolDef(
        name="scaffold_starter",
        description=(
            "Scaffold this build's host-owned starter frame into the workspace (the "
            "contract's starter_kit), so you edit a real frame instead of hand-drawing "
            "common chrome. Writes ONLY files that don't already exist — it never "
            "clobbers your work. Pass the artifact title."
        ),
        args_model=ScaffoldStarterArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
    )

    async def run(self, args: ScaffoldStarterArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        starter_id = getattr(ctx, "starter_kit", None)
        if not starter_id:
            # Deliberately NOT "the contract declares no starter_kit": the contract
            # may well declare one, but activation (set_build_kind) hasn't resolved
            # it for this run — the old wording sent the 2026-07-09 autopsy down
            # the wrong path. Normally unreachable now that the runtime withholds
            # this tool from the advertised set when no kit resolves; kept as the
            # typed backstop for qualified-name calls.
            return ToolOutcome(
                success=False, error="no_starter",
                content=(
                    "no starter kit resolved for this run — author the artifact "
                    "directly with file_write."
                ),
            )
        kit = StarterKitRegistry.default().get(starter_id)
        if kit is None:
            return ToolOutcome(success=False, error="unknown_starter", content=f"no starter kit {starter_id!r}")
        try:
            files = kit.scaffold(args.title)
        except ValueError as exc:  # path-safety / build error
            return ToolOutcome(success=False, error="invalid_starter", content=str(exc))

        written: list[str] = []
        skipped: list[str] = []
        for path, text in files.items():
            # use the sandbox's own existence check — NOT a read_file try/except, which
            # would treat a transient read error as "missing" and clobber real work.
            if await ctx.sandbox.file_exists(path):
                skipped.append(path)  # never clobber existing work
                continue
            await ctx.sandbox.write_file(path, text.encode("utf-8"))
            written.append(path)
        msg = f"scaffolded '{starter_id}' starter — wrote {written or '(nothing new)'}"
        if skipped:
            msg += f"; left existing {skipped} untouched"
        structured = {"starter": starter_id, "written": written, "skipped": skipped}
        notes = files.get("NOTES.md")
        if notes is not None:
            msg += f"\n\nNOTES.md\n{notes.strip()}"
            structured["notes_path"] = "NOTES.md"
            structured["notes"] = notes
        return ToolOutcome(
            success=True, content=msg,
            structured=structured,
        )

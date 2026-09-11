"""`create_reference_pack` — save workspace files as a reusable Reference Pack.

A Reference Pack is the user's own collection of files (a brand kit, a spec, a
data sample) that later Builds can select. The action is explicit: it runs only
when the user asks for a pack, copies the exact bytes of the named workspace
files through the host-owned writer (never a raw path to the library), and
returns the pack's id and summary. Uploading a file alone never creates a pack;
Settings owns renaming and file edits afterwards.
"""

from __future__ import annotations

import posixpath

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from ..reference_packs import ReferencePackWriteError

_MAX_FILES = 50
_FORBIDDEN_PREFIXES = (".pmx/", ".disco/", "references/")


class CreateReferencePackArgs(BaseModel):
    name: str = Field(description="Short pack name the user will see in Settings and Build.")
    description: str = Field(default="", description="One or two lines on what the files are for.")
    files: list[str] = Field(
        description=(
            "Workspace-relative paths of the files to copy, in the order they should "
            "appear (uploads/brand.pdf, notes/spec.md, …). Only files that already "
            "exist in this workspace; at most 50."
        ),
        min_length=1,
        max_length=_MAX_FILES,
    )


def _clean_path(raw: str) -> str | None:
    path = posixpath.normpath(raw.strip().lstrip("/"))
    if not path or path == "." or path.startswith("..") or "/../" in f"/{path}/":
        return None
    if path.startswith(_FORBIDDEN_PREFIXES):
        return None
    return path


class CreateReferencePackTool:
    definition = ToolDef(
        name="create_reference_pack",
        description=(
            "Save files from this workspace as a reusable Reference Pack the user can "
            "select in later Builds (managed under Settings → Reference packs). Use it "
            "ONLY when the user asks to make a pack from files they attached or you "
            "produced for them. Copies the exact bytes of the named files; returns the "
            "pack id and a summary. Uploading a file never creates a pack by itself."
        ),
        args_model=CreateReferencePackArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,  # the effect is host library state, not a workspace mutation
        behavior=declares(EffectCapability.WORKSPACE_CONTENT_READ, planner_safe=False),
    )

    async def run(self, args: CreateReferencePackArgs, ctx: ToolContext) -> ToolOutcome:
        if ctx.reference_pack_writer is None:
            return ToolOutcome(
                success=False,
                content="",
                error="reference_pack_writer_unavailable: this run cannot save packs",
            )
        assert ctx.sandbox is not None
        collected: list[tuple[str, bytes]] = []
        seen: set[str] = set()
        for raw in args.files:
            path = _clean_path(raw)
            if path is None:
                return ToolOutcome(
                    success=False,
                    content="",
                    error=f"invalid_path: {raw!r} is not a workspace file",
                )
            if path in seen:
                return ToolOutcome(success=False, content="", error=f"duplicate_path: {path}")
            seen.add(path)
            try:
                data = await ctx.sandbox.read_file(path)
            except Exception:  # noqa: BLE001 — a missing/unreadable file is a model-facing error
                return ToolOutcome(
                    success=False,
                    content="",
                    error=f"file_not_found: {path} is not in the workspace",
                )
            collected.append((posixpath.basename(path), data))
        try:
            summary = await ctx.reference_pack_writer(
                ctx.owner_id, args.name, args.description, collected
            )
        except ReferencePackWriteError as exc:
            return ToolOutcome(success=False, content="", error=f"{exc.code}: {exc}")
        names = ", ".join(name for name, _ in collected)
        return ToolOutcome(
            success=True,
            content=(
                f"Saved Reference Pack '{summary.get('name')}' ({summary.get('pack_id')}) with "
                f"{len(collected)} file(s): {names}. The user can select it in a later Build "
                "and edit it under Settings → Reference packs."
            ),
            structured={"pack": summary},
        )

"""scaffold_starter — the starter-component CATALOG (P7, redesigned 2026-07-09).

Modeled on the Claude-design `copy_starter_component` doctrine
(docs/claude-design-playbook.md §4): starters are a catalog of host-owned,
composable scaffolds the MODEL selects by fit — never locked to a contract kind.
The catalog (with when-to-use guidance) lives in the tool schema itself; the
active contract may RECOMMEND a kit (ctx.starter_kit, used when `kind` is
omitted) but never gates availability. Precedence funnel: a bound design
direction / design-system template outranks a starter; a starter outranks
hand-rolling common chrome. Copy semantics: files are written into the
workspace (never clobbering existing work) and the kit's usage notes (NOTES.md)
come back in the result.

The original P7 wired the kit 1:1 to the contract (`static.site → app_shell`),
which both locked builds to a single frame AND — with contract activation
unwired — made the tool fail `no_starter` on 100% of calls. Catalog selection
removes the lock and the failure mode in one move.
"""

from __future__ import annotations

from typing import Literal

from disco.core import SecurityRisk
from disco.core.effects import EffectCapability
from disco.core.flags import appkit_enabled
from disco.core.kits import StarterKitRegistry
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares

StarterKind = Literal[
    "app_shell",
    "lead_form",
    "game_loop_vanilla",
    "pwa_shell",
    "device_frames",
    "ui_kit_dense",
]

# One when-to-use line per kind — the catalog the LLM selects from. Rendered into
# the tool description so selection guidance travels WITH the schema (the
# Claude-design pattern: teach the why so the choice generalizes).
_CATALOG: dict[str, str] = {
    "app_shell": (
        "minimal semantic single-page HTML frame (head/meta/nav/footer + CSS "
        "baseline). Use for a static site or prototype page when no richer kit fits."
    ),
    "lead_form": (
        "AppKit lead-gen seed (.disco/appspec.json + rendered index.html: hero + "
        "contact form). Use ONLY for AppKit/appspec builds — it is app_create's default."
    ),
    "game_loop_vanilla": (
        "zero-dependency Canvas2D game shell: fixed-timestep loop, input map, "
        "scenes, pause/resume, HiDPI scaling. Use for ANY playable game or "
        "interactive canvas toy instead of hand-rolling the loop."
    ),
    "pwa_shell": (
        "installable mobile-first PWA: manifest, service worker, offline cache, "
        "icons, app chrome. Use when the user wants an installable/mobile app feel."
    ),
    "device_frames": (
        "clean-room phone / desktop-window preview frames (CSS only). Use whenever "
        "a design should be SHOWN inside realistic device chrome."
    ),
    "ui_kit_dense": (
        "dense dashboard shell: sidebar + topbar + card/table/stat primitives with "
        "a tokenized stylesheet. Use for admin panels, dashboards, data-heavy tools."
    ),
}


def _catalog_lines() -> str:
    return "\n".join(f"- {kind} — {desc}" for kind, desc in _CATALOG.items())


class ScaffoldStarterArgs(BaseModel):
    title: str = Field(
        description=(
            "The artifact/site title the starter frame should carry (page <title> and header)."
        )
    )
    kind: StarterKind | None = Field(
        default=None,
        description=(
            "Which starter component to scaffold (see the catalog in the tool "
            "description). Omit to use the build contract's recommended kit, when "
            "one is active."
        ),
    )


class ScaffoldStarterTool:
    definition = ToolDef(
        name="scaffold_starter",
        description=(
            "Copy a ready-made starter component into the workspace so you edit a "
            "real, host-hardened frame instead of hand-drawing common chrome. Pick "
            "the kind that fits the build (compose with your own files freely):\n"
            + _catalog_lines()
            + "\nPrecedence: a bound design direction / design-system template "
            "outranks a starter; a starter outranks hand-rolling its domain. "
            "Writes ONLY files that don't already exist — it never clobbers your "
            "work — and returns the kit's usage notes."
        ),
        args_model=ScaffoldStarterArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: ScaffoldStarterArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        # Model's pick wins; the active contract's kit is the RECOMMENDATION fallback.
        starter_id = args.kind or getattr(ctx, "starter_kit", None)
        if not starter_id:
            return ToolOutcome(
                success=False,
                error="no_starter",
                content=(
                    "pass `kind` to pick a starter component — available: "
                    + ", ".join(_CATALOG)
                    + ". (No build contract recommends one for this run.)"
                ),
            )
        # KILL SWITCH: lead_form seeds an AppKit app (.disco/appspec.json) — with
        # AppKit disabled that seed is a dead end, so refuse with the free-form path.
        if starter_id == "lead_form" and not appkit_enabled():
            return ToolOutcome(
                success=False,
                error="appkit_disabled",
                content=(
                    "the 'lead_form' starter seeds an AppKit app, and AppKit is "
                    "disabled on this deployment (DISCO_APPKIT_ENABLED=0). Use "
                    "'app_shell' and add a plain HTML form to it instead."
                ),
            )
        kit = StarterKitRegistry.default().get(starter_id)
        if kit is None:
            return ToolOutcome(
                success=False,
                error="unknown_starter",
                content=(f"no starter kit {starter_id!r} — available: " + ", ".join(_CATALOG)),
            )
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
            success=True,
            content=msg,
            structured=structured,
        )

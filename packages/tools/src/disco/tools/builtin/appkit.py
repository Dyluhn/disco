"""AppKit specialized mutation tools (P4 / TOOL-1).

Small, SEMANTIC edits to an app's structured AppSpec (`.disco/appspec.json`), each
re-rendering a self-contained `index.html`. The model edits the spec — not raw HTML —
so a copy/color/section change touches exactly one thing (the targeted-edit law). A raw
file rewrite is NOT in this set; it lives only in the repair/custom contract scopes.

The overlapping legacy creation/edit/design tools are no longer registered on the
normal Build surface; v2 AppKit tools are registered explicitly by the strict
AppKit executor path. This module remains for persisted-AppSpec adapter paths and
keeps the two legacy wrappers that are still essential:
app_set_tweak / app_snapshot_version.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import AppSection, AppSpec, render_html
from disco.core.appkit.models import DEFAULT_DESIGN
from disco.core.effects import EffectCapability
from disco.core.kits import lead_form_appspec
from disco.core.tweaks import TweakEditor, TweakField, TweakSpec
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from .tweaks_io import TWEAKS_PATH, read_tweakspec, write_tweakspec

_SPEC_PATH = ".disco/appspec.json"
_HTML_PATH = "index.html"


class AppSpecStore:
    """Reads/writes the durable AppSpec + the rendered index.html over a sandbox FS."""

    def __init__(self, fs: Any) -> None:
        self._fs = fs

    async def read(self) -> AppSpec | None:
        try:
            data = await self._fs.read_file(_SPEC_PATH)
        except Exception:
            return None
        return AppSpec.model_validate_json(data)  # raises on corrupt → caller maps to error

    async def write(self, spec: AppSpec) -> None:
        await self._fs.write_file(
            _SPEC_PATH, (spec.model_dump_json(indent=2) + "\n").encode("utf-8")
        )
        await self._fs.write_file(_HTML_PATH, render_html(spec).encode("utf-8"))


async def _apply(ctx: ToolContext, mutate: Callable[[AppSpec], AppSpec]) -> ToolOutcome:
    """Load the spec, apply a pure mutation, persist spec + re-rendered HTML."""
    assert ctx.sandbox is not None
    store = AppSpecStore(ctx.sandbox)
    try:
        spec = await store.read()
    except Exception:
        return ToolOutcome(
            success=False, error="corrupt_appspec", content=f"{_SPEC_PATH} is unreadable"
        )
    if spec is None:
        return ToolOutcome(
            success=False, error="no_app", content="no app yet — call app_create first"
        )
    try:
        new_spec = mutate(spec)
    except ValueError as exc:
        return ToolOutcome(success=False, error="invalid_app_edit", content=str(exc))
    await store.write(new_spec)
    return ToolOutcome(
        success=True,
        content=f"updated app + re-rendered {_HTML_PATH}",
        structured={"sections": [s.id for s in new_spec.sections]},
    )


def _def(name: str, desc: str, args_model: type[BaseModel]) -> ToolDef:
    return ToolDef(
        name=name,
        description=desc,
        args_model=args_model,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=False,
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )


# --- app_create ---------------------------------------------------------------
class _SectionArg(BaseModel):
    id: str
    kind: str
    fields: dict[str, str] = {}


class AppCreateArgs(BaseModel):
    title: str
    sections: list[_SectionArg] | None = Field(
        default=None, description="optional sections; default scaffolds a hero + lead form"
    )


def _default_tweakspec() -> TweakSpec:
    """The owner controls seeded for the DEFAULT lead app, so app_set_tweak is usable the
    moment app_create runs (no governed-but-unauthorable false affordance)."""
    return TweakSpec(
        fields=(
            TweakField(
                key="lead.include_phone",
                label="Include phone field",
                editor=TweakEditor.BOOLEAN,
                affects=("lead.phone",),
                default=False,
            ),
            TweakField(
                key="brand.accent",
                label="Accent color",
                editor=TweakEditor.PALETTE,
                colors=(DEFAULT_DESIGN["accent"], DEFAULT_DESIGN["primary"]),
                affects=("design.accent",),
            ),
        )
    )


class AppCreateTool:
    definition = _def(
        "app_create",
        "Create a new AppKit app (.disco/appspec.json) + render index.html. Default "
        "scaffolds a hero + lead_form; pass sections to override. Use the semantic app_* "
        "tools for all later edits.",
        AppCreateArgs,
    )

    async def run(self, args: AppCreateArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        store = AppSpecStore(ctx.sandbox)
        if args.sections is not None:
            try:
                sections = tuple(
                    AppSection(id=s.id, kind=s.kind, fields=s.fields) for s in args.sections
                )
            except Exception as exc:  # bad kind etc.
                return ToolOutcome(success=False, error="invalid_app_edit", content=str(exc))
            try:
                spec = AppSpec(title=args.title, sections=sections)
            except Exception as exc:
                return ToolOutcome(success=False, error="invalid_app_edit", content=str(exc))
        else:
            # P7: scaffold the default from the host-owned lead_form starter (single
            # source — the StarterKit and app_create can't drift).
            spec = lead_form_appspec(args.title)
        await store.write(spec)
        # Seed the owner-control schema so app_set_tweak is immediately usable: the default
        # lead app gets real grounded tweaks; a custom-sections app gets an empty (but present)
        # TweakSpec — so the tool reports unknown_tweak, never no_tweakspec, post-create.
        tspec = _default_tweakspec() if args.sections is None else TweakSpec(fields=())
        await write_tweakspec(ctx.sandbox, tspec)
        return ToolOutcome(
            success=True,
            content=f"created app '{args.title}' + index.html",
            structured={"sections": [s.id for s in spec.sections]},
        )


# --- app_update_content -------------------------------------------------------
class AppUpdateContentArgs(BaseModel):
    section_id: str
    field: str
    value: str


class AppUpdateContentTool:
    definition = _def(
        "app_update_content",
        "Change ONE field of ONE section (e.g. the hero headline, a CTA label). Targeted "
        "semantic edit — never rewrites the whole app.",
        AppUpdateContentArgs,
    )

    async def run(self, args: AppUpdateContentArgs, ctx: ToolContext) -> ToolOutcome:
        return await _apply(ctx, lambda s: s.with_content(args.section_id, args.field, args.value))


# --- app_add_section ----------------------------------------------------------
class AppAddSectionArgs(BaseModel):
    id: str
    kind: str
    fields: dict[str, str] = {}
    index: int | None = None


class AppAddSectionTool:
    definition = _def(
        "app_add_section", "Add a section of a known kind at an optional index.", AppAddSectionArgs
    )

    async def run(self, args: AppAddSectionArgs, ctx: ToolContext) -> ToolOutcome:
        def _m(s: AppSpec) -> AppSpec:
            return s.with_section_added(
                AppSection(id=args.id, kind=args.kind, fields=args.fields), index=args.index
            )

        return await _apply(ctx, _m)


# --- app_remove_section / app_reorder_section ---------------------------------
class AppSectionRefArgs(BaseModel):
    section_id: str


class AppRemoveSectionTool:
    definition = _def("app_remove_section", "Remove a section by id.", AppSectionRefArgs)

    async def run(self, args: AppSectionRefArgs, ctx: ToolContext) -> ToolOutcome:
        return await _apply(ctx, lambda s: s.with_section_removed(args.section_id))


class AppReorderSectionArgs(BaseModel):
    section_id: str
    to_index: int


class AppReorderSectionTool:
    definition = _def(
        "app_reorder_section", "Move a section to a new index.", AppReorderSectionArgs
    )

    async def run(self, args: AppReorderSectionArgs, ctx: ToolContext) -> ToolOutcome:
        return await _apply(ctx, lambda s: s.with_section_reordered(args.section_id, args.to_index))


# --- app_set_design / app_set_tweak -------------------------------------------
class AppSetKVArgs(BaseModel):
    key: str = Field(
        description=(
            "Tweak key exactly as defined in .disco/tweaks.json (e.g. 'lead.include_phone')."
        )
    )
    value: str = Field(
        description="New value, as a string; validated against the tweak's spec (e.g. 'true')."
    )


class AppSetDesignTool:
    definition = _def(
        "app_set_design", "Set a design token (e.g. primary='#0b5'). Re-renders.", AppSetKVArgs
    )

    async def run(self, args: AppSetKVArgs, ctx: ToolContext) -> ToolOutcome:
        return await _apply(ctx, lambda s: s.with_design(args.key, args.value))


class AppSetTweakTool:
    definition = _def(
        "app_set_tweak",
        "Set a tweak DEFINED in .disco/tweaks.json (validated against its TweakSpec, e.g. "
        "lead.include_phone='true'). Rejects unknown tweaks + invalid values.",
        AppSetKVArgs,
    )

    async def run(self, args: AppSetKVArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        store = AppSpecStore(ctx.sandbox)
        # Precedence: no_app > corrupt_appspec > no_tweakspec > corrupt_tweakspec >
        # unknown_tweak > invalid_tweak_value. Validate fully BEFORE writing the appspec.
        try:
            spec = await store.read()
        except Exception:
            return ToolOutcome(
                success=False, error="corrupt_appspec", content=f"{_SPEC_PATH} is unreadable"
            )
        if spec is None:
            return ToolOutcome(
                success=False, error="no_app", content="no app yet — call app_create first"
            )
        try:
            tspec = await read_tweakspec(
                ctx.sandbox
            )  # ValueError = corrupt; backend errors propagate
        except ValueError:
            return ToolOutcome(
                success=False, error="corrupt_tweakspec", content=f"{TWEAKS_PATH} is unreadable"
            )
        if tspec is None:
            return ToolOutcome(
                success=False,
                error="no_tweakspec",
                content=f"this app defines no tweaks ({TWEAKS_PATH} missing)",
            )
        field = tspec.get(args.key)
        if field is None:
            avail = ", ".join(f.key for f in tspec.fields) or "(none)"
            return ToolOutcome(
                success=False,
                error="unknown_tweak",
                content=f"unknown tweak {args.key!r}; defined: {avail}",
            )
        try:
            coerced = tspec.validate_value(args.key, args.value)
        except ValueError as exc:
            return ToolOutcome(success=False, error="invalid_tweak_value", content=str(exc))
        await store.write(spec.with_tweak(args.key, coerced))  # only mutate after all validation
        return ToolOutcome(
            success=True,
            content=f"set tweak {args.key} = {coerced!r}",
            structured={"tweak": args.key, "value": coerced, "affects": list(field.affects)},
        )


# --- app_snapshot_version -----------------------------------------------------
class AppSnapshotArgs(BaseModel):
    label: str = Field(
        default="", description="optional version label; defaults to a sequence number"
    )


class AppSnapshotVersionTool:
    definition = _def(
        "app_snapshot_version",
        "Snapshot the current AppSpec to .disco/versions/<label>.json for iteration history.",
        AppSnapshotArgs,
    )

    async def run(self, args: AppSnapshotArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        store = AppSpecStore(ctx.sandbox)
        try:
            spec = await store.read()
        except Exception:
            return ToolOutcome(
                success=False, error="corrupt_appspec", content=f"{_SPEC_PATH} is unreadable"
            )
        if spec is None:
            return ToolOutcome(
                success=False, error="no_app", content="no app yet — call app_create first"
            )
        # deterministic, content-addressed label (NOT builtin hash() — that is
        # per-process randomized, so it would produce unstable/colliding names).
        digest = hashlib.sha256(spec.model_dump_json().encode("utf-8")).hexdigest()[:12]
        label = args.label.strip() or f"v-{digest}"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        path = f".disco/versions/{safe}.json"
        await ctx.sandbox.write_file(path, (spec.model_dump_json(indent=2) + "\n").encode("utf-8"))
        return ToolOutcome(
            success=True, content=f"snapshotted app → {path}", structured={"rel_path": path}
        )


APP_TOOLS: tuple[type, ...] = (
    AppSetTweakTool,
    AppSnapshotVersionTool,
)

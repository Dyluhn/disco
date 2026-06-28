"""AppKit specialized mutation tools (P4 / TOOL-1).

Small, SEMANTIC edits to an app's structured AppSpec (`.disco/appspec.json`), each
re-rendering a self-contained `index.html`. The model edits the spec — not raw HTML —
so a copy/color/section change touches exactly one thing (the targeted-edit law). A raw
file rewrite is NOT in this set; it lives only in the repair/custom contract scopes.

Tools: app_create / app_update_content / app_add_section / app_remove_section /
app_reorder_section / app_set_design / app_set_tweak / app_snapshot_version.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from disco.core import SecurityRisk
from disco.core.appkit import AppSection, AppSpec, render_html
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

_SPEC_PATH = ".disco/appspec.json"
_HTML_PATH = "index.html"


def _coerce_scalar(value: str) -> Any:
    """Coerce a string tweak value to a stable scalar: bools (case-insensitive
    true/false), ints, else the trimmed string — so the spec's tweak shape is
    predictable rather than 'true' the string sometimes and True other times."""
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    body = value.strip()
    if body.lstrip("-").isdigit():
        return int(body)
    return value


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
        await self._fs.write_file(_SPEC_PATH, (spec.model_dump_json(indent=2) + "\n").encode("utf-8"))
        await self._fs.write_file(_HTML_PATH, render_html(spec).encode("utf-8"))


async def _apply(ctx: ToolContext, mutate: Callable[[AppSpec], AppSpec]) -> ToolOutcome:
    """Load the spec, apply a pure mutation, persist spec + re-rendered HTML."""
    assert ctx.sandbox is not None
    store = AppSpecStore(ctx.sandbox)
    try:
        spec = await store.read()
    except Exception:
        return ToolOutcome(success=False, error="corrupt_appspec", content=f"{_SPEC_PATH} is unreadable")
    if spec is None:
        return ToolOutcome(success=False, error="no_app", content="no app yet — call app_create first")
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
                sections = tuple(AppSection(id=s.id, kind=s.kind, fields=s.fields) for s in args.sections)
            except Exception as exc:  # bad kind etc.
                return ToolOutcome(success=False, error="invalid_app_edit", content=str(exc))
        else:
            sections = (
                AppSection(id="hero", kind="hero", fields={"headline": args.title, "subhead": "", "cta_text": "Get started"}),
                AppSection(id="lead", kind="lead_form", fields={"title": "Contact us", "submit_text": "Send"}),
            )
        try:
            spec = AppSpec(title=args.title, sections=sections)
        except Exception as exc:
            return ToolOutcome(success=False, error="invalid_app_edit", content=str(exc))
        await store.write(spec)
        return ToolOutcome(success=True, content=f"created app '{args.title}' + index.html",
                           structured={"sections": [s.id for s in spec.sections]})


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
    definition = _def("app_add_section", "Add a section of a known kind at an optional index.", AppAddSectionArgs)

    async def run(self, args: AppAddSectionArgs, ctx: ToolContext) -> ToolOutcome:
        def _m(s: AppSpec) -> AppSpec:
            return s.with_section_added(AppSection(id=args.id, kind=args.kind, fields=args.fields), index=args.index)

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
    definition = _def("app_reorder_section", "Move a section to a new index.", AppReorderSectionArgs)

    async def run(self, args: AppReorderSectionArgs, ctx: ToolContext) -> ToolOutcome:
        return await _apply(ctx, lambda s: s.with_section_reordered(args.section_id, args.to_index))


# --- app_set_design / app_set_tweak -------------------------------------------
class AppSetKVArgs(BaseModel):
    key: str
    value: str


class AppSetDesignTool:
    definition = _def("app_set_design", "Set a design token (e.g. primary='#0b5'). Re-renders.", AppSetKVArgs)

    async def run(self, args: AppSetKVArgs, ctx: ToolContext) -> ToolOutcome:
        return await _apply(ctx, lambda s: s.with_design(args.key, args.value))


class AppSetTweakTool:
    definition = _def("app_set_tweak", "Set a tweak value (e.g. lead_form.include_phone='true').", AppSetKVArgs)

    async def run(self, args: AppSetKVArgs, ctx: ToolContext) -> ToolOutcome:
        val = _coerce_scalar(args.value)
        return await _apply(ctx, lambda s: s.with_tweak(args.key, val))


# --- app_snapshot_version -----------------------------------------------------
class AppSnapshotArgs(BaseModel):
    label: str = Field(default="", description="optional version label; defaults to a sequence number")


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
            return ToolOutcome(success=False, error="corrupt_appspec", content=f"{_SPEC_PATH} is unreadable")
        if spec is None:
            return ToolOutcome(success=False, error="no_app", content="no app yet — call app_create first")
        # deterministic, content-addressed label (NOT builtin hash() — that is
        # per-process randomized, so it would produce unstable/colliding names).
        digest = hashlib.sha256(spec.model_dump_json().encode("utf-8")).hexdigest()[:12]
        label = args.label.strip() or f"v-{digest}"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        path = f".disco/versions/{safe}.json"
        await ctx.sandbox.write_file(path, (spec.model_dump_json(indent=2) + "\n").encode("utf-8"))
        return ToolOutcome(success=True, content=f"snapshotted app → {path}", structured={"rel_path": path})


APP_TOOLS: tuple[type, ...] = (
    AppCreateTool,
    AppUpdateContentTool,
    AppAddSectionTool,
    AppRemoveSectionTool,
    AppReorderSectionTool,
    AppSetDesignTool,
    AppSetTweakTool,
    AppSnapshotVersionTool,
)

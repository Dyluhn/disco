"""AppKit EPIC E2/E3 — the validated-patch app tools.

Four tools that MUTATE the structured specs (always re-validated) and then
REGENERATE the affected files from those specs — never free-form editing of the
generated tree:

* `app_create`          — derive/accept a lead-gen AppSpec + a recipe DesignSpec,
                          run the pure generator, and WRITE the whole tree plus
                          `.disco/appspec.json` + `.disco/designspec.json`. Refuses
                          to overwrite an existing app without `overwrite`.
* `app_add_section`     — insert a validated Section (unique id, a real catalog
                          variant of its kind) into a page, re-save the spec, and
                          regenerate only the affected files.
* `app_update_content`  — patch a Section's bounded `content`, re-save, regenerate
                          only the touched files.
* `app_set_design`      — swap the DesignSpec (a recipe, or a raw spec only if the
                          regenerated output passes design_lint), re-save, and
                          regenerate the design/CSS files.

All four follow the SAME airtight cycle: load the specs from the sandbox →
validate the mutation → regenerate the FULL tree from the mutated specs → run the
design_lint gate (reject a patch that would produce slop) → write back ONLY the
files whose content changed (write-on-diff), plus the spec(s) that changed. The
write-on-diff step is what keeps the `.disco/` specs and the generated files in
sync while touching a minimal, predictable set — and it is why these tools never
hand-edit generated files: the spec is the single source of truth, the tree is
its projection.

Layering: tools → core is allowed. This imports the pure generator + the airtight
spec IO from `disco.core.appkit`, and `lint_design` from the sibling module.

Registration: `APPKIT_V2_TOOLS` is registered by the appkit contract (B3);
intentionally NOT in builtin/__init__ to avoid shadowing the legacy tools.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    LEAD_GEN_PRIMITIVE_ID,
    generate,
    get_primitive,
    get_recipe,
    get_variant,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
    primitive_ids,
    resolve_primitive,
    serialize_app_spec,
    serialize_design_spec,
    spec_digest,
    summarize_specs,
    tree_digest,
    tree_file_hashes,
)
from disco.core.appkit.spec import AppSpec, DesignSpec, Section, SectionContent
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SkipValidation,
    field_validator,
    model_validator,
)

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from .design_lint import lint_design

_FS = frozenset({Capability.FILESYSTEM})


def _unwrap_weak_fc_items_wrapper(value: Any) -> Any:
    """Unwrap MiniMax weak-FC array wrappers at the `items` slot only.

    Live 2026-07-03: with prose-only array shape guidance, MiniMax wrapped the
    list as {"item": [...]} / {"items": [...]} and pydantic rejected the call.
    """
    if isinstance(value, dict) and len(value) == 1:
        key, wrapped = next(iter(value.items()))
        if key in {"item", "items"} and isinstance(wrapped, list):
            return wrapped
    return value


def _normalize_content_items_for_weak_fc(content: Any) -> Any:
    if not isinstance(content, dict):
        return content
    if len(content) == 1:
        key, wrapped = next(iter(content.items()))
        if key == "item" and isinstance(wrapped, list):
            return {"items": wrapped}

    raw_items = content.get("items")
    normalized_items = _unwrap_weak_fc_items_wrapper(raw_items)
    if normalized_items is raw_items:
        return content
    return {**content, "items": normalized_items}


def _normalize_section_content_for_weak_fc(section: dict[str, Any]) -> dict[str, Any]:
    content = section.get("content")
    normalized_content = _normalize_content_items_for_weak_fc(content)
    if normalized_content is content:
        return section
    return {**section, "content": normalized_content}


def _section_validation_input(section: object) -> object:
    if isinstance(section, Section):
        return section.model_dump(mode="json")
    if isinstance(section, dict):
        return _normalize_section_content_for_weak_fc(section)
    return section


# ---- shared sandbox spec IO + tree application --------------------------------


class _AppKitError(Exception):
    """A model-facing failure inside an app-kit tool (mapped to a failure outcome)."""


async def _read_spec_bytes(ctx: ToolContext, relpath: str) -> bytes | None:
    assert ctx.sandbox is not None
    try:
        exists = await ctx.sandbox.file_exists(relpath)
    except Exception:  # noqa: BLE001 — treat a probe failure as absent
        exists = False
    if not exists:
        return None
    try:
        return await ctx.sandbox.read_file(relpath)
    except Exception:  # noqa: BLE001 — vanished between probe and read
        return None


async def _load_app_spec(ctx: ToolContext) -> AppSpec:
    data = await _read_spec_bytes(ctx, APPSPEC_RELPATH)
    if data is None:
        raise _AppKitError(
            f"no {APPSPEC_RELPATH} in the workspace — run app_create first."
        )
    try:
        return load_app_spec_from_bytes(data)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"{APPSPEC_RELPATH} is invalid: {exc}") from exc


async def _load_design_spec(ctx: ToolContext) -> DesignSpec:
    data = await _read_spec_bytes(ctx, DESIGNSPEC_RELPATH)
    if data is None:
        raise _AppKitError(
            f"no {DESIGNSPEC_RELPATH} in the workspace — run app_create first."
        )
    try:
        return load_design_spec_from_bytes(data)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"{DESIGNSPEC_RELPATH} is invalid: {exc}") from exc


def _lint_gate(tree: dict[str, str], design: DesignSpec) -> None:
    """Reject a mutation whose regenerated output would be design-slop. The recipe
    path is clean by construction, so this is a no-op there; it is the real guard
    on the raw-DesignSpec path (P1) and a backstop everywhere else — a patch can
    never land a lint-dirty tree on disk."""
    verdict = lint_design(tree, design, spec_present=True, spec_valid=True)
    if not verdict["ok"]:
        rules = ", ".join(dict.fromkeys(f["rule_id"] for f in verdict["findings"]))
        raise _AppKitError(
            "refused: the regenerated app would contain design slop "
            f"({verdict['counts']['error']} error / {verdict['counts']['warning']} warning) "
            f"— {rules}. Use a recipe, or justify the off-defaults in the DesignSpec."
        )


async def _apply_tree(ctx: ToolContext, tree: dict[str, str]) -> list[str]:
    """Write back ONLY the generated files whose content differs from what's already
    in the sandbox (write-on-diff). Returns the touched paths, sorted. New files
    (absent) are always written. This is what keeps the touched set minimal and
    the spec ⇄ tree projection exact."""
    assert ctx.sandbox is not None
    touched: list[str] = []
    for path in sorted(tree):
        new = tree[path].encode("utf-8")
        old: bytes | None
        try:
            old = await ctx.sandbox.read_file(path)
        except Exception:  # noqa: BLE001 — absent / unreadable → treat as new
            old = None
        if old != new:
            await ctx.sandbox.write_file(path, new)
            touched.append(path)
    return touched


async def _save_app_spec(ctx: ToolContext, spec: AppSpec) -> None:
    assert ctx.sandbox is not None
    await ctx.sandbox.write_file(APPSPEC_RELPATH, serialize_app_spec(spec).encode("utf-8"))


async def _save_design_spec(ctx: ToolContext, spec: DesignSpec) -> None:
    assert ctx.sandbox is not None
    await ctx.sandbox.write_file(
        DESIGNSPEC_RELPATH, serialize_design_spec(spec).encode("utf-8")
    )


def _validate_variant(section: Section) -> None:
    """Referential integrity: a section's variant_id (if set) must name a real
    catalog variant OF that section's kind."""
    if section.variant_id is None:
        return
    variant = get_variant(section.variant_id)
    if variant is None:
        raise _AppKitError(f"unknown section variant_id: {section.variant_id!r}")
    if variant.kind != section.kind:
        raise _AppKitError(
            f"variant {section.variant_id!r} is a {variant.kind!r} layout, "
            f"not {section.kind!r}"
        )


# ---- app_create ---------------------------------------------------------------


def _unknown_primitive_msg(prim_id: str) -> str:
    known = ", ".join(sorted(primitive_ids()))
    return f"unknown primitive_id: {prim_id!r}. Known primitives: {known}."


def _safe_load_app_kind(spec_bytes: bytes) -> str | None:
    """The `app_kind` of an on-disk AppSpec, or None if it won't load — used only by
    the overwrite guard to compare the prior primitive against the new one."""
    try:
        return load_app_spec_from_bytes(spec_bytes).app_kind
    except Exception:  # noqa: BLE001 — an unreadable prior spec doesn't block overwrite
        return None


class AppCreateArgs(BaseModel):
    recipe_id: str = Field(
        description="The SiteRecipe id to derive the DesignSpec from (e.g. 'editorial-ledger')."
    )
    primitive_id: str = Field(
        default=LEAD_GEN_PRIMITIVE_ID,
        description="The AppKit primitive to scaffold: 'lead_gen' (a lead-capture app) or "
        "'directory' (a static, searchable directory site). Ignored when an explicit "
        "app_spec is given (the primitive is taken from app_spec.app_kind).",
    )
    brief: str | None = Field(
        default=None,
        description="A SHORT app/brand NAME only (a few words, e.g. 'Ember & Oak' — "
        "max 60 chars). NOT the full request: it becomes the app's display name and "
        "hero heading seed. The scaffold seeds PLACEHOLDER copy — replace it with "
        "app_update_content after creating.",
    )
    app_spec: SkipValidation[AppSpec] | None = Field(
        default=None,
        description="Optional explicit AppSpec (JSON). When omitted, a sensible default "
        "AppSpec for the chosen primitive is derived from the brief + recipe.",
    )
    overwrite: bool = Field(
        default=False,
        description="Overwrite an existing app (.disco/appspec.json). Refused unless true.",
    )


class AppCreateTool:
    """[CONTRACT] Create a lead-gen Cloudflare app: derive/accept an AppSpec, lower a
    recipe to a DesignSpec, run the deterministic generator, and write the whole
    tree + the two `.disco/` specs. Refuses to overwrite an existing app."""

    definition = ToolDef(
        name="app_create",
        description=(
            "Scaffold a lead-gen Cloudflare app (Vite React SPA + Worker + D1) from a "
            "design RECIPE. Provide `recipe_id` (required) and optionally an explicit "
            "`app_spec` JSON or a `brief` to name a sensible default. Writes the generated "
            "tree plus .disco/appspec.json + .disco/designspec.json. The output is "
            "design_lint-clean by construction. Refuses to overwrite an existing app "
            "unless `overwrite` is true."
        ),
        args_model=AppCreateArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
    )

    async def run(self, args: AppCreateArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            recipe = get_recipe(args.recipe_id)
            if recipe is None:
                raise _AppKitError(
                    f"unknown recipe_id: {args.recipe_id!r}. See the SiteRecipe catalog."
                )
            design = recipe.to_design_spec()

            # Resolve the primitive. With an explicit app_spec the primitive is taken
            # from app_spec.app_kind (how generate() dispatches); a caller-supplied
            # primitive_id must then AGREE (reject a mismatch). With no app_spec we use
            # the explicit primitive_id strictly (an unknown id is refused — it never
            # silently falls back to lead-gen).
            if args.app_spec is not None:
                try:
                    app = AppSpec.model_validate(args.app_spec)
                except Exception as exc:  # noqa: BLE001
                    raise _AppKitError(f"invalid app_spec: {exc}") from exc
                primitive = resolve_primitive(app.app_kind)
                if "primitive_id" in args.model_fields_set:
                    requested = get_primitive(args.primitive_id)
                    if requested is None:
                        raise _AppKitError(_unknown_primitive_msg(args.primitive_id))
                    if requested.id != primitive.id:
                        raise _AppKitError(
                            f"primitive_id={args.primitive_id!r} does not match the "
                            f"app_spec.app_kind={app.app_kind!r} (resolves to "
                            f"{primitive.id!r}). Omit primitive_id, or make them agree."
                        )
            else:
                primitive = get_primitive(args.primitive_id)
                if primitive is None:
                    raise _AppKitError(_unknown_primitive_msg(args.primitive_id))
                brief = (args.brief or "").strip()
                if len(brief) > 60:
                    # Live-caught 2026-07-03: the model passed the ENTIRE build
                    # request here, poisoning the app name + seeded hero copy
                    # (which then made its content edits look like no-ops).
                    # Refuse with guidance instead of silently truncating.
                    raise _AppKitError(
                        f"brief is {len(brief)} chars — it must be a SHORT brand "
                        "name (max 60 chars, e.g. 'Ember & Oak'). Put the rest of "
                        "the request into app_update_content edits after creating."
                    )
                app = primitive.default_app_spec(brief or "Your Brand", recipe)
            # The primitive's airtight normalization (e.g. lead-gen appends the lead
            # entity the worker targets; directory is identity).
            app = primitive.prepare_app_spec(app)

            existing = await _read_spec_bytes(ctx, APPSPEC_RELPATH)
            if existing is not None:
                if not args.overwrite:
                    raise _AppKitError(
                        f"{APPSPEC_RELPATH} already exists — pass overwrite=true to "
                        "replace the app."
                    )
                # Overwrite guard: a CROSS-PRIMITIVE overwrite changes the generated path
                # set (e.g. lead-gen's .dev.vars.example / lead components are not part of
                # a directory tree). The sandbox protocol has no delete, so write-on-diff
                # would leave those stale files behind. Refuse rather than ship a polluted
                # tree; the owner clears the workspace and re-creates.
                prior = _safe_load_app_kind(existing)
                if prior is not None and resolve_primitive(prior).id != primitive.id:
                    raise _AppKitError(
                        f"refused: cannot overwrite an existing "
                        f"'{resolve_primitive(prior).id}' app with a '{primitive.id}' app "
                        "in place — the generated file sets differ, and stale files from "
                        "the old primitive can't be removed through the sandbox. Clear the "
                        "workspace (or use a fresh one) before scaffolding a different "
                        "primitive."
                    )

            tree = generate(app, design)
            _lint_gate(tree, design)

            touched = await _apply_tree(ctx, tree)
            await _save_app_spec(ctx, app)
            await _save_design_spec(ctx, design)
            artifacts = sorted({*touched, APPSPEC_RELPATH, DESIGNSPEC_RELPATH})
            return ToolOutcome(
                success=True,
                content=(
                    f"app_create: scaffolded '{app.name}' ({primitive.id}) from recipe "
                    f"'{recipe.id}' — {len(tree)} files + 2 specs."
                ),
                structured={
                    "recipe_id": recipe.id,
                    "primitive_id": primitive.id,
                    "app_kind": app.app_kind,
                    "app_name": app.name,
                    "files_written": touched,
                    "specs": [APPSPEC_RELPATH, DESIGNSPEC_RELPATH],
                },
                artifacts=artifacts,
            )
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_create_refused")


# ---- app_add_section ----------------------------------------------------------


class AppAddSectionArgs(BaseModel):
    page_id: str = Field(description="The page to insert the section into.")
    section: SkipValidation[Section] = Field(
        description="The Section JSON to insert "
        "(id, kind, optional variant_id/content/content_ref)."
    )
    after_section_id: str | None = Field(
        default=None,
        description="Insert AFTER this existing section id (omit to append at the end).",
    )


class AppAddSectionTool:
    """[CONTRACT] Insert a VALIDATED section into a page and regenerate the affected
    files. Not free-form editing: the section is schema-validated, its variant must
    be a real catalog variant of its kind, ids stay unique, and the whole spec is
    re-validated before the tree is regenerated."""

    definition = ToolDef(
        name="app_add_section",
        description=(
            "Insert a new section into a page of the current app (validated patch): the "
            "section is schema-checked, its variant_id must be a catalog variant of its kind, "
            "and ids must stay unique. Re-saves .disco/appspec.json and regenerates only the "
            "affected files (the new component, App.tsx, content.ts, manifest). Use "
            "after_section_id to control placement."
        ),
        args_model=AppAddSectionArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
    )

    async def run(self, args: AppAddSectionArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            app = await _load_app_spec(ctx)
            design = await _load_design_spec(ctx)
            try:
                section = Section.model_validate(_section_validation_input(args.section))
            except Exception as exc:  # noqa: BLE001
                raise _AppKitError(f"invalid section: {exc}") from exc
            _validate_variant(section)

            data = app.model_dump(mode="json")
            page = next((p for p in data["pages"] if p["id"] == args.page_id), None)
            if page is None:
                raise _AppKitError(f"no page with id {args.page_id!r}")
            secs = list(page["sections"])
            sec_json = section.model_dump(mode="json")
            if args.after_section_id is not None:
                idx = next(
                    (i for i, s in enumerate(secs) if s["id"] == args.after_section_id),
                    None,
                )
                if idx is None:
                    raise _AppKitError(
                        f"no section {args.after_section_id!r} on page {args.page_id!r}"
                    )
                secs.insert(idx + 1, sec_json)
            else:
                secs.append(sec_json)
            page["sections"] = secs
            try:
                new_app = AppSpec.model_validate(data)  # enforces unique ids + caps
            except Exception as exc:  # noqa: BLE001
                raise _AppKitError(f"adding the section made the spec invalid: {exc}") from exc

            tree = generate(new_app, design)
            _lint_gate(tree, design)
            touched = await _apply_tree(ctx, tree)
            await _save_app_spec(ctx, new_app)
            return ToolOutcome(
                success=True,
                content=(
                    f"app_add_section: added '{section.id}' ({section.kind}) to "
                    f"page '{args.page_id}' — updated {len(touched)} file(s)."
                ),
                structured={"files_written": touched, "spec": APPSPEC_RELPATH},
                artifacts=sorted({*touched, APPSPEC_RELPATH}),
            )
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_add_section_refused")


# ---- app_update_content -------------------------------------------------------


class ContentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str | None = None
    subheading: str | None = None
    body: str | None = None
    cta_label: str | None = None
    items: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_top_level_items_wrapper(cls, value: Any) -> Any:
        if isinstance(value, dict) and len(value) == 1:
            key, wrapped = next(iter(value.items()))
            if key == "item" and isinstance(wrapped, list):
                return {"items": wrapped}
        return value

    @field_validator("items", mode="before")
    @classmethod
    def _normalize_items_wrapper(cls, value: Any) -> Any:
        # Live 2026-07-03: MiniMax weak function-calling wrapped arrays as
        # {"item": [...]} / {"items": [...]} when the slot was prose-only.
        return _unwrap_weak_fc_items_wrapper(value)


class AppUpdateContentArgs(BaseModel):
    page_id: str = Field(description="The page that holds the section.")
    section_id: str = Field(description="The section whose content to patch.")
    updates: ContentUpdate = Field(
        description="Content slots to set/merge: heading, subheading, body, cta_label, items "
        "(a list of strings). Unknown keys are rejected."
    )


class AppUpdateContentTool:
    """[CONTRACT] Patch a section's bounded `content` in the AppSpec and regenerate
    only the touched files. The updates are merged onto the existing content and
    re-validated (bounded slots, no unknown keys) before regeneration."""

    definition = ToolDef(
        name="app_update_content",
        description=(
            "Patch a section's content (heading/subheading/body/cta_label/items) in the "
            "current app's spec. Updates are merged onto the existing content and validated "
            "(bounded length, no unknown keys). Re-saves .disco/appspec.json and regenerates "
            "only the touched files (content.ts + manifest). Content lives in the spec, never "
            "hand-edited in the generated tree."
        ),
        args_model=AppUpdateContentArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
    )

    async def run(self, args: AppUpdateContentArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            app = await _load_app_spec(ctx)
            design = await _load_design_spec(ctx)

            data = app.model_dump(mode="json")
            page = next((p for p in data["pages"] if p["id"] == args.page_id), None)
            if page is None:
                raise _AppKitError(f"no page with id {args.page_id!r}")
            sec = next(
                (s for s in page["sections"] if s["id"] == args.section_id), None
            )
            if sec is None:
                raise _AppKitError(
                    f"no section {args.section_id!r} on page {args.page_id!r}"
                )
            updates = args.updates.model_dump(mode="json", exclude_unset=True)
            if not updates:
                raise _AppKitError(
                    "no updates provided — set at least one of "
                    "heading/subheading/body/cta_label/items"
                )
            existing = sec.get("content") or {}
            merged = {**existing, **updates}
            if merged == existing:
                # RC-M convention (live-caught 2026-07-03): a semantic no-op MUST
                # refuse with ground truth, not report success with "0 file(s)" —
                # the model cannot tell the edit didn't take and repeats it into
                # the stuck breaker. Carry the current values so the refusal is
                # self-recovering.
                import json as _json

                raise _AppKitError(
                    f"no-op: section {args.section_id!r} already has exactly these "
                    f"values — nothing changed. Current content: "
                    f"{_json.dumps(existing, ensure_ascii=False)[:600]}. Send a "
                    "DIFFERENT value for a slot (heading/subheading/body/cta_label/"
                    "items) or target another section."
                )
            try:
                # Validate the bounded content in isolation for a precise error.
                _ = SectionContent.model_validate(merged)
            except Exception as exc:  # noqa: BLE001
                raise _AppKitError(f"invalid content update: {exc}") from exc
            sec["content"] = merged
            try:
                new_app = AppSpec.model_validate(data)
            except Exception as exc:  # noqa: BLE001
                raise _AppKitError(f"the content update made the spec invalid: {exc}") from exc

            tree = generate(new_app, design)
            _lint_gate(tree, design)
            touched = await _apply_tree(ctx, tree)
            await _save_app_spec(ctx, new_app)
            return ToolOutcome(
                success=True,
                content=(
                    f"app_update_content: patched '{args.section_id}' on page "
                    f"'{args.page_id}' — updated {len(touched)} file(s)."
                ),
                structured={"files_written": touched, "spec": APPSPEC_RELPATH},
                artifacts=sorted({*touched, APPSPEC_RELPATH}),
            )
        except _AppKitError as exc:
            return ToolOutcome(
                success=False, content=str(exc), error="app_update_content_refused"
            )


# ---- app_set_design -----------------------------------------------------------


class AppSetDesignArgs(BaseModel):
    recipe_id: str | None = Field(
        default=None,
        description="Swap to this SiteRecipe's DesignSpec (the P0 path). "
        "Mutually exclusive with design_spec.",
    )
    design_spec: SkipValidation[DesignSpec] | None = Field(
        default=None,
        description="A raw DesignSpec JSON (P1). Accepted only if the regenerated output "
        "passes design_lint (no unjustified slop).",
    )
    variant_policy: str = Field(
        default="preserve",
        description="'preserve' keeps each section's variant_id; 'recipe' reassigns section "
        "variants from the new recipe's preferred layouts (recipe_id required).",
    )


class AppSetDesignTool:
    """[CONTRACT] Swap the DesignSpec (a recipe, or a raw spec only if its generated
    output passes design_lint) and regenerate the design/CSS files. Optionally
    reassigns section variants from the new recipe's preferred layouts."""

    definition = ToolDef(
        name="app_set_design",
        description=(
            "Swap the app's DesignSpec. Pass recipe_id (P0, always clean) OR a raw design_spec "
            "JSON (accepted only if the regenerated output passes design_lint). variant_policy "
            "'preserve' keeps section layouts; 'recipe' reassigns them from the new recipe's "
            "preferred variants. Re-saves .disco/designspec.json and regenerates the design "
            "files (styles.css, index.html fonts, manifest)."
        ),
        args_model=AppSetDesignArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
    )

    async def run(self, args: AppSetDesignArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            if (args.recipe_id is None) == (args.design_spec is None):
                raise _AppKitError(
                    "provide exactly one of recipe_id (P0) or design_spec (P1)."
                )
            app = await _load_app_spec(ctx)

            recipe = None
            if args.recipe_id is not None:
                recipe = get_recipe(args.recipe_id)
                if recipe is None:
                    raise _AppKitError(f"unknown recipe_id: {args.recipe_id!r}")
                design = recipe.to_design_spec()
            else:
                try:
                    design = DesignSpec.model_validate(args.design_spec)
                except Exception as exc:  # noqa: BLE001
                    raise _AppKitError(f"invalid design_spec: {exc}") from exc

            if args.variant_policy not in ("preserve", "recipe"):
                raise _AppKitError("variant_policy must be 'preserve' or 'recipe'.")

            app_changed = False
            if args.variant_policy == "recipe":
                if recipe is None:
                    raise _AppKitError("variant_policy='recipe' requires recipe_id.")
                prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
                data = app.model_dump(mode="json")
                for page in data["pages"]:
                    for sec in page["sections"]:
                        new_vid = prefs.get(sec["kind"])
                        if new_vid is not None and sec.get("variant_id") != new_vid:
                            sec["variant_id"] = new_vid
                            app_changed = True
                if app_changed:
                    app = AppSpec.model_validate(data)

            tree = generate(app, design)
            _lint_gate(tree, design)
            touched = await _apply_tree(ctx, tree)
            if not touched and not app_changed:
                # Same RC-M rule as app_update_content: an identical design applied
                # to an unchanged app regenerates an identical tree — refuse loudly
                # instead of reporting a hollow success the model will retry.
                raise _AppKitError(
                    "no-op: this design produced an identical generated tree — the "
                    "app already uses it. Pick a different recipe_id/design_spec or "
                    "change sections first."
                )
            await _save_design_spec(ctx, design)
            specs = [DESIGNSPEC_RELPATH]
            if app_changed:
                await _save_app_spec(ctx, app)
                specs.append(APPSPEC_RELPATH)
            label = f"recipe '{recipe.id}'" if recipe is not None else "a custom DesignSpec"
            return ToolOutcome(
                success=True,
                content=(
                    f"app_set_design: applied {label} — updated {len(touched)} file(s)."
                ),
                structured={"files_written": touched, "specs": specs},
                artifacts=sorted({*touched, *specs}),
            )
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_set_design_refused")


# ---- app_snapshot_version (EPIC H3) -------------------------------------------

# Where immutable version records land — under `.disco/`, so they ride the same
# snapshot/rehydrate as the specs but never collide with the GENERATED tree
# (which the generator owns; this dir is never emitted by `generate`).
_SNAPSHOTS_RELDIR = ".disco/app_snapshots"


async def _read_disk_text(ctx: ToolContext, relpath: str) -> str | None:
    """Read a generated file's CURRENT on-disk bytes for drift comparison. Absent /
    unreadable → None (treated as a missing file)."""
    assert ctx.sandbox is not None
    try:
        if not await ctx.sandbox.file_exists(relpath):
            return None
        return (await ctx.sandbox.read_file(relpath)).decode("utf-8")
    except Exception:  # noqa: BLE001 — unreadable / vanished → treat as missing
        return None


def _compute_drift(
    tree: dict[str, str], on_disk: dict[str, str | None]
) -> dict[str, Any]:
    """Compare the spec-regenerated tree against what's actually on disk.

    `modified` = a generated file whose on-disk bytes differ; `missing` = a
    generated file absent from disk. `clean` iff neither — i.e. the on-disk tree
    is exactly the projection of the current specs (no hand-edits / no staleness)."""
    modified: list[str] = []
    missing: list[str] = []
    for path in sorted(tree):
        actual = on_disk.get(path)
        if actual is None:
            missing.append(path)
        elif actual != tree[path]:
            modified.append(path)
    return {
        "clean": not modified and not missing,
        "modified": modified,
        "missing": missing,
    }


class AppSnapshotVersionArgs(BaseModel):
    label: str | None = Field(
        default=None,
        max_length=80,
        description="Optional human label recorded with the version (e.g. 'pre-launch').",
    )


class AppSnapshotVersionTool:
    """[CONTRACT] Version the current app: hash the two `.disco` specs + the tree
    they deterministically generate, record an immutable snapshot under
    `.disco/app_snapshots/`, and report whether the on-disk generated tree still
    matches its specs (drift). A read-mostly lifecycle probe — it writes ONLY the
    version record, never the app tree or the specs."""

    definition = ToolDef(
        name="app_snapshot_version",
        description=(
            "Record an immutable VERSION of the current AppKit app: a content digest of "
            ".disco/appspec.json + designspec.json, per-file hashes of the tree they "
            "regenerate, and a DRIFT report (generated files whose on-disk bytes differ "
            "from the specs, or are missing). Writes one .disco/app_snapshots/<id>.json "
            "record and changes nothing else. Use it to checkpoint a known-good build or "
            "to prove the generated tree is still in sync with its specs."
        ),
        args_model=AppSnapshotVersionArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
    )

    async def run(self, args: AppSnapshotVersionArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            app = await _load_app_spec(ctx)
            design = await _load_design_spec(ctx)

            tree = generate(app, design)
            digest = spec_digest(app, design)
            file_hashes = tree_file_hashes(tree)
            t_digest = tree_digest(tree)

            on_disk = {path: await _read_disk_text(ctx, path) for path in tree}
            drift = _compute_drift(tree, on_disk)

            created_at = datetime.now(UTC)
            # `sha256:<hex>` → the bare hex's first 12 chars for a short, filename-safe id.
            short = digest.split(":", 1)[-1][:12]
            base_id = f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{short}"
            # A snapshot is an IMMUTABLE record: the second-resolution timestamp means two
            # checkpoints of unchanged specs within the SAME second would compute the same
            # `base_id` and the second `write_file` would OVERWRITE the first. Probe for a
            # free path and disambiguate deterministically (`<id>`, `<id>-2`, `<id>-3`, …)
            # so every call writes a FRESH file and NEVER clobbers a prior snapshot.
            version_id = base_id
            relpath = f"{_SNAPSHOTS_RELDIR}/{version_id}.json"
            collision = 2
            while await ctx.sandbox.file_exists(relpath):
                version_id = f"{base_id}-{collision}"
                relpath = f"{_SNAPSHOTS_RELDIR}/{version_id}.json"
                collision += 1
            record: dict[str, Any] = {
                "version": version_id,
                "created_at": created_at.isoformat(),
                "label": args.label,
                "spec_digest": digest,
                "tree_digest": t_digest,
                "summary": summarize_specs(app, design),
                "files": file_hashes,
                "file_count": len(file_hashes),
                "drift": drift,
            }
            await ctx.sandbox.write_file(
                relpath,
                (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            )

            drift_note = (
                "in sync with the specs"
                if drift["clean"]
                else (
                    f"DRIFT: {len(drift['modified'])} modified, "
                    f"{len(drift['missing'])} missing vs the specs"
                )
            )
            return ToolOutcome(
                success=True,
                content=(
                    f"app_snapshot_version: recorded '{version_id}' "
                    f"({len(file_hashes)} files, {drift_note})."
                ),
                structured={
                    "version": version_id,
                    "path": relpath,
                    "spec_digest": digest,
                    "tree_digest": t_digest,
                    "drift": drift,
                },
                artifacts=[relpath],
            )
        except _AppKitError as exc:
            return ToolOutcome(
                success=False, content=str(exc), error="app_snapshot_version_refused"
            )


APPKIT_V2_TOOLS: tuple[type, ...] = (
    AppCreateTool,
    AppAddSectionTool,
    AppUpdateContentTool,
    AppSetDesignTool,
    AppSnapshotVersionTool,
)


__all__ = [
    "AppAddSectionArgs",
    "AppAddSectionTool",
    "AppCreateArgs",
    "AppCreateTool",
    "AppSetDesignArgs",
    "AppSetDesignTool",
    "AppSnapshotVersionArgs",
    "AppSnapshotVersionTool",
    "AppUpdateContentArgs",
    "AppUpdateContentTool",
    "APPKIT_V2_TOOLS",
]

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
* `app_update_content`  — patch the app's display identity and/or a Section's
                          bounded `content`, re-save, regenerate only the touched
                          files.
* `app_set_design`      — swap the DesignSpec (a recipe, or a raw spec only if the
                          regenerated output passes design_lint), re-save, and
                          regenerate the design/CSS files.
* `app_add_primitive`   — (WO-A1) validate a spec against a registered primitive's
                          declared `spec_schema`, fold it into the AppSpec via the
                          primitive's `apply_spec`, and regenerate. The AppSpec stays
                          the single source of truth — no per-addon file overlays.

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
    LOCAL_LIST_PRIMITIVE_ID,
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
from disco.core.appkit.recipes import (
    CHOICE_COMPONENT_STYLE,
    CHOICE_DENSITY,
    CHOICE_PALETTE_ACCENT,
    CHOICE_PALETTE_PRIMARY,
    CHOICE_PALETTE_SURFACE,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
    RECIPES,
)
from disco.core.appkit.spec import (
    AppSpec,
    DesignSpec,
    Justification,
    Palette,
    Section,
    SectionContent,
    Typography,
)
from disco.core.design import DesignDirection, to_brand_tokens
from disco.core.effects import EffectCapability
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SkipValidation,
    field_validator,
    model_validator,
)

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from .design_lint import lint_design, load_committed_direction
from .mutation_batch import (
    DeterministicBatchResult,
    PlannedFileMutation,
    commit_deterministic_file_batch,
)

_FS = frozenset({Capability.FILESYSTEM})
_PRIMITIVES_RELDIR = ".disco/primitives"


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
        return await ctx.sandbox.read_file(relpath)
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 — unreadable is never absence
        raise _AppKitError(f"cannot read {relpath}: {type(exc).__name__}: {exc}") from exc


async def _primitive_record_paths(ctx: ToolContext) -> set[str]:
    """List existing AppKit add-on provenance without treating IO failure as empty."""
    assert ctx.sandbox is not None
    try:
        entries = await ctx.sandbox.list_dir(_PRIMITIVES_RELDIR)
    except FileNotFoundError:
        return set()
    except Exception as exc:  # noqa: BLE001 - cleanup ownership must be known before overwrite
        raise _AppKitError(
            f"cannot inspect {_PRIMITIVES_RELDIR} before app creation: {type(exc).__name__}: {exc}"
        ) from exc

    records: set[str] = set()
    prefix = _PRIMITIVES_RELDIR + "/"
    for entry in entries:
        normalized = str(entry).replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.startswith(prefix):
            candidate = normalized
        elif "/" not in normalized:
            candidate = prefix + normalized
        else:
            continue
        if candidate.endswith(".json"):
            records.add(candidate)
    return records


async def _load_app_spec(ctx: ToolContext) -> AppSpec:
    data = await _read_spec_bytes(ctx, APPSPEC_RELPATH)
    if data is None:
        raise _AppKitError(f"no {APPSPEC_RELPATH} in the workspace — run app_create first.")
    try:
        return load_app_spec_from_bytes(data)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"{APPSPEC_RELPATH} is invalid: {exc}") from exc


async def _load_design_spec(ctx: ToolContext) -> DesignSpec:
    data = await _read_spec_bytes(ctx, DESIGNSPEC_RELPATH)
    if data is None:
        raise _AppKitError(f"no {DESIGNSPEC_RELPATH} in the workspace — run app_create first.")
    try:
        return load_design_spec_from_bytes(data)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"{DESIGNSPEC_RELPATH} is invalid: {exc}") from exc


def _align_recipe_design_to_direction(design: DesignSpec, direction: DesignDirection) -> DesignSpec:
    """Project a recipe's layout through the plan's committed visual direction.

    The recipe still owns its section/layout system. The immutable direction owns
    the fonts, palette, surface treatment, and density that final verification
    treats as ground truth. Rebuilding a validated ``DesignSpec`` here keeps every
    emitted value inside the injection-safe spec boundary and replaces stale recipe
    justifications with truthful direction-derived ones.
    """

    theme = to_brand_tokens(direction)
    replaced_choices = {
        CHOICE_TYPOGRAPHY_HEADING,
        CHOICE_TYPOGRAPHY_BODY,
        CHOICE_PALETTE_PRIMARY,
        CHOICE_PALETTE_SURFACE,
        CHOICE_PALETTE_ACCENT,
        CHOICE_COMPONENT_STYLE,
        CHOICE_DENSITY,
    }
    retained = tuple(
        justification
        for justification in design.justifications
        if justification.choice not in replaced_choices
    )
    committed = (
        Justification(
            choice=CHOICE_TYPOGRAPHY_HEADING,
            reason=(
                f"{direction.font_pairing.heading.family} is the committed "
                f"{direction.id} display family."
            ),
        ),
        Justification(
            choice=CHOICE_TYPOGRAPHY_BODY,
            reason=(
                f"{direction.font_pairing.body.family} is the committed "
                f"{direction.id} reading family."
            ),
        ),
        Justification(
            choice=CHOICE_PALETTE_PRIMARY,
            reason=f"{direction.palette_seed} is the committed {direction.id} palette seed.",
        ),
        Justification(
            choice=CHOICE_PALETTE_SURFACE,
            reason=(
                f"{theme.bg} is the derived {direction.id} surface paired with "
                f"the committed {theme.text} text role."
            ),
        ),
        Justification(
            choice=CHOICE_PALETTE_ACCENT,
            reason=(
                f"{direction.accents[0].hex} is the committed "
                f"{direction.id} {direction.accents[0].name} accent."
            ),
        ),
        Justification(
            choice=CHOICE_COMPONENT_STYLE,
            reason=(
                f"{direction.surface_treatment.treatment} is the committed "
                f"{direction.id} surface treatment."
            ),
        ),
        Justification(
            choice=CHOICE_DENSITY,
            reason=f"{direction.density} is the committed {direction.id} information density.",
        ),
    )
    return DesignSpec(
        schema_version=design.schema_version,
        typography=Typography(
            heading_font=direction.font_pairing.heading.family,
            body_font=direction.font_pairing.body.family,
        ),
        palette=Palette(
            primary=direction.palette_seed,
            surface=theme.bg,
            text=theme.text,
            accent=direction.accents[0].hex,
        ),
        layout_family=design.layout_family,
        component_style=direction.surface_treatment.treatment,
        density=direction.density,
        justifications=(*retained, *committed),
    )


async def _lint_gate(
    tree: dict[str, str],
    design: DesignSpec,
    ctx: ToolContext,
    *,
    direction: DesignDirection | None = None,
) -> None:
    """Reject any mutation final ``design_lint`` would reject.

    The same committed direction loader and pure lint engine used by the final
    verifier govern this pre-write gate, so a successful semantic mutation cannot
    strand a tree that fails unchanged at ``verify_appkit_app``.
    """

    committed_direction = (
        direction if direction is not None else await load_committed_direction(ctx)
    )
    verdict = lint_design(
        tree,
        design,
        spec_present=True,
        spec_valid=True,
        direction=committed_direction,
    )
    if not verdict["ok"]:
        rules = ", ".join(dict.fromkeys(f["rule_id"] for f in verdict["findings"]))
        raise _AppKitError(
            "refused: the regenerated app would contain design slop "
            f"({verdict['counts']['error']} error / {verdict['counts']['warning']} warning) "
            f"— {rules}. Use a recipe, or justify the off-defaults in the DesignSpec."
        )


def _appkit_plan(
    tree: dict[str, str],
    *,
    app: AppSpec | None = None,
    design: DesignSpec | None = None,
    extra_writes: dict[str, bytes] | None = None,
    deletes: set[str] | None = None,
) -> list[PlannedFileMutation]:
    """Materialize every generated/spec/provenance byte before the first write."""
    try:
        intents = [
            PlannedFileMutation(path=path, after=content.encode("utf-8"))
            for path, content in sorted(tree.items())
        ]
        if extra_writes:
            intents.extend(
                PlannedFileMutation(path=path, after=content)
                for path, content in sorted(extra_writes.items())
            )
        if design is not None:
            intents.append(
                PlannedFileMutation(
                    path=DESIGNSPEC_RELPATH,
                    after=serialize_design_spec(design).encode("utf-8"),
                )
            )
        if app is not None:
            intents.append(
                PlannedFileMutation(
                    path=APPSPEC_RELPATH,
                    after=serialize_app_spec(app).encode("utf-8"),
                )
            )
        intents.extend(
            PlannedFileMutation(path=path, after=None) for path in sorted(deletes or set())
        )
        return intents
    except Exception as exc:  # noqa: BLE001 — serialization is a pre-write gate
        raise _AppKitError(
            f"refused before writing because the complete AppKit output could not be "
            f"serialized: {type(exc).__name__}: {exc}"
        ) from exc


def _changed_generated(
    result: DeterministicBatchResult,
    tree: dict[str, str],
) -> list[str]:
    generated = set(tree)
    return [path for path in result.changed_paths if path in generated]


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
            f"variant {section.variant_id!r} is a {variant.kind!r} layout, not {section.kind!r}"
        )


# ---- app_create ---------------------------------------------------------------


def _unknown_primitive_msg(prim_id: str) -> str:
    known = ", ".join(sorted(primitive_ids()))
    return f"unknown primitive_id: {prim_id!r}. Known primitives: {known}."


class AppCreateArgs(BaseModel):
    recipe_id: str = Field(
        # Catalog-in-schema (same doctrine as scaffold_starter, proven live 2026-07-10):
        # the model must see the FULL valid vocabulary before its first call — the
        # appkit-lane trace showed a hallucinated id costing a whole round-trip.
        description=(
            "The SiteRecipe id to derive the DesignSpec from. Valid ids (complete "
            "catalog): " + ", ".join(f"'{r.id}'" for r in RECIPES) + "."
        )
    )
    primitive_id: str = Field(
        default=LEAD_GEN_PRIMITIVE_ID,
        description="The AppKit primitive to scaffold: 'lead_gen' (a lead-capture app) or "
        "'directory' (a static, searchable directory site) or 'records' (related "
        "entities with CRUD list/insert routes) or 'local_list' (a browser-local, "
        "persistent add/list/delete app with no server data plane). Ignored when an "
        "explicit app_spec is given (the primitive is taken from app_spec.app_kind).",
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
        description="Optional explicit AppSpec (JSON). PREFER OMITTING THIS: the "
        "derived default spec is valid BY CONSTRUCTION — scaffold with just "
        "recipe_id + primitive_id + brief, then shape it with app_add_section / "
        "app_update_content / app_add_primitive (each validates one small change "
        "with an actionable error). A hand-written full spec must satisfy ~40 "
        "validation rules and is the slow path. If you DO pass one — RULES "
        "(violations are refused): app_kind must be one of 'lead_gen' | "
        "'directory' | 'records' | 'local_list' (use local_list for a "
        "browser-local persistent add/list/delete app with no server data plane; "
        "otherwise model your app onto the closest kind); entity field names must "
        "be snake_case identifiers and must "
        "NOT use reserved names like 'id' or 'created_at' (implicit columns); pages "
        "carry sections (each with id/kind/content) — a page has NO direct 'content' "
        "key of its own; a 'records' spec needs at least one NON-form entity.",
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
            "Scaffold an AppKit Cloudflare app from a design RECIPE. Provide "
            "`recipe_id` (required), optionally `primitive_id`, and optionally an "
            "explicit `app_spec` JSON or a `brief` to name a sensible default. "
            "Writes the generated tree plus .disco/appspec.json + "
            ".disco/designspec.json. The output is design_lint-clean by "
            "construction. Refuses to overwrite an existing app unless `overwrite` "
            "is true."
        ),
        args_model=AppCreateArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: AppCreateArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            recipe = get_recipe(args.recipe_id)
            if recipe is None:
                raise _AppKitError(
                    f"unknown recipe_id: {args.recipe_id!r}. See the SiteRecipe catalog."
                )
            direction = await load_committed_direction(ctx)
            design = recipe.to_design_spec()
            if direction is not None:
                design = _align_recipe_design_to_direction(design, direction)

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
            try:
                app = primitive.prepare_app_spec(app)
            except (TypeError, ValueError) as exc:
                raise _AppKitError(f"invalid {primitive.id!r} app_spec: {exc}") from exc

            existing = await _read_spec_bytes(ctx, APPSPEC_RELPATH)
            prior_tree: dict[str, str] = {}
            if existing is not None:
                if not args.overwrite:
                    raise _AppKitError(
                        f"{APPSPEC_RELPATH} already exists — pass overwrite=true to "
                        "replace the app."
                    )
                try:
                    prior_app = load_app_spec_from_bytes(existing)
                    prior_design = await _load_design_spec(ctx)
                    prior_tree = generate(prior_app, prior_design)
                except _AppKitError:
                    raise
                except Exception as exc:  # noqa: BLE001 - prove the prior owned path set
                    raise _AppKitError(
                        "refused: cannot safely overwrite because the existing AppKit tree "
                        f"cannot be reconstructed for stale-file cleanup: {exc}. Use a fresh "
                        "workspace if the prior app is intentionally being discarded."
                    ) from exc

            try:
                tree = generate(app, design)
            except Exception as exc:  # noqa: BLE001 - typed tool boundary
                raise _AppKitError(f"generating the {primitive.id!r} app failed: {exc}") from exc
            await _lint_gate(tree, design, ctx, direction=direction)
            stale_generated = set(prior_tree) - set(tree)
            stale_records = await _primitive_record_paths(ctx)
            committed = await commit_deterministic_file_batch(
                ctx,
                _appkit_plan(
                    tree,
                    app=app,
                    design=design,
                    deletes=stale_generated | stale_records,
                ),
                commit_last=(DESIGNSPEC_RELPATH, APPSPEC_RELPATH),
            )
            if isinstance(committed, ToolOutcome):
                return committed
            if not committed.changed_paths:
                raise _AppKitError(
                    "no-op: the workspace already contains this exact generated app and specs; "
                    "the requested scaffold is COMPLETE. Move on to verify/finish instead of "
                    "rewriting identical bytes."
                )
            touched = _changed_generated(committed, tree)
            deleted_paths = stale_generated | stale_records
            deleted = [path for path in committed.changed_paths if path in deleted_paths]
            artifacts = [path for path in committed.changed_paths if path not in deleted_paths]
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
                    "direction_id": direction.id if direction is not None else None,
                    "files_written": touched,
                    "files_deleted": deleted,
                    "specs": [
                        path
                        for path in (APPSPEC_RELPATH, DESIGNSPEC_RELPATH)
                        if path in committed.changed_paths
                    ],
                },
                artifacts=artifacts,
                effect_receipts=committed.receipts,
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
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
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
            duplicate_index = next(
                (index for index, existing in enumerate(secs) if existing["id"] == section.id),
                None,
            )
            if duplicate_index is not None:
                if args.after_section_id is None:
                    placement_matches = duplicate_index == len(secs) - 1
                else:
                    anchor_index = next(
                        (
                            index
                            for index, existing in enumerate(secs)
                            if existing["id"] == args.after_section_id
                        ),
                        None,
                    )
                    placement_matches = (
                        anchor_index is not None and duplicate_index == anchor_index + 1
                    )
                if secs[duplicate_index] == sec_json and placement_matches:
                    raise _AppKitError(
                        f"no-op: section {section.id!r} already exists with exactly this "
                        "content and placement. The requested add is COMPLETE; move on to "
                        "verify/finish instead of retrying it."
                    )
                raise _AppKitError(
                    f"duplicate section id {section.id!r}: an existing section with that id "
                    "has different content or placement. Choose a new id or update the "
                    "existing section."
                )
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
            await _lint_gate(tree, design, ctx)
            committed = await commit_deterministic_file_batch(
                ctx,
                _appkit_plan(tree, app=new_app),
                commit_last=(APPSPEC_RELPATH,),
            )
            if isinstance(committed, ToolOutcome):
                return committed
            touched = _changed_generated(committed, tree)
            return ToolOutcome(
                success=True,
                content=(
                    f"app_add_section: added '{section.id}' ({section.kind}) to "
                    f"page '{args.page_id}' — updated {len(touched)} file(s)."
                ),
                structured={"files_written": touched, "spec": APPSPEC_RELPATH},
                artifacts=list(committed.changed_paths),
                effect_receipts=committed.receipts,
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
    success_message: str | None = None

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


def carry_identity_into_display_slots(
    data: dict[str, Any], previous_name: str, new_name: str
) -> list[str]:
    """Move display copies of the app identity along with a rename.

    Every seeded spec builder copies the app name into section headings at
    scaffold time, so renaming ONLY `AppSpec.name` left the app called one thing
    and DISPLAYING another: the title changed while the on-screen heading still
    read the old name — and the tool reported success (counted seed 440074,
    p4_appkit_semantic_edit). All five seeded builders do this, so it was
    guaranteed for every AppKit rename, not a quirk of one recipe.

    Deliberately EXACT-match only: a slot still holding the previous name verbatim
    is a copy of the identity, while any edited slot is authored content and is
    left alone. Section copy stays owned by the section-update path — this closes
    the second copy of the identity, it does not make a rename rewrite prose.

    Returns the `page.section.slot` paths it moved, so the caller can say what it
    did: a silent content rewrite is worse than a named one.
    """

    moved: list[str] = []
    for page in data.get("pages") or []:
        for section in page.get("sections") or []:
            content = section.get("content")
            if not isinstance(content, dict):
                continue
            for slot, value in list(content.items()):
                if isinstance(value, str) and value == previous_name:
                    content[slot] = new_name
                    moved.append(f"{page.get('id')}.{section.get('id')}.{slot}")
    return moved


class AppUpdateContentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Optional authoritative application display name. This updates AppSpec.name; "
            "it is application identity, not section copy."
        ),
    )
    page_id: str | None = Field(
        default=None,
        description="The page that holds the section. Required with section updates.",
    )
    section_id: str | None = Field(
        default=None,
        description="The section whose content to patch. Required with section updates.",
    )
    updates: ContentUpdate | None = Field(
        default=None,
        description="Optional content slots to set/merge: heading, subheading, body, "
        "cta_label, items (a list of strings), success_message. Unknown keys are "
        "rejected. page_id and section_id are required when this is present.",
    )

    @model_validator(mode="after")
    def _complete_section_selector(self) -> AppUpdateContentArgs:
        section_fields = (self.page_id, self.section_id, self.updates)
        if any(value is not None for value in section_fields) and any(
            value is None for value in section_fields
        ):
            raise ValueError(
                "page_id, section_id, and updates must be provided together for a "
                "section content update"
            )
        if self.app_name is None and self.updates is None:
            raise ValueError("set app_name and/or provide a complete section content update")
        return self


class AppUpdateContentTool:
    """[CONTRACT] Patch application identity and/or bounded section content in the
    AppSpec and regenerate only the touched files. Section updates are merged onto
    the existing content and all changes are re-validated before regeneration."""

    definition = ToolDef(
        name="app_update_content",
        description=(
            "Update the current app's authoritative display identity (`app_name`) and/or "
            "patch one section's content (heading/subheading/body/cta_label/items/"
            "success_message). Section updates require page_id + section_id + updates. "
            "Every change is validated in AppSpec, then .disco/appspec.json is re-saved "
            "and only touched generated files are regenerated. Identity and content live "
            "in the spec, never in hand-edited generated files."
        ),
        args_model=AppUpdateContentArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: AppUpdateContentArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            app = await _load_app_spec(ctx)
            design = await _load_design_spec(ctx)

            data = app.model_dump(mode="json")
            identity_changed = args.app_name is not None and args.app_name != app.name
            renamed_slots: list[str] = []
            if args.app_name is not None:
                data["name"] = args.app_name
            if identity_changed:
                assert args.app_name is not None
                renamed_slots = carry_identity_into_display_slots(data, app.name, args.app_name)

            section_changed = False
            existing: dict[str, Any] | None = None
            if args.updates is not None:
                assert args.page_id is not None
                assert args.section_id is not None
                page = next((p for p in data["pages"] if p["id"] == args.page_id), None)
                if page is None:
                    raise _AppKitError(f"no page with id {args.page_id!r}")
                sec = next(
                    (s for s in page["sections"] if s["id"] == args.section_id),
                    None,
                )
                if sec is None:
                    raise _AppKitError(f"no section {args.section_id!r} on page {args.page_id!r}")
                updates = args.updates.model_dump(mode="json", exclude_unset=True)
                if not updates and not identity_changed:
                    raise _AppKitError(
                        "no updates provided — set app_name and/or at least one of "
                        "heading/subheading/body/cta_label/items/success_message"
                    )
                raw_existing = sec.get("content")
                existing = (
                    {str(key): value for key, value in raw_existing.items()}
                    if isinstance(raw_existing, dict)
                    else {}
                )
                merged = {**existing, **updates}
                section_changed = merged != existing
                if section_changed:
                    try:
                        # Validate bounded content in isolation for a precise error.
                        _ = SectionContent.model_validate(merged)
                    except Exception as exc:  # noqa: BLE001
                        raise _AppKitError(f"invalid content update: {exc}") from exc
                    sec["content"] = merged

            if not identity_changed and not section_changed:
                # RC-M convention (live-caught 2026-07-03): a semantic no-op MUST
                # refuse with ground truth, not report success with "0 file(s)".
                import json as _json

                current = (
                    f" Current content: {_json.dumps(existing, ensure_ascii=False)[:600]}."
                    if existing is not None
                    else ""
                )
                raise _AppKitError(
                    "no-op: AppSpec already has exactly the requested values — nothing "
                    f"changed. Current app_name: {app.name!r}.{current} Send a DIFFERENT "
                    "app_name or section value, target another section, or move on to "
                    "verify/finish if this is already the intended state."
                )
            try:
                new_app = AppSpec.model_validate(data)
            except Exception as exc:  # noqa: BLE001
                raise _AppKitError(f"the semantic update made the spec invalid: {exc}") from exc

            tree = generate(new_app, design)
            await _lint_gate(tree, design, ctx)
            committed = await commit_deterministic_file_batch(
                ctx,
                _appkit_plan(tree, app=new_app),
                commit_last=(APPSPEC_RELPATH,),
            )
            if isinstance(committed, ToolOutcome):
                return committed
            touched = _changed_generated(committed, tree)
            changes = []
            if identity_changed:
                changes.append(f"application name to {new_app.name!r}")
                if renamed_slots:
                    # Say what else moved: a silent content rewrite is worse than a
                    # named one, even when it is the coherent thing to do.
                    changes.append(
                        f"{len(renamed_slots)} display slot(s) that still carried the "
                        f"previous name ({', '.join(renamed_slots[:4])}"
                        f"{'…' if len(renamed_slots) > 4 else ''})"
                    )
            if section_changed:
                changes.append(f"section {args.section_id!r} on page {args.page_id!r}")
            return ToolOutcome(
                success=True,
                content=(
                    f"app_update_content: updated {' and '.join(changes)} — "
                    f"updated {len(touched)} file(s)."
                ),
                structured={
                    "files_written": touched,
                    "spec": APPSPEC_RELPATH,
                    "application_name": new_app.name,
                },
                artifacts=list(committed.changed_paths),
                effect_receipts=committed.receipts,
            )
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_update_content_refused")


# ---- app_set_design -----------------------------------------------------------


class AppSetDesignArgs(BaseModel):
    recipe_id: str | None = Field(
        default=None,
        description="Swap to this SiteRecipe's DesignSpec (the P0 path). "
        "Mutually exclusive with design_spec.",
    )
    design_spec: SkipValidation[DesignSpec] | None = Field(
        default=None,
        description="A raw DesignSpec JSON (P1). Each typography field is ONE primary font "
        "family (letters/digits/spaces), never a comma-separated CSS fallback stack. "
        "Accepted only if the regenerated output passes design_lint (no unjustified slop).",
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
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: AppSetDesignArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            if (args.recipe_id is None) == (args.design_spec is None):
                raise _AppKitError("provide exactly one of recipe_id (P0) or design_spec (P1).")
            app = await _load_app_spec(ctx)
            prior_design = await _load_design_spec(ctx)

            recipe = None
            direction = await load_committed_direction(ctx)
            if args.recipe_id is not None:
                recipe = get_recipe(args.recipe_id)
                if recipe is None:
                    raise _AppKitError(f"unknown recipe_id: {args.recipe_id!r}")
                design = recipe.to_design_spec()
                if direction is not None:
                    design = _align_recipe_design_to_direction(design, direction)
            else:
                try:
                    design = DesignSpec.model_validate(args.design_spec)
                except Exception as exc:  # noqa: BLE001
                    raise _AppKitError(f"invalid design_spec: {exc}") from exc

            if args.variant_policy not in ("preserve", "recipe"):
                raise _AppKitError("variant_policy must be 'preserve' or 'recipe'.")

            app_changed = False
            data: dict[str, Any] | None = None
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
                assert data is not None
                app = AppSpec.model_validate(data)

            if not app_changed and design == prior_design:
                raise _AppKitError(
                    "no-op: this exact design is already persisted; the requested design "
                    "change is COMPLETE. Move on to verify/finish or choose a different design."
                )

            tree = generate(app, design)
            await _lint_gate(tree, design, ctx, direction=direction)
            committed = await commit_deterministic_file_batch(
                ctx,
                _appkit_plan(tree, app=app if app_changed else None, design=design),
                commit_last=(
                    (DESIGNSPEC_RELPATH, APPSPEC_RELPATH) if app_changed else (DESIGNSPEC_RELPATH,)
                ),
            )
            if isinstance(committed, ToolOutcome):
                return committed
            if not committed.changed_paths:
                raise _AppKitError(
                    "no-op: this design produced no byte change; move on to verify/finish."
                )
            touched = _changed_generated(committed, tree)
            specs = [
                path
                for path in (DESIGNSPEC_RELPATH, APPSPEC_RELPATH)
                if path in committed.changed_paths
            ]
            label = f"recipe '{recipe.id}'" if recipe is not None else "a custom DesignSpec"
            return ToolOutcome(
                success=True,
                content=(f"app_set_design: applied {label} — updated {len(touched)} file(s)."),
                structured={"files_written": touched, "specs": specs},
                artifacts=list(committed.changed_paths),
                effect_receipts=committed.receipts,
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


def _compute_drift(tree: dict[str, str], on_disk: dict[str, str | None]) -> dict[str, Any]:
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
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
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
            committed = await commit_deterministic_file_batch(
                ctx,
                [
                    PlannedFileMutation(
                        path=relpath,
                        after=(json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode(
                            "utf-8"
                        ),
                    )
                ],
            )
            if isinstance(committed, ToolOutcome):
                return committed

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
                artifacts=list(committed.changed_paths),
                effect_receipts=committed.receipts,
            )
        except _AppKitError as exc:
            return ToolOutcome(
                success=False, content=str(exc), error="app_snapshot_version_refused"
            )


# ---- app_add_primitive (WO-A1) --------------------------------------------------


def _addable_primitive_ids() -> list[str]:
    """Primitives with BOTH a spec_schema and an apply_spec — the ones
    app_add_primitive accepts. Base scaffolds (lead_gen, directory, …) are not."""
    addable: list[str] = []
    for pid in sorted(primitive_ids()):
        prim = get_primitive(pid)
        if prim is not None and prim.spec_schema is not None and prim.apply_spec is not None:
            addable.append(pid)
    return addable


class AppAddPrimitiveArgs(BaseModel):
    primitive_id: str = Field(
        description="The registered ADDABLE primitive to apply (one with a declared "
        "spec_schema). Base scaffold primitives are created with app_create instead."
    )
    spec: dict[str, Any] = Field(
        description="The primitive's declarative spec (JSON), validated against the "
        "primitive's spec_schema — a validation refusal names the offending fields "
        "and carries the expected schema."
    )


class AppAddPrimitiveTool:
    """[CONTRACT] Add a registered primitive to the CURRENT app: validate the given
    spec against the primitive's declared `spec_schema`, fold it into the AppSpec via
    the primitive's `apply_spec`, and regenerate the whole tree through the app's own
    base primitive (write-on-diff). The validated spec is persisted under
    `.disco/primitives/<id>.json` for provenance. For a `template_only` primitive the
    model's role is spec-only: the generated output is Disco-owned."""

    definition = ToolDef(
        name="app_add_primitive",
        description=(
            "Add a primitive to the current app (validated patch): the `spec` JSON is "
            "validated against the primitive's declared spec_schema, folded into the "
            "app spec, and the tree is regenerated (write-on-diff). Requires an "
            "existing app (run app_create first). Only spec-addable primitives are "
            "accepted — base scaffolds (lead_gen/directory/records) are created via "
            "app_create, not added. Re-saves .disco/appspec.json and records the "
            "applied spec under .disco/primitives/."
        ),
        args_model=AppAddPrimitiveArgs,
        needs=_FS,
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        behavior=declares(EffectCapability.WORKSPACE_MUTATE, planner_safe=False),
    )

    async def run(self, args: AppAddPrimitiveArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            prim = get_primitive(args.primitive_id)
            if prim is None:
                raise _AppKitError(_unknown_primitive_msg(args.primitive_id))
            if prim.spec_schema is None or prim.apply_spec is None:
                addable = ", ".join(_addable_primitive_ids()) or "(none registered yet)"
                raise _AppKitError(
                    f"primitive {prim.id!r} is a base scaffold, not spec-addable — "
                    f"create it with app_create (primitive_id={prim.id!r}) instead. "
                    f"Addable primitives: {addable}."
                )

            app = await _load_app_spec(ctx)
            host = resolve_primitive(app.app_kind)
            # local_list's reviewed contract is the complete browser-local base.
            # No registered add-on currently has a provenance verifier + lowering
            # contract for that host, even when its generic apply_spec happens to
            # produce a structurally valid AppSpec. Refuse before spec validation
            # (and before any design/tree/provenance work) instead of silently
            # changing list semantics or accepting a partially lowered feature.
            if host.id == LOCAL_LIST_PRIMITIVE_ID:
                raise _AppKitError(
                    f"the {LOCAL_LIST_PRIMITIVE_ID!r} host does not support add-on "
                    f"primitive {prim.id!r}; create a supported base primitive for "
                    "that feature instead"
                )

            design = await _load_design_spec(ctx)

            try:
                validated = prim.spec_schema.model_validate(args.spec)
            except Exception as exc:  # noqa: BLE001
                # Self-recovering refusal (RC-M): carry the expected schema so the
                # model can correct the spec without a second discovery step.
                schema_json = json.dumps(prim.spec_schema.model_json_schema())[:600]
                raise _AppKitError(
                    f"invalid {prim.id!r} spec: {exc}. Expected schema: {schema_json}"
                ) from exc

            # F3.3 is intentionally repeat-addable (one spec per endpoint). Its
            # provenance must cover EVERY endpoint, not just the latest write;
            # otherwise direct AppSpec injection could widen the trusted Worker
            # routes while leaving one unrelated provenance record behind.
            webhook_specs: list[dict[str, Any]] | None = None
            if prim.id == "webhook":
                record_relpath = f"{_PRIMITIVES_RELDIR}/{prim.id}.json"
                webhook_specs = []
                if await ctx.sandbox.file_exists(record_relpath):
                    try:
                        old_record = json.loads(
                            (await ctx.sandbox.read_file(record_relpath)).decode("utf-8")
                        )
                        if not isinstance(old_record, dict):
                            raise ValueError("record is not an object")
                        old_values = (
                            old_record.get("specs")
                            if "specs" in old_record
                            else [old_record.get("spec")]
                        )
                        if not isinstance(old_values, list) or not old_values:
                            raise ValueError("record has no specs")
                        webhook_specs = [
                            prim.spec_schema.model_validate(value).model_dump(mode="json")
                            for value in old_values
                        ]
                    except Exception as exc:  # noqa: BLE001 - fail closed before tree writes
                        raise _AppKitError(
                            "existing webhook provenance is invalid; refusing to widen "
                            "the security-sensitive endpoint set"
                        ) from exc
                validated_json = validated.model_dump(mode="json")
                if validated_json not in webhook_specs:
                    webhook_specs.append(validated_json)

            try:
                new_app = prim.apply_spec(app, validated)
            except Exception as exc:  # noqa: BLE001
                raise _AppKitError(f"applying the {prim.id!r} spec failed: {exc}") from exc

            # The add-on only owns its spec fold; the CURRENT base primitive owns
            # the complete output shape. Re-run that base's airtight preparation as
            # a host-compatibility boundary before any tree/spec/provenance write.
            # This catches folds that are valid AppSpec data but unsupported by the
            # selected host (for example Stripe metadata on browser-local local_list)
            # instead of letting generation crash or silently omit the feature.
            try:
                new_app = host.prepare_app_spec(new_app)
            except Exception as exc:  # noqa: BLE001 - typed fail-closed tool boundary
                raise _AppKitError(
                    f"primitive {prim.id!r} is incompatible with the {host.id!r} host: {exc}"
                ) from exc
            if new_app.model_dump(mode="json") == app.model_dump(mode="json"):
                # Same RC-M rule as the sibling tools: a no-op must refuse loudly,
                # never report a hollow success the model will retry into the breaker.
                raise _AppKitError(
                    f"no-op: the app already reflects this {prim.id!r} spec — nothing "
                    "changed. Send different values, or move on."
                )

            try:
                tree = generate(new_app, design)
            except Exception as exc:  # noqa: BLE001 - never leak a core crash to the agent
                raise _AppKitError(
                    f"generating the {host.id!r} host after applying primitive "
                    f"{prim.id!r} failed: {exc}"
                ) from exc
            await _lint_gate(tree, design, ctx)

            record_relpath = f"{_PRIMITIVES_RELDIR}/{prim.id}.json"
            record: dict[str, Any] = {
                "primitive_id": prim.id,
                "tier": prim.tier,
                "applied_at": datetime.now(UTC).isoformat(),
            }
            if webhook_specs is not None:
                record["specs"] = webhook_specs
            else:
                record["spec"] = validated.model_dump(mode="json")
            committed = await commit_deterministic_file_batch(
                ctx,
                _appkit_plan(
                    tree,
                    app=new_app,
                    extra_writes={
                        record_relpath: (
                            json.dumps(record, indent=2, ensure_ascii=False) + "\n"
                        ).encode("utf-8")
                    },
                ),
                # Provenance lands before AppSpec, the canonical retry marker. A
                # retry after a partial webhook record write de-duplicates its spec.
                commit_last=(record_relpath, APPSPEC_RELPATH),
            )
            if isinstance(committed, ToolOutcome):
                return committed
            touched = _changed_generated(committed, tree)

            tier_note = (
                " Its generated output is Disco-owned (template_only): contribute via "
                "this spec only — do not hand-edit those files."
                if prim.tier == "template_only"
                else ""
            )
            return ToolOutcome(
                success=True,
                content=(
                    f"app_add_primitive: applied '{prim.id}' to '{new_app.name}' — "
                    f"updated {len(touched)} file(s).{tier_note}"
                ),
                structured={
                    "primitive_id": prim.id,
                    "tier": prim.tier,
                    "files_written": touched,
                    "spec_record": record_relpath,
                    "spec": APPSPEC_RELPATH,
                },
                artifacts=list(committed.changed_paths),
                effect_receipts=committed.receipts,
            )
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_add_primitive_refused")


APPKIT_V2_TOOLS: tuple[type, ...] = (
    AppCreateTool,
    AppAddSectionTool,
    AppUpdateContentTool,
    AppSetDesignTool,
    AppSnapshotVersionTool,
    AppAddPrimitiveTool,
)


__all__ = [
    "AppAddPrimitiveArgs",
    "AppAddPrimitiveTool",
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

"""`app_create` — derive/accept a lead-gen AppSpec + a recipe DesignSpec, run the
pure generator, and WRITE the whole tree plus `.disco/appspec.json` +
`.disco/designspec.json`. Refuses to overwrite an existing app without
`overwrite`.

``AppCreateTool.run`` is decomposed into: design resolution (recipe + committed
direction alignment), app/primitive resolution (explicit app_spec XOR
brief-derived default), prior-tree resolution for a safe overwrite, tree
generation, and response assembly — each a narrowly-branching named step so the
orchestrator itself stays a short, readable sequence.
"""

from __future__ import annotations

from disco.core import SecurityRisk
from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    LEAD_GEN_PRIMITIVE_ID,
    PrimitiveDefinition,
    SiteRecipe,
    generate,
    get_primitive,
    get_recipe,
    load_app_spec_from_bytes,
    resolve_primitive,
    section_component_names,
)
from disco.core.appkit.recipes import RECIPES
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.design import DesignDirection
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field, SkipValidation

from ...anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ..design_lint import load_committed_direction
from ..mutation_batch import DeterministicBatchResult, commit_deterministic_file_batch
from .design_alignment import _align_recipe_design_to_direction
from .errors import _AppKitError
from .gates import _appkit_plan, _changed_generated, _lint_gate
from .primitive_catalog import _unknown_primitive_msg
from .spec_io import _load_design_spec, _primitive_record_paths, _read_spec_bytes

_FS = frozenset({Capability.FILESYSTEM})


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
        "key of its own; a 'records' spec needs at least one NON-form entity. "
        "For trusted records authorization, declare top-level roles and put a "
        "record_policy on each governed entity: public_read gives anonymous/guest "
        "read access; create_roles controls posting; owner_managed derives "
        "owner_user_id from the signed session; manage_roles may edit/delete any "
        "row; lock_roles enables server-enforced lock/unlock; parent_lock_field "
        "names a declared FK whose locked parent blocks child writes. "
        "role_admin_roles enables the built-in user-role administration route/UI. "
        "Do not declare a separate user entity or owner_user_id/locked fields—the "
        "trusted generator owns those.",
    )
    overwrite: bool = Field(
        default=False,
        description="Overwrite an existing app (.disco/appspec.json). Refused unless true.",
    )


async def _resolve_create_design(
    ctx: ToolContext, args: AppCreateArgs
) -> tuple[SiteRecipe, DesignSpec, DesignDirection | None]:
    recipe = get_recipe(args.recipe_id)
    if recipe is None:
        raise _AppKitError(f"unknown recipe_id: {args.recipe_id!r}. See the SiteRecipe catalog.")
    direction = await load_committed_direction(ctx)
    design = recipe.to_design_spec()
    if direction is not None:
        design = _align_recipe_design_to_direction(design, direction)
    return recipe, design, direction


def _resolve_create_app_and_primitive(
    args: AppCreateArgs, recipe: SiteRecipe
) -> tuple[AppSpec, PrimitiveDefinition]:
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
    return app, primitive


async def _resolve_create_overwrite(ctx: ToolContext, args: AppCreateArgs) -> dict[str, str]:
    """Load the prior generated tree for stale-file cleanup, refusing an
    unrequested overwrite. Returns {} when no app exists yet."""
    existing = await _read_spec_bytes(ctx, APPSPEC_RELPATH)
    if existing is None:
        return {}
    if not args.overwrite:
        raise _AppKitError(
            f"{APPSPEC_RELPATH} already exists — pass overwrite=true to replace the app."
        )
    try:
        prior_app = load_app_spec_from_bytes(existing)
        prior_design = await _load_design_spec(ctx)
        return generate(prior_app, prior_design)
    except _AppKitError:
        raise
    except Exception as exc:  # noqa: BLE001 - prove the prior owned path set
        raise _AppKitError(
            "refused: cannot safely overwrite because the existing AppKit tree "
            f"cannot be reconstructed for stale-file cleanup: {exc}. Use a fresh "
            "workspace if the prior app is intentionally being discarded."
        ) from exc


def _generate_create_tree(
    app: AppSpec, design: DesignSpec, primitive: PrimitiveDefinition
) -> dict[str, str]:
    try:
        return generate(app, design)
    except Exception as exc:  # noqa: BLE001 - typed tool boundary
        raise _AppKitError(f"generating the {primitive.id!r} app failed: {exc}") from exc


def _build_create_outcome(
    *,
    app: AppSpec,
    primitive: PrimitiveDefinition,
    recipe: SiteRecipe,
    direction: DesignDirection | None,
    tree: dict[str, str],
    committed: DeterministicBatchResult,
    stale_paths: set[str],
) -> ToolOutcome:
    touched = _changed_generated(committed, tree)
    deleted = [path for path in committed.changed_paths if path in stale_paths]
    artifacts = [path for path in committed.changed_paths if path not in stale_paths]
    generated_names = section_component_names(app)
    content_targets = [
        {
            "page_id": page.id,
            "section_id": section.id,
            "generated_key": generated_names[(page.id, section.id)],
        }
        for page in app.pages
        for section in page.sections
    ]
    target_summary = ", ".join(
        f"{target['page_id']}/{target['section_id']} (generated key {target['generated_key']})"
        for target in content_targets
    )
    return ToolOutcome(
        success=True,
        content=(
            f"app_create: scaffolded '{app.name}' ({primitive.id}) from recipe "
            f"'{recipe.id}' — {len(tree)} files + 2 specs. Content targets: "
            f"{target_summary}."
        ),
        structured={
            "recipe_id": recipe.id,
            "primitive_id": primitive.id,
            "app_kind": app.app_kind,
            "app_name": app.name,
            "direction_id": direction.id if direction is not None else None,
            "files_written": touched,
            "files_deleted": deleted,
            "content_targets": content_targets,
            "specs": [
                path
                for path in (APPSPEC_RELPATH, DESIGNSPEC_RELPATH)
                if path in committed.changed_paths
            ],
        },
        artifacts=artifacts,
        effect_receipts=committed.receipts,
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
            recipe, design, direction = await _resolve_create_design(ctx, args)
            app, primitive = _resolve_create_app_and_primitive(args, recipe)
            prior_tree = await _resolve_create_overwrite(ctx, args)
            tree = _generate_create_tree(app, design, primitive)
            await _lint_gate(tree, design, ctx, direction=direction)
            stale_paths = (set(prior_tree) - set(tree)) | await _primitive_record_paths(ctx)
            committed = await commit_deterministic_file_batch(
                ctx,
                _appkit_plan(tree, app=app, design=design, deletes=stale_paths),
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
            return _build_create_outcome(
                app=app,
                primitive=primitive,
                recipe=recipe,
                direction=direction,
                tree=tree,
                committed=committed,
                stale_paths=stale_paths,
            )
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_create_refused")

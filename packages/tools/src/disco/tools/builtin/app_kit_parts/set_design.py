"""`app_set_design` — swap the DesignSpec (a recipe, or a raw spec only if the
regenerated output passes design_lint), re-save, and regenerate the
design/CSS files.

``AppSetDesignTool.run`` is decomposed into: new-design resolution (recipe P0
or raw-spec P1, aligned to the committed direction), the optional
recipe-driven section-variant reassignment, the semantic no-op refusal, and
response assembly.
"""

from __future__ import annotations

from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import APPSPEC_RELPATH, DESIGNSPEC_RELPATH, generate, get_recipe
from disco.core.appkit.recipes import SiteRecipe
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
from .spec_io import _load_app_spec, _load_design_spec

_FS = frozenset({Capability.FILESYSTEM})


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


async def _resolve_new_design(
    ctx: ToolContext, args: AppSetDesignArgs
) -> tuple[SiteRecipe | None, DesignSpec, DesignDirection | None]:
    direction = await load_committed_direction(ctx)
    if args.recipe_id is not None:
        recipe = get_recipe(args.recipe_id)
        if recipe is None:
            raise _AppKitError(f"unknown recipe_id: {args.recipe_id!r}")
        design = recipe.to_design_spec()
        if direction is not None:
            design = _align_recipe_design_to_direction(design, direction)
        return recipe, design, direction
    try:
        design = DesignSpec.model_validate(args.design_spec)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"invalid design_spec: {exc}") from exc
    return None, design, direction


def _apply_variant_policy(
    app: AppSpec, args: AppSetDesignArgs, recipe: SiteRecipe | None
) -> tuple[AppSpec, bool]:
    if args.variant_policy not in ("preserve", "recipe"):
        raise _AppKitError("variant_policy must be 'preserve' or 'recipe'.")
    if args.variant_policy != "recipe":
        return app, False
    if recipe is None:
        raise _AppKitError("variant_policy='recipe' requires recipe_id.")
    prefs = {p.kind: p.variant_id for p in recipe.preferred_section_variants}
    data: dict[str, Any] = app.model_dump(mode="json")
    app_changed = False
    for page in data["pages"]:
        for sec in page["sections"]:
            new_vid = prefs.get(sec["kind"])
            if new_vid is not None and sec.get("variant_id") != new_vid:
                sec["variant_id"] = new_vid
                app_changed = True
    if not app_changed:
        return app, False
    return AppSpec.model_validate(data), True


def _refuse_design_noop(app_changed: bool, design: DesignSpec, prior_design: DesignSpec) -> None:
    if app_changed or design != prior_design:
        return
    raise _AppKitError(
        "no-op: this exact design is already persisted; the requested design "
        "change is COMPLETE. Move on to verify/finish or choose a different design."
    )


def _build_set_design_outcome(
    recipe: SiteRecipe | None, committed: DeterministicBatchResult, tree: dict[str, str]
) -> ToolOutcome:
    touched = _changed_generated(committed, tree)
    specs = [
        path for path in (DESIGNSPEC_RELPATH, APPSPEC_RELPATH) if path in committed.changed_paths
    ]
    label = f"recipe '{recipe.id}'" if recipe is not None else "a custom DesignSpec"
    return ToolOutcome(
        success=True,
        content=(f"app_set_design: applied {label} — updated {len(touched)} file(s)."),
        structured={"files_written": touched, "specs": specs},
        artifacts=list(committed.changed_paths),
        effect_receipts=committed.receipts,
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
            recipe, design, direction = await _resolve_new_design(ctx, args)
            app, app_changed = _apply_variant_policy(app, args, recipe)
            _refuse_design_noop(app_changed, design, prior_design)

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
            return _build_set_design_outcome(recipe, committed, tree)
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_set_design_refused")

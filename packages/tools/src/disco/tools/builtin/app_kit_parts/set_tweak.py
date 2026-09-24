"""Canonical AppKit owner edits use the same lint and commit path as the model."""

from typing import Any

from disco.core.appkit import APPSPEC_RELPATH, DESIGNSPEC_RELPATH, generate
from disco.core.appkit.owner_controls import apply_owner_tweak, owner_tweak_values
from disco.core.appkit.spec import AppSpec, DesignSpec

from ...anatomy import ToolContext, ToolOutcome
from ..mutation_batch import commit_deterministic_file_batch
from .errors import _AppKitError
from .gates import _appkit_plan, _lint_gate
from .spec_io import _load_app_spec, _load_design_spec


async def prepare_owner_tweaks(
    ctx: ToolContext,
    values: dict[str, Any],
) -> tuple[AppSpec, DesignSpec, dict[str, str]]:
    app, design = await _load_app_spec(ctx), await _load_design_spec(ctx)
    for key, value in sorted(values.items()):
        app, design = apply_owner_tweak(app, design, key, value)
    tree = generate(app, design)
    await _lint_gate(tree, design, ctx)
    return app, design, tree


async def commit_owner_tweaks(
    ctx: ToolContext,
    app: AppSpec,
    design: DesignSpec,
    tree: dict[str, str],
) -> ToolOutcome:
    result = await commit_deterministic_file_batch(
        ctx,
        _appkit_plan(tree, app=app, design=design),
        commit_last=(DESIGNSPEC_RELPATH, APPSPEC_RELPATH),
    )
    if isinstance(result, ToolOutcome):
        return result
    return ToolOutcome(
        success=True,
        content="Updated owner controls; regenerated the AppKit app.",
        structured={
            "values": owner_tweak_values(app, design),
            "files_written": list(result.changed_paths),
        },
        artifacts=list(result.changed_paths),
        effect_receipts=result.receipts,
    )


async def set_owner_tweak(ctx: ToolContext, key: str, value: Any) -> ToolOutcome:
    try:
        app, design, tree = await prepare_owner_tweaks(ctx, {key: value})
        return await commit_owner_tweaks(ctx, app, design, tree)
    except (ValueError, _AppKitError) as exc:
        return ToolOutcome(success=False, error="app_set_tweak_refused", content=str(exc))

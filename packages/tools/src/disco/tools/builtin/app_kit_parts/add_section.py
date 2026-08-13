"""`app_add_section` — insert a validated Section (unique id, a real catalog
variant of its kind) into a page, re-save the spec, and regenerate only the
affected files.

``AppAddSectionTool.run`` is decomposed into: section validation, duplicate
detection (a no-op re-add refuses loudly, a genuine id collision refuses with
guidance), placement, and response assembly.
"""

from __future__ import annotations

from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import APPSPEC_RELPATH, generate
from disco.core.appkit.spec import AppSpec, Section
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field, SkipValidation

from ...anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ..mutation_batch import DeterministicBatchResult, commit_deterministic_file_batch
from .errors import _AppKitError
from .gates import _appkit_plan, _changed_generated, _lint_gate, _validate_variant
from .spec_io import _load_app_spec, _load_design_spec
from .weak_fc import _section_validation_input

_FS = frozenset({Capability.FILESYSTEM})


class AppAddSectionArgs(BaseModel):
    page_id: str = Field(description="The page to insert the section into.")
    section: SkipValidation[Section] = Field(
        description="The Section JSON to insert "
        "(id, kind, optional variant_id/content/content_ref). Generated content "
        "slot names ctaLabel and successMessage are accepted."
    )
    after_section_id: str | None = Field(
        default=None,
        description="Insert AFTER this existing section id (omit to append at the end).",
    )


def _validate_new_section(args: AppAddSectionArgs) -> Section:
    try:
        section = Section.model_validate(_section_validation_input(args.section))
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"invalid section: {exc}") from exc
    _validate_variant(section)
    return section


def _check_section_duplicate(
    secs: list[dict[str, Any]], args: AppAddSectionArgs, section: Section, sec_json: dict[str, Any]
) -> None:
    """Refuse a colliding section id — loudly as a no-op when it is an exact
    repeat of the same content and placement, otherwise as a real collision."""
    duplicate_index = next(
        (index for index, existing in enumerate(secs) if existing["id"] == section.id),
        None,
    )
    if duplicate_index is None:
        return
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
        placement_matches = anchor_index is not None and duplicate_index == anchor_index + 1
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


def _place_section(
    secs: list[dict[str, Any]], args: AppAddSectionArgs, sec_json: dict[str, Any]
) -> None:
    if args.after_section_id is None:
        secs.append(sec_json)
        return
    idx = next(
        (i for i, s in enumerate(secs) if s["id"] == args.after_section_id),
        None,
    )
    if idx is None:
        raise _AppKitError(f"no section {args.after_section_id!r} on page {args.page_id!r}")
    secs.insert(idx + 1, sec_json)


def _insert_section(app: AppSpec, args: AppAddSectionArgs, section: Section) -> AppSpec:
    data = app.model_dump(mode="json")
    page = next((p for p in data["pages"] if p["id"] == args.page_id), None)
    if page is None:
        raise _AppKitError(f"no page with id {args.page_id!r}")
    secs = list(page["sections"])
    sec_json = section.model_dump(mode="json")
    _check_section_duplicate(secs, args, section, sec_json)
    _place_section(secs, args, sec_json)
    page["sections"] = secs
    try:
        return AppSpec.model_validate(data)  # enforces unique ids + caps
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"adding the section made the spec invalid: {exc}") from exc


def _build_add_section_outcome(
    section: Section,
    args: AppAddSectionArgs,
    committed: DeterministicBatchResult,
    tree: dict[str, str],
) -> ToolOutcome:
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
            section = _validate_new_section(args)
            new_app = _insert_section(app, args, section)

            tree = generate(new_app, design)
            await _lint_gate(tree, design, ctx)
            committed = await commit_deterministic_file_batch(
                ctx,
                _appkit_plan(tree, app=new_app),
                commit_last=(APPSPEC_RELPATH,),
            )
            if isinstance(committed, ToolOutcome):
                return committed
            return _build_add_section_outcome(section, args, committed, tree)
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_add_section_refused")

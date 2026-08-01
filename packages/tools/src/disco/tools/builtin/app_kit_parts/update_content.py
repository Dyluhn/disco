"""`app_update_content` — patch the app's display identity and/or a Section's
bounded `content`, re-save, regenerate only the touched files.

``AppUpdateContentTool.run`` is decomposed into: identity patching (with the
display-slot rename carry-along), section content merging, the semantic no-op
refusal (RC-M convention — a no-op must refuse with ground truth, never report
a hollow success), and response assembly.
"""

from __future__ import annotations

import json
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import APPSPEC_RELPATH, generate
from disco.core.appkit.spec import AppSpec, SectionContent
from disco.core.effects import EffectCapability
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ..mutation_batch import DeterministicBatchResult, commit_deterministic_file_batch
from .errors import _AppKitError
from .gates import _appkit_plan, _changed_generated, _lint_gate
from .spec_io import _load_app_spec, _load_design_spec
from .weak_fc import _unwrap_weak_fc_items_wrapper

_FS = frozenset({Capability.FILESYSTEM})


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


def _apply_identity_update(
    data: dict[str, Any], app: AppSpec, args: AppUpdateContentArgs
) -> tuple[bool, list[str]]:
    """Patch the app name into `data` in place. Returns (identity_changed,
    renamed_slots)."""
    identity_changed = args.app_name is not None and args.app_name != app.name
    if args.app_name is not None:
        data["name"] = args.app_name
    if not identity_changed:
        return False, []
    assert args.app_name is not None
    renamed_slots = carry_identity_into_display_slots(data, app.name, args.app_name)
    return True, renamed_slots


def _find_section(data: dict[str, Any], page_id: str, section_id: str) -> dict[str, Any]:
    page = next((p for p in data["pages"] if p["id"] == page_id), None)
    if page is None:
        raise _AppKitError(f"no page with id {page_id!r}")
    sec = next((s for s in page["sections"] if s["id"] == section_id), None)
    if sec is None:
        raise _AppKitError(f"no section {section_id!r} on page {page_id!r}")
    return sec


def _apply_section_update(
    data: dict[str, Any], args: AppUpdateContentArgs, identity_changed: bool
) -> tuple[bool, dict[str, Any] | None]:
    """Merge a section content patch into `data` in place. Returns
    (section_changed, existing_content) — existing_content is None when no
    section update was requested (an identity-only call)."""
    if args.updates is None:
        return False, None
    assert args.page_id is not None
    assert args.section_id is not None
    sec = _find_section(data, args.page_id, args.section_id)
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
    if merged == existing:
        return False, existing
    try:
        # Validate bounded content in isolation for a precise error.
        _ = SectionContent.model_validate(merged)
    except Exception as exc:  # noqa: BLE001
        raise _AppKitError(f"invalid content update: {exc}") from exc
    sec["content"] = merged
    return True, existing


def _refuse_content_noop(
    app: AppSpec,
    identity_changed: bool,
    section_changed: bool,
    existing: dict[str, Any] | None,
) -> None:
    if identity_changed or section_changed:
        return
    # RC-M convention (live-caught 2026-07-03): a semantic no-op MUST
    # refuse with ground truth, not report success with "0 file(s)".
    current = (
        f" Current content: {json.dumps(existing, ensure_ascii=False)[:600]}."
        if existing is not None
        else ""
    )
    raise _AppKitError(
        "no-op: AppSpec already has exactly the requested values — nothing "
        f"changed. Current app_name: {app.name!r}.{current} Send a DIFFERENT "
        "app_name or section value, target another section, or move on to "
        "verify/finish if this is already the intended state."
    )


def _build_update_content_outcome(
    args: AppUpdateContentArgs,
    new_app: AppSpec,
    identity_changed: bool,
    renamed_slots: list[str],
    section_changed: bool,
    committed: DeterministicBatchResult,
    tree: dict[str, str],
) -> ToolOutcome:
    touched = _changed_generated(committed, tree)
    changes: list[str] = []
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
            identity_changed, renamed_slots = _apply_identity_update(data, app, args)
            section_changed, existing = _apply_section_update(data, args, identity_changed)
            _refuse_content_noop(app, identity_changed, section_changed, existing)
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
            return _build_update_content_outcome(
                args, new_app, identity_changed, renamed_slots, section_changed, committed, tree
            )
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_update_content_refused")

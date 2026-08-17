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

The cohesive implementation lives in the private ``app_kit_parts`` package: shared
spec IO / weak-FC normalization / design-alignment / mutation-gate helpers, and
one module per tool (``create``, ``add_section``, ``update_content``,
``set_design``, ``add_primitive``). This module is the state-free compatibility
facade: it re-imports every public name this module exposed before the split, so
every existing import path — including the test suite, which reaches
``carry_identity_into_display_slots`` and monkeypatches
``serialize_app_spec``/``datetime`` on this module object — keeps working
unchanged. `app_snapshot_version` (EPIC H3) is a read-mostly lifecycle probe with
no split-sensitive helpers of its own and stays defined here directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import (
    generate,
    spec_digest,
    summarize_specs,
    tree_digest,
    tree_file_hashes,
)

# Re-exported (not called directly here): `app_kit_parts.gates._appkit_plan` reads
# these back off THIS module object at call time — `serialize_app_spec` is what the
# test suite monkeypatches to prove a serialization failure happens before any
# write, and a name bound at the parts module's own import time would silently
# defeat that patch.
from disco.core.appkit import serialize_app_spec as serialize_app_spec
from disco.core.appkit import serialize_design_spec as serialize_design_spec
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ..behavior import declares
from .app_kit_parts.add_primitive import AppAddPrimitiveArgs, AppAddPrimitiveTool
from .app_kit_parts.add_section import AppAddSectionArgs, AppAddSectionTool
from .app_kit_parts.create import AppCreateArgs, AppCreateTool
from .app_kit_parts.drift import _compute_drift, _read_disk_text
from .app_kit_parts.errors import _AppKitError
from .app_kit_parts.set_design import AppSetDesignArgs, AppSetDesignTool
from .app_kit_parts.spec_io import _load_app_spec, _load_design_spec
from .app_kit_parts.update_content import (
    AppUpdateContentArgs,
    AppUpdateContentTool,
    carry_identity_into_display_slots,
)
from .mutation_batch import PlannedFileMutation, commit_deterministic_file_batch

_FS = frozenset({Capability.FILESYSTEM})

# ---- app_snapshot_version (EPIC H3) -------------------------------------------

# Where immutable version records land — under `.disco/`, so they ride the same
# snapshot/rehydrate as the specs but never collide with the GENERATED tree
# (which the generator owns; this dir is never emitted by `generate`).
_SNAPSHOTS_RELDIR = ".disco/app_snapshots"


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
    "carry_identity_into_display_slots",
]

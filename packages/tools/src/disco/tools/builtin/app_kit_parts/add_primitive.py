"""`app_add_primitive` (WO-A1) — validate a spec against a registered
primitive's declared `spec_schema`, fold it into the AppSpec via the
primitive's `apply_spec`, and regenerate.

``AppAddPrimitiveTool.run`` is decomposed into: addable-primitive resolution,
host-compatibility resolution, spec validation, webhook provenance
(repeat-addable, one spec per endpoint), applying the primitive to the app
(fold + host re-preparation + no-op refusal), tree generation, and response
assembly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from disco.core import SecurityRisk
from disco.core.appkit import (
    APPSPEC_RELPATH,
    LOCAL_LIST_PRIMITIVE_ID,
    PrimitiveDefinition,
    generate,
    get_primitive,
    resolve_primitive,
)
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.effects import EffectCapability
from pydantic import BaseModel, Field

from ...anatomy import Capability, ToolContext, ToolDef, ToolOutcome
from ...behavior import declares
from ..mutation_batch import DeterministicBatchResult, commit_deterministic_file_batch
from .errors import _AppKitError
from .gates import _appkit_plan, _changed_generated, _lint_gate
from .primitive_catalog import _addable_primitive_ids, _unknown_primitive_msg
from .spec_io import _PRIMITIVES_RELDIR, _load_app_spec, _load_design_spec

_FS = frozenset({Capability.FILESYSTEM})


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


def _resolve_addable_primitive(args: AppAddPrimitiveArgs) -> PrimitiveDefinition:
    prim = get_primitive(args.primitive_id)
    if prim is None:
        raise _AppKitError(_unknown_primitive_msg(args.primitive_id))
    if prim.spec_schema is not None and prim.apply_spec is not None:
        return prim
    addable = ", ".join(_addable_primitive_ids()) or "(none registered yet)"
    raise _AppKitError(
        f"primitive {prim.id!r} is a base scaffold, not spec-addable — "
        f"create it with app_create (primitive_id={prim.id!r}) instead. "
        f"Addable primitives: {addable}."
    )


def _resolve_addable_host(app: AppSpec, prim: PrimitiveDefinition) -> PrimitiveDefinition:
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
    return host


def _validate_primitive_spec(prim: PrimitiveDefinition, args: AppAddPrimitiveArgs) -> Any:
    assert prim.spec_schema is not None
    try:
        return prim.spec_schema.model_validate(args.spec)
    except Exception as exc:  # noqa: BLE001
        # Self-recovering refusal (RC-M): carry the expected schema so the
        # model can correct the spec without a second discovery step.
        schema_json = json.dumps(prim.spec_schema.model_json_schema())[:600]
        raise _AppKitError(
            f"invalid {prim.id!r} spec: {exc}. Expected schema: {schema_json}"
        ) from exc


async def _resolve_webhook_specs(
    ctx: ToolContext, prim: PrimitiveDefinition, validated: Any
) -> list[dict[str, Any]] | None:
    """F3.3 is intentionally repeat-addable (one spec per endpoint). Its
    provenance must cover EVERY endpoint, not just the latest write;
    otherwise direct AppSpec injection could widen the trusted Worker
    routes while leaving one unrelated provenance record behind."""
    if prim.id != "webhook":
        return None
    assert ctx.sandbox is not None
    assert prim.spec_schema is not None
    record_relpath = f"{_PRIMITIVES_RELDIR}/{prim.id}.json"
    webhook_specs: list[dict[str, Any]] = []
    if await ctx.sandbox.file_exists(record_relpath):
        try:
            old_record = json.loads(
                (await ctx.sandbox.read_file(record_relpath)).decode("utf-8")
            )
            if not isinstance(old_record, dict):
                raise ValueError("record is not an object")
            old_values = (
                old_record.get("specs") if "specs" in old_record else [old_record.get("spec")]
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
    return webhook_specs


def _apply_primitive_to_app(
    app: AppSpec, host: PrimitiveDefinition, prim: PrimitiveDefinition, validated: Any
) -> AppSpec:
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
    return new_app


def _generate_primitive_tree(
    new_app: AppSpec, design: DesignSpec, host: PrimitiveDefinition, prim: PrimitiveDefinition
) -> dict[str, str]:
    try:
        return generate(new_app, design)
    except Exception as exc:  # noqa: BLE001 - never leak a core crash to the agent
        raise _AppKitError(
            f"generating the {host.id!r} host after applying primitive "
            f"{prim.id!r} failed: {exc}"
        ) from exc


def _build_primitive_record(
    prim: PrimitiveDefinition, validated: Any, webhook_specs: list[dict[str, Any]] | None
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "primitive_id": prim.id,
        "tier": prim.tier,
        "applied_at": datetime.now(UTC).isoformat(),
    }
    if webhook_specs is not None:
        record["specs"] = webhook_specs
    else:
        record["spec"] = validated.model_dump(mode="json")
    return record


def _build_add_primitive_outcome(
    prim: PrimitiveDefinition,
    new_app: AppSpec,
    record_relpath: str,
    committed: DeterministicBatchResult,
    tree: dict[str, str],
) -> ToolOutcome:
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
            prim = _resolve_addable_primitive(args)
            app = await _load_app_spec(ctx)
            host = _resolve_addable_host(app, prim)
            design = await _load_design_spec(ctx)
            validated = _validate_primitive_spec(prim, args)
            webhook_specs = await _resolve_webhook_specs(ctx, prim, validated)
            new_app = _apply_primitive_to_app(app, host, prim, validated)
            tree = _generate_primitive_tree(new_app, design, host, prim)
            await _lint_gate(tree, design, ctx)

            record_relpath = f"{_PRIMITIVES_RELDIR}/{prim.id}.json"
            record = _build_primitive_record(prim, validated, webhook_specs)
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
            return _build_add_primitive_outcome(prim, new_app, record_relpath, committed, tree)
        except _AppKitError as exc:
            return ToolOutcome(success=False, content=str(exc), error="app_add_primitive_refused")

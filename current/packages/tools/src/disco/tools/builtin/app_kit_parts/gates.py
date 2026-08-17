"""AppKit mutation gates: the pre-write design_lint gate, the write plan
builder, the changed-generated-files filter, and section variant referential
integrity.

``_appkit_plan`` resolves ``serialize_app_spec`` / ``serialize_design_spec``
through the live ``disco.tools.builtin.app_kit`` module at call time (not a
top-level import bound here) — the test suite monkeypatches
``app_kit.serialize_app_spec`` to prove a serialization failure happens before
any write, and a name bound at THIS module's import time would silently defeat
that patch.
"""

from __future__ import annotations

from disco.core.appkit import APPSPEC_RELPATH, DESIGNSPEC_RELPATH, get_variant
from disco.core.appkit.spec import AppSpec, DesignSpec, Section
from disco.core.design import DesignDirection

from ...anatomy import ToolContext
from ..design_lint import lint_design, load_committed_direction
from ..mutation_batch import DeterministicBatchResult, PlannedFileMutation
from .errors import _AppKitError


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
    from disco.tools.builtin import app_kit as _app_kit_mod

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
                    after=_app_kit_mod.serialize_design_spec(design).encode("utf-8"),
                )
            )
        if app is not None:
            intents.append(
                PlannedFileMutation(
                    path=APPSPEC_RELPATH,
                    after=_app_kit_mod.serialize_app_spec(app).encode("utf-8"),
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

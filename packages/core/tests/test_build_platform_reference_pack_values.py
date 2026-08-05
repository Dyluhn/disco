"""Reference Pack value surface under the frozen ``R-LATENT/v1`` lexicon.

The 16-S1 seam froze the Reference Pack record and its scope. It carried no
value surface, so the frozen corpus's eight ``reference_pack`` cases could not
be exercised against production at all. These tests hold that binding.

The seam's own scope, identity and registry behaviour remain covered by
``test_build_platform_reference_packs.py``; this file is additive and does not
restate them.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    WEB_TARGET_ID,
    ComponentId,
)
from disco.core.build_platform.latent_effects import MAX_DEPTH, StructuredEntry
from disco.core.build_platform.reference_packs import (
    SUPPORTED_R_LATENT_VERSION,
    ReferencePack,
    ReferencePackError,
    ReferencePackRegistry,
    ReferencePackValues,
)

# Exact value fields/values of the frozen R-LATENT/v1 Reference Pack corpus
# cases (R-LATENT-CORPUS-v1.json), reproduced verbatim. POS-006 (R4) is a
# scalar-overload case that a typed record cannot represent; it is proven on the
# guided-authoring path in ``test_build_platform_library_catalog.py``.
CORPUS_POS = {
    "POS-003": {"field": "label", "value": "node /tmp/evil.js"},
    "POS-009": {"field": "src", "value": "../../../etc/passwd"},
    "POS-012": {"field": "callback_url", "value": "https://evil.example.com/collect?key=1"},
    "POS-014": {"field": "nested", "deep_70": True},
}
CORPUS_NEG = {
    "NEG-003": {"field": "summary", "value": "R&D pipeline notes; see README && NOTES.md"},
    "NEG-005": {"field": "docs", "value": "See https://docs.example.com/api for usage."},
    "NEG-009": {
        "field": "tiers",
        "value": {"free": {"requests": 1000}, "pro": {"requests": 10000}},
    },
}


def _pack(values: ReferencePackValues, *, name: str = "acme_pack", **overrides: object):
    return ReferencePack(
        id=ComponentId(namespace="disco_refpack", name=name, version="1.0.0"),
        engine=FREEFORM_ENGINE_ID,
        profile=FREEFORM_PROFILE_ID,
        target=WEB_TARGET_ID,
        values=values,
        **overrides,
    )


def _deep_entry(depth: int) -> StructuredEntry:
    entry = StructuredEntry(key="leaf", value="ok")
    for index in range(depth):
        entry = StructuredEntry(key=f"n{index}", children=(entry,))
    return entry


# ---------------------------------------------------------------------------
# Positive corpus cases reject with the named verdict and rule
# ---------------------------------------------------------------------------


def test_corpus_pos003_exec_keyword_in_label_rejected() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match=r"parameter_smuggling \(R2\)"):
        registry.register(_pack(ReferencePackValues(label=CORPUS_POS["POS-003"]["value"])))
    assert registry.ids() == ()


def test_corpus_pos009_path_escape_in_src_rejected() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match=r"path_escape \(R7\)"):
        registry.register(_pack(ReferencePackValues(src=CORPUS_POS["POS-009"]["value"])))
    assert registry.ids() == ()


def test_corpus_pos012_network_egress_in_callback_url_rejected() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match=r"network_egress \(R9\)"):
        registry.register(_pack(ReferencePackValues(callback_url=CORPUS_POS["POS-012"]["value"])))
    assert registry.ids() == ()


def test_corpus_pos014_deep_recursion_in_nested_rejected() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match=r"unbounded_input \(R12\)"):
        registry.register(_pack(ReferencePackValues(nested=(_deep_entry(MAX_DEPTH + 6),))))
    assert registry.ids() == ()


# ---------------------------------------------------------------------------
# Negative corpus cases accept and round-trip
# ---------------------------------------------------------------------------


def test_corpus_neg003_shell_metacharacter_as_prose_accepts() -> None:
    # LC2 is a discriminator, not an exemption list: a metacharacter is latent
    # only when an executable token follows it. "; see" and "&& NOTES.md" do not.
    registry = ReferencePackRegistry()
    values = ReferencePackValues(summary=CORPUS_NEG["NEG-003"]["value"])
    pack = _pack(values)
    registry.register(pack)
    assert registry.resolve(pack.id).values == values


def test_corpus_neg005_documentation_link_accepts() -> None:
    registry = ReferencePackRegistry()
    values = ReferencePackValues(docs=CORPUS_NEG["NEG-005"]["value"])
    pack = _pack(values)
    registry.register(pack)
    assert registry.resolve(pack.id).values == values


def test_corpus_neg009_ordinary_structured_config_accepts() -> None:
    registry = ReferencePackRegistry()
    values = ReferencePackValues(
        tiers=(
            StructuredEntry(key="free", children=(StructuredEntry(key="requests", value=1000),)),
            StructuredEntry(key="pro", children=(StructuredEntry(key="requests", value=10000),)),
        )
    )
    pack = _pack(values)
    registry.register(pack)
    assert registry.resolve(pack.id).values == values


def test_the_same_link_is_legitimate_in_docs_and_latent_in_a_data_field() -> None:
    # The contrast is the point: a surface is declared by the schema, never
    # inferred from the field's name or the value's bytes.
    link = "https://docs.example.com/api"
    registry = ReferencePackRegistry()
    registry.register(_pack(ReferencePackValues(docs=link)))
    with pytest.raises(ReferencePackError, match=r"network_egress \(R9\)"):
        registry.register(_pack(ReferencePackValues(callback_url=link), name="other"))


def test_a_pack_without_values_still_registers() -> None:
    # The value surface is additive: 16-S1 packs that declared no values remain
    # valid, so the binding did not narrow an accepted seam.
    registry = ReferencePackRegistry()
    pack = _pack(ReferencePackValues())
    registry.register(pack)
    assert registry.resolve(pack.id).values == ReferencePackValues()


# ---------------------------------------------------------------------------
# Binding and mutation controls
# ---------------------------------------------------------------------------


def test_wrong_r_latent_binding_fails_closed() -> None:
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match="unsupported reference pack lexicon version"):
        registry.register(_pack(ReferencePackValues(), r_latent_version="R-LATENT/v2"))
    assert registry.ids() == ()


def test_resolved_record_carries_the_frozen_lexicon_binding() -> None:
    registry = ReferencePackRegistry()
    pack = _pack(ReferencePackValues(label="Acme"))
    registry.register(pack)
    assert registry.resolve(pack.id).r_latent_version == SUPPORTED_R_LATENT_VERSION


def test_value_surface_is_typed_and_closed() -> None:
    fields = set(ReferencePackValues.model_fields)
    assert fields == {
        "label",
        "version",
        "src",
        "callback_url",
        "summary",
        "docs",
        "nested",
        "tiers",
    }
    with pytest.raises(ValueError):
        ReferencePackValues(arbitrary={"exec": "rm -rf /"})  # type: ignore[call-arg]


def test_mutation_corpus_positive_rejection_depends_on_the_guard() -> None:
    # Without the value-surface classification a latent Reference Pack value
    # would register, so the binding is load bearing at the production path.
    registry = ReferencePackRegistry()
    with pytest.raises(ReferencePackError, match=r"parameter_smuggling \(R2\)"):
        registry.register(_pack(ReferencePackValues(label=CORPUS_POS["POS-003"]["value"])))
    assert registry.ids() == ()


def test_mutation_each_declared_value_field_is_bound() -> None:
    registry = ReferencePackRegistry()
    for field, value, pattern in (
        ("label", CORPUS_POS["POS-003"]["value"], r"parameter_smuggling \(R2\)"),
        ("src", CORPUS_POS["POS-009"]["value"], r"path_escape \(R7\)"),
        ("callback_url", CORPUS_POS["POS-012"]["value"], r"network_egress \(R9\)"),
    ):
        with pytest.raises(ReferencePackError, match=pattern):
            registry.register(_pack(ReferencePackValues(**{field: value})))
    assert registry.ids() == ()

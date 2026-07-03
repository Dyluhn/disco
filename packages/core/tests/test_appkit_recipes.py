"""EPIC D1/D2 — SiteRecipe + section-variant catalog tests.

Covers: recipe schema strictness + immutability + unique ids, the non-default
font/palette discipline, `to_design_spec()` producing a VALID DesignSpec with
canonical justifications, referential integrity of `preferred_section_variants`,
and the catalog invariant (>= 2 variants per structural SectionKind, ids unique).
"""

from __future__ import annotations

import pytest
from disco.core.appkit import (
    CANONICAL_CHOICE_KEYS,
    COVERED_KINDS,
    RECIPES,
    SECTION_VARIANTS,
    DesignSpec,
    SectionVariantPreference,
    SiteRecipe,
    get_recipe,
    get_variant,
    recipe_ids,
    variant_ids,
    variants_for,
)
from disco.core.appkit.recipes import (
    CHOICE_PALETTE_PRIMARY,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
)
from pydantic import ValidationError

# --- section catalog (D2) ----------------------------------------------------


def test_every_structural_kind_has_at_least_two_variants():
    for kind in COVERED_KINDS:
        variants = variants_for(kind)
        assert len(variants) >= 2, f"{kind} has only {len(variants)} variant(s)"
        # each variant actually carries that kind
        assert all(v.kind == kind for v in variants)


def test_variant_ids_globally_unique():
    ids = [v.id for v in SECTION_VARIANTS]
    assert len(ids) == len(set(ids))
    assert variant_ids() == frozenset(ids)


def test_get_variant_roundtrip_and_miss():
    sample = SECTION_VARIANTS[0]
    assert get_variant(sample.id) is sample
    assert get_variant("nope.does-not-exist") is None


def test_section_variant_is_frozen():
    v = SECTION_VARIANTS[0]
    with pytest.raises(ValidationError):
        v.layout = "mutated"  # type: ignore[misc]


# --- recipe schema strictness + immutability (D1) ----------------------------


def test_recipe_ids_unique_and_count():
    assert len(RECIPES) == 5
    ids = [r.id for r in RECIPES]
    assert len(ids) == len(set(ids))
    assert recipe_ids() == frozenset(ids)
    for r in RECIPES:
        assert get_recipe(r.id) is r


def test_recipe_is_frozen_extra_forbidden():
    r = RECIPES[0]
    with pytest.raises(ValidationError):
        r.heading_font = "Inter"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SiteRecipe(
            id="x",
            name="X",
            summary="a summary long enough",
            heading_font="Fraunces",
            body_font="Newsreader",
            primary="#111111",
            surface="#ffffff",
            text="#000000",
            layout_family="editorial",
            component_style="flat",
            density="comfortable",
            bogus_field="nope",  # type: ignore[call-arg]
        )


def test_recipe_collections_are_tuples():
    r = RECIPES[0]
    assert isinstance(r.preferred_section_variants, tuple)


def test_recipe_rejects_generic_default_fonts():
    for bad in ("Inter", "Geist", "system-ui", "Arial", "Helvetica", "default"):
        with pytest.raises(ValidationError):
            SiteRecipe(
                id="x",
                name="X",
                summary="a summary long enough",
                heading_font=bad,
                body_font="Newsreader",
                primary="#111111",
                surface="#ffffff",
                text="#000000",
                layout_family="editorial",
                component_style="flat",
                density="comfortable",
            )


def test_recipe_rejects_bad_hex():
    with pytest.raises(ValidationError):
        SiteRecipe(
            id="x",
            name="X",
            summary="a summary long enough",
            heading_font="Fraunces",
            body_font="Newsreader",
            primary="not-a-color",
            surface="#ffffff",
            text="#000000",
            layout_family="editorial",
            component_style="flat",
            density="comfortable",
        )


def test_no_seeded_recipe_uses_ai_purple_or_generic_font():
    purples = {"#7c3aed", "#8b5cf6", "#6366f1", "#a855f7"}
    generics = {"inter", "geist", "system-ui", "arial", "helvetica", "roboto"}
    for r in RECIPES:
        assert r.primary.lower() not in purples
        assert r.heading_font.lower() not in generics
        assert r.body_font.lower() not in generics


# --- preferred_section_variants referential integrity ------------------------


def test_preferred_variants_reference_real_catalog_entries():
    for r in RECIPES:
        kinds_seen: set[str] = set()
        for pref in r.preferred_section_variants:
            variant = get_variant(pref.variant_id)
            assert variant is not None, pref.variant_id
            assert variant.kind == pref.kind
            assert pref.kind not in kinds_seen  # one preference per kind
            kinds_seen.add(pref.kind)


def test_preference_rejects_unknown_variant():
    with pytest.raises(ValidationError):
        SectionVariantPreference(kind="hero", variant_id="hero.does-not-exist")


def test_preference_rejects_kind_mismatch():
    # a real variant id but the wrong kind
    with pytest.raises(ValidationError):
        SectionVariantPreference(kind="footer", variant_id="hero.centered-stacked")


# --- to_design_spec() --------------------------------------------------------


def test_to_design_spec_is_valid_and_carries_canonical_justifications():
    for r in RECIPES:
        ds = r.to_design_spec()
        assert isinstance(ds, DesignSpec)
        # round-trips through the strict schema (so it's persistable/loadable)
        DesignSpec.model_validate(ds.model_dump(mode="json"))
        assert ds.typography.heading_font == r.heading_font
        assert ds.palette.primary.lower() == r.primary.lower()
        choices = {j.choice for j in ds.justifications}
        # the core design choices are all justified with canonical keys
        assert CHOICE_TYPOGRAPHY_HEADING in choices
        assert CHOICE_TYPOGRAPHY_BODY in choices
        assert CHOICE_PALETTE_PRIMARY in choices
        # every emitted choice key is in the canonical set
        assert choices <= CANONICAL_CHOICE_KEYS
        # every reason is substantive (the schema enforces >= 12 chars)
        assert all(len(j.reason.strip()) >= 12 for j in ds.justifications)


def test_to_design_spec_accent_justified_only_when_present():
    with_accent = next(r for r in RECIPES if r.accent is not None)
    choices = {j.choice for j in with_accent.to_design_spec().justifications}
    assert "palette.accent" in choices

"""P8B tests: the canonical data-disco-* vocabulary, the renderer emitting through it, and
the anchor validators (duplicate / invented) + stable screen labels."""

from __future__ import annotations

import pytest
from disco.core.appkit import AppSection, AppSpec, render_html
from disco.core.appkit.semantic_metadata import (
    METADATA_VERSION,
    DataDiscoAttr,
    SemanticMetadataError,
    attr,
    declared_anchors_from_spec,
    extract_metadata,
    item_attrs,
    screen_label_value,
    validate_anchors_not_invented,
    validate_no_duplicate_anchors,
)

SPEC = AppSpec(
    title="Acme Roofing",
    sections=(
        AppSection(id="hero", kind="hero", fields={"headline": "Roofs", "subhead": "Done right", "cta_text": "Quote"}),
        AppSection(id="lead", kind="lead_form", fields={"title": "Contact us"}),
        AppSection(id="feat", kind="features", fields={"title": "Why us", "body": "Fast & fair"}),
    ),
)


# --- renderer emits metadata through the vocabulary ---------------------------
def test_hero_metadata_emitted() -> None:
    out = render_html(SPEC)
    assert 'data-disco-section="hero"' in out
    assert 'data-disco-screen-label="hero"' in out
    for f in ("headline", "subhead", "cta_text"):
        assert f'data-disco-field="{f}"' in out


def test_contact_form_metadata_emitted() -> None:
    out = render_html(SPEC)
    assert 'data-disco-section="lead"' in out
    assert 'data-disco-field="title"' in out and 'data-disco-screen-label="lead"' in out


def test_generic_section_metadata_emitted() -> None:
    # an allowlisted generic kind ("features") still carries section + field + screen-label
    out = render_html(SPEC)
    assert 'data-disco-section="feat"' in out and 'data-disco-screen-label="feat"' in out


def test_version_on_html_element() -> None:
    out = render_html(SPEC)
    assert f'<html lang="en" data-disco-version="{METADATA_VERSION}">' in out


def test_screen_labels_are_stable_across_renders() -> None:
    assert render_html(SPEC) == render_html(SPEC)  # deterministic, byte-identical
    assert screen_label_value("Hero Section!") == screen_label_value("hero-section")


# --- indexed/repeated-item emit helper (P8A IndexedLocator) -------------------
def test_item_attrs_emits_collection_index_kind_in_order() -> None:
    s = item_attrs("services.cards", 1, "card")
    assert s == ' data-disco-collection="services.cards" data-disco-index="1" data-disco-item-kind="card"'
    md = extract_metadata(f"<div{s}></div>")
    assert md[DataDiscoAttr.COLLECTION] == ["services.cards"]
    assert md[DataDiscoAttr.INDEX] == ["1"] and md[DataDiscoAttr.ITEM_KIND] == ["card"]


def test_item_attrs_rejects_negative_index() -> None:
    with pytest.raises(SemanticMetadataError):
        item_attrs("x.y", -1, "card")


# --- extraction: escaping + roundtrip -----------------------------------------
def test_attr_escapes_and_extract_decodes() -> None:
    frag = attr(DataDiscoAttr.FIELD, 'a"&<b')
    assert 'a"' not in frag  # the quote is escaped, doesn't break the attribute
    assert "&quot;" in frag and "&amp;" in frag and "&lt;" in frag
    # extract_metadata round-trips back to the decoded value
    assert extract_metadata(f"<i{frag}></i>")[DataDiscoAttr.FIELD] == ['a"&<b']


# --- anchor validators --------------------------------------------------------
def test_duplicate_anchors_rejected_even_when_encoded_differently() -> None:
    ok = '<a data-disco-comment-anchor="intro"></a><a data-disco-comment-anchor="body"></a>'
    validate_no_duplicate_anchors(ok)  # no raise
    dup = '<a data-disco-comment-anchor="note"></a><a data-disco-comment-anchor="note"></a>'
    with pytest.raises(SemanticMetadataError):
        validate_no_duplicate_anchors(dup)
    # encoded vs decoded forms of the SAME value are still duplicates
    decoded_dup = '<a data-disco-comment-anchor="a&amp;b"></a><a data-disco-comment-anchor="a&b"></a>'
    with pytest.raises(SemanticMetadataError):
        validate_no_duplicate_anchors(decoded_dup)


def test_declared_anchors_derive_from_spec_section_ids() -> None:
    assert declared_anchors_from_spec(SPEC) == frozenset({"hero", "lead", "feat"})


def test_anchors_not_invented() -> None:
    declared = declared_anchors_from_spec(SPEC)
    # a real render emits no comment anchors yet → trivially none invented
    validate_anchors_not_invented(render_html(SPEC), declared)
    # an anchor outside the spec namespace is rejected
    bad = '<a data-disco-comment-anchor="ghost"></a>'
    with pytest.raises(SemanticMetadataError):
        validate_anchors_not_invented(bad, declared)
    # an anchor inside the namespace is accepted
    good = '<a data-disco-comment-anchor="hero"></a>'
    validate_anchors_not_invented(good, declared)

"""P8A tests: deterministic, 1-index-safe semantic reference resolution that rejects
ambiguity. Required cases from the spec + pinned ordinal/normalize/collision behaviors.
Negative/ambiguous phrases sourced from the DeepSeek scout fixture."""

from __future__ import annotations

import pytest
from disco.core.semantic_refs import (
    AnchorEntry,
    CollectionEntry,
    CommentAnchorLocator,
    FieldEntry,
    FieldLocator,
    IndexedLocator,
    ReferenceResolution,
    ResolutionReason,
    SectionEntry,
    SectionLocator,
    SemanticReferenceContext,
    SlideEntry,
    SlideLocator,
    human_ordinal_to_index,
    normalize_screen_label,
    parse_human_ordinal,
    resolve_human_reference,
    target_id,
)
from pydantic import ValidationError

CTX = SemanticReferenceContext(
    sections=(
        SectionEntry(id="hero", label="hero", ordinal=1),
        SectionEntry(id="services", label="services", ordinal=2),
        SectionEntry(id="pricing", label="pricing", ordinal=3),
        SectionEntry(id="about", label="about", ordinal=4),
    ),
    fields=(
        FieldEntry(section_id="hero", field_id="headline", labels=("headline", "title")),
        FieldEntry(section_id="hero", field_id="cta", labels=("cta", "call to action")),
    ),
    collections=(
        CollectionEntry(id="services.cards", item_kind="card", length=3, labels=("service card",)),
        CollectionEntry(id="pricing.columns", item_kind="column", length=4, labels=("pricing column",)),
    ),
    slides=tuple(SlideEntry(id=f"slide-{i}", title=f"Slide {i}", ordinal=i) for i in range(1, 7)),
    comment_anchors=(AnchorEntry(id="cmt-intro", label="intro note"),),
)


def _r(phrase: str) -> ReferenceResolution:
    return resolve_human_reference(phrase, context=CTX)


# --- required spec cases ------------------------------------------------------
def test_hero_headline() -> None:
    assert _r("the hero headline").resolved == FieldLocator(section_id="hero", field_id="headline")


def test_hero_cta() -> None:
    assert _r("hero cta").resolved == FieldLocator(section_id="hero", field_id="cta")


def test_second_service_card_is_index_1() -> None:
    res = _r("second service card")
    assert res.resolved == IndexedLocator(collection_id="services.cards", index=1)


def test_third_pricing_column_is_index_2() -> None:
    assert _r("third pricing column").resolved == IndexedLocator(collection_id="pricing.columns", index=2)


def test_slide_5_is_fifth_slide_index_4() -> None:
    res = _r("slide 5")
    assert res.resolved == SlideLocator(slide_id="slide-5")  # 1-index→0-index: 5th slide
    assert CTX.slides[4].ordinal == 5  # off-by-one guard


def test_section_3() -> None:
    assert _r("section 3").resolved == SectionLocator(section_id="pricing")  # ordinal 3


def test_comment_anchor() -> None:
    assert _r("the intro note").resolved == CommentAnchorLocator(anchor_id="cmt-intro")


def test_unknown_is_ambiguous_or_no_match_never_guessed() -> None:
    # DeepSeek scout fixtures: phrases lacking a discriminating ordinal/label
    for phrase in ("that thing", "the element", "click there", "the button", "make it pop"):
        res = _r(phrase)
        assert res.resolved is None  # NEVER a guess
        assert res.reason in (ResolutionReason.NO_MATCH, ResolutionReason.AMBIGUOUS)


# --- ambiguity: a normalized-label collision -> AMBIGUOUS (candidates) ---------
def test_normalized_label_collision_is_ambiguous() -> None:
    ctx = SemanticReferenceContext(
        sections=(
            SectionEntry(id="feat1", label="Features", ordinal=1),
            SectionEntry(id="feat2", label="features", ordinal=2),  # same slug
        )
    )
    res = resolve_human_reference("features", context=ctx)
    assert res.resolved is None and res.ambiguous and res.reason is ResolutionReason.AMBIGUOUS
    assert res.candidates == ("section:feat1", "section:feat2")  # kind-qualified


# --- ordinal parsing (words AND digits, invalid, off-by-one) ------------------
def test_parse_ordinal_words_and_digits() -> None:
    assert parse_human_ordinal("second service card").value == 2
    assert parse_human_ordinal("the 5th slide").value == 5
    assert parse_human_ordinal("slide 5").value == 5
    assert parse_human_ordinal("third one").value == 3
    assert parse_human_ordinal("the headline").value is None  # no ordinal present


def test_invalid_ordinal_rejected() -> None:
    for bad in ("the 0th slide", "slide 0", "the -1 card", "zeroth slide"):
        assert resolve_human_reference(bad, context=CTX).reason is ResolutionReason.INVALID_ORDINAL


def test_out_of_range() -> None:
    assert _r("slide 99").reason is ResolutionReason.OUT_OF_RANGE
    assert _r("fifth pricing column").reason is ResolutionReason.OUT_OF_RANGE  # only 4 columns


def test_human_ordinal_to_index() -> None:
    assert human_ordinal_to_index(1) == 0 and human_ordinal_to_index(5) == 4
    with pytest.raises(ValueError):
        human_ordinal_to_index(0)


# --- normalize_screen_label (deterministic + idempotent) ----------------------
def test_normalize_is_deterministic_and_idempotent() -> None:
    for s in ("Hero Headline!", "  Get   Started  ", "Sign-Up / Login", "CTA"):
        n = normalize_screen_label(s)
        assert n == normalize_screen_label(n)  # idempotent
        assert n == normalize_screen_label(s)  # deterministic
    assert normalize_screen_label("Hero Headline!") == "hero-headline"
    assert normalize_screen_label("Sign Up") == normalize_screen_label("sign-up")  # case/punct fold


# --- result invariants enforced -----------------------------------------------
def test_resolution_invariants_enforced() -> None:
    with pytest.raises(ValidationError):  # RESOLVED needs a target
        ReferenceResolution(reason=ResolutionReason.RESOLVED)
    with pytest.raises(ValidationError):  # a reject must not carry a target
        ReferenceResolution(resolved=SectionLocator(section_id="x"), reason=ResolutionReason.NO_MATCH)
    with pytest.raises(ValidationError):  # ambiguous needs candidates
        ReferenceResolution(ambiguous=True, reason=ResolutionReason.AMBIGUOUS)


def test_target_id_stable() -> None:
    assert target_id(FieldLocator(section_id="hero", field_id="headline")) == "hero.headline"
    assert target_id(IndexedLocator(collection_id="services.cards", index=1)) == "services.cards[1]"


# --- P8A code-review fixes: cross-tier / sparse ordinals / parsing edges -------
def test_field_subsumes_its_section_not_ambiguous() -> None:
    # "hero headline" matches the field AND (loosely) the bare "hero" section; the field
    # refines the section, so it resolves cleanly rather than going ambiguous.
    res = _r("the hero headline")
    assert res.reason is ResolutionReason.RESOLVED
    assert res.resolved == FieldLocator(section_id="hero", field_id="headline")


def test_cross_tier_collision_is_ambiguous() -> None:
    # a section AND a comment anchor that share the SAME id string "notes" must stay distinct
    # (kind-qualified) and resolve AMBIGUOUS — not collapse to one via a colliding bare id.
    ctx = SemanticReferenceContext(
        sections=(SectionEntry(id="notes", label="notes", ordinal=1),),
        comment_anchors=(AnchorEntry(id="notes", label="notes"),),
    )
    res = resolve_human_reference("notes", context=ctx)
    assert res.ambiguous and res.candidates == ("comment_anchor:notes", "section:notes")


def test_sparse_and_out_of_order_ordinals_resolve_by_value() -> None:
    ctx = SemanticReferenceContext(
        slides=(
            SlideEntry(id="intro", title="Intro", ordinal=30),
            SlideEntry(id="body", title="Body", ordinal=10),
            SlideEntry(id="end", title="End", ordinal=20),
        )
    )
    assert resolve_human_reference("slide 20", context=ctx).resolved == SlideLocator(slide_id="end")
    assert resolve_human_reference("slide 10", context=ctx).resolved == SlideLocator(slide_id="body")
    # an ordinal with no matching slide → OUT_OF_RANGE, not a wrong positional guess
    assert resolve_human_reference("slide 2", context=ctx).reason is ResolutionReason.OUT_OF_RANGE


def test_duplicate_ordinal_is_ambiguous() -> None:
    ctx = SemanticReferenceContext(
        slides=(SlideEntry(id="a", title="A", ordinal=1), SlideEntry(id="b", title="B", ordinal=1))
    )
    res = resolve_human_reference("slide 1", context=ctx)
    assert res.ambiguous and res.candidates == ("slide:a", "slide:b")


def test_bare_cardinal_digits_are_ordinals() -> None:
    # bare "10"/"50" are intentionally treated as ordinals (1-based), and 10≠a "0" match
    assert parse_human_ordinal("card 10").value == 10
    assert parse_human_ordinal("item 50").value == 50
    ctx = SemanticReferenceContext(
        collections=(CollectionEntry(id="g.cards", item_kind="card", length=12),)
    )
    assert resolve_human_reference("card 10", context=ctx).resolved == IndexedLocator(
        collection_id="g.cards", index=9
    )


def test_punctuation_bound_negative_is_invalid() -> None:
    assert _r("slide:-1").reason is ResolutionReason.INVALID_ORDINAL
    assert _r("slide -1").reason is ResolutionReason.INVALID_ORDINAL
    # but a hyphen-joined "slide-5" is NOT a negative — it is the 5th slide
    assert _r("slide-5").resolved == SlideLocator(slide_id="slide-5")


def test_inverse_invariants_rejected() -> None:
    with pytest.raises(ValidationError):  # AMBIGUOUS reason but ambiguous=False
        ReferenceResolution(reason=ResolutionReason.AMBIGUOUS, ambiguous=False, candidates=("x",))
    with pytest.raises(ValidationError):  # a reject reason carrying candidates
        ReferenceResolution(reason=ResolutionReason.NO_MATCH, candidates=("x",))
    with pytest.raises(ValidationError):  # RESOLVED carrying candidates
        ReferenceResolution(
            reason=ResolutionReason.RESOLVED, resolved=SectionLocator(section_id="x"), candidates=("x",)
        )


def test_indexed_locator_rejects_negative_index() -> None:
    with pytest.raises(ValidationError):
        IndexedLocator(collection_id="x.y", index=-1)

"""Epic F5.2-lite — the `collection` primitive (core: spec + fold semantics).

Covers:
  * CollectionSpec validation — unknown key (extra=forbid), slug shape, item
    bounds (empty/oversize list, blank/oversize labels), unique labels;
  * the fold — the derived `list` section lands on the FIRST page by default /
    a NAMED page when `page_id` is given, heading = title, items formatted
    "label — detail" (bare label when detail is absent);
  * precise refusals — unknown page (lists known ids), derived-id collision
    with a non-list section (lists taken ids), cross-page re-target, formatted
    item overflowing SectionContent's per-item cap (names the offenders);
  * re-apply semantics — same collection_id REPLACES the section's content in
    place (preserving variant_id), identical re-apply returns an EQUAL app (so
    app_add_primitive's no-op refusal fires);
  * registration — fillable, addable (spec_schema + apply_spec), verify=None,
    empty host_contract; standalone generate renders folded items (no silent
    drop).
"""

from __future__ import annotations

import pytest
from disco.core.appkit.collection_primitive import (
    CollectionItem,
    CollectionSpec,
    apply_collection_spec,
    default_collection_app_spec,
    generate_collection,
    section_id_for,
)
from disco.core.appkit.primitives import COLLECTION_PRIMITIVE_ID, get_primitive
from disco.core.appkit.recipes import SiteRecipe, get_recipe
from disco.core.appkit.spec import AppSpec, Page, Section, SectionContent
from pydantic import ValidationError

# ---- helpers --------------------------------------------------------------------


def _recipe() -> SiteRecipe:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _app(*, extra_home_section: Section | None = None) -> AppSpec:
    """A two-page AppSpec (home + about) to fold collections into."""
    home_sections: tuple[Section, ...] = (Section(id="hero", kind="hero"),)
    if extra_home_section is not None:
        home_sections = (*home_sections, extra_home_section)
    return AppSpec(
        schema_version=1,
        app_kind="lead_gen",
        name="Acme Studio",
        pages=(
            Page(id="home", route="/", title="Home", sections=home_sections),
            Page(id="about", route="/about", title="About"),
        ),
    )


def _spec(**overrides: object) -> CollectionSpec:
    payload: dict[str, object] = {
        "collection_id": "team",
        "title": "Meet the team",
        "items": [
            {"label": "Ana Ferreira", "detail": "Founder"},
            {"label": "Ben Okafor", "detail": "Design lead"},
            {"label": "Cleo Marsh"},
        ],
    }
    payload.update(overrides)
    return CollectionSpec.model_validate(payload)


def _section(app: AppSpec, page_id: str, section_id: str) -> Section:
    page = next(p for p in app.pages if p.id == page_id)
    return next(s for s in page.sections if s.id == section_id)


# ---- spec validation --------------------------------------------------------------


def test_unknown_key_refused() -> None:
    with pytest.raises(ValidationError, match="collection_name"):
        _spec(collection_name="oops")


def test_collection_id_must_be_a_slug() -> None:
    for bad in ("Team", "1team", "team members", "team!", "", "x" * 49):
        with pytest.raises(ValidationError):
            _spec(collection_id=bad)
    # underscores and hyphens are fine
    assert _spec(collection_id="team_members-2").collection_id == "team_members-2"


def test_title_bounds() -> None:
    with pytest.raises(ValidationError):
        _spec(title="")
    with pytest.raises(ValidationError):
        _spec(title="   ")
    with pytest.raises(ValidationError):
        _spec(title="x" * 201)


def test_items_bounds() -> None:
    with pytest.raises(ValidationError):
        _spec(items=[])
    with pytest.raises(ValidationError):
        _spec(items=[{"label": f"person {i}"} for i in range(25)])
    with pytest.raises(ValidationError):
        _spec(items=[{"label": ""}])
    with pytest.raises(ValidationError):
        _spec(items=[{"label": "   "}])
    with pytest.raises(ValidationError):
        _spec(items=[{"label": "x" * 121}])
    with pytest.raises(ValidationError):
        _spec(items=[{"label": "ok", "detail": "y" * 241}])
    with pytest.raises(ValidationError):
        _spec(items=[{"label": "ok", "detail": "  "}])


def test_duplicate_labels_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate collection item label"):
        _spec(items=[{"label": "Ana"}, {"label": "Ana", "detail": "again"}])


# ---- the fold ---------------------------------------------------------------------


def test_fold_lands_on_first_page_by_default() -> None:
    app = apply_collection_spec(_app(), _spec())
    section = _section(app, "home", section_id_for("team"))
    assert section.kind == "list"
    assert section.content is not None
    assert section.content.heading == "Meet the team"
    assert section.content.items == (
        "Ana Ferreira — Founder",
        "Ben Okafor — Design lead",
        "Cleo Marsh",
    )
    # the about page is untouched
    about = next(p for p in app.pages if p.id == "about")
    assert about.sections == ()


def test_fold_lands_on_named_page() -> None:
    app = apply_collection_spec(_app(), _spec(page_id="about"))
    section = _section(app, "about", section_id_for("team"))
    assert section.kind == "list"
    home = next(p for p in app.pages if p.id == "home")
    assert [s.id for s in home.sections] == ["hero"]


def test_unknown_page_refused_listing_known_ids() -> None:
    with pytest.raises(ValueError, match=r"unknown page_id 'nope'.*'home'.*'about'"):
        apply_collection_spec(_app(), _spec(page_id="nope"))


def test_section_id_collision_refused_listing_taken_ids() -> None:
    squatter = Section(id=section_id_for("team"), kind="hero")
    with pytest.raises(ValueError, match=r"already taken by a 'hero' section.*'hero'"):
        apply_collection_spec(_app(extra_home_section=squatter), _spec())


def test_cross_page_retarget_refused() -> None:
    app = apply_collection_spec(_app(), _spec())  # lands on home
    with pytest.raises(ValueError, match=r"already lives on page 'home'"):
        apply_collection_spec(app, _spec(page_id="about"))


def test_formatted_item_overflow_refused_precisely() -> None:
    # label (120) + " — " + detail (240) = 363 > the 300-char SectionContent cap:
    # each slot is individually in bounds, only the FORMATTED string overflows.
    big = {"label": "L" * 120, "detail": "D" * 240}
    with pytest.raises(ValueError, match=r"too long once formatted.*300.*363 chars"):
        apply_collection_spec(_app(), _spec(items=[big, {"label": "fine"}]))


def test_wrong_spec_type_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="needs a CollectionSpec"):
        apply_collection_spec(_app(), SectionContent(heading="not a collection spec"))


# ---- re-apply semantics -----------------------------------------------------------


def test_reapply_replaces_content_in_place() -> None:
    app = apply_collection_spec(_app(), _spec())
    updated = apply_collection_spec(
        app,
        _spec(
            title="The team",
            items=[{"label": "Ana Ferreira", "detail": "CEO"}, {"label": "Dana Wu"}],
        ),
    )
    home = next(p for p in updated.pages if p.id == "home")
    matches = [s for s in home.sections if s.id == section_id_for("team")]
    assert len(matches) == 1  # replaced, not duplicated
    content = matches[0].content
    assert content is not None
    assert content.heading == "The team"
    assert content.items == ("Ana Ferreira — CEO", "Dana Wu")
    # placement is stable: the section keeps its slot after the hero
    assert [s.id for s in home.sections] == ["hero", section_id_for("team")]


def test_reapply_preserves_variant_id() -> None:
    app = apply_collection_spec(_app(), _spec())
    # the owner (or a patch tool) picked a specific list layout since
    data = app.model_dump(mode="json")
    for sec in data["pages"][0]["sections"]:
        if sec["id"] == section_id_for("team"):
            sec["variant_id"] = "list.dense-rows"
    app = AppSpec.model_validate(data)

    updated = apply_collection_spec(app, _spec(items=[{"label": "Only One"}]))
    section = _section(updated, "home", section_id_for("team"))
    assert section.variant_id == "list.dense-rows"
    assert section.content is not None
    assert section.content.items == ("Only One",)


def test_identical_reapply_returns_equal_app() -> None:
    # app_add_primitive compares model_dump()s to fire its no-op refusal — an
    # identical re-apply must produce an EQUAL app, never spurious drift.
    once = apply_collection_spec(_app(), _spec())
    twice = apply_collection_spec(once, _spec())
    assert twice.model_dump(mode="json") == once.model_dump(mode="json")


# ---- registration + the standalone shape ------------------------------------------


def test_registered_as_fillable_and_addable() -> None:
    prim = get_primitive(COLLECTION_PRIMITIVE_ID)
    assert prim is not None
    assert prim.tier == "fillable"
    assert prim.spec_schema is CollectionSpec
    assert prim.apply_spec is apply_collection_spec
    assert prim.verify is None
    assert prim.host_contract == ()


def test_standalone_generate_renders_folded_items() -> None:
    # The standalone shape exists to satisfy the contract, but a folded
    # collection must still be VISIBLE in its output — never a silent drop.
    recipe = _recipe()
    app = default_collection_app_spec("Roster", recipe)
    assert app.app_kind == COLLECTION_PRIMITIVE_ID
    folded = apply_collection_spec(
        app,
        _spec(title="Café crew <3", items=[{"label": "Ana & Bo", "detail": "Baristas"}]),
    )
    tree = generate_collection(folded, recipe.to_design_spec())
    page = tree["index.html"]
    assert "<h1>Roster</h1>" in page
    assert "Café crew &lt;3" in page  # escaped heading
    assert "<li>Ana &amp; Bo — Baristas</li>" in page  # escaped formatted item


def test_format_helper_shapes() -> None:
    spec = _spec(items=[{"label": "Bare"}, {"label": "Pair", "detail": "detail"}])
    app = apply_collection_spec(_app(), spec)
    content = _section(app, "home", section_id_for("team")).content
    assert content is not None
    assert content.items == ("Bare", "Pair — detail")


def test_collection_item_model_roundtrip() -> None:
    item = CollectionItem(label="Ana", detail=None)
    assert item.model_dump() == {"label": "Ana", "detail": None}

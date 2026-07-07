"""The AppKit `collection` primitive — Epic F5.2-lite (structured content collections).

The "add a team-members section / menu / testimonials list" UX as a DECLARATIVE
fill: the model fills a `CollectionSpec` (a slug id, a title, 1..24 labeled
items) and `app_add_primitive` folds it into the AppSpec via
`apply_collection_spec`. The fold inserts (or updates) ONE `list`-kind `Section`
whose bounded `SectionContent` carries the items — so rendering rides the
EXISTING list-section lowering in every base primitive's generator, with zero
new emitters.

SCOPE — no owner-facing edit UI in this slice. This is the F5.2-lite cut: there
is NO owner-facing collection editor yet (that is the rest of Epic 5.2). Until
it lands, the ONLY way to edit a collection is to re-apply the primitive with
the SAME `collection_id` and changed values: `apply_collection_spec` then
REPLACES that section's content in place (update-by-section-id), preserving any
`variant_id`/`content_ref` the section has picked up since. An IDENTICAL
re-apply returns an unchanged app, so `app_add_primitive`'s no-op refusal fires
naturally. Do not present any other edit affordance for collections.

Fold semantics (all refusals are precise ValueErrors):

* the section id is derived as ``collection-<collection_id>``;
* `page_id=None` targets the FIRST page; an unknown `page_id` refuses, listing
  the known page ids;
* a derived-id collision with a NON-`list` section on the target page refuses,
  listing the taken section ids; a `list` section with the derived id is treated
  as THIS collection's section and its content is replaced (see above — that is
  the documented re-apply/edit path);
* if the derived id already lives on a DIFFERENT page, the fold refuses and
  names that page (no silent cross-page moves);
* items are formatted ``label — detail`` (bare ``label`` when detail is absent)
  and validated against `SectionContent`'s per-item length cap BEFORE the fold —
  an overflow refuses naming every offending label.

The standalone `default_app_spec`/`generate` mirror the hello primitive's
minimal shape: this primitive exists to be ADDED to a real app
(`app_add_primitive`), not scaffolded standalone. The standalone generator still
renders any folded list sections (heading + items) so a collection folded into a
standalone `collection` app stays VISIBLE — never a silent drop.

Layering: `disco.core` is the leaf package; this module imports pydantic + the
stdlib + siblings only, and is force-imported by `generator.py` so registration
happens before any resolve/generate call (same as the hello/records siblings).
"""

from __future__ import annotations

import html

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .primitives import COLLECTION_PRIMITIVE_ID, PrimitiveDefinition, register_primitive
from .recipes import SiteRecipe
from .spec import (
    _MAX_HEADING,
    _MAX_ITEM,
    _MAX_ITEMS,
    AppSpec,
    DesignSpec,
    Page,
    Section,
    SectionContent,
)

# The derived section id is `_SECTION_ID_PREFIX + collection_id`. Section ids are
# capped at spec._ID_MAX (64) because they feed the generated component filename,
# so the slug cap below keeps the derived id comfortably inside that bound:
# len("collection-") + 48 = 59 <= 64.
_SECTION_ID_PREFIX = "collection-"
_MAX_COLLECTION_ID = 48

# Item slots are bounded INDIVIDUALLY looser than the combined SectionContent
# per-item cap (_MAX_ITEM = 300): label 120 + " — " + detail 240 = 363 can
# overflow, so `apply_collection_spec` explicitly validates every FORMATTED
# string fits and refuses with the offending labels — the caps here bound the
# schema, the fit-check guards the fold.
_MAX_LABEL = 120
_MAX_DETAIL = 240
_ITEM_SEPARATOR = " — "


class CollectionItem(BaseModel):
    """One entry of a collection: a bounded `label` plus an optional bounded
    `detail` (rendered as ``label — detail``). Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        min_length=1,
        max_length=_MAX_LABEL,
        description=f"The item's display label (1-{_MAX_LABEL} chars, e.g. a name).",
    )
    detail: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_DETAIL,
        description=(
            f"Optional supporting detail (1-{_MAX_DETAIL} chars, e.g. a role or "
            'price); rendered as "label — detail".'
        ),
    )

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("collection item label must not be blank")
        return value

    @field_validator("detail")
    @classmethod
    def _detail_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("collection item detail must not be blank when given")
        return value


class CollectionSpec(BaseModel):
    """The `collection` primitive's declarative spec — what the model fills.

    `collection_id` is a slug that derives the section id (stable across
    re-applies: the same id UPDATES the same section — the edit path for this
    slice, see the module docstring). `page_id=None` targets the first page.
    Labels must be unique. `extra="forbid"` so an unknown key is a precise
    model-facing error, never a silent drop."""

    model_config = ConfigDict(extra="forbid")

    collection_id: str = Field(
        min_length=1,
        max_length=_MAX_COLLECTION_ID,
        pattern=r"^[a-z][a-z0-9_-]*$",
        description=(
            "Slug identifying this collection (lowercase, starts with a letter, "
            "only letters/digits/'_'/'-'; max "
            f"{_MAX_COLLECTION_ID} chars). Re-applying with the same id UPDATES "
            "that collection's section in place."
        ),
    )
    title: str = Field(
        min_length=1,
        max_length=_MAX_HEADING,
        description=f"The section heading shown above the items (1-{_MAX_HEADING} chars).",
    )
    page_id: str | None = Field(
        default=None,
        description="The page to place the collection on (omit for the first page).",
    )
    items: list[CollectionItem] = Field(
        min_length=1,
        max_length=_MAX_ITEMS,
        description=f"The collection entries, in display order (1-{_MAX_ITEMS}).",
    )

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("collection title must not be blank")
        return value

    @model_validator(mode="after")
    def _labels_unique(self) -> CollectionSpec:
        seen: set[str] = set()
        for item in self.items:
            if item.label in seen:
                raise ValueError(f"duplicate collection item label: {item.label!r}")
            seen.add(item.label)
        return self


def section_id_for(collection_id: str) -> str:
    """The derived, stable section id for a collection (`collection-<slug>`)."""
    return f"{_SECTION_ID_PREFIX}{collection_id}"


def _format_item(item: CollectionItem) -> str:
    """One SectionContent item string: `label — detail`, or the bare label."""
    if item.detail is None:
        return item.label
    return f"{item.label}{_ITEM_SEPARATOR}{item.detail}"


def _formatted_items(spec: CollectionSpec) -> tuple[str, ...]:
    """Format every item and validate the RESULT fits SectionContent's per-item
    cap — the schema bounds label/detail individually, but the combined
    formatted string can overflow; refuse precisely, naming every offender."""
    formatted: list[str] = []
    offenders: list[str] = []
    for item in spec.items:
        text = _format_item(item)
        if len(text) > _MAX_ITEM:
            offenders.append(f"{item.label!r} ({len(text)} chars)")
        formatted.append(text)
    if offenders:
        raise ValueError(
            "collection item(s) too long once formatted as 'label — detail' "
            f"(the section-content cap is {_MAX_ITEM} chars per item): "
            f"{', '.join(offenders)}. Shorten the label or detail."
        )
    return tuple(formatted)


def apply_collection_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated CollectionSpec into the AppSpec: insert (or update — the
    re-apply/edit path, see the module docstring) ONE `list`-kind Section whose
    bounded `SectionContent` carries the formatted items. Same house dance as
    `app_add_section`: validated models → dump → splice → `AppSpec.model_validate`."""
    if not isinstance(spec, CollectionSpec):  # defensive: app_add_primitive validated it
        raise TypeError(f"apply_spec for {COLLECTION_PRIMITIVE_ID!r} needs a CollectionSpec")

    formatted = _formatted_items(spec)

    data = app.model_dump(mode="json")
    pages = data["pages"]
    if not pages:
        raise ValueError("the app has no pages to place a collection on")
    known_page_ids = [p["id"] for p in pages]
    if spec.page_id is None:
        page = pages[0]
    else:
        page = next((p for p in pages if p["id"] == spec.page_id), None)
        if page is None:
            raise ValueError(
                f"unknown page_id {spec.page_id!r}; known pages: {known_page_ids}"
            )

    section_id = section_id_for(spec.collection_id)
    # Build through the REAL Section/SectionContent models so every bound and
    # validator applies — exactly how app_add_section validates its input.
    section = Section(
        id=section_id,
        kind="list",
        content=SectionContent(heading=spec.title, items=formatted),
    )
    sec_json = section.model_dump(mode="json")

    target_secs = list(page["sections"])
    idx = next((i for i, s in enumerate(target_secs) if s["id"] == section_id), None)
    if idx is not None:
        if target_secs[idx]["kind"] != "list":
            taken = sorted(s["id"] for s in target_secs)
            raise ValueError(
                f"section id {section_id!r} is already taken by a "
                f"{target_secs[idx]['kind']!r} section on page {page['id']!r} "
                f"(taken ids: {taken}); choose a different collection_id"
            )
        # This collection's section: REPLACE its content in place (the edit path
        # for this slice), preserving variant_id/content_ref picked up since.
        target_secs[idx] = {**target_secs[idx], "content": sec_json["content"]}
        page["sections"] = target_secs
    else:
        for other in pages:
            if other is page:
                continue
            if any(s["id"] == section_id for s in other["sections"]):
                raise ValueError(
                    f"collection {spec.collection_id!r} already lives on page "
                    f"{other['id']!r} (section {section_id!r}); pass "
                    f"page_id={other['id']!r} to update it there — collections "
                    "are not moved across pages"
                )
        page["sections"] = [*target_secs, sec_json]

    return AppSpec.model_validate(data)


def default_collection_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The minimal valid standalone AppSpec — mirrors the hello primitive.

    This primitive exists to be ADDED to a real app via `app_add_primitive`;
    the standalone shape is the contract-satisfying minimum. `recipe` is
    deliberately unused — the standalone page has no design-preference surface."""
    del recipe
    title = name.strip() or "Collection"
    return AppSpec(
        schema_version=1,
        app_kind=COLLECTION_PRIMITIVE_ID,
        name=title,
        pages=(Page(id="home", route="/", title="Home"),),
    )


def prepare_collection_app_spec(app: AppSpec) -> AppSpec:
    """Identity — the collection primitive needs no normalization."""
    return app


def generate_collection(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower a standalone collection AppSpec into a minimal static tree (hello's
    shape). Any `list`-kind section's heading + items ARE rendered, so a folded
    collection stays visible even in the standalone shape — a folded spec must
    never silently vanish from the output. `design` is deliberately unused."""
    del design
    title = html.escape(app.name)
    blocks: list[str] = []
    for page in app.pages:
        for section in page.sections:
            if section.kind != "list" or section.content is None:
                continue
            heading = html.escape(section.content.heading or "")
            items = "\n".join(
                f"        <li>{html.escape(item)}</li>" for item in section.content.items
            )
            blocks.append(
                f'    <section id="{html.escape(section.id, quote=True)}">\n'
                f"      <h2>{heading}</h2>\n"
                "      <ul>\n"
                f"{items}\n"
                "      </ul>\n"
                "    </section>\n"
            )
    return {
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="utf-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"    <title>{title}</title>\n"
            "  </head>\n"
            "  <body>\n"
            f"    <h1>{title}</h1>\n"
            + "".join(blocks)
            + "  </body>\n"
            "</html>\n"
        )
    }


register_primitive(
    PrimitiveDefinition(
        id=COLLECTION_PRIMITIVE_ID,
        default_app_spec=default_collection_app_spec,
        prepare_app_spec=prepare_collection_app_spec,
        generate=generate_collection,
        tier="fillable",
        host_contract=(),
        spec_schema=CollectionSpec,
        verify=None,
        apply_spec=apply_collection_spec,
    )
)


__all__ = [
    "COLLECTION_PRIMITIVE_ID",
    "CollectionItem",
    "CollectionSpec",
    "apply_collection_spec",
    "default_collection_app_spec",
    "generate_collection",
    "prepare_collection_app_spec",
    "section_id_for",
]

"""AppKit EPIC D2 — the section-variant catalog.

Every generated site otherwise collapses to the SAME shape: a centered hero, a
row of three feature cards, and a call-to-action. That monoculture is the visual
tell of an LLM-generated site. This module is the antidote: for EACH structural
`SectionKind` it declares >= 2 DISTINCT layout variants, so the scaffold
generator (Epic E) and the recipes (D1) can pick a deliberate, non-median layout
per section instead of defaulting to the slop sequence.

It is PURE DATA: a frozen `SectionVariant` model plus a flat catalog tuple and a
couple of lookups. No IO, no LLM, no side effects.

Layering: `disco.core` is the leaf package (.importlinter). This module imports
ONLY pydantic + the stdlib + the sibling `.spec` (also core leaf). It is the
single source of truth for "what layouts exist" — `recipes.py` validates its
`preferred_section_variants` against it, and the Epic E scaffolder reads it to
emit a real layout instead of guessing.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from .spec import SectionKind

_STRICT = ConfigDict(extra="forbid", frozen=True)


class SectionVariant(BaseModel):
    """One concrete layout option for a `SectionKind`.

    `id` is globally unique across the catalog (namespaced `"<kind>.<slug>"`);
    `layout` is a short machine slug the scaffolder can switch on; `description`
    is the human-facing one-liner that says how this variant differs from the
    median so a recipe can pick on intent."""

    model_config = _STRICT

    id: str
    kind: SectionKind
    layout: str
    description: str

    @field_validator("id", "layout", "description")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SectionVariant fields must be non-empty")
        return value


# The catalog: >= 2 DISTINCT variants for every structural SectionKind (the
# `custom` escape hatch is intentionally absent — it has no fixed layout). These
# are deliberately NOT "centered hero / 3 cards / cta": each kind offers genuinely
# different compositions so a site can be assembled out of non-median parts.
SECTION_VARIANTS: tuple[SectionVariant, ...] = (
    # ---- hero ----------------------------------------------------------------
    SectionVariant(
        id="hero.split-media-right",
        kind="hero",
        layout="split-media-right",
        description="Headline + CTA in the left column, a supporting image/figure pinned right.",
    ),
    SectionVariant(
        id="hero.full-bleed-image",
        kind="hero",
        layout="full-bleed-image",
        description="Edge-to-edge background image with an overlaid headline and a single CTA.",
    ),
    SectionVariant(
        id="hero.centered-stacked",
        kind="hero",
        layout="centered-stacked",
        description="Centered eyebrow/headline/subhead stack — restrained, type-led, no media.",
    ),
    SectionVariant(
        id="hero.asymmetric-editorial",
        kind="hero",
        layout="asymmetric-editorial",
        description="Oversized left-aligned display headline with an off-grid kicker and rule.",
    ),
    # ---- features ------------------------------------------------------------
    SectionVariant(
        id="features.alternating-rows",
        kind="features",
        layout="alternating-rows",
        description="Full-width rows that alternate text/media side down the page (zig-zag).",
    ),
    SectionVariant(
        id="features.bento-grid",
        kind="features",
        layout="bento-grid",
        description="Mixed-size bento tiles — a few large cells anchor a field of smaller ones.",
    ),
    SectionVariant(
        id="features.icon-list-2col",
        kind="features",
        layout="icon-list-2col",
        description="Two compact columns of icon + label + one-line benefit (no card chrome).",
    ),
    SectionVariant(
        id="features.comparison-columns",
        kind="features",
        layout="comparison-columns",
        description="Side-by-side columns contrasting before/after or us/them capabilities.",
    ),
    # ---- cta -----------------------------------------------------------------
    SectionVariant(
        id="cta.banner-inline",
        kind="cta",
        layout="banner-inline",
        description="A single horizontal band: one sentence of copy and one primary action.",
    ),
    SectionVariant(
        id="cta.split-form-left",
        kind="cta",
        layout="split-form-left",
        description="Value prop on the right, an inline capture form (email/start) on the left.",
    ),
    SectionVariant(
        id="cta.sticky-footer-bar",
        kind="cta",
        layout="sticky-footer-bar",
        description="A slim sticky bar that docks the primary action to the viewport bottom.",
    ),
    # ---- list ----------------------------------------------------------------
    SectionVariant(
        id="list.timeline-vertical",
        kind="list",
        layout="timeline-vertical",
        description="A vertical spine with dated/ordered nodes — changelog, roadmap, history.",
    ),
    SectionVariant(
        id="list.masonry-cards",
        kind="list",
        layout="masonry-cards",
        description="Variable-height cards packed into a masonry column flow.",
    ),
    SectionVariant(
        id="list.dense-rows",
        kind="list",
        layout="dense-rows",
        description="Tight one-line rows with leading meta and a trailing action — index style.",
    ),
    SectionVariant(
        id="list.media-list",
        kind="list",
        layout="media-list",
        description="Each item is a thumbnail + title + excerpt row (articles, episodes).",
    ),
    # ---- form ----------------------------------------------------------------
    SectionVariant(
        id="form.single-column-labeled",
        kind="form",
        layout="single-column-labeled",
        description="One column, top-aligned labels, generous field spacing — the legible default.",
    ),
    SectionVariant(
        id="form.two-column-grouped",
        kind="form",
        layout="two-column-grouped",
        description="Related fields paired across two columns under labeled fieldset groups.",
    ),
    SectionVariant(
        id="form.stepper-wizard",
        kind="form",
        layout="stepper-wizard",
        description="A multi-step wizard with a progress rail; one logical group per step.",
    ),
    # ---- table ---------------------------------------------------------------
    SectionVariant(
        id="table.striped-data-grid",
        kind="table",
        layout="striped-data-grid",
        description="Zebra-striped rows, right-aligned numerics, a sticky toolbar above.",
    ),
    SectionVariant(
        id="table.card-collapse-mobile",
        kind="table",
        layout="card-collapse-mobile",
        description="A real table on wide screens that reflows to labeled cards on narrow ones.",
    ),
    SectionVariant(
        id="table.sticky-header-scroll",
        kind="table",
        layout="sticky-header-scroll",
        description="A frozen header (and optional first column) over a horizontal-scroll body.",
    ),
    # ---- gallery -------------------------------------------------------------
    SectionVariant(
        id="gallery.justified-grid",
        kind="gallery",
        layout="justified-grid",
        description="A justified rows grid (Flickr-style) that preserves each image aspect ratio.",
    ),
    SectionVariant(
        id="gallery.masonry",
        kind="gallery",
        layout="masonry",
        description="Pinterest-style masonry columns for mixed portrait/landscape media.",
    ),
    SectionVariant(
        id="gallery.carousel-filmstrip",
        kind="gallery",
        layout="carousel-filmstrip",
        description="A horizontal filmstrip carousel with peeking neighbors and snap points.",
    ),
    # ---- testimonials --------------------------------------------------------
    SectionVariant(
        id="testimonials.quote-wall",
        kind="testimonials",
        layout="quote-wall",
        description="A dense wall of short quote cards at mixed weights (social-proof field).",
    ),
    SectionVariant(
        id="testimonials.single-spotlight",
        kind="testimonials",
        layout="single-spotlight",
        description="One large pull-quote with portrait and attribution — a single strong voice.",
    ),
    SectionVariant(
        id="testimonials.logo-strip-quotes",
        kind="testimonials",
        layout="logo-strip-quotes",
        description="A customer logo strip paired with one rotating supporting quote.",
    ),
    # ---- pricing -------------------------------------------------------------
    SectionVariant(
        id="pricing.three-tier-cards",
        kind="pricing",
        layout="three-tier-cards",
        description="Three plan cards with a highlighted middle tier and per-plan feature lists.",
    ),
    SectionVariant(
        id="pricing.comparison-matrix",
        kind="pricing",
        layout="comparison-matrix",
        description="A feature-by-plan matrix with check/cross cells — for dense plan comparison.",
    ),
    SectionVariant(
        id="pricing.single-plan-emphasis",
        kind="pricing",
        layout="single-plan-emphasis",
        description="One confident plan with an itemized what's-included list and a single CTA.",
    ),
    # ---- faq -----------------------------------------------------------------
    SectionVariant(
        id="faq.accordion",
        kind="faq",
        layout="accordion",
        description="Single-open accordion rows that expand each answer in place.",
    ),
    SectionVariant(
        id="faq.two-column-qa",
        kind="faq",
        layout="two-column-qa",
        description="Always-expanded Q/A pairs laid out in two reading columns.",
    ),
    SectionVariant(
        id="faq.searchable-list",
        kind="faq",
        layout="searchable-list",
        description="A filter/search box above a flat, grouped list of questions.",
    ),
    # ---- footer --------------------------------------------------------------
    SectionVariant(
        id="footer.multi-column-sitemap",
        kind="footer",
        layout="multi-column-sitemap",
        description="A multi-column sitemap (product/company/legal) with a brand block.",
    ),
    SectionVariant(
        id="footer.minimal-centered",
        kind="footer",
        layout="minimal-centered",
        description="A single centered row: wordmark, a few links, copyright — restrained.",
    ),
    SectionVariant(
        id="footer.mega-footer-cta",
        kind="footer",
        layout="mega-footer-cta",
        description="A tall footer that leads with a final CTA above the sitemap columns.",
    ),
)


# Build the lookup once (id -> variant) and assert global id uniqueness at import
# time so a duplicate is a hard error, not a silent shadow.
def _build_index() -> dict[str, SectionVariant]:
    index: dict[str, SectionVariant] = {}
    for variant in SECTION_VARIANTS:
        if variant.id in index:
            raise ValueError(f"duplicate SectionVariant id: {variant.id!r}")
        index[variant.id] = variant
    return index


_BY_ID: dict[str, SectionVariant] = _build_index()

# The structural kinds the catalog must cover (every SectionKind except `custom`).
COVERED_KINDS: tuple[SectionKind, ...] = (
    "hero",
    "features",
    "cta",
    "list",
    "form",
    "table",
    "gallery",
    "testimonials",
    "pricing",
    "faq",
    "footer",
)


def variants_for(kind: SectionKind) -> tuple[SectionVariant, ...]:
    """All catalog variants for a given SectionKind, in catalog order."""
    return tuple(v for v in SECTION_VARIANTS if v.kind == kind)


def get_variant(variant_id: str) -> SectionVariant | None:
    """Look up a variant by its global id, or None if absent."""
    return _BY_ID.get(variant_id)


def variant_ids() -> frozenset[str]:
    """The set of all known variant ids (for referential-integrity checks)."""
    return frozenset(_BY_ID)


__all__ = [
    "COVERED_KINDS",
    "SECTION_VARIANTS",
    "SectionVariant",
    "get_variant",
    "variant_ids",
    "variants_for",
]

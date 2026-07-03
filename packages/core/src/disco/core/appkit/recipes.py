"""AppKit EPIC D1 — SiteRecipe: coherent, NON-default design systems.

A `SiteRecipe` is a complete, internally-coherent design system: a real
typography pairing, a real palette, a layout family, a component style, a density,
and a set of preferred section layouts. The five seeded recipes are deliberately
NOT the shadcn/Tailwind/"AI-purple" median — each is a distinct, opinionated look
(an editorial ledger, field notes, a civic service, an ops console, an atelier
storefront) built from real declared fonts and a hand-tuned palette.

The crucial bridge is `to_design_spec(recipe)`: it lowers a recipe into the Epic C
`DesignSpec` AND emits `justifications` keyed by the CANONICAL choice keys
(`typography.heading`, `palette.primary`, `layout.family`, ...). Because every
deliberate choice arrives pre-justified with a substantive reason, a site built
from a recipe is AUTO-JUSTIFIED: Epic D3's `design_lint` sees the intent behind
each off-default value and does not flag it as slop. The canonical key constants
defined here are the SINGLE SOURCE OF TRUTH that `design_lint` matches against —
recipes write them, the linter reads them, so the two never drift.

Layering: `disco.core` is the leaf package (.importlinter). This module imports
ONLY pydantic + the stdlib + the sibling `.spec` / `.section_catalog` (both core
leaf). No agent-server / tools / runtime imports.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .section_catalog import get_variant
from .spec import (
    DesignSpec,
    Justification,
    Palette,
    SectionKind,
    Typography,
)

_STRICT = ConfigDict(extra="forbid", frozen=True)

# ---- canonical choice keys ----------------------------------------------------
#
# THE shared vocabulary that ties a recipe's justifications to design_lint's
# suppression. A justification keyed `CHOICE_PALETTE_PRIMARY` suppresses the
# `ai_purple` finding (whose `choice_key` is the same constant); a
# `CHOICE_TYPOGRAPHY_HEADING` justification (with the declared heading font)
# suppresses `generic_font` for that font. Keep these EXACTLY in sync with the
# rule `choice_key`s in tools/builtin/design_lint.py — they are matched by string.
CHOICE_TYPOGRAPHY_HEADING = "typography.heading"
CHOICE_TYPOGRAPHY_BODY = "typography.body"
CHOICE_PALETTE_PRIMARY = "palette.primary"
CHOICE_PALETTE_SURFACE = "palette.surface"
CHOICE_PALETTE_ACCENT = "palette.accent"
CHOICE_LAYOUT_FAMILY = "layout.family"
CHOICE_COMPONENT_STYLE = "component.style"
CHOICE_DENSITY = "density"

# ---- design_lint-owned slop-rule keys -----------------------------------------
#
# These name the off-default SLOP CONDITIONS design_lint scans for. No recipe
# default trips these rules, so recipes never EMIT them (a recipe-built site
# simply doesn't trigger gradient-hero/glow/pill/emoji/motion-soup/section-shape).
# They ARE part of the canonical, suppressible vocabulary, though, so they live
# HERE as the single source of truth design_lint IMPORTS — no local copy in the
# tools layer means the linter's keys and core's set can never drift.
CHOICE_EFFECTS_GRADIENT_TEXT = "effects.gradient_text"
CHOICE_EFFECTS_GLOW = "effects.glow"
CHOICE_COMPONENT_BUTTON_RADIUS = "component.button_radius"
CHOICE_ICONS_STYLE = "icons.style"
CHOICE_MOTION_DENSITY = "motion.density"
CHOICE_LAYOUT_SECTION_SEQUENCE = "layout.section_sequence"

# The COMPLETE canonical set: the recipe-emitted choices PLUS the slop-rule keys
# design_lint owns. The single exported source of truth; design_lint's
# suppressible keys are a subset of this (a drift test enforces it).
CANONICAL_CHOICE_KEYS: frozenset[str] = frozenset(
    {
        CHOICE_TYPOGRAPHY_HEADING,
        CHOICE_TYPOGRAPHY_BODY,
        CHOICE_PALETTE_PRIMARY,
        CHOICE_PALETTE_SURFACE,
        CHOICE_PALETTE_ACCENT,
        CHOICE_LAYOUT_FAMILY,
        CHOICE_COMPONENT_STYLE,
        CHOICE_DENSITY,
        CHOICE_EFFECTS_GRADIENT_TEXT,
        CHOICE_EFFECTS_GLOW,
        CHOICE_COMPONENT_BUTTON_RADIUS,
        CHOICE_ICONS_STYLE,
        CHOICE_MOTION_DENSITY,
        CHOICE_LAYOUT_SECTION_SEQUENCE,
    }
)

# Generic / median fonts a recipe must NEVER declare — the whole point of a recipe
# is a deliberate pairing, and these are the LLM-default tells. Mirrors (a superset
# of) design_lint's `generic_font` slop list.
_GENERIC_FONTS: frozenset[str] = frozenset(
    {
        "inter",
        "geist",
        "system",
        "system-ui",
        "ui-sans-serif",
        "-apple-system",
        "blinkmacsystemfont",
        "sf pro",
        "sf pro text",
        "segoe ui",
        "arial",
        "helvetica",
        "roboto",
        "default",
    }
)

_MAX_PREFERENCES = 24
_MIN_RECIPE_REASON = 12


class SectionVariantPreference(BaseModel):
    """A recipe's preferred layout for one section kind: `(kind, variant_id)`.

    `variant_id` must name a real variant of `kind` in the section catalog — the
    cross-field validator enforces that referential integrity so a recipe can
    never prefer a layout the scaffolder doesn't know how to emit."""

    model_config = _STRICT

    kind: SectionKind
    variant_id: str

    @model_validator(mode="after")
    def _variant_exists_and_matches_kind(self) -> SectionVariantPreference:
        variant = get_variant(self.variant_id)
        if variant is None:
            raise ValueError(f"unknown section variant id: {self.variant_id!r}")
        if variant.kind != self.kind:
            raise ValueError(
                f"variant {self.variant_id!r} is a {variant.kind!r} layout, "
                f"not {self.kind!r}"
            )
        return self


class SiteRecipe(BaseModel):
    """A complete, coherent, NON-default design system.

    Strict (`extra='forbid'`, frozen, tuple collections) exactly like the Epic C
    specs: a recipe is a contract the generator trusts, so an unknown field or a
    post-validation mutation must fail loudly rather than smuggle a half-formed
    design through. `to_design_spec()` lowers it to a justified `DesignSpec`."""

    model_config = _STRICT

    id: str
    name: str
    summary: str
    # Typography — a REAL declared pairing (never Inter/Geist/system default).
    heading_font: str
    body_font: str
    # Palette — hex roles (validated by reusing the Epic C Palette model in
    # `to_design_spec`; validated here too so a bad hex fails at recipe build).
    primary: str
    surface: str
    text: str
    accent: str | None = None
    # Descriptive design vocabulary (open strings; design_lint owns semantics).
    layout_family: str
    component_style: str
    density: str
    preferred_section_variants: tuple[SectionVariantPreference, ...] = ()

    @field_validator("heading_font", "body_font")
    @classmethod
    def _real_non_default_font(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("recipe font must be a non-empty declared family")
        if stripped.lower() in _GENERIC_FONTS:
            raise ValueError(
                f"recipe font must be a deliberate, non-default family, got {value!r} "
                "(Inter/Geist/system/Arial/... are the slop defaults a recipe exists to avoid)"
            )
        return value

    @field_validator("id", "name", "summary", "layout_family", "component_style", "density")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recipe text fields must be non-empty")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> SiteRecipe:
        # Build the palette through the Epic C model so hex validation is shared
        # (one source of truth for "is this a real color").
        _ = Palette(
            primary=self.primary,
            surface=self.surface,
            text=self.text,
            accent=self.accent,
        )
        if len(self.preferred_section_variants) > _MAX_PREFERENCES:
            raise ValueError("too many preferred_section_variants")
        # At most one preference per kind (a recipe picks ONE layout per section).
        seen: set[str] = set()
        for pref in self.preferred_section_variants:
            if pref.kind in seen:
                raise ValueError(f"duplicate preferred variant for kind {pref.kind!r}")
            seen.add(pref.kind)
        return self

    def to_design_spec(self) -> DesignSpec:
        """Lower this recipe into a justified Epic C `DesignSpec`.

        Emits a substantive `Justification` for every deliberate choice, keyed by
        the canonical choice key, so a recipe-built site is auto-justified and
        `design_lint` reads intent (not slop) behind each off-default value."""
        justifications: list[Justification] = [
            Justification(
                choice=CHOICE_TYPOGRAPHY_HEADING,
                reason=(
                    f"{self.heading_font} is the deliberate display face for the "
                    f"'{self.name}' system — a non-default pairing chosen for character, "
                    "not the Inter/Geist median."
                ),
            ),
            Justification(
                choice=CHOICE_TYPOGRAPHY_BODY,
                reason=(
                    f"{self.body_font} sets running text to pair with {self.heading_font} "
                    "at comfortable reading measures; a deliberate body face, not a fallback."
                ),
            ),
            Justification(
                choice=CHOICE_PALETTE_PRIMARY,
                reason=(
                    f"{self.primary} is the brand/primary role for the '{self.name}' palette, "
                    "hand-picked to suit the subject — not a stock framework hue."
                ),
            ),
            Justification(
                choice=CHOICE_PALETTE_SURFACE,
                reason=(
                    f"{self.surface} is the page surface tuned for legible contrast with the "
                    f"{self.text} text role in this system."
                ),
            ),
            Justification(
                choice=CHOICE_LAYOUT_FAMILY,
                reason=(
                    f"the '{self.layout_family}' layout family fits the content shape of "
                    f"'{self.name}' rather than defaulting to a centered single column."
                ),
            ),
            Justification(
                choice=CHOICE_COMPONENT_STYLE,
                reason=(
                    f"components use the '{self.component_style}' treatment to match the "
                    "system's voice (a deliberate component language, not framework defaults)."
                ),
            ),
            Justification(
                choice=CHOICE_DENSITY,
                reason=(
                    f"'{self.density}' density is set to suit how much information each "
                    "section carries in this system."
                ),
            ),
        ]
        if self.accent is not None:
            justifications.append(
                Justification(
                    choice=CHOICE_PALETTE_ACCENT,
                    reason=(
                        f"{self.accent} is the accent role, chosen to punctuate the "
                        f"{self.primary} primary without becoming the generic violet accent."
                    ),
                )
            )

        return DesignSpec(
            schema_version=1,
            typography=Typography(heading_font=self.heading_font, body_font=self.body_font),
            palette=Palette(
                primary=self.primary,
                surface=self.surface,
                text=self.text,
                accent=self.accent,
            ),
            layout_family=self.layout_family,
            component_style=self.component_style,
            density=self.density,
            justifications=tuple(justifications),
        )


# ============================ the seeded recipes ===============================
#
# Five coherent, DISTINCT, non-default systems. Each uses a real Google-Fonts
# pairing and a hand-tuned palette — none is the shadcn/Tailwind/AI-purple median.

RECIPES: tuple[SiteRecipe, ...] = (
    SiteRecipe(
        id="editorial-ledger",
        name="Editorial Ledger",
        summary=(
            "A long-form editorial system: high-contrast serif display over a warm "
            "paper ground, ruled columns, and rust accents — for essays, reports, journals."
        ),
        heading_font="Fraunces",
        body_font="Newsreader",
        primary="#1f2a44",
        surface="#faf7f0",
        text="#1a1a1a",
        accent="#b4541f",
        layout_family="editorial-columns",
        component_style="underlined-flat",
        density="comfortable",
        preferred_section_variants=(
            SectionVariantPreference(kind="hero", variant_id="hero.asymmetric-editorial"),
            SectionVariantPreference(kind="features", variant_id="features.alternating-rows"),
            SectionVariantPreference(kind="list", variant_id="list.timeline-vertical"),
            SectionVariantPreference(kind="footer", variant_id="footer.multi-column-sitemap"),
        ),
    ),
    SiteRecipe(
        id="field-notes",
        name="Field Notes",
        summary=(
            "A grounded, outdoorsy notebook system: a sturdy slab-ish serif over a humanist "
            "sans, forest and clay tones on oat paper — for guides, logs, and field reports."
        ),
        heading_font="Domine",
        body_font="Karla",
        primary="#2d4739",
        surface="#f4f1ea",
        text="#20231f",
        accent="#c2703d",
        layout_family="notebook-margin",
        component_style="soft-bordered",
        density="comfortable",
        preferred_section_variants=(
            SectionVariantPreference(kind="hero", variant_id="hero.centered-stacked"),
            SectionVariantPreference(kind="list", variant_id="list.media-list"),
            SectionVariantPreference(kind="gallery", variant_id="gallery.justified-grid"),
            SectionVariantPreference(kind="faq", variant_id="faq.two-column-qa"),
        ),
    ),
    SiteRecipe(
        id="civic-service",
        name="Civic Service",
        summary=(
            "A high-legibility public-service system: a clear grotesque over a readable serif, "
            "civic blue and red at strong contrast, utility topnav — for gov, health, and docs."
        ),
        heading_font="Libre Franklin",
        body_font="Lora",
        primary="#14457b",
        surface="#ffffff",
        text="#1b1b1b",
        accent="#b51d2a",
        layout_family="topnav-utility",
        component_style="outlined",
        density="compact",
        preferred_section_variants=(
            SectionVariantPreference(kind="hero", variant_id="hero.split-media-right"),
            SectionVariantPreference(kind="form", variant_id="form.single-column-labeled"),
            SectionVariantPreference(kind="table", variant_id="table.striped-data-grid"),
            SectionVariantPreference(kind="footer", variant_id="footer.multi-column-sitemap"),
        ),
    ),
    SiteRecipe(
        id="product-ops-console",
        name="Product Ops Console",
        summary=(
            "A calm, light operational dashboard system: a geometric sans over a humanist sans, "
            "indigo and teal on a cool near-white, sidebar app shell — for tools and admin UIs."
        ),
        heading_font="Space Grotesk",
        body_font="IBM Plex Sans",
        primary="#3b5bdb",
        surface="#f5f6f8",
        text="#14161a",
        accent="#0ca678",
        layout_family="sidebar-app",
        component_style="soft-shadow",
        density="compact",
        preferred_section_variants=(
            SectionVariantPreference(kind="hero", variant_id="hero.centered-stacked"),
            SectionVariantPreference(kind="features", variant_id="features.bento-grid"),
            SectionVariantPreference(kind="table", variant_id="table.sticky-header-scroll"),
            SectionVariantPreference(kind="pricing", variant_id="pricing.comparison-matrix"),
        ),
    ),
    SiteRecipe(
        id="atelier-commerce",
        name="Atelier Commerce",
        summary=(
            "A spare luxury-retail system: a high-contrast Garamond display over a geometric "
            "sans, near-black on warm bone with a taupe-gold accent, full-bleed imagery — for "
            "fashion, objects, and editorial storefronts."
        ),
        heading_font="Cormorant Garamond",
        body_font="Jost",
        primary="#1a1a1a",
        surface="#f7f4f1",
        text="#1a1a1a",
        accent="#8a7355",
        layout_family="full-bleed-gallery",
        component_style="hairline",
        density="airy",
        preferred_section_variants=(
            SectionVariantPreference(kind="hero", variant_id="hero.full-bleed-image"),
            SectionVariantPreference(kind="gallery", variant_id="gallery.masonry"),
            SectionVariantPreference(
                kind="testimonials", variant_id="testimonials.single-spotlight"
            ),
            SectionVariantPreference(kind="footer", variant_id="footer.minimal-centered"),
        ),
    ),
)


def _build_index() -> dict[str, SiteRecipe]:
    index: dict[str, SiteRecipe] = {}
    for recipe in RECIPES:
        if recipe.id in index:
            raise ValueError(f"duplicate SiteRecipe id: {recipe.id!r}")
        index[recipe.id] = recipe
    return index


_BY_ID: dict[str, SiteRecipe] = _build_index()


def get_recipe(recipe_id: str) -> SiteRecipe | None:
    """Look up a seeded recipe by id, or None if absent."""
    return _BY_ID.get(recipe_id)


def recipe_ids() -> frozenset[str]:
    """The set of all seeded recipe ids."""
    return frozenset(_BY_ID)


__all__ = [
    "CANONICAL_CHOICE_KEYS",
    "CHOICE_COMPONENT_BUTTON_RADIUS",
    "CHOICE_COMPONENT_STYLE",
    "CHOICE_DENSITY",
    "CHOICE_EFFECTS_GLOW",
    "CHOICE_EFFECTS_GRADIENT_TEXT",
    "CHOICE_ICONS_STYLE",
    "CHOICE_LAYOUT_FAMILY",
    "CHOICE_LAYOUT_SECTION_SEQUENCE",
    "CHOICE_MOTION_DENSITY",
    "CHOICE_PALETTE_ACCENT",
    "CHOICE_PALETTE_PRIMARY",
    "CHOICE_PALETTE_SURFACE",
    "CHOICE_TYPOGRAPHY_BODY",
    "CHOICE_TYPOGRAPHY_HEADING",
    "RECIPES",
    "SectionVariantPreference",
    "SiteRecipe",
    "get_recipe",
    "recipe_ids",
]

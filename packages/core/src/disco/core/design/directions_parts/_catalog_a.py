"""Direction data table: catalog record chunk A (editorial-magazine .. dark-glass).

Extracted from ``directions`` to keep the facade module under the module
logical-line budget. Pure data/content carry — record contents (ids, labels,
copy, hex values, keywords) are unchanged and in original catalog order.
"""

from __future__ import annotations

from typing import Final

from ..directions import (
    Accent,
    DesignDirection,
    FontPairing,
    GlassRecipe,
    SurfaceTokens,
    SurfaceTreatment,
)
from ._catalog_common import (
    _EXPRESSIVE_MOTION,
    _IMAGE_GEN_PREFERRED_ART_GUIDANCE,
    _NO_MOTION,
    _SUBTLE_MOTION,
    _SVG_FIRST_ART_GUIDANCE,
    _font,
)

_CATALOG_A: Final[tuple[DesignDirection, ...]] = (
    DesignDirection(
        id="editorial-magazine",
        label="Editorial Magazine",
        summary="serif-led story pages with overlap grids, generous whitespace, and print texture",
        font_pairing=FontPairing(
            heading=_font("Newsreader", "Georgia", "serif"),
            body=_font("Schibsted Grotesk", "Avenir Next", "system-ui", "sans-serif"),
            mono=_font("IBM Plex Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#4B3F72",
        accents=(
            Accent(name="ink-plum", hex="#4B3F72"),
            Accent(name="folio-red", hex="#B54A3A"),
            Accent(name="paper-gold", hex="#D4A73D"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="6px",
                shadow_level="none",
                border_style="1px solid currentColor",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "editorial photography, halftone grain, offset print texture, dramatic crops"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="editorial",
        keywords=(
            "editorial",
            "magazine",
            "publisher",
            "newsroom",
            "story",
            "journalism",
            "longform",
            "culture",
        ),
    ),
    DesignDirection(
        id="swiss-international",
        label="Swiss International",
        summary=(
            "grid-first precision with terse type, white space, hairlines, and disciplined color"
        ),
        font_pairing=FontPairing(
            heading=_font("IBM Plex Sans", "Helvetica Neue", "system-ui", "sans-serif"),
            body=_font("IBM Plex Sans", "Helvetica Neue", "system-ui", "sans-serif"),
            mono=_font("IBM Plex Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#0D6B78",
        accents=(
            Accent(name="signal-cyan", hex="#0D6B78"),
            Accent(name="registration-red", hex="#D72638"),
            Accent(name="process-yellow", hex="#F3C623"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="flat",
            tokens=SurfaceTokens(
                radius="2px",
                shadow_level="none",
                border_style=("1px solid color-mix(in oklch, currentColor 18%, transparent)"),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "documentary product shots, strict grid crop, neutral studio light, high clarity"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="balanced",
        keywords=(
            "swiss",
            "international",
            "grid",
            "museum",
            "precision",
            "architecture",
            "systems",
            "corporate",
        ),
    ),
    DesignDirection(
        id="brutalist",
        label="Brutalist",
        summary="raw mono type, hard contrast, exposed structure, and poster-like blocks",
        font_pairing=FontPairing(
            heading=_font("Space Mono", "ui-monospace", "monospace"),
            body=_font("Space Mono", "ui-monospace", "monospace"),
            mono=_font("Space Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#C23616",
        accents=(
            Accent(name="hazard-red", hex="#C23616"),
            Accent(name="paper-white", hex="#F5F1E8"),
            Accent(name="utility-blue", hex="#0B5CAD"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="neobrutalist",
            tokens=SurfaceTokens(
                radius="0px",
                shadow_level="hard-offset",
                border_style="2px solid #111111",
            ),
        ),
        motion=_EXPRESSIVE_MOTION,
        image_art_direction=(
            "xerox poster, stark flash photography, raw edges, visible registration marks"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="dense",
        keywords=(
            "brutalist",
            "brutal",
            "raw",
            "stark",
            "poster",
            "anti-grid",
            "punk",
            "manifesto",
        ),
    ),
    DesignDirection(
        id="soft-depth-saas",
        label="Soft Depth SaaS",
        summary=(
            "calm B2B interface polish with shallow depth, rounded systems, and clear hierarchy"
        ),
        font_pairing=FontPairing(
            heading=_font("Manrope", "Avenir Next", "system-ui", "sans-serif"),
            body=_font("Source Sans 3", "system-ui", "sans-serif"),
            mono=_font("JetBrains Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2E7D66",
        accents=(
            Accent(name="trust-teal", hex="#2E7D66"),
            Accent(name="metric-blue", hex="#3B6EA8"),
            Accent(name="alert-amber", hex="#D9902F"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="soft-depth",
            tokens=SurfaceTokens(
                radius="8px",
                shadow_level="soft-2",
                border_style=("1px solid color-mix(in oklch, currentColor 12%, transparent)"),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "clean SaaS product imagery, soft shadows, quiet gradients, realistic UI detail"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="balanced",
        keywords=(
            "saas",
            "b2b",
            "dashboard",
            "analytics",
            "crm",
            "workflow",
            "productivity",
            "platform",
        ),
    ),
    DesignDirection(
        id="terminal-mono",
        label="Terminal Mono",
        summary=(
            "developer-console clarity with mono typography, "
            "low chrome, and signal-focused contrast"
        ),
        font_pairing=FontPairing(
            heading=_font("JetBrains Mono", "ui-monospace", "monospace"),
            body=_font("JetBrains Mono", "ui-monospace", "monospace"),
            mono=_font("JetBrains Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2F6F4E",
        accents=(
            Accent(name="phosphor-green", hex="#2F6F4E"),
            Accent(name="prompt-violet", hex="#7957D5"),
            Accent(name="warning-oxide", hex="#C7772E"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="4px",
                shadow_level="none",
                border_style=("1px solid color-mix(in oklch, currentColor 24%, transparent)"),
            ),
        ),
        motion=_NO_MOTION,
        image_art_direction=(
            "terminal captures, code fragments, monochrome grids, subtle scanline texture"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="compact",
        keywords=(
            "terminal",
            "code",
            "cli",
            "developer",
            "devtool",
            "infrastructure",
            "infra",
            "logs",
            "api",
        ),
    ),
    DesignDirection(
        id="luxury-serif",
        label="Luxury Serif",
        summary=(
            "high-contrast serif elegance with restrained color, "
            "tactile imagery, and spacious pacing"
        ),
        font_pairing=FontPairing(
            heading=_font("Cormorant Garamond", "Didot", "Georgia", "serif"),
            body=_font("Optima", "Avenir Next", "system-ui", "sans-serif"),
            mono=_font("IBM Plex Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#6F4E37",
        accents=(
            Accent(name="cognac", hex="#6F4E37"),
            Accent(name="antique-gold", hex="#C8A45D"),
            Accent(name="deep-rose", hex="#8E3E63"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="flat",
            tokens=SurfaceTokens(
                radius="4px",
                shadow_level="none",
                border_style=("1px solid color-mix(in oklch, currentColor 16%, transparent)"),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "luxury editorial photography, tactile materials, low-key light, refined grain"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="spacious",
        keywords=(
            "luxury",
            "premium",
            "jewelry",
            "fragrance",
            "fashion",
            "boutique",
            "maison",
            "hospitality",
        ),
    ),
    DesignDirection(
        id="playful-geometric",
        label="Playful Geometric",
        summary="rounded geometry, optimistic color, and animated friendly components",
        font_pairing=FontPairing(
            heading=_font("Space Grotesk", "Avenir Next", "system-ui", "sans-serif"),
            body=_font("Nunito Sans", "system-ui", "sans-serif"),
            mono=_font("Fira Code", "ui-monospace", "monospace"),
        ),
        palette_seed="#1E9E89",
        accents=(
            Accent(name="mint-orbit", hex="#1E9E89"),
            Accent(name="coral-pop", hex="#EE6C4D"),
            Accent(name="sun-block", hex="#F4C542"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="soft-depth",
            tokens=SurfaceTokens(
                radius="8px",
                shadow_level="soft-1",
                border_style=("1px solid color-mix(in oklch, currentColor 10%, transparent)"),
            ),
        ),
        motion=_EXPRESSIVE_MOTION,
        image_art_direction=(
            "bright geometric illustration, rounded shapes, friendly product scenes, crisp shadows"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="balanced",
        keywords=(
            "playful",
            "kids",
            "toy",
            "education",
            "learning",
            "game",
            "geometric",
            "colorful",
            "friendly",
        ),
    ),
    DesignDirection(
        id="dark-glass",
        label="Dark Glass",
        summary=(
            "near-black immersive surfaces with saturated glass, glow edges, and image-backed depth"
        ),
        font_pairing=FontPairing(
            heading=_font("Sora", "Avenir Next", "system-ui", "sans-serif"),
            body=_font("DM Sans", "system-ui", "sans-serif"),
            mono=_font("JetBrains Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2B6CB0",
        accents=(
            Accent(name="electric-blue", hex="#2B6CB0"),
            Accent(name="plasma-cyan", hex="#14B8A6"),
            Accent(name="signal-magenta", hex="#B83280"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="glass",
            tokens=SurfaceTokens(
                radius="18px",
                shadow_level="soft-large",
                border_style="mandatory 1px border-highlight",
            ),
            glass=GlassRecipe(),
        ),
        motion=_EXPRESSIVE_MOTION,
        image_art_direction=(
            "dark cinematic glass UI, saturated backdrops, luminous edges, premium contrast"
        ),
        art_guidance=_IMAGE_GEN_PREFERRED_ART_GUIDANCE,
        density="balanced",
        keywords=(
            "dark glass",
            "glass",
            "glassmorphism",
            "dark",
            "neon",
            "ai",
            "crypto",
            "command center",
            "realtime",
        ),
    ),
)

"""Direction data table: catalog record chunk C (literate-docs .. pressed-botanical).

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
    SurfaceTokens,
    SurfaceTreatment,
)
from ._catalog_common import (
    _EXPRESSIVE_MOTION,
    _IMAGE_GEN_PREFERRED_ART_GUIDANCE,
    _SUBTLE_MOTION,
    _SVG_FIRST_ART_GUIDANCE,
    _font,
)

_CATALOG_C: Final[tuple[DesignDirection, ...]] = (
    DesignDirection(
        id="literate-docs",
        label="Literate",
        summary=(
            "developer-docs reading comfort with serif body, teal links, and calm "
            "reference hierarchy"
        ),
        font_pairing=FontPairing(
            heading=_font("Fraunces", "Georgia", "serif"),
            body=_font("Source Serif 4", "Georgia", "serif"),
            mono=_font("JetBrains Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#1F5E52",
        accents=(
            Accent(name="link-teal", hex="#2F7A6F"),
            Accent(name="citation-amber", hex="#B7822E"),
            Accent(name="note-rose", hex="#B5566B"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="6px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 12%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "documentation diagrams, annotated schematics, calm serif specimen, "
            "restrained teal call-outs"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="editorial",
        keywords=(
            "documentation",
            "docs",
            "knowledge-base",
            "wiki",
            "handbook",
            "tutorial",
            "manual",
            "whitepaper",
        ),
    ),
    DesignDirection(
        id="noir-deco",
        label="Noir Deco",
        summary=(
            "dark art-deco luxe with antique gold, geometric caps, emerald and oxblood, "
            "symmetrical grandeur"
        ),
        font_pairing=FontPairing(
            heading=_font("Marcellus", "Georgia", "serif"),
            body=_font("Josefin Sans", "system-ui", "sans-serif"),
            mono=_font("Space Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#B8963F",
        accents=(
            Accent(name="deco-emerald", hex="#215E4C"),
            Accent(name="oxblood", hex="#7A2E2E"),
            Accent(name="champagne", hex="#D9C48A"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="0px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 30%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "art-deco fan motifs, gold linework on charcoal, symmetrical geometry, "
            "1920s hotel glamour"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="balanced",
        keywords=("deco", "artdeco", "gatsby", "luxe", "nightlife", "jazz", "hotel", "speakeasy"),
    ),
    DesignDirection(
        id="inkline-sketch",
        label="Inkline",
        summary=(
            "hand-drawn ink character on paper with sketch borders, warm neutrals, and "
            "friendly imperfection"
        ),
        font_pairing=FontPairing(
            heading=_font("Shantell Sans", "Comic Sans MS", "system-ui", "sans-serif"),
            body=_font("Nunito Sans", "system-ui", "sans-serif"),
            mono=_font("Space Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#26303A",
        accents=(
            Accent(name="sketch-red", hex="#C34B3E"),
            Accent(name="pencil-ochre", hex="#B98A3C"),
            Accent(name="slate", hex="#5A6470"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="10px",
                shadow_level="none",
                border_style="1.5px solid color-mix(in oklch, currentColor 40%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "hand-drawn ink illustration, loose pen strokes, cross-hatching, margin "
            "doodles, sketchbook paper texture"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="balanced",
        keywords=(
            "handdrawn",
            "sketch",
            "doodle",
            "illustrated",
            "whiteboard",
            "journal",
            "notebook",
            "playful",
        ),
    ),
    DesignDirection(
        id="controlled-maximalism",
        label="Controlled Maximalism",
        summary=(
            "dark jewel-tone maximalism, layered saturated color with disciplined grid "
            "and expressive motion"
        ),
        font_pairing=FontPairing(
            heading=_font("Bricolage Grotesque", "system-ui", "sans-serif"),
            body=_font("Albert Sans", "system-ui", "sans-serif"),
            mono=_font("Space Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#0E7C7B",
        accents=(
            Accent(name="jewel-magenta", hex="#B5297E"),
            Accent(name="gold", hex="#D6A93B"),
            Accent(name="coral", hex="#E0654A"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="soft-depth",
            tokens=SurfaceTokens(
                radius="14px",
                shadow_level="soft-2",
                border_style="1px solid color-mix(in oklch, currentColor 22%, transparent)",
            ),
        ),
        motion=_EXPRESSIVE_MOTION,
        image_art_direction=(
            "maximalist jewel-tone collage, layered saturated shapes, bold editorial "
            "energy on deep ground"
        ),
        art_guidance=_IMAGE_GEN_PREFERRED_ART_GUIDANCE,
        density="balanced",
        keywords=(
            "maximalist",
            "bold",
            "campaign",
            "expressive",
            "vibrant",
            "agency",
            "statement",
            "editorial-brand",
        ),
    ),
    DesignDirection(
        id="gradient-mesh-warm",
        label="Warm Mesh",
        summary=(
            "tasteful warm mesh-gradient landing style, terracotta to marigold, grain, "
            "generous space"
        ),
        font_pairing=FontPairing(
            heading=_font("Sora", "system-ui", "sans-serif"),
            body=_font("Be Vietnam Pro", "system-ui", "sans-serif"),
            mono=_font("JetBrains Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#C65D3B",
        accents=(
            Accent(name="sunset-coral", hex="#E0654A"),
            Accent(name="marigold", hex="#E5A93C"),
            Accent(name="plum-shadow", hex="#6B3A54"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="soft-depth",
            tokens=SurfaceTokens(
                radius="16px",
                shadow_level="soft-1",
                border_style="1px solid color-mix(in oklch, currentColor 10%, transparent)",
            ),
        ),
        motion=_EXPRESSIVE_MOTION,
        image_art_direction=(
            "controlled two-to-three-stop warm mesh gradient (terracotta, coral, "
            "marigold) with fine grain, one soft focal glow, never purple SaaS blur"
        ),
        art_guidance=_IMAGE_GEN_PREFERRED_ART_GUIDANCE,
        density="spacious",
        keywords=(
            "gradient",
            "mesh",
            "sunset",
            "warm",
            "launch",
            "hero",
            "marketing-landing",
            "vibrant",
        ),
    ),
    DesignDirection(
        id="pressed-botanical",
        label="Pressed Botanical",
        summary=(
            "organic herbarium feel with sage and clay, pressed-plant art, and "
            "unhurried editorial rhythm"
        ),
        font_pairing=FontPairing(
            heading=_font("Spectral", "Georgia", "serif"),
            body=_font("Karla", "system-ui", "sans-serif"),
            mono=_font("IBM Plex Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#4F6B47",
        accents=(
            Accent(name="clay", hex="#B26A4A"),
            Accent(name="mustard", hex="#C79A3E"),
            Accent(name="berry", hex="#8A3A4E"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="flat",
            tokens=SurfaceTokens(
                radius="8px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 14%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "pressed botanical specimens, herbarium plates, sage and clay tones, "
            "natural paper, delicate leaf detail"
        ),
        art_guidance=_IMAGE_GEN_PREFERRED_ART_GUIDANCE,
        density="editorial",
        keywords=(
            "botanical",
            "herbarium",
            "organic",
            "garden",
            "floral",
            "naturalist",
            "apothecary",
            "artisan",
        ),
    ),
)

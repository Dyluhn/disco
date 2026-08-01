"""Direction data table: catalog record chunk B (warm-craft .. lab-precise).

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
    _IMAGE_GEN_PREFERRED_ART_GUIDANCE,
    _NO_MOTION,
    _SUBTLE_MOTION,
    _SVG_FIRST_ART_GUIDANCE,
    _font,
)

_CATALOG_B: Final[tuple[DesignDirection, ...]] = (
    DesignDirection(
        id="warm-craft",
        label="Warm Craft",
        summary="human, tactile layouts with warm neutrals, crafted details, and modest motion",
        font_pairing=FontPairing(
            heading=_font("Fraunces", "Georgia", "serif"),
            body=_font("Atkinson Hyperlegible", "system-ui", "sans-serif"),
            mono=_font("IBM Plex Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#8A5A44",
        accents=(
            Accent(name="clay", hex="#8A5A44"),
            Accent(name="sage", hex="#6F8F72"),
            Accent(name="marigold", hex="#D7A13B"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="8px",
                shadow_level="soft-1",
                border_style=("1px solid color-mix(in oklch, currentColor 18%, transparent)"),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "warm natural light, hand-crafted texture, tactile materials, honest documentary scenes"
        ),
        art_guidance=_IMAGE_GEN_PREFERRED_ART_GUIDANCE,
        density="spacious",
        keywords=(
            "craft",
            "handmade",
            "local",
            "restaurant",
            "cafe",
            "bakery",
            "organic",
            "artisan",
            "earthy",
            "warm",
        ),
    ),
    DesignDirection(
        id="console-dense",
        label="Console Dense",
        summary=(
            "operations-console density with semantic status color, hairline tables, "
            "and signal-first layout"
        ),
        font_pairing=FontPairing(
            heading=_font("Geist", "system-ui", "sans-serif"),
            body=_font("Geist", "system-ui", "sans-serif"),
            mono=_font("Geist Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2D6CDF",
        accents=(
            Accent(name="healthy-green", hex="#1F9D6B"),
            Accent(name="warn-amber", hex="#D98A0B"),
            Accent(name="critical-red", hex="#D24141"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="6px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 16%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "operational dashboards, time-series charts, status tiles, muted grid, high data-ink"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="dense",
        keywords=(
            "observability",
            "monitoring",
            "telemetry",
            "ops",
            "incident",
            "metrics",
            "uptime",
            "alerting",
        ),
    ),
    DesignDirection(
        id="enterprise-navy",
        label="Institutional",
        summary=(
            "institutional B2B authority with navy structure, outlined cards, and "
            "dense records tables"
        ),
        font_pairing=FontPairing(
            heading=_font("Archivo", "Helvetica Neue", "system-ui", "sans-serif"),
            body=_font("Public Sans", "system-ui", "sans-serif"),
            mono=_font("Spline Sans Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#21406B",
        accents=(
            Accent(name="harbor-blue", hex="#2C5A94"),
            Accent(name="steel-teal", hex="#2A7D8C"),
            Accent(name="signal-amber", hex="#C08A2E"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="4px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 20%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "corporate documentary photography, boardroom neutrals, structured grids, "
            "restrained flag-blue accents"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="dense",
        keywords=(
            "enterprise",
            "institutional",
            "government",
            "compliance",
            "procurement",
            "erp",
            "records",
            "b2b",
        ),
    ),
    DesignDirection(
        id="clinical-calm",
        label="Clean Room",
        summary=(
            "calm clinical clarity with aqua accents, generous spacing, flat surfaces, "
            "and legible type"
        ),
        font_pairing=FontPairing(
            heading=_font("Lexend", "system-ui", "sans-serif"),
            body=_font("Mulish", "system-ui", "sans-serif"),
            mono=_font("IBM Plex Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2C8BA0",
        accents=(
            Accent(name="care-mint", hex="#3FA787"),
            Accent(name="periwinkle", hex="#6E8FD6"),
            Accent(name="soft-coral", hex="#E0806B"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="flat",
            tokens=SurfaceTokens(
                radius="10px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 10%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "clean healthcare imagery, soft daylight, uncluttered rooms, reassuring "
            "human warmth, no clutter"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="spacious",
        keywords=(
            "healthcare",
            "clinical",
            "medical",
            "patient",
            "telehealth",
            "care",
            "pharmacy",
            "hospital",
        ),
    ),
    DesignDirection(
        id="trust-fintech",
        label="Ledger & Copper",
        summary=(
            "fintech trust in pine and copper with soft depth, tabular numerics, and "
            "quiet confidence"
        ),
        font_pairing=FontPairing(
            heading=_font("Hanken Grotesk", "system-ui", "sans-serif"),
            body=_font("Figtree", "system-ui", "sans-serif"),
            mono=_font("Spline Sans Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#1E5F52",
        accents=(
            Accent(name="copper", hex="#B0682F"),
            Accent(name="slate", hex="#3C4A5A"),
            Accent(name="deep-pine", hex="#14463C"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="soft-depth",
            tokens=SurfaceTokens(
                radius="8px",
                shadow_level="soft-1",
                border_style="1px solid color-mix(in oklch, currentColor 12%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "financial still life, copper and evergreen tones, ledgers and coin macro, "
            "warm trustworthy light"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="balanced",
        keywords=(
            "fintech",
            "banking",
            "ledger",
            "payments",
            "invoicing",
            "treasury",
            "accounting",
            "wallet",
        ),
    ),
    DesignDirection(
        id="premium-consumer",
        label="Porcelain & Vermilion",
        summary=(
            "premium consumer minimalism on porcelain with a single vermilion strike "
            "and editorial calm"
        ),
        font_pairing=FontPairing(
            heading=_font("Instrument Sans", "system-ui", "sans-serif"),
            body=_font("Onest", "system-ui", "sans-serif"),
            mono=_font("DM Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2A2622",
        accents=(
            Accent(name="vermilion", hex="#D8452B"),
            Accent(name="brass", hex="#C79A3E"),
            Accent(name="stone", hex="#8A8078"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="flat",
            tokens=SurfaceTokens(
                radius="12px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 8%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "premium product photography, porcelain backdrops, single vermilion prop, "
            "soft studio gradient, generous negative space"
        ),
        art_guidance=_IMAGE_GEN_PREFERRED_ART_GUIDANCE,
        density="spacious",
        keywords=(
            "premium",
            "consumer",
            "lifestyle",
            "dtc",
            "boutique",
            "retail",
            "ecommerce",
            "flagship",
        ),
    ),
    DesignDirection(
        id="signal-noir",
        label="Signal Noir",
        summary=(
            "dark command-center HUD with cyan signal, hairline glow panels, and tactical density"
        ),
        font_pairing=FontPairing(
            heading=_font("Space Grotesk", "system-ui", "sans-serif"),
            body=_font("Chivo", "system-ui", "sans-serif"),
            mono=_font("JetBrains Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2BB8D6",
        accents=(
            Accent(name="signal-magenta", hex="#D6336C"),
            Accent(name="alert-amber", hex="#E0A82E"),
            Accent(name="hud-lime", hex="#8FBF3F"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="outlined",
            tokens=SurfaceTokens(
                radius="4px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 28%, transparent)",
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "dark HUD interfaces, cyan wireframe overlays, scanlines, tactical readouts, "
            "neon signal on near-black"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="compact",
        keywords=(
            "cyber",
            "hud",
            "gaming",
            "esports",
            "streaming",
            "command-center",
            "tactical",
            "nightmode",
        ),
    ),
    DesignDirection(
        id="lab-precise",
        label="Instrument",
        summary=(
            "scientific-instrument precision, monochrome with one signal orange, no "
            "motion, tight grid"
        ),
        font_pairing=FontPairing(
            heading=_font("Libre Franklin", "Helvetica Neue", "system-ui", "sans-serif"),
            body=_font("Libre Franklin", "Helvetica Neue", "system-ui", "sans-serif"),
            mono=_font("IBM Plex Mono", "ui-monospace", "monospace"),
        ),
        palette_seed="#2B2F33",
        accents=(
            Accent(name="signal-orange", hex="#C75B2A"),
            Accent(name="graphite-blue", hex="#48535E"),
            Accent(name="slate", hex="#6B7580"),
        ),
        surface_treatment=SurfaceTreatment(
            treatment="flat",
            tokens=SurfaceTokens(
                radius="2px",
                shadow_level="none",
                border_style="1px solid color-mix(in oklch, currentColor 18%, transparent)",
            ),
        ),
        motion=_NO_MOTION,
        image_art_direction=(
            "laboratory instrument close-ups, calibration marks, monochrome precision, "
            "single orange indicator"
        ),
        art_guidance=_SVG_FIRST_ART_GUIDANCE,
        density="dense",
        keywords=(
            "scientific",
            "laboratory",
            "research",
            "measurement",
            "precision",
            "calibration",
            "biotech",
            "sensor",
        ),
    ),
)

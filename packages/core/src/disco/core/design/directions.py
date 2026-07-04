"""Pure design-direction library for site/app build commitments.

The records here intentionally contain only deterministic data and pure helpers:
no time, no random module, no workspace or loop imports. C1 writes one rendered
direction contract to durable context when a build plan is approved.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DirectionId = Literal[
    "editorial-magazine",
    "swiss-international",
    "brutalist",
    "soft-depth-saas",
    "terminal-mono",
    "luxury-serif",
    "playful-geometric",
    "dark-glass",
    "warm-craft",
]
SurfaceTreatmentName = Literal["flat", "soft-depth", "glass", "neobrutalist", "outlined"]
MotionLevel = Literal["none", "subtle", "expressive"]
Density = Literal["spacious", "balanced", "dense", "compact", "editorial"]

BANNED_PRIMARY_FONTS: Final[frozenset[str]] = frozenset({"inter", "roboto", "arial"})
_HEX_PATTERN: Final[str] = r"^#[0-9A-Fa-f]{6}$"


class FontStack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str
    fallbacks: tuple[str, ...]


class FontPairing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    heading: FontStack
    body: FontStack
    mono: FontStack


class Accent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    hex: str = Field(pattern=_HEX_PATTERN)


class SurfaceTokens(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    radius: str
    shadow_level: str
    border_style: str


class GlassRecipe(BaseModel):
    """H2 glass constants. Values are exact defaults from the catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    blur_px: int = 14
    saturate_pct: int = 160
    light_fill_lane: str = "rgba(255,255,255,.08-.20)"
    dark_fill_lane: str = "rgba(17,25,40,.45-.65)"
    border_highlight_required: bool = True
    light_border_alpha_range: tuple[float, float] = (0.25, 0.35)
    dark_border_alpha_range: tuple[float, float] = (0.10, 0.15)
    shadow: str = "0 8px 32px rgba(0,0,0,.15-.30)"
    radius_px_range: tuple[int, int] = (12, 24)
    grain_base_frequency_range: tuple[float, float] = (0.6, 0.9)
    grain_opacity_range: tuple[float, float] = (0.03, 0.06)
    rulebook: tuple[str, ...] = (
        "glass only over colorful or image backdrops, never flat color",
        "never blur without saturate",
        "use one fill lane per view",
        "border highlight is mandatory",
        "soft shadow is mandatory",
        "max 3 glass layers per screen; content tables, forms, and code are never glass",
        "no glass-on-glass nesting",
        "emit fallback fills and prefers-reduced-transparency handling",
        "use a scrim under text over unpredictable imagery",
        "liquid displacement is at most one hero element with blur fallback",
        "grain is for marketing surfaces only",
        "mobile gets 3-5 blurred layers max and backdrop-filter is never animated",
    )


class SurfaceTreatment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    treatment: SurfaceTreatmentName
    tokens: SurfaceTokens
    glass: GlassRecipe | None = None

    @model_validator(mode="after")
    def _glass_matches_treatment(self) -> Self:
        if (self.treatment == "glass") != (self.glass is not None):
            raise ValueError("glass treatment must carry GlassRecipe and non-glass must not")
        return self


class DurationToken(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ms: int


class EasingToken(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    css: str


class MotionTokens(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: MotionLevel
    durations: tuple[DurationToken, ...]
    easings: tuple[EasingToken, ...]


class DesignDirection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: DirectionId
    label: str
    summary: str
    font_pairing: FontPairing
    palette_seed: str = Field(pattern=_HEX_PATTERN)
    accents: tuple[Accent, Accent, Accent]
    surface_treatment: SurfaceTreatment
    motion: MotionTokens
    image_art_direction: str
    density: Density
    keywords: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_direction_contract(self) -> Self:
        primaries = (
            self.font_pairing.heading.family,
            self.font_pairing.body.family,
            self.font_pairing.mono.family,
        )
        banned = [name for name in primaries if name.strip().lower() in BANNED_PRIMARY_FONTS]
        if banned:
            raise ValueError(f"banned primary font(s): {', '.join(banned)}")
        return self


def _font(family: str, *fallbacks: str) -> FontStack:
    return FontStack(family=family, fallbacks=fallbacks)


def _durations(*pairs: tuple[str, int]) -> tuple[DurationToken, ...]:
    return tuple(DurationToken(name=name, ms=ms) for name, ms in pairs)


def _easings(*pairs: tuple[str, str]) -> tuple[EasingToken, ...]:
    return tuple(EasingToken(name=name, css=css) for name, css in pairs)


_SUBTLE_MOTION: Final[MotionTokens] = MotionTokens(
    level="subtle",
    durations=_durations(("fast", 120), ("base", 200), ("slow", 320)),
    easings=_easings(
        ("standard", "cubic-bezier(.2,0,0,1)"),
        ("decelerate", "cubic-bezier(.16,1,.3,1)"),
    ),
)
_EXPRESSIVE_MOTION: Final[MotionTokens] = MotionTokens(
    level="expressive",
    durations=_durations(("fast", 160), ("base", 320), ("slow", 500)),
    easings=_easings(
        ("decelerate", "cubic-bezier(.16,1,.3,1)"),
        ("spring", "cubic-bezier(.34,1.56,.64,1)"),
    ),
)
_NO_MOTION: Final[MotionTokens] = MotionTokens(
    level="none",
    durations=_durations(("instant", 0), ("state", 120)),
    easings=_easings(("linear", "linear")),
)


DIRECTIONS: Final[tuple[DesignDirection, ...]] = (
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
                border_style=(
                    "1px solid color-mix(in oklch, currentColor 18%, transparent)"
                ),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "documentary product shots, strict grid crop, neutral studio light, high clarity"
        ),
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
                border_style=(
                    "1px solid color-mix(in oklch, currentColor 12%, transparent)"
                ),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "clean SaaS product imagery, soft shadows, quiet gradients, realistic UI detail"
        ),
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
                border_style=(
                    "1px solid color-mix(in oklch, currentColor 24%, transparent)"
                ),
            ),
        ),
        motion=_NO_MOTION,
        image_art_direction=(
            "terminal captures, code fragments, monochrome grids, subtle scanline texture"
        ),
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
                border_style=(
                    "1px solid color-mix(in oklch, currentColor 16%, transparent)"
                ),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "luxury editorial photography, tactile materials, low-key light, refined grain"
        ),
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
                border_style=(
                    "1px solid color-mix(in oklch, currentColor 10%, transparent)"
                ),
            ),
        ),
        motion=_EXPRESSIVE_MOTION,
        image_art_direction=(
            "bright geometric illustration, rounded shapes, friendly product scenes, crisp shadows"
        ),
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
                border_style=(
                    "1px solid color-mix(in oklch, currentColor 18%, transparent)"
                ),
            ),
        ),
        motion=_SUBTLE_MOTION,
        image_art_direction=(
            "warm natural light, hand-crafted texture, tactile materials, honest documentary scenes"
        ),
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
)

DIRECTION_IDS: Final[tuple[str, ...]] = tuple(direction.id for direction in DIRECTIONS)
DIRECTION_BY_ID: Final[dict[str, DesignDirection]] = {
    direction.id: direction for direction in DIRECTIONS
}


def _normalize(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def _keyword_score(brief_text: str, direction: DesignDirection) -> int:
    haystack = _normalize(brief_text)
    score = 0
    for keyword in direction.keywords:
        needle = _normalize(keyword)
        if needle in haystack:
            score += 3 if " " in keyword.strip() else 1
    return score


def _tie_break(direction_id: str, seed: str | int) -> int:
    digest = hashlib.sha256(f"{seed}:{direction_id}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def pick_direction(brief_text: str, seed: str | int) -> DesignDirection:
    """Pick a direction by deterministic keyword score with a seeded tiebreak."""

    return max(
        DIRECTIONS,
        key=lambda direction: (
            _keyword_score(brief_text, direction),
            _tie_break(direction.id, seed),
        ),
    )


def _format_font(label: str, stack: FontStack) -> str:
    return f"- {label}: {stack.family}; fallbacks: {', '.join(stack.fallbacks)}"


def _format_durations(tokens: tuple[DurationToken, ...]) -> str:
    return ", ".join(f"{token.name}={token.ms}ms" for token in tokens)


def _format_easings(tokens: tuple[EasingToken, ...]) -> str:
    return ", ".join(f"{token.name}={token.css}" for token in tokens)


def _format_accents(accents: tuple[Accent, Accent, Accent]) -> str:
    return ", ".join(f"{accent.name}={accent.hex}" for accent in accents)


def _append_glass_contract(lines: list[str], glass: GlassRecipe) -> None:
    radius_min, radius_max = glass.radius_px_range
    light_alpha_min, light_alpha_max = glass.light_border_alpha_range
    dark_alpha_min, dark_alpha_max = glass.dark_border_alpha_range
    lines.extend(
        [
            "- Glass defaults: "
            f"blur={glass.blur_px}px; saturate={glass.saturate_pct}%; "
            f"light fill={glass.light_fill_lane}; dark fill={glass.dark_fill_lane}",
            "- Glass edge: "
            "border-highlight is mandatory; "
            f"light alpha={light_alpha_min}-{light_alpha_max}; "
            f"dark alpha={dark_alpha_min}-{dark_alpha_max}",
            f"- Glass shadow: {glass.shadow}; radius range={radius_min}-{radius_max}px",
            "- Glass rulebook: " + "; ".join(glass.rulebook),
        ]
    )


def render_design_direction(direction: DesignDirection) -> str:
    """Render the committed direction contract as deterministic markdown."""

    surface = direction.surface_treatment
    lines = [
        f"## Design Direction: {direction.label}",
        f"- ID: {direction.id}",
        f"- Summary: {direction.summary}",
        "- Type:",
        _format_font("heading", direction.font_pairing.heading),
        _format_font("body", direction.font_pairing.body),
        _format_font("mono", direction.font_pairing.mono),
        "- Palette:",
        f"- seed={direction.palette_seed}; accents: {_format_accents(direction.accents)}",
        "- Surface:",
        f"- treatment={surface.treatment}; radius={surface.tokens.radius}; "
        f"shadow={surface.tokens.shadow_level}; border={surface.tokens.border_style}",
    ]
    if surface.glass is not None:
        _append_glass_contract(lines, surface.glass)
    lines.extend(
        [
            "- Motion:",
            "- level="
            f"{direction.motion.level}; durations: {_format_durations(direction.motion.durations)}",
            f"- easings: {_format_easings(direction.motion.easings)}",
            f"- Density: {direction.density}",
            f"- Image art direction: {direction.image_art_direction}",
            "",
            "DO:",
            "- Commit to this named direction before writing sections or components.",
            "- Derive the full palette from the seed in OKLCH-friendly tokens; "
            "keep accents named.",
            "- Use one coherent surface treatment across the project.",
            "- Keep typography, density, imagery, and motion aligned with this contract.",
            "",
            "DON'T:",
            "- Do not use Inter, Roboto, or Arial as primary fonts.",
            "- Do not use the H8 template tells: purple-blue gradient default, "
            "indigo-600 CTA, reflexive hover:scale-105, three identical feature cards, "
            "same-treatment elevated middle pricing tier, isometric-people art, "
            "flat feature lists, padded logo strips, generic FAQs, five-column footer soup, "
            "kinetic type with CLS, or verbatim identical section skeletons.",
            "- Do not invent ad-hoc hex colors, mix surface treatments, "
            "or switch direction mid-build.",
        ]
    )
    return "\n".join(lines) + "\n"

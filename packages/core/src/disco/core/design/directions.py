"""Pure design-direction library for site/app build commitments.

The records here intentionally contain only deterministic data and pure helpers:
no time, no random module, no workspace or loop imports. C1 writes one rendered
direction contract to durable context when a build plan is approved.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, Literal, Self

from disco.core.brand.css import theme_css_vars
from disco.core.brand.tokens import Theme
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
    "console-dense",
    "enterprise-navy",
    "clinical-calm",
    "trust-fintech",
    "premium-consumer",
    "signal-noir",
    "lab-precise",
    "literate-docs",
    "noir-deco",
    "inkline-sketch",
    "controlled-maximalism",
    "gradient-mesh-warm",
    "pressed-botanical",
]
SurfaceTreatmentName = Literal["flat", "soft-depth", "glass", "neobrutalist", "outlined"]
MotionLevel = Literal["none", "subtle", "expressive"]
Density = Literal["spacious", "balanced", "dense", "compact", "editorial"]

BANNED_PRIMARY_FONTS: Final[frozenset[str]] = frozenset({"inter", "roboto", "arial"})
_HEX_PATTERN: Final[str] = r"^#[0-9A-Fa-f]{6}$"
_GENERIC_FONT_FAMILIES: Final[frozenset[str]] = frozenset(
    {"serif", "sans-serif", "monospace", "system-ui", "ui-monospace", "-apple-system"}
)
_DARK_DIRECTION_IDS: Final[frozenset[str]] = frozenset(
    {"dark-glass", "terminal-mono", "signal-noir", "noir-deco", "controlled-maximalism"}
)


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
    art_guidance: str
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


# The 22 catalog records are a pure data/content table extracted to
# ``directions_parts`` to keep this facade under the module logical-line
# budget. Re-imported here so every existing
# `disco.core.design.directions.DIRECTIONS` import path keeps working.
from .directions_parts._records import DIRECTIONS  # noqa: E402

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
    digest = hashlib.sha256(f"{seed}:{direction_id}".encode()).hexdigest()
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


def direction_from_markdown(markdown: str) -> DesignDirection | None:
    """Resolve a rendered design-direction contract back to its library record."""

    match = re.search(r"(?m)^-\s*ID:\s*([a-z0-9-]+)\s*$", markdown or "")
    if not match:
        return None
    return DIRECTION_BY_ID.get(match.group(1))


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, c)) for c in rgb))


def _mix(left: str, right: str, right_weight: float) -> str:
    lr, lg, lb = _rgb(left)
    rr, rg, rb = _rgb(right)
    w = max(0.0, min(1.0, right_weight))
    return _hex(
        (
            round(lr * (1.0 - w) + rr * w),
            round(lg * (1.0 - w) + rg * w),
            round(lb * (1.0 - w) + rb * w),
        )
    )


def _font_part(family: str) -> str:
    clean = family.strip()
    if not clean:
        return clean
    if clean.lower() in _GENERIC_FONT_FAMILIES or clean.startswith(("'", '"')):
        return clean
    return f"'{clean}'" if re.search(r"\s", clean) else clean


def _font_stack_css(stack: FontStack) -> str:
    parts = (stack.family, *stack.fallbacks)
    return ",".join(part for part in (_font_part(p) for p in parts) if part)


def to_brand_tokens(direction: DesignDirection) -> Theme:
    """Map a committed design direction into the brand Theme registry shape.

    This is a pure bridge: direction tokens in, the same Theme dataclass consumed by
    ``core.brand.css`` and the PPTX/HTML deck renderers out.
    """

    seed = direction.palette_seed
    primary = direction.accents[0].hex
    support = direction.accents[1].hex
    warm = direction.accents[2].hex
    dark = direction.id in _DARK_DIRECTION_IDS

    if dark:
        bg = _mix("#050607", seed, 0.24)
        surface_1 = _mix("#0d0f13", seed, 0.26)
        surface_2 = _mix("#151922", seed, 0.30)
        hairline = _mix(surface_2, "#ffffff", 0.16)
        hairline_strong = _mix(surface_2, "#ffffff", 0.28)
        text = _mix("#f6f4ee", seed, 0.04)
        text_muted = _mix(text, bg, 0.42)
        text_faint = _mix(text, bg, 0.62)
        link = _mix(primary, "#ffffff", 0.18)
        verify_unsupported = _mix("#f07f77", primary, 0.16)
    else:
        bg = _mix("#ffffff", seed, 0.04)
        surface_1 = _mix("#ffffff", seed, 0.08)
        surface_2 = _mix("#ffffff", seed, 0.14)
        hairline = _mix("#e5e1da", seed, 0.18)
        hairline_strong = _mix("#ccc6bd", seed, 0.18)
        text = _mix("#15140f", seed, 0.08)
        text_muted = _mix(text, bg, 0.38)
        text_faint = _mix(text, bg, 0.58)
        link = _mix(primary, "#111111", 0.12)
        verify_unsupported = _mix("#b14e49", primary, 0.12)

    fonts = direction.font_pairing
    return Theme(
        name=f"direction-{direction.id}",
        mode="dark" if dark else "light",
        bg=bg,
        surface_1=surface_1,
        surface_2=surface_2,
        hairline=hairline,
        hairline_strong=hairline_strong,
        text=text,
        text_muted=text_muted,
        text_faint=text_faint,
        accent=primary.lower(),
        link=link,
        verify_supported=support.lower(),
        verify_weak=warm.lower(),
        verify_unsupported=verify_unsupported,
        warn=warm.lower(),
        font_display=_font_stack_css(fonts.heading),
        font_ui=_font_stack_css(fonts.body),
        font_reading=_font_stack_css(fonts.body),
        font_mono=_font_stack_css(fonts.mono),
        branded=True,
    )


def direction_tokens_css(direction: DesignDirection) -> str:
    """Emit a ready-to-use ``:root{…}`` tokens.css for a committed direction.

    Pure composition of the two existing bridges — map the direction into the brand
    ``Theme`` (``to_brand_tokens``) and render it as CSS custom properties
    (``theme_css_vars``) — with a one-line header comment naming the direction id so
    the emitted file is self-identifying. Deterministic: same direction in → byte-
    identical CSS out. No new token logic lives here.
    """

    header = f"/* disco direction tokens — {direction.id} */\n"
    return header + theme_css_vars(to_brand_tokens(direction))


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
            "- Art guidance:",
            f"- {direction.art_guidance}",
            "",
            "DO:",
            "- Commit to this named direction before writing sections or components.",
            "- Tailor the theme and design tokens to THIS direction before generating any "
            "component — never ship default shadcn / component-library styling.",
            "- Derive the full palette from the seed in OKLCH-friendly tokens; keep accents named.",
            "- A ready-to-use tokens file exists at .disco/context/direction_tokens.css; "
            "import or copy those CSS variables instead of hand-picking values.",
            "- Use one coherent surface treatment across the project.",
            "- Keep typography, density, imagery, and motion aligned with this contract.",
            "",
            "DON'T:",
            "- Do not use emoji anywhere in the site — not as icons, not in "
            "headings, buttons, or copy. Use real SVG icons or plain text; emoji "
            "glyphs read as placeholder-grade design (the lint flags them, but "
            "don't write them in the first place).",
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

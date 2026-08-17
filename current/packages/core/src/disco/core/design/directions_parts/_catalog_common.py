"""Direction data table: shared builder helpers + motion/art-guidance constants.

Extracted from ``directions`` alongside the catalog record chunks in this
package. Pure carry of the builder helpers and constants shared by every
`DesignDirection` record — content is unchanged.
"""

from __future__ import annotations

from typing import Final

from ..directions import DurationToken, EasingToken, FontStack, MotionTokens


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

_SVG_FIRST_ART_GUIDANCE: Final[str] = (
    "SVG-FIRST: Draw bespoke inline <svg> illustration for hero scenes, spot icons, "
    "dividers, textures, and background fields using this direction's palette tokens. "
    'Keep SVGs viewBox-scaled and decorative SVGs aria-hidden="true"; never use '
    "external stock URLs."
)
_IMAGE_GEN_PREFERRED_ART_GUIDANCE: Final[str] = (
    "IMAGE-GEN-PREFERRED: For hero or photographic slots, call image_generate when "
    "configured; if it fails or is unavailable, immediately draw a bespoke inline "
    "<svg> in this direction's palette instead. Spot icons, dividers, and texture "
    "marks stay SVG; never use external stock URLs."
)

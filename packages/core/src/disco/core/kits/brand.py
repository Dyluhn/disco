"""Brand kit (P7) — an AppKit PROJECTION over the existing brand theme engine.

The platform already has a real brand system: ``disco.core.brand`` (Theme + THEMES +
resolve_theme), the same tokens that drive report/deck exports. AppKit uses a SMALLER
token vocabulary ({primary, accent, bg, fg, font}); this projects a brand theme into
those keys so a build's brand stays consistent with the rest of Disco — instead of a
parallel, drifting token catalog. Pure; reuses disco.core.brand.
"""

from __future__ import annotations

from ..brand import THEMES, Theme, resolve_theme

# The brand names available to project (the registry keys of disco.core.brand.THEMES).
BRAND_NAMES: frozenset[str] = frozenset(name for name, _mode in THEMES)


def brand_to_appkit_tokens(theme: Theme) -> dict[str, str]:
    """Project a disco.core.brand Theme into AppKit design tokens. The brand's accent is
    the primary action color; bg/text become the canvas/ink; the UI font stack drives
    type. Keys match appkit.DEFAULT_DESIGN exactly so app_set_design can apply them."""
    return {
        "primary": theme.accent,
        "accent": theme.accent,
        "bg": theme.bg,
        "fg": theme.text,
        "font": theme.font_ui,
    }


def appkit_brand(name: str, mode: str = "light") -> dict[str, str]:
    """The AppKit design tokens for a named brand (e.g. 'disco', 'neutral', 'ink').
    Raises ValueError on an unknown brand (reuses resolve_theme's validation)."""
    return brand_to_appkit_tokens(resolve_theme(name, mode))

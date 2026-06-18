"""Brand token registry — Theme dataclass + the three canonical themes.

Values are ported verbatim from the signed-off evidence CSS:
  docs/evidence/_brand_common.css:14-22  (Disco light)
  docs/evidence/_brand_dark.css:15-23    (Disco dark)

Verify/warn hex values are derived from theme.css:68-71 (light) and
theme.css:87-90 (dark) via the same oklch→sRGB pipeline used to produce the
mockups (pure-Python, no external deps).  Neutral uses greyscale, system
fonts, branded=False.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    """One resolved brand theme (a snapshot of all tokens + font stacks).

    Fields mirror the CSS custom properties in theme.css and the evidence CSS
    so consumers can build a ``--var: value`` block directly.  All colour
    fields are lower-case 7-char CSS hex strings (``#rrggbb``).
    """

    name: str           # registry key, e.g. "disco"
    mode: str           # "light" | "dark"

    # ---- backgrounds / surfaces ----
    bg: str
    surface_1: str
    surface_2: str

    # ---- borders ----
    hairline: str
    hairline_strong: str

    # ---- text ----
    text: str
    text_muted: str
    text_faint: str

    # ---- chroma (rationed — only meaningful elements) ----
    accent: str
    link: str

    # ---- verification chips (NLI grounding) ----
    verify_supported: str
    verify_weak: str
    verify_unsupported: str
    warn: str

    # ---- font stacks (CSS font-family values) ----
    font_display: str
    font_ui: str
    font_reading: str
    font_mono: str

    # ---- identity flag ----
    branded: bool  # False → neutral: no accent chroma, no mark on exports


# ---------------------------------------------------------------------------
# Disco light  ← _brand_common.css:14-22 (oklch→sRGB already resolved there)
# ---------------------------------------------------------------------------
DISCO_LIGHT = Theme(
    name="disco",
    mode="light",
    # from _brand_common.css:15-18
    bg="#fcfcfa",
    surface_1="#f7f7f4",
    surface_2="#f1f0ed",
    hairline="#dfdedb",
    hairline_strong="#cbcac7",
    # from _brand_common.css:19-21
    text="#1a1813",
    text_muted="#5a5853",
    text_faint="#878682",
    # from _brand_common.css:22-23
    accent="#4077a3",
    link="#39688e",
    # oklch(0.52 0.09 155), oklch(0.62 0.1 80), oklch(0.55 0.13 25)
    # via docs/evidence/_brand_common.css pipeline (light, theme.css:68-71)
    verify_supported="#397852",
    verify_weak="#a67f38",
    verify_unsupported="#b14e49",
    warn="#b14e49",
    # from _brand_common.css:24-26
    font_display="'Fraunces',Georgia,serif",
    font_ui="'Schibsted Grotesk',system-ui,sans-serif",
    font_reading="'Newsreader',Georgia,serif",
    font_mono="ui-monospace,Menlo,'Cascadia Code',monospace",
    branded=True,
)

# ---------------------------------------------------------------------------
# Disco dark  ← _brand_dark.css:15-23 (accent shifts to #79c0f1)
# ---------------------------------------------------------------------------
DISCO_DARK = Theme(
    name="disco",
    mode="dark",
    # from _brand_dark.css:16-19
    bg="#0e0f12",
    surface_1="#16171a",
    surface_2="#1e2124",
    hairline="#303337",
    hairline_strong="#4a4d53",
    # from _brand_dark.css:20-22
    text="#e5e8ec",
    text_muted="#9b9fa3",
    text_faint="#727579",
    # from _brand_dark.css:23-24
    accent="#79c0f1",
    link="#74b3de",
    # oklch(0.74 0.1 155), oklch(0.8 0.11 85), oklch(0.72 0.14 25)
    # via docs/evidence/_brand_dark.css pipeline (dark, theme.css:87-90)
    verify_supported="#75be8f",
    verify_weak="#deb866",
    verify_unsupported="#f07f77",
    warn="#f07f77",
    # same font stacks — variable fonts + fallbacks
    font_display="'Fraunces',Georgia,serif",
    font_ui="'Schibsted Grotesk',system-ui,sans-serif",
    font_reading="'Newsreader',Georgia,serif",
    font_mono="ui-monospace,Menlo,'Cascadia Code',monospace",
    branded=True,
)

# ---------------------------------------------------------------------------
# Neutral light — system fonts, ink-on-white, no accent, branded=False
# ---------------------------------------------------------------------------
_NEUTRAL_TEXT = "#1a1a1a"
_NEUTRAL_TEXT_MUTED = "#555555"

NEUTRAL_LIGHT = Theme(
    name="neutral",
    mode="light",
    bg="#ffffff",
    surface_1="#f5f5f5",
    surface_2="#ebebeb",
    hairline="#d0d0d0",
    hairline_strong="#b8b8b8",
    text=_NEUTRAL_TEXT,
    text_muted=_NEUTRAL_TEXT_MUTED,
    text_faint="#888888",
    # No chroma — accent and link collapse to text-muted (greyscale)
    accent=_NEUTRAL_TEXT_MUTED,
    link=_NEUTRAL_TEXT_MUTED,
    verify_supported="#4a7a5a",
    verify_weak="#7a6030",
    verify_unsupported="#7a3a3a",
    warn="#7a3a3a",
    font_display="Georgia,'Times New Roman',serif",
    font_ui="system-ui,-apple-system,sans-serif",
    font_reading="Georgia,'Times New Roman',serif",
    font_mono="ui-monospace,Menlo,monospace",
    branded=False,
)

# ---------------------------------------------------------------------------
# Neutral dark — system fonts, dark canvas, no accent, branded=False
# ---------------------------------------------------------------------------
_NEUTRAL_DARK_TEXT = "#e8e8e8"
_NEUTRAL_DARK_TEXT_MUTED = "#a0a0a0"

NEUTRAL_DARK = Theme(
    name="neutral",
    mode="dark",
    bg="#121212",
    surface_1="#1e1e1e",
    surface_2="#282828",
    hairline="#383838",
    hairline_strong="#505050",
    text=_NEUTRAL_DARK_TEXT,
    text_muted=_NEUTRAL_DARK_TEXT_MUTED,
    text_faint="#707070",
    accent=_NEUTRAL_DARK_TEXT_MUTED,
    link=_NEUTRAL_DARK_TEXT_MUTED,
    verify_supported="#80c090",
    verify_weak="#c0a060",
    verify_unsupported="#d08080",
    warn="#d08080",
    font_display="Georgia,'Times New Roman',serif",
    font_ui="system-ui,-apple-system,sans-serif",
    font_reading="Georgia,'Times New Roman',serif",
    font_mono="ui-monospace,Menlo,monospace",
    branded=False,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

THEMES: dict[tuple[str, str], Theme] = {
    ("disco", "light"): DISCO_LIGHT,
    ("disco", "dark"): DISCO_DARK,
    ("neutral", "light"): NEUTRAL_LIGHT,
    ("neutral", "dark"): NEUTRAL_DARK,
}


def resolve_theme(name: str, mode: str = "light") -> Theme:
    """Return the Theme for ``name`` / ``mode``.

    ``name`` is case-folded; ``mode`` defaults to ``"light"``.
    Raises ``ValueError`` for an unknown ``name`` (caller converts to HTTP 400).
    Falls back to the light variant when the requested mode is not registered
    (e.g. a theme that only exists in light).
    """
    key = (name.lower(), mode.lower())
    if key in THEMES:
        return THEMES[key]
    # Try fallback to light
    light_key = (name.lower(), "light")
    if light_key in THEMES:
        return THEMES[light_key]
    raise ValueError(
        f"Unknown theme {name!r}. Valid themes: "
        + ", ".join(sorted({k[0] for k in THEMES}))
    )

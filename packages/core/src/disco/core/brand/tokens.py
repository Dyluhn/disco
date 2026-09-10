"""Brand token registry — Theme dataclass + the three canonical themes.

Values are ported verbatim from the signed-off evidence CSS:
  development/notes/evidence/_brand_common.css:14-22  (Disco light)
  development/notes/evidence/_brand_dark.css:15-23    (Disco dark)

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

    name: str  # registry key, e.g. "disco"
    mode: str  # "light" | "dark"

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
    # via development/notes/evidence/_brand_common.css pipeline (light, theme.css:68-71)
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
    # via development/notes/evidence/_brand_dark.css pipeline (dark, theme.css:87-90)
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

# ===========================================================================
# Template variants — spins on the Disco editorial style. Each keeps the
# bundled font families (so they render offline in WeasyPrint) and the same
# rationed-chroma structure; the MOOD changes via palette + font pairing.
# All are branded=True (the wordmark + colophon ride them on exports).
# ===========================================================================

# ---- Ink — stark monochrome print, a single oxblood accent ----------------
INK_LIGHT = Theme(
    name="ink",
    mode="light",
    bg="#ffffff",
    surface_1="#f5f4f2",
    surface_2="#eae8e4",
    hairline="#ddd9d3",
    hairline_strong="#c3beb6",
    text="#15140f",
    text_muted="#47453f",
    text_faint="#7c7a73",
    accent="#9d2b2b",
    link="#872525",
    verify_supported="#2f6b45",
    verify_weak="#8f6a2c",
    verify_unsupported="#9d2b2b",
    warn="#9d2b2b",
    font_display="'Fraunces',Georgia,serif",
    font_ui="'Schibsted Grotesk',system-ui,sans-serif",
    font_reading="'Newsreader',Georgia,serif",
    font_mono="ui-monospace,Menlo,'Cascadia Code',monospace",
    branded=True,
)

# ---- Sepia — warm archival parchment, terracotta accent -------------------
SEPIA_LIGHT = Theme(
    name="sepia",
    mode="light",
    bg="#f7f1e3",
    surface_1="#f1ead8",
    surface_2="#e8dfc8",
    hairline="#d9cfb4",
    hairline_strong="#c5b894",
    text="#2c2417",
    text_muted="#5d513c",
    text_faint="#897c62",
    accent="#a85d2e",
    link="#8f4f28",
    verify_supported="#5a6b3a",
    verify_weak="#9a7430",
    verify_unsupported="#a8472e",
    warn="#a8472e",
    font_display="'Fraunces',Georgia,serif",
    font_ui="'Schibsted Grotesk',system-ui,sans-serif",
    font_reading="'Newsreader',Georgia,serif",
    font_mono="ui-monospace,Menlo,'Cascadia Code',monospace",
    branded=True,
)

# ---- Signal — modern sans (Schibsted display), cobalt accent --------------
SIGNAL_LIGHT = Theme(
    name="signal",
    mode="light",
    bg="#fbfcfd",
    surface_1="#f2f5f9",
    surface_2="#e8edf4",
    hairline="#d8dee7",
    hairline_strong="#c0c8d4",
    text="#13161c",
    text_muted="#4a515c",
    text_faint="#7c838f",
    accent="#2f5fd0",
    link="#2950bb",
    verify_supported="#2f7d54",
    verify_weak="#a07a30",
    verify_unsupported="#c0473f",
    warn="#c0473f",
    # The spin: a sans DISPLAY face (not Fraunces) for a clean, technical voice;
    # the reading face stays serif for long-form legibility.
    font_display="'Schibsted Grotesk',system-ui,sans-serif",
    font_ui="'Schibsted Grotesk',system-ui,sans-serif",
    font_reading="'Newsreader',Georgia,serif",
    font_mono="ui-monospace,Menlo,'Cascadia Code',monospace",
    branded=True,
)

# ---- Midnight — deep navy dark canvas, warm ink, amber accent -------------
MIDNIGHT_DARK = Theme(
    name="midnight",
    mode="dark",
    bg="#0d1017",
    surface_1="#151926",
    surface_2="#1d2233",
    hairline="#2c3346",
    hairline_strong="#454e66",
    text="#e9e6df",
    text_muted="#9b9890",
    text_faint="#6e6c66",
    accent="#d9a441",
    link="#c89a45",
    verify_supported="#76c08c",
    verify_weak="#d9b566",
    verify_unsupported="#ef8076",
    warn="#ef8076",
    font_display="'Fraunces',Georgia,serif",
    font_ui="'Schibsted Grotesk',system-ui,sans-serif",
    font_reading="'Newsreader',Georgia,serif",
    font_mono="ui-monospace,Menlo,'Cascadia Code',monospace",
    branded=True,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

THEMES: dict[tuple[str, str], Theme] = {
    ("disco", "light"): DISCO_LIGHT,
    ("disco", "dark"): DISCO_DARK,
    ("ink", "light"): INK_LIGHT,
    ("sepia", "light"): SEPIA_LIGHT,
    ("signal", "light"): SIGNAL_LIGHT,
    ("midnight", "dark"): MIDNIGHT_DARK,
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
        f"Unknown theme {name!r}. Valid themes: " + ", ".join(sorted({k[0] for k in THEMES}))
    )


# ---------------------------------------------------------------------------
# Template catalogue — the user-facing gallery surfaced in both export flows
# (DR-report PDF + slide deck). `id` is the "{name}-{mode}" selector value;
# `accent`/`bg` drive a swatch chip. Order = display order; the first entry
# with default=True is the pre-selected template.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateInfo:
    id: str  # selector value, "{name}-{mode}"
    name: str  # registry name
    mode: str  # "light" | "dark"
    label: str  # short UI label
    description: str  # one-line mood description
    accent: str  # swatch chroma (hex)
    bg: str  # swatch canvas (hex)
    default: bool


def _tpl(theme: Theme, label: str, description: str, *, default: bool = False) -> TemplateInfo:
    return TemplateInfo(
        id=f"{theme.name}-{theme.mode}",
        name=theme.name,
        mode=theme.mode,
        label=label,
        description=description,
        accent=theme.accent,
        bg=theme.bg,
        default=default,
    )


TEMPLATE_CATALOG: list[TemplateInfo] = [
    _tpl(
        DISCO_LIGHT,
        "Disco",
        "Warm editorial — Fraunces serif on paper-white, blue accent.",
        default=True,
    ),
    _tpl(INK_LIGHT, "Ink", "Stark monochrome print — near-black on white, oxblood accent."),
    _tpl(SEPIA_LIGHT, "Sepia", "Warm archival — parchment + terracotta, vintage-academic."),
    _tpl(
        SIGNAL_LIGHT, "Signal", "Modern sans — Schibsted display, cobalt accent, clean & technical."
    ),
    _tpl(DISCO_DARK, "Disco Dark", "The default, inverted — dark canvas, sky-blue accent."),
    _tpl(MIDNIGHT_DARK, "Midnight", "Editorial dark — deep navy, warm ink, amber accent."),
    _tpl(NEUTRAL_LIGHT, "Neutral", "Unbranded greyscale, system fonts — no marks."),
]


_VALID_TEMPLATE_IDS: frozenset[str] = frozenset(t.id for t in TEMPLATE_CATALOG)


def list_templates() -> list[TemplateInfo]:
    """The export-template gallery (shared by the PDF + slide-deck export UIs)."""
    return TEMPLATE_CATALOG


def is_valid_template(template_id: str) -> bool:
    """True iff ``template_id`` ("{name}-{mode}") is a GALLERY template. Stricter
    than resolve_theme, which light-falls-back an unknown mode (so e.g. "ink-dark"
    would silently render light) — export routes must reject non-catalogue ids with
    a 400 rather than serve a surprising theme."""
    return template_id.strip().lower() in _VALID_TEMPLATE_IDS


def parse_template_id(template_id: str) -> tuple[str, str]:
    """Split a "{name}-{mode}" template id → (name, mode). A bare name (e.g.
    "neutral") defaults to light. Generalizes the old deck `_parse_theme`."""
    tid = template_id.strip().lower()
    if "-" in tid:
        name, _, mode = tid.rpartition("-")
        if mode in ("light", "dark"):
            return name, mode
    return tid, "light"

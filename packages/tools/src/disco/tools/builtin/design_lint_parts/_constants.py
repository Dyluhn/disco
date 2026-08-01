"""Curated slop constants + scan/regex config shared across the `design_lint`
rule families. Extracted from `design_lint.py` verbatim (values unchanged) —
this is data, not logic, so the split here is purely about module size, not
behavior. The parent facade (`design_lint.py`) re-imports every name unchanged.
"""

from __future__ import annotations

import re

from disco.core.appkit import MAX_DESIGNSPEC_BYTES

# Median/default fonts — the LLM tell. Matched only in a FONT context (see rule).
_GENERIC_FONTS: frozenset[str] = frozenset(
    {
        "inter",
        "geist",
        "system-ui",
        "ui-sans-serif",
        "-apple-system",
        "blinkmacsystemfont",
        "sf pro",
        "sf pro text",
        "sf pro display",
        "segoe ui",
        "arial",
        "helvetica",
        "helvetica neue",
        "roboto",
    }
)

# The #7c3aed violet family (+ the indigo it's usually paired with). Hex only;
# a couple of rgb() spellings of the canonical purples too.
_AI_PURPLE_HEXES: frozenset[str] = frozenset(
    {
        "#7c3aed",
        "#8b5cf6",
        "#6d28d9",
        "#5b21b6",
        "#a855f7",
        "#9333ea",
        "#7e22ce",
        "#c084fc",
        "#6366f1",
        "#818cf8",
        "#4f46e5",
        "#4338ca",
        "#a78bfa",
    }
)
_AI_PURPLE_RGB: frozenset[str] = frozenset(
    {
        "rgb(124,58,237)",
        "rgb(139,92,246)",
        "rgb(99,102,241)",
        "rgb(168,85,247)",
    }
)

# Neon accents for the dark-neon-glow tell.
_NEON_HEXES: frozenset[str] = frozenset(
    {
        "#39ff14",
        "#00ff00",
        "#0f0",
        "#00ffff",
        "#0ff",
        "#ff00ff",
        "#f0f",
        "#fe53bb",
        "#08f7fe",
        "#ff2079",
        "#00f5d4",
        "#f5d300",
        "#ff073a",
        "#bc13fe",
        "#00ffea",
    }
)

# Pill (fully-rounded) radius values.
_PILL_RADII: tuple[str, ...] = ("9999px", "999px", "9999rem", "100vmax", "100vh", "50%")

_BUTTON_SELECTOR_TOKENS: tuple[str, ...] = (
    "button",
    ".btn",
    ".button",
    "[type=submit]",
    "[type='submit']",
    "btn",
)

_HEADING_SELECTOR_TOKENS: tuple[str, ...] = (
    "h1",
    "h2",
    "hero",
    "headline",
    "banner",
    "masthead",
    "display",
    "title",
)

# Broad emoji ranges (pictographs, symbols, transport, dingbats, supplemental).
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f0ff\U00002b00-\U00002bff]"
)

_CSS_BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b")
_FONT_CONTEXT_RE = re.compile(r"font-family|font\s*:|--font|family=|googleapis\.com/css", re.I)
_SECTION_OPEN_RE = re.compile(r"<section\b(?P<attrs>[^>]*)>", re.I | re.DOTALL)
_STYLE_ATTR_RE = re.compile(r"\bstyle\s*=\s*(['\"])(?P<style>.*?)\1", re.I | re.DOTALL)
_CLASS_ATTR_RE = re.compile(r"\bclass\s*=\s*(['\"])(?P<class>.*?)\1", re.I | re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|template)\b[^>]*>.*?</\1\s*>", re.I | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?")
_FONT_SIZE_DECL_RE = re.compile(
    r"font-size\s*:\s*(?P<value>[0-9]*\.?[0-9]+)\s*(?P<unit>px|pt|vw|rem|em)?\b",
    re.I,
)
_LINE_HEIGHT_DECL_RE = re.compile(r"line-height:\s*(\d*\.?\d+)\s*;", re.I)
_BACKDROP_DECL_RE = re.compile(
    r"(?:-webkit-)?backdrop-filter\s*:\s*(?P<value>[^;{}]+)",
    re.I,
)
_BACKGROUND_DECL_RE = re.compile(
    r"(?:background|background-color|background-image)\s*:\s*(?P<value>[^;{}]+)",
    re.I,
)

_ANIMATION_THRESHOLD = 12  # transition/animation declarations over this = soup
_MAX_FONT_FAMILIES = 3
_MIN_FONT_SIZE_PX = 14.0
_MAX_BRAND_COLORS = 6
_COLOR_SATURATION_FLOOR = 60
_COLOR_MERGE_DISTANCE = 40.0
_MIN_LINE_HEIGHT = 1.2
_EMOJI_THRESHOLD = 3  # this many emoji glyphs in markup = emoji-as-icons
_DECK_TEXT_WORD_LIMIT = 90
_DECK_BULLET_LIMIT = 6
_DECK_TYPE_FLOOR_PX = 24.0
_DECK_BASE_WIDTH_PX = 1920.0
_CHOICE_DECK_TYPE_FLOOR = "deck.type_floor"
_CHOICE_DECK_TEXT_BUDGET = "deck.text_budget"
_CHOICE_DECK_ARCHETYPE_MONOTONY = "deck.archetype_monotony"
_CHOICE_DECK_IMAGERY = "deck.imagery"
_CHOICE_DECK_GLASS = "deck.glass"
_CHOICE_DECK_ORPHAN_SLIDE = "deck.orphan_slide"
_CHOICE_DECK_HANDWRITTEN_HTML = "deck.handwritten_html"
_CHOICE_WEB_TEXT_OVER_IMAGE = "web.text_over_image"
_CHOICE_WEB_HOVER_A11Y = "web.hover_a11y"
_CHOICE_WEB_HOVER_SCALE = "web.hover_scale"
_CHOICE_WEB_GLASS = "web.glass"
_CHOICE_WEB_DEFAULT_HIDDEN_CONTENT = "web.default_hidden_content"
_CHOICE_WEB_FONT_COUNT = "web.font_count"
_CHOICE_WEB_FONT_SIZE = "web.font_size"
_CHOICE_WEB_COLOR_COUNT = "web.color_count"
_CHOICE_WEB_LINE_HEIGHT = "web.line_height"
_DECK_PIPELINE_GENERATOR_MARKER = "disco-slides-generate:pipeline-html"

_WEB_BANNED_DEFAULT_FONTS: frozenset[str] = frozenset({"inter", "roboto", "arial"})
_WEB_HEADING_SELECTOR_RE = re.compile(
    r"(^|[\s,>+~])(?:body|h[1-6])(?:$|[\s,>+~:#.\[])|"
    r"(?:hero|headline|heading|masthead|display|title)",
    re.I,
)
_WEB_INTERACTIVE_SELECTOR_RE = re.compile(
    r"(^|[\s,>+~])(?:a|button|input|select|textarea|summary)(?:$|[\s,>+~:#.\[])|"
    r"\.(?:btn|button|cta)\b|\[role=['\"]?button|\[tabindex",
    re.I,
)
_WEB_MEDIA_CONTAINER_RE = re.compile(
    r"<(?P<tag>section|header|div|article)\b(?P<attrs>[^>]*)>"
    r"(?P<body>.*?)</(?P=tag)\s*>",
    re.I | re.DOTALL,
)
_WEB_TEXT_TAG_RE = re.compile(r"<\s*(?:h[1-6]|p|a|span|strong|em)\b", re.I)
_WEB_HEROISH_RE = re.compile(
    r"\b(?:hero|masthead|banner|cover|full-bleed|relative|absolute)\b", re.I
)
_WEB_HIDDEN_DECL_RE = re.compile(
    r"(?:opacity\s*:\s*(?:0|0\.0+)\s*(?:!important\s*)?(?:;|$)|"
    r"visibility\s*:\s*hidden\s*(?:!important\s*)?(?:;|$))",
    re.I,
)
_WEB_JS_GATED_SELECTOR_RE = re.compile(
    r"(^|[,\s>+~])(?:html\.js|\.js(?:[\s>+~.]|$)|\.js-enabled\b|"
    r"\.js-gated\b|\.has-js\b)",
    re.I,
)
_WEB_CONTENT_SELECTOR_RE = re.compile(
    r"(^|[,\s>+~])(?:section|div)(?:$|[\s>+~.#:\[])|"
    r"\.(?:section|content|copy|text|hero|panel|card|feature|tile|block|"
    r"reveal|scroll-reveal|fade|fade-up|split|stack|grid)\b",
    re.I,
)
_WEB_HEADING_TARGET_RE = re.compile(
    r"(^|[\s>+~])h[1-6](?:$|[\s>+~:#.\[])|"
    r"(?:hero|headline|heading|masthead|display|title)",
    re.I,
)

# Generic fallbacks that are NEVER an intentional brand font choice — a
# `direction_font_mismatch` must stay quiet for these (a `system-ui`/`serif`
# fallback AFTER the chosen family is legitimate, not a contradiction).
_DIRECTION_GENERIC_FONTS: frozenset[str] = frozenset(
    {
        "serif",
        "sans-serif",
        "monospace",
        "system-ui",
        "-apple-system",
        "blinkmacsystemfont",
        "cursive",
        "fantasy",
        "inherit",
        "initial",
        "unset",
        "revert",
        "currentcolor",
    }
)

# Off-palette color thresholds (RGB space; the diagonal is ~441). A brand color
# must be BOTH saturated (a real hue — not a neutral/grey or a tint/shade toward
# white/black) AND far from EVERY committed color before it counts as drift.
# Conservative on purpose: near-matches, tints and shades legitimately vary, so
# the rule stays quiet rather than false-positive (severity is `info`).
_DIRECTION_MIN_SATURATION = 60
_DIRECTION_DRIFT_MIN_DISTANCE = 120.0

# Extensions split by how we scan them.
_CSS_EXTS: frozenset[str] = frozenset({".css", ".scss", ".sass", ".less"})
_MARKUP_EXTS: frozenset[str] = frozenset(
    {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".astro", ".js", ".ts"}
)
SCAN_EXTS: frozenset[str] = _CSS_EXTS | _MARKUP_EXTS

# Directories never worth scanning (deps/build output/vcs/the .disco spec dir).
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".astro",
        "out",
        "coverage",
        "__pycache__",
        "vendor",
        ".disco",
        ".cache",
        ".vercel",
    }
)

# Walk / read caps so a hostile or huge workspace can't blow up the scan.
_MAX_FILES = 400
_MAX_DEPTH = 12
_MAX_FILE_BYTES = 512 * 1024
# Source files cap; the designspec cap is core's (`MAX_DESIGNSPEC_BYTES`) so the
# pre-read probe bounds against the SAME limit the loader enforces post-read.
_MAX_SPEC_BYTES = MAX_DESIGNSPEC_BYTES
# Small ceiling for the in-sandbox `wc -c` size probe — it only stats one file.
_SIZE_PROBE_TIMEOUT_S = 10

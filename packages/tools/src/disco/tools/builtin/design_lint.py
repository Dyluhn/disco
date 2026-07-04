"""`design_lint` — the AST-light slop scanner (AppKit EPIC D3).

A generated web app betrays its LLM origin through a small, recurring set of
visual tells: Inter/Geist everywhere, an AI-purple primary, gradient-clipped hero
text, dark-neon-glow surfaces, pill-button monoculture, emoji-as-icons, animation
soup, and the centered-hero / three-cards / CTA section sequence. `design_lint`
SCANS the generated workspace (CSS + HTML/JSX/Vue/Svelte) plus the workspace's
`.disco/designspec.json` and returns a STRUCTURED verdict: a ranked list of
findings, each `{rule_id, severity, path, line, evidence, choice_key, message}`.

Two deliberate design choices:

* AST-light. No CSS/JS parser dependency (P0): rules are regex + string scanning
  over curated slop constants. Best-effort and conservative — a rule that can't be
  sure stays quiet rather than firing a false positive.
* Justification-aware, VALUE-AWARE SUPPRESSION. An off-default value is NOT
  automatically slop — it is slop only when it is UNJUSTIFIED. A finding is
  suppressed when `.disco/designspec.json` carries a SUBSTANTIVE justification for
  the finding's canonical `choice_key` (the same keys `recipes.to_design_spec`
  emits) AND — for the rules whose `choice_key` maps to a concrete DesignSpec
  field — the spec's ACTUAL field value equals the FLAGGED value. So justifying
  `palette.primary=#000000` does NOT excuse a `#7c3aed` finding, and justifying a
  Roboto heading does NOT excuse an Inter one: the justified value must BE the
  thing the implementation uses. A missing or invalid DesignSpec suppresses
  NOTHING — you cannot claim intent without the spec — so the relevant rules fire.

The pure engine (`lint_design`) takes an in-memory `{path: text}` map + an
optional `DesignSpec`; the `DesignLintTool` is a thin, READ-ONLY sandbox wrapper
that walks the workspace, loads the spec, and calls it. Read-only by contract so
Epic G can use it as a verification probe and re-linting is non-productive work.

Layering: tools -> core is allowed; this imports `DesignSpec` + the canonical
choice keys from `disco.core.appkit` (the single source of truth recipes write).
"""

from __future__ import annotations

import html as html_lib
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from disco.core import SecurityRisk
from disco.core.appkit import (
    CHOICE_COMPONENT_BUTTON_RADIUS,
    CHOICE_EFFECTS_GLOW,
    CHOICE_EFFECTS_GRADIENT_TEXT,
    CHOICE_ICONS_STYLE,
    CHOICE_LAYOUT_SECTION_SEQUENCE,
    CHOICE_MOTION_DENSITY,
    CHOICE_PALETTE_ACCENT,
    CHOICE_PALETTE_PRIMARY,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
    MAX_DESIGNSPEC_BYTES,
    load_design_spec_from_bytes,
)
from disco.core.appkit.spec import DesignSpec
from pydantic import BaseModel, ConfigDict, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

if TYPE_CHECKING:
    from ..sandbox.base import SandboxInstance

# The canonical choice keys design_lint can SUPPRESS a finding for. EVERY one is
# defined in core (`disco.core.appkit`) and imported above — there is NO local
# copy, so the linter's vocabulary and core's exported `CANONICAL_CHOICE_KEYS`
# can never drift (a test asserts this set is a subset of core's). Two of these
# keys map to a concrete DesignSpec FIELD whose value is matched against the
# flagged value (palette.primary/accent -> ai_purple; typography.heading/body ->
# generic_font); the rest name binary slop conditions with no spec field, so a
# substantive justification keyed to them is the suppression granularity.
SUPPRESSIBLE_CHOICE_KEYS: frozenset[str] = frozenset(
    {
        CHOICE_PALETTE_PRIMARY,
        CHOICE_PALETTE_ACCENT,
        CHOICE_TYPOGRAPHY_HEADING,
        CHOICE_TYPOGRAPHY_BODY,
        CHOICE_EFFECTS_GRADIENT_TEXT,
        CHOICE_EFFECTS_GLOW,
        CHOICE_COMPONENT_BUTTON_RADIUS,
        CHOICE_ICONS_STYLE,
        CHOICE_MOTION_DENSITY,
        CHOICE_LAYOUT_SECTION_SEQUENCE,
    }
)

_MIN_REASON_LEN = 12  # mirrors spec.Justification._reason_substantive

# ---- curated slop constants ---------------------------------------------------

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
    'btn',
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
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f000-\U0001f0ff"
    "\U00002b00-\U00002bff"
    "]"
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
_BACKDROP_DECL_RE = re.compile(
    r"(?:-webkit-)?backdrop-filter\s*:\s*(?P<value>[^;{}]+)",
    re.I,
)
_BACKGROUND_DECL_RE = re.compile(
    r"(?:background|background-color|background-image)\s*:\s*(?P<value>[^;{}]+)",
    re.I,
)

_ANIMATION_THRESHOLD = 12  # transition/animation declarations over this = soup
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
_CHOICE_WEB_TEXT_OVER_IMAGE = "web.text_over_image"
_CHOICE_WEB_HOVER_A11Y = "web.hover_a11y"
_CHOICE_WEB_HOVER_SCALE = "web.hover_scale"
_CHOICE_WEB_GLASS = "web.glass"

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
_WEB_HEROISH_RE = re.compile(r"\b(?:hero|masthead|banner|cover|full-bleed|relative|absolute)\b", re.I)

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


async def _bounded_read(
    sandbox: SandboxInstance, path: str, cap: int
) -> bytes | None:
    """Read a file ONLY if it is within `cap` bytes — and prove that BEFORE pulling
    any bytes into this process. We stat the size in-sandbox (`wc -c < file` streams
    the file through the guest's own `wc`; nothing lands in Python memory) and refuse
    to `read_file` an over-cap (or unmeasurable) file. A multi-GB hostile file is
    therefore never a memory/time bomb here, regardless of any post-read length check.

    Returns the file bytes when readable and within `cap`; None when the size can't
    be determined (non-zero exit / unparseable) or exceeds `cap` — the caller treats
    None as absent/oversized (a source file is skipped; the designspec is treated as
    present-but-invalid, so rules fire and nothing is suppressed)."""
    try:
        res = await sandbox.exec_shell(
            f"wc -c < {shlex.quote(path)}", timeout_s=_SIZE_PROBE_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001 — probe failed → treat as unmeasurable
        return None
    if res.exit_code != 0 or res.timed_out:
        return None
    try:
        size = int(res.stdout.strip())
    except (ValueError, AttributeError):
        return None
    if size < 0 or size > cap:
        return None
    try:
        return await sandbox.read_file(path)
    except Exception:  # noqa: BLE001 — vanished/unreadable between probe and read
        return None


# ---- finding model ------------------------------------------------------------


class DesignFinding(BaseModel):
    """One structured slop finding. `choice_key` is the canonical key whose
    justification (in `.disco/designspec.json`) would suppress it."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    severity: str  # "error" | "warning" | "info"
    path: str
    line: int
    evidence: str
    choice_key: str
    message: str


# rule_id -> (severity, rank-priority). Lower priority sorts first within a
# severity. Severity dominates the sort (error < warning < info).
_RULE_META: dict[str, tuple[str, int]] = {
    "deck_type_floor": ("error", 5),
    "ai_purple": ("error", 10),
    "generic_font": ("warning", 20),
    "gradient_hero_text": ("warning", 30),
    "dark_neon_glow": ("warning", 40),
    "deck_text_budget": ("warning", 45),
    "deck_bullet_monotony": ("warning", 46),
    "deck_missing_imagery": ("warning", 47),
    "deck_glass_missing_saturate": ("warning", 48),
    "deck_glass_flat_backdrop": ("warning", 49),
    "web_banned_default_font": ("warning", 50),
    "web_reflexive_hover_scale": ("warning", 51),
    "web_hover_only_interactivity": ("warning", 52),
    "web_text_over_image_no_scrim": ("warning", 53),
    "web_glass_missing_saturate": ("warning", 54),
    "web_glass_flat_backdrop": ("warning", 55),
    "centered_hero_3_cards_cta": ("warning", 50),
    "too_many_animations": ("info", 60),
    "pill_button_monoculture": ("info", 70),
    "emoji_as_icons": ("info", 80),
}
_SEVERITY_RANK: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


# ---- small scanning helpers ---------------------------------------------------


def _line_of(text: str, offset: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, max(offset, 0)) + 1


def _ext_of(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def _luminance(hexstr: str) -> float | None:
    """Relative luminance (0..1) of a #rgb/#rrggbb color, or None if unparseable."""
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        r = int(h[0:2], 16) / 255
        g = int(h[2:4], 16) / 255
        b = int(h[4:6], 16) / 255
    except ValueError:
        return None
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _norm_color(raw: str) -> str:
    """Lowercase + strip whitespace inside rgb()/hex so spellings collapse."""
    return re.sub(r"\s+", "", raw.strip().lower())


def _resolve_rgb(raw: str) -> tuple[int, int, int] | None:
    """Resolve a `#rgb`/`#rrggbb`/`rgb(r,g,b)` color to an (r,g,b) int tuple, or
    None if unparseable. Resolving to RGB lets a hex field value and an `rgb()`
    flagged value compare as the SAME color so suppression is spelling-agnostic
    (the DesignSpec palette is hex-only; the implementation may write either)."""
    s = _norm_color(raw)
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return None
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            return None
    m = re.match(r"rgba?\((\d+),(\d+),(\d+)", s)
    if m is not None:
        try:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class _DeckSlide:
    """One slide section found in a generated HTML deck."""

    number: int
    line: int
    attrs: str
    html: str
    open_start: int
    html_start: int


def _attr_value(attrs: str, name: str) -> str | None:
    m = re.search(
        rf"\b{re.escape(name)}\s*=\s*(['\"])(?P<value>.*?)\1",
        attrs,
        re.I | re.DOTALL,
    )
    return m.group("value") if m is not None else None


def _has_attr(attrs: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\s*=", attrs, re.I) is not None


def _class_tokens(raw: str) -> set[str]:
    return {token.strip().lower() for token in re.split(r"\s+", raw) if token.strip()}


def _has_class(attrs: str, token: str) -> bool:
    classes = _attr_value(attrs, "class")
    return classes is not None and token.lower() in _class_tokens(classes)


def _has_deck_wrapper(text: str) -> bool:
    return any("deck" in _class_tokens(m.group("class")) for m in _CLASS_ATTR_RE.finditer(text))


def _is_deck_section_candidate(attrs: str) -> bool:
    return _has_attr(attrs, "data-slide-id") or _has_attr(attrs, "data-label") or _has_class(
        attrs, "slide"
    )


def _extract_deck_slides(text: str) -> list[_DeckSlide]:
    """Return slide sections only when the markup looks like a deck artifact.

    The actual deck renderer stamps `<div class="deck">` and
    `<section class="slide" data-slide-id=... data-layout=...>`. Some artifact
    paths use `<section data-label=...>` instead, so repeated data-label sections
    are also accepted as deck-like. Ordinary web pages with a single labelled
    section stay out of the deck rule pack.
    """
    candidates: list[tuple[re.Match[str], str, int, int]] = []
    strong_markers = 0
    data_label_markers = 0
    for m in _SECTION_OPEN_RE.finditer(text):
        attrs = m.group("attrs") or ""
        if not _is_deck_section_candidate(attrs):
            continue
        close = re.search(r"</section\s*>", text[m.end() :], re.I)
        if close is None:
            continue
        body_start = m.end()
        body_end = m.end() + close.start()
        candidates.append((m, attrs, body_start, body_end))
        if _has_attr(attrs, "data-slide-id") or _has_class(attrs, "slide"):
            strong_markers += 1
        if _has_attr(attrs, "data-label"):
            data_label_markers += 1

    if not candidates:
        return []
    if not (_has_deck_wrapper(text) or strong_markers > 0 or data_label_markers >= 2):
        return []

    slides: list[_DeckSlide] = []
    for i, (m, attrs, body_start, body_end) in enumerate(candidates, 1):
        slides.append(
            _DeckSlide(
                number=i,
                line=_line_of(text, m.start()),
                attrs=attrs,
                html=text[body_start:body_end],
                open_start=m.start(),
                html_start=body_start,
            )
        )
    return slides


def _visible_text(fragment: str) -> str:
    without_invisible = _SCRIPT_STYLE_RE.sub(" ", fragment)
    without_comments = _COMMENT_RE.sub(" ", without_invisible)
    without_tags = _TAG_RE.sub(" ", without_comments)
    return re.sub(r"\s+", " ", html_lib.unescape(without_tags)).strip()


def _word_count(fragment: str) -> int:
    return len(_WORD_RE.findall(_visible_text(fragment)))


def _bullet_count(fragment: str) -> int:
    return len(re.findall(r"<li\b", fragment, re.I))


def _font_size_px(value: str, unit: str | None) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    u = (unit or "px").lower()
    if u == "px":
        return number
    if u == "pt":
        return number * (96.0 / 72.0)
    if u == "vw":
        return number * (_DECK_BASE_WIDTH_PX / 100.0)
    if u in {"rem", "em"}:
        return number * 16.0
    return None


def _style_attrs(slide: _DeckSlide) -> list[tuple[str, int]]:
    styles: list[tuple[str, int]] = []
    for m in _STYLE_ATTR_RE.finditer(slide.attrs):
        styles.append((m.group("style"), slide.open_start + m.start()))
    for m in _STYLE_ATTR_RE.finditer(slide.html):
        styles.append((m.group("style"), slide.html_start + m.start()))
    return styles


def _deck_image_count(slides: list[_DeckSlide]) -> int:
    count = 0
    for slide in slides:
        raw = f"{slide.attrs}\n{slide.html}"
        count += len(re.findall(r"<\s*(?:img|picture|source)\b", raw, re.I))
        count += len(re.findall(r"\bbackground(?:-image)?\s*:[^;{}]*url\(", raw, re.I))
    return count


def _has_colorful_backdrop(raw: str) -> bool:
    return re.search(
        r"(?:linear|radial|conic)-gradient|url\(|<\s*(?:img|picture|video)\b",
        raw,
        re.I,
    ) is not None


def _is_flat_background_value(value: str) -> bool:
    low = value.strip().lower()
    if not low or any(tok in low for tok in ("gradient(", "url(", "image-set(")):
        return False
    if low.startswith("var("):
        return False
    if re.search(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?\b|rgba?\(|hsla?\(|oklch\(|oklab\(", low):
        return True
    named = re.fullmatch(r"[a-z]+", low)
    return named is not None and low not in {"none", "transparent", "inherit", "initial"}


def _style_has_flat_background(style: str) -> bool:
    return any(_is_flat_background_value(m.group("value")) for m in _BACKGROUND_DECL_RE.finditer(style))


def _slide_has_flat_backdrop(slide: _DeckSlide) -> bool:
    return any(_style_has_flat_background(m.group("style")) for m in _STYLE_ATTR_RE.finditer(slide.attrs))


def _global_deck_backdrop_is_flat(text: str) -> bool:
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).lower()
        if not any(tok in selector for tok in (".slide", ".deck", "body")):
            continue
        body = m.group(2)
        if _has_colorful_backdrop(body):
            return False
        if _style_has_flat_background(body):
            return True
    return False


def _backdrop_without_saturate(style: str) -> str | None:
    for m in _BACKDROP_DECL_RE.finditer(style):
        value = m.group("value")
        if "saturate(" not in value.lower():
            return m.group(0).strip()
    return None


def _style_has_backdrop_filter(style: str) -> bool:
    return _BACKDROP_DECL_RE.search(style) is not None


# ---- suppression --------------------------------------------------------------


def _normalize_choice(raw: str) -> str:
    """Canonicalize a justification's `choice` so a recipe-written key and a
    hand-written one collapse. Lowercase, drop an optional `=value` suffix (the
    field+value form `palette.primary=#7c3aed`), and unify space/slash separators
    to '.'. Underscores are PRESERVED — they're meaningful inside the canonical
    keys design_lint owns (`effects.gradient_text`, `component.button_radius`,
    `layout.section_sequence`), so each constant normalizes to itself."""
    s = raw.strip().lower().split("=", 1)[0].strip()
    s = s.replace(" ", "").replace("/", ".")
    return s


def _justified_keys(design_spec: DesignSpec | None) -> set[str]:
    """The set of canonical choice keys SUBSTANTIVELY justified by the spec.

    A justification only counts if its reason clears the substantive-length bar
    (the same bar the DesignSpec schema enforces — re-checked here so a raw,
    hand-edited spec that slipped a stub reason past loading can't suppress)."""
    if design_spec is None:
        return set()
    out: set[str] = set()
    for j in design_spec.justifications:
        if len((j.reason or "").strip()) >= _MIN_REASON_LEN:
            out.add(_normalize_choice(j.choice))
    return out


def _font_justified(design_spec: DesignSpec | None, token: str, justified: set[str]) -> bool:
    """generic_font is suppressed only by a FIELD+VALUE match: the spec must
    actually DECLARE that exact font in heading/body AND justify the matching
    typography key. Declaring Inter without saying why never suppresses."""
    if design_spec is None:
        return False
    t = token.strip().lower()
    typ = design_spec.typography
    if t == typ.heading_font.strip().lower() and CHOICE_TYPOGRAPHY_HEADING in justified:
        return True
    if t == typ.body_font.strip().lower() and CHOICE_TYPOGRAPHY_BODY in justified:
        return True
    return False


# ---- the rules ----------------------------------------------------------------


def _rule_generic_font(
    path: str, text: str, design_spec: DesignSpec | None, justified: set[str]
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    seen: set[str] = set()
    body_font = (
        design_spec.typography.body_font.strip().lower() if design_spec is not None else ""
    )
    for lineno, line in enumerate(text.splitlines(), 1):
        if not _FONT_CONTEXT_RE.search(line):
            continue
        # Only the PRIMARY (chosen) family is the tell — fallbacks like
        # `system-ui`/`sans-serif` after the real font are legitimate and must
        # not be flagged. Collect the chosen family from each declaration.
        for token in _primary_families(line):
            if token in seen or token not in _GENERIC_FONTS:
                continue
            seen.add(token)
            if _font_justified(design_spec, token, justified):
                continue
            choice = CHOICE_TYPOGRAPHY_BODY if token == body_font else CHOICE_TYPOGRAPHY_HEADING
            findings.append(
                DesignFinding(
                    rule_id="generic_font",
                    severity="warning",
                    path=path,
                    line=lineno,
                    evidence=f"font '{token}' in: {line.strip()[:140]}",
                    choice_key=choice,
                    message=(
                        f"Generic/default font '{token}' as the primary family — the LLM-median "
                        "tell. Declare a deliberate family in .disco/designspec.json and justify "
                        "it, or pick a real pairing (see SiteRecipe)."
                    ),
                )
            )
    return findings


def _rule_web_banned_default_font(
    path: str, text: str, design_spec: DesignSpec | None, justified: set[str]
) -> list[DesignFinding]:
    """Web-specific default-font tell: Inter/Roboto/Arial as the first family on
    body or heading rules. This is narrower than `generic_font`, which also covers
    app-wide variables/imports and broader system stacks."""
    findings: list[DesignFinding] = []
    seen: set[tuple[str, str]] = set()
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        if not _selector_targets_body_or_heading(selector):
            continue
        body = m.group(2)
        for fm in re.finditer(r"font-family\s*:\s*([^;{}]+)", body, re.I):
            token = _first_font_family(fm.group(1))
            if token not in _WEB_BANNED_DEFAULT_FONTS:
                continue
            key = (selector.lower(), token)
            if key in seen:
                continue
            seen.add(key)
            if _font_justified(design_spec, token, justified):
                continue
            choice = (
                CHOICE_TYPOGRAPHY_BODY
                if re.search(r"(^|[\s,>+~])body(?:$|[\s,>+~:#.\[])", selector, re.I)
                else CHOICE_TYPOGRAPHY_HEADING
            )
            findings.append(
                DesignFinding(
                    rule_id="web_banned_default_font",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start(2) + fm.start()),
                    evidence=f"{selector} {{ font-family: {fm.group(1).strip()} }}",
                    choice_key=choice,
                    message=(
                        f"Web default font '{token}' is the first family on a body/heading "
                        "rule. Commit a deliberate font pairing or justify the exact brand font."
                    ),
                )
            )
    return findings


def _primary_families(line: str) -> list[str]:
    """The CHOSEN (primary) font families declared on a line — the first family
    in each `font-family`/`--font*` declaration and every family imported via a
    Google-Fonts `family=` URL. Fallbacks after the primary are intentionally
    excluded (a `system-ui` fallback is not slop)."""
    low = line.lower()
    out: list[str] = []

    def _first(value: str) -> str:
        return value.split(",", 1)[0].strip().strip("'\"")

    for m in re.finditer(r"font-family\s*:\s*([^;{}]+)", low):
        out.append(_first(m.group(1)))
    for m in re.finditer(r"--[a-z0-9-]*font[a-z0-9-]*\s*:\s*([^;{}]+)", low):
        out.append(_first(m.group(1)))
    for m in re.finditer(r"family=([^&\"'<>;:]+)", low):
        for fam in re.split(r"[|]|&family=", m.group(1)):
            out.append(fam.split(":", 1)[0].replace("+", " ").strip())
    return [f for f in out if f]


def _first_font_family(value: str) -> str:
    return value.split(",", 1)[0].strip().strip("'\"").lower()


def _selector_targets_body_or_heading(selector: str) -> bool:
    return _WEB_HEADING_SELECTOR_RE.search(selector) is not None


def _selector_targets_interactive(selector: str) -> bool:
    return _WEB_INTERACTIVE_SELECTOR_RE.search(selector) is not None


def _class_has_hover_utility(raw: str) -> bool:
    return any(token.startswith("hover:") or ":hover:" in token for token in raw.split())


def _class_has_focus_visible_utility(raw: str) -> bool:
    return any(token.startswith("focus-visible:") or ":focus-visible:" in token for token in raw.split())


def _has_scrim_or_overlay(raw: str) -> bool:
    low = raw.lower()
    return any(
        token in low
        for token in (
            "linear-gradient",
            "radial-gradient",
            "conic-gradient",
            "scrim",
            "overlay",
            "rgba(",
            "hsla(",
            "color-mix(",
            "::before",
            "::after",
            "bg-black/",
            "bg-white/",
            "inset-0",
            "absolute inset",
        )
    )


def _has_background_image_url(raw: str) -> bool:
    return re.search(r"background(?:-image)?\s*:[^;{}]*url\(", raw, re.I) is not None


def _justified_palette_rgbs(
    design_spec: DesignSpec | None, justified: set[str]
) -> set[tuple[int, int, int]]:
    """The resolved (r,g,b) of each palette ROLE the spec DECLARES *and*
    justifies. A purple finding is suppressed only if its color IS one of these —
    i.e. the spec's own primary/accent value is that exact violet. Justifying one
    color can never excuse a DIFFERENT one."""
    if design_spec is None:
        return set()
    out: set[tuple[int, int, int]] = set()
    pal = design_spec.palette
    if CHOICE_PALETTE_PRIMARY in justified:
        rgb = _resolve_rgb(pal.primary)
        if rgb is not None:
            out.add(rgb)
    if CHOICE_PALETTE_ACCENT in justified and pal.accent is not None:
        rgb = _resolve_rgb(pal.accent)
        if rgb is not None:
            out.add(rgb)
    return out


def _rule_ai_purple(
    path: str, text: str, design_spec: DesignSpec | None, justified: set[str]
) -> list[DesignFinding]:
    # VALUE-AWARE suppression: a flagged violet is suppressed ONLY when a
    # justified palette role (primary/accent) actually declares THAT exact color.
    justified_rgbs = _justified_palette_rgbs(design_spec, justified)
    findings: list[DesignFinding] = []
    low = text.lower()
    seen: set[str] = set()
    for m in re.finditer(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?|rgb\([^)]*\)", low):
        val = _norm_color(m.group(0))
        if val in seen:
            continue
        if val in _AI_PURPLE_HEXES or val in _AI_PURPLE_RGB:
            seen.add(val)
            flagged_rgb = _resolve_rgb(val)
            if flagged_rgb is not None and flagged_rgb in justified_rgbs:
                continue  # the spec declares + justifies THIS exact violet
            findings.append(
                DesignFinding(
                    rule_id="ai_purple",
                    severity="error",
                    path=path,
                    line=_line_of(low, m.start()),
                    evidence=m.group(0),
                    choice_key=CHOICE_PALETTE_PRIMARY,
                    message=(
                        f"AI-purple primary ({m.group(0)}) — the single most common generated "
                        "site tell. Pick a palette that suits the subject, or justify "
                        "palette.primary in .disco/designspec.json if the violet is intended."
                    ),
                )
            )
    return findings


def _rule_gradient_hero_text(
    path: str, text: str, justified: set[str]
) -> list[DesignFinding]:
    if CHOICE_EFFECTS_GRADIENT_TEXT in justified:
        return []
    findings: list[DesignFinding] = []
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).lower()
        body = m.group(2).lower()
        if not any(tok in selector for tok in _HEADING_SELECTOR_TOKENS):
            continue
        clipped = ("background-clip" in body and "text" in body) or (
            "-webkit-background-clip" in body and "text" in body
        )
        gradient = (
            "linear-gradient" in body
            or "conic-gradient" in body
            or "radial-gradient" in body
        )
        if clipped and gradient:
            findings.append(
                DesignFinding(
                    rule_id="gradient_hero_text",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=m.group(1).strip()[:120],
                    choice_key=CHOICE_EFFECTS_GRADIENT_TEXT,
                    message=(
                        "Gradient-clipped hero text (background-clip:text over a gradient on a "
                        "heading) — a generated-site cliche. Use a solid type treatment, or "
                        "justify effects.gradient_text in .disco/designspec.json."
                    ),
                )
            )
    return findings


def _rule_dark_neon_glow(
    path: str, text: str, justified: set[str]
) -> list[DesignFinding]:
    if CHOICE_EFFECTS_GLOW in justified:
        return []
    low = text.lower()
    # dark surface: a low-luminance hex on a root/body/app surface declaration
    dark = False
    _dark_re = r"(background[^;{}]*?|--bg[^;:]*?|--background[^;:]*?):[^;{}]*?(#[0-9a-f]{3,6})"
    for bm in re.finditer(_dark_re, low):
        lum = _luminance(bm.group(2))
        if lum is not None and lum < 0.22:
            dark = True
            break
    if not dark:
        return []
    # neon accent present
    neon_match: re.Match[str] | None = None
    for nm in re.finditer(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?", low):
        if _norm_color(nm.group(0)) in _NEON_HEXES:
            neon_match = nm
            break
    if neon_match is None:
        return []
    # glow: a shadow/blur effect
    glow = bool(
        re.search(r"box-shadow|text-shadow|drop-shadow|filter\s*:[^;{}]*blur", low)
    )
    if not glow:
        return []
    return [
        DesignFinding(
            rule_id="dark_neon_glow",
            severity="warning",
            path=path,
            line=_line_of(low, neon_match.start()),
            evidence=neon_match.group(0),
            choice_key=CHOICE_EFFECTS_GLOW,
            message=(
                "Dark surface + neon accent + glow — the 'cyberpunk SaaS' generated look. Tone the "
                "accent and drop the glow, or justify effects.glow in .disco/designspec.json."
            ),
        )
    ]


def _rule_too_many_animations(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_MOTION_DENSITY in justified:
        return []
    low = text.lower()
    count = len(re.findall(r"\b(transition|animation)\s*:", low))
    count += len(re.findall(r"@keyframes\b", low))
    count += len(re.findall(r"\banimate-[a-z]", low))  # tailwind animate utilities
    if count <= _ANIMATION_THRESHOLD:
        return []
    return [
        DesignFinding(
            rule_id="too_many_animations",
            severity="info",
            path=path,
            line=1,
            evidence=f"{count} animation/transition declarations",
            choice_key=CHOICE_MOTION_DENSITY,
            message=(
                f"{count} animation/transition declarations (> {_ANIMATION_THRESHOLD}) — motion "
                "soup. Reserve motion for a few intentional moments, or justify motion.density."
            ),
        )
    ]


def _rule_web_reflexive_hover_scale(path: str, text: str) -> list[DesignFinding]:
    selectors: dict[str, int] = {}
    low = text.lower()
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        body = m.group(2)
        if ":hover" not in selector.lower():
            continue
        if re.search(r"\btransform\s*:[^;{}]*scale(?:3d|x|y)?\(", body, re.I):
            for part in selector.split(","):
                hover_selector = part.strip()
                if ":hover" in hover_selector.lower():
                    selectors.setdefault(hover_selector, _line_of(text, m.start()))

    for m in re.finditer(r"\bclass\s*=\s*(['\"])(?P<class>.*?)\1", text, re.I | re.DOTALL):
        classes = m.group("class")
        hover_scale = [token for token in classes.split() if token.startswith("hover:scale")]
        if not hover_scale:
            continue
        selectors.setdefault(" ".join(sorted(hover_scale)), _line_of(text, m.start()))

    if len(selectors) <= 3:
        return []
    first_selector, first_line = next(iter(selectors.items()))
    return [
        DesignFinding(
            rule_id="web_reflexive_hover_scale",
            severity="warning",
            path=path,
            line=first_line,
            evidence=f"{len(selectors)} hover scale selectors; first: {first_selector[:100]}",
            choice_key=_CHOICE_WEB_HOVER_SCALE,
            message=(
                "Hover scale appears on more than three distinct selectors — the reflexive "
                "template interaction tell. Reserve scale for one or two meaningful affordances."
            ),
        )
    ]


def _rule_web_hover_only_interactivity(
    path: str, text: str, *, workspace_has_focus_visible: bool
) -> list[DesignFinding]:
    if workspace_has_focus_visible:
        return []
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        if ":hover" not in selector.lower() or not _selector_targets_interactive(selector):
            continue
        return [
            DesignFinding(
                rule_id="web_hover_only_interactivity",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=selector[:120],
                choice_key=_CHOICE_WEB_HOVER_A11Y,
                message=(
                    "Interactive elements have hover styling but the workspace has no "
                    ":focus-visible state. Add keyboard-visible focus treatment."
                ),
            )
        ]

    for m in re.finditer(
        r"<(?P<tag>a|button|input|select|textarea|summary)\b(?P<attrs>[^>]*)>",
        text,
        re.I | re.DOTALL,
    ):
        attrs = m.group("attrs")
        classes = _attr_value(attrs, "class") or ""
        if not _class_has_hover_utility(classes):
            continue
        return [
            DesignFinding(
                rule_id="web_hover_only_interactivity",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=f"<{m.group('tag')} class=\"{classes[:90]}\">",
                choice_key=_CHOICE_WEB_HOVER_A11Y,
                message=(
                    "Interactive elements have hover utility classes but the workspace has no "
                    "focus-visible state. Add keyboard-visible focus treatment."
                ),
            )
        ]
    return []


def _rule_web_text_over_image_no_scrim(path: str, text: str) -> list[DesignFinding]:
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        body = m.group(2)
        if not _has_background_image_url(body):
            continue
        if not _selector_targets_body_or_heading(selector) and _WEB_HEROISH_RE.search(selector) is None:
            continue
        if _has_scrim_or_overlay(body):
            continue
        return [
            DesignFinding(
                rule_id="web_text_over_image_no_scrim",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=f"{selector} uses background image without gradient/overlay",
                choice_key=_CHOICE_WEB_TEXT_OVER_IMAGE,
                message=(
                    "Text appears intended over imagery without a scrim/overlay heuristic. Add "
                    "a gradient scrim, overlay layer, card, or blur protection and verify contrast."
                ),
            )
        ]

    for m in _WEB_MEDIA_CONTAINER_RE.finditer(text):
        attrs = m.group("attrs") or ""
        body = m.group("body") or ""
        raw = f"{attrs}\n{body}"
        styled_background = _has_background_image_url(attrs)
        layered_image = "<img" in body.lower() and _WEB_TEXT_TAG_RE.search(body) is not None
        if not styled_background and not layered_image:
            continue
        if _WEB_TEXT_TAG_RE.search(body) is None:
            continue
        if layered_image and _WEB_HEROISH_RE.search(raw) is None:
            continue
        if _has_scrim_or_overlay(raw):
            continue
        return [
            DesignFinding(
                rule_id="web_text_over_image_no_scrim",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=f"<{m.group('tag')}{attrs[:100]}>",
                choice_key=_CHOICE_WEB_TEXT_OVER_IMAGE,
                message=(
                    "Text appears over background/image media without a scrim/overlay sibling. "
                    "Add a protection layer and verify worst-case contrast."
                ),
            )
        ]
    return []


def _rule_web_glass(path: str, text: str) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    glass_uses: list[tuple[int, str]] = []

    for m in _CSS_BLOCK_RE.finditer(text):
        body = m.group(2)
        if not _style_has_backdrop_filter(body):
            continue
        evidence = _backdrop_without_saturate(body)
        if evidence is not None:
            findings.append(
                DesignFinding(
                    rule_id="web_glass_missing_saturate",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=evidence,
                    choice_key=_CHOICE_WEB_GLASS,
                    message="backdrop-filter without saturate() makes site glass read as gray mud",
                )
            )
        glass_uses.append((m.start(), evidence or "backdrop-filter"))

    for m in _STYLE_ATTR_RE.finditer(text):
        style = m.group("style")
        if not _style_has_backdrop_filter(style):
            continue
        evidence = _backdrop_without_saturate(style)
        if evidence is not None:
            findings.append(
                DesignFinding(
                    rule_id="web_glass_missing_saturate",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=evidence,
                    choice_key=_CHOICE_WEB_GLASS,
                    message="backdrop-filter without saturate() makes site glass read as gray mud",
                )
            )
        glass_uses.append((m.start(), evidence or "backdrop-filter"))

    if not glass_uses:
        return findings
    if _has_colorful_backdrop(text):
        return findings
    if not _style_has_flat_background(text):
        return findings

    offset, evidence = glass_uses[0]
    findings.append(
        DesignFinding(
            rule_id="web_glass_flat_backdrop",
            severity="warning",
            path=path,
            line=_line_of(text, offset),
            evidence=evidence,
            choice_key=_CHOICE_WEB_GLASS,
            message=(
                "site glass sits over a flat single-color backdrop; glass needs colorful, "
                "gradient, image, or video content behind it"
            ),
        )
    )
    return findings


def _rule_pill_buttons(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_COMPONENT_BUTTON_RADIUS in justified:
        return []
    low = text.lower()
    pill = 0
    nonpill = 0
    first: tuple[int, str] | None = None
    # CSS: button-ish selector blocks with a border-radius
    for m in _CSS_BLOCK_RE.finditer(low):
        selector = m.group(1)
        body = m.group(2)
        if not any(tok in selector for tok in _BUTTON_SELECTOR_TOKENS):
            continue
        rm = re.search(r"border-radius\s*:\s*([^;{}]+)", body)
        if rm is None:
            continue
        value = rm.group(1).strip()
        if any(p in value for p in _PILL_RADII):
            pill += 1
            if first is None:
                first = (_line_of(low, m.start()), m.group(1).strip()[:80])
        else:
            nonpill += 1
    # Markup: tailwind rounded-full on button-ish elements
    for m in re.finditer(r'class\s*=\s*"([^"]*)"', low):
        cls = m.group(1)
        if ("btn" in cls or "button" in cls) and "rounded-full" in cls:
            pill += 1
            if first is None:
                first = (_line_of(low, m.start()), "rounded-full")
    if pill >= 2 and nonpill == 0 and first is not None:
        return [
            DesignFinding(
                rule_id="pill_button_monoculture",
                severity="info",
                path=path,
                line=first[0],
                evidence=first[1],
                choice_key=CHOICE_COMPONENT_BUTTON_RADIUS,
                message=(
                    "Every button is a full pill — a one-note component language. Vary the radius "
                    "by emphasis, or justify component.button_radius in .disco/designspec.json."
                ),
            )
        ]
    return []


def _rule_emoji_icons(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_ICONS_STYLE in justified:
        return []
    matches = _EMOJI_RE.findall(text)
    if len(matches) < _EMOJI_THRESHOLD:
        return []
    first = next(iter(_EMOJI_RE.finditer(text)))
    sample = "".join(dict.fromkeys(matches[:6]))
    return [
        DesignFinding(
            rule_id="emoji_as_icons",
            severity="info",
            path=path,
            line=_line_of(text, first.start()),
            evidence=f"{len(matches)} emoji used as icons (e.g. {sample})",
            choice_key=CHOICE_ICONS_STYLE,
            message=(
                "Emoji standing in for icons — reads as a placeholder. Use a real icon set, or "
                "justify icons.style in .disco/designspec.json."
            ),
        )
    ]


def _rule_centered_hero_3_cards_cta(
    markup: list[tuple[str, str]], justified: set[str]
) -> list[DesignFinding]:
    """Workspace-level section-sequence check across the markup files."""
    if CHOICE_LAYOUT_SECTION_SEQUENCE in justified:
        return []
    centering = ("text-center", "items-center", "justify-center", "mx-auto", "text-align:center")
    for path, text in markup:
        low = re.sub(r"\s+", " ", text.lower())
        # hero (centered)
        hero = re.search(r'(<section[^>]*|class\s*=\s*"[^"]*)hero', low)
        if hero is None:
            continue
        window = low[hero.start() : hero.start() + 400]
        if not any(c in window for c in centering):
            continue
        hero_idx = hero.start()
        # exactly three cards: a 3-col grid OR exactly three card-classed elements
        cards_idx: int | None = None
        grid = re.search(r"grid-cols-3|repeat\(3,|grid-template-columns\s*:\s*repeat\(\s*3", low)
        card_iter = list(re.finditer(r'class\s*=\s*"[^"]*card', low))
        if grid is not None and grid.start() > hero_idx:
            cards_idx = grid.start()
        elif len(card_iter) == 3 and card_iter[0].start() > hero_idx:
            cards_idx = card_iter[0].start()
        if cards_idx is None:
            continue
        # a CTA strictly AFTER the cards (search the tail so a "Get started" button
        # sitting INSIDE the hero can't be mistaken for the closing CTA).
        cta = re.search(
            r'(class\s*=\s*"[^"]*cta|<section[^>]*cta|get started|sign up|start free)',
            low[cards_idx:],
        )
        if cta is None:
            continue
        return [
            DesignFinding(
                rule_id="centered_hero_3_cards_cta",
                severity="warning",
                path=path,
                line=_line_of(text, 0),
                evidence="centered hero -> 3 feature cards -> CTA",
                choice_key=CHOICE_LAYOUT_SECTION_SEQUENCE,
                message=(
                    "The centered-hero / three-cards / CTA sequence is THE generated-landing-page "
                    "shape. Compose from the section-variant catalog instead, or justify "
                    "layout.section_sequence in .disco/designspec.json."
                ),
            )
        ]
    return []


def _rule_deck_type_floor(
    path: str, text: str, slides: list[_DeckSlide]
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    for slide in slides:
        for style, offset in _style_attrs(slide):
            for m in _FONT_SIZE_DECL_RE.finditer(style):
                px = _font_size_px(m.group("value"), m.group("unit"))
                if px is None or px >= _DECK_TYPE_FLOOR_PX:
                    continue
                evidence = m.group(0).strip()
                findings.append(
                    DesignFinding(
                        rule_id="deck_type_floor",
                        severity="error",
                        path=path,
                        line=_line_of(text, offset + m.start()),
                        evidence=evidence,
                        choice_key=_CHOICE_DECK_TYPE_FLOOR,
                        message=(
                            f"Deck slide type floor violation on slide {slide.number}: "
                            f"inline {evidence} resolves below 24px."
                        ),
                    )
                )
    return findings


def _rule_deck_text_budget(path: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    for slide in slides:
        words = _word_count(slide.html)
        bullets = _bullet_count(slide.html)
        if words <= _DECK_TEXT_WORD_LIMIT and bullets <= _DECK_BULLET_LIMIT:
            continue
        findings.append(
            DesignFinding(
                rule_id="deck_text_budget",
                severity="warning",
                path=path,
                line=slide.line,
                evidence=f"{words} words, {bullets} bullet lines",
                choice_key=_CHOICE_DECK_TEXT_BUDGET,
                message=(
                    f"too much text on slide {slide.number}: {words} words and "
                    f"{bullets} bullet lines. Keep slides under about 90 words "
                    "and no more than 6 bullet lines."
                ),
            )
        )
    return findings


def _slide_is_pure_bullet_list(slide: _DeckSlide) -> bool:
    layout = (_attr_value(slide.attrs, "data-layout") or "").strip().lower()
    if layout in {"image_right", "image_left", "full_image", "section_header", "title", "closing"}:
        return False
    if layout in {"bullets", "bullet", "bullet_list"}:
        return _bullet_count(slide.html) > 0
    if _bullet_count(slide.html) == 0:
        return False
    if re.search(r"<\s*(?:img|picture|svg|canvas|video|table|figure)\b", slide.html, re.I):
        return False
    non_bullet_blocks = re.findall(r"<\s*(?:p|blockquote|pre|table|figure)\b", slide.html, re.I)
    return len(non_bullet_blocks) == 0


def _rule_deck_bullet_monotony(
    path: str, slides: list[_DeckSlide]
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    run_start: int | None = None
    for idx, slide in enumerate(slides):
        if _slide_is_pure_bullet_list(slide):
            if run_start is None:
                run_start = idx
            continue
        if run_start is not None and idx - run_start >= 3:
            first = slides[run_start]
            last = slides[idx - 1]
            findings.append(
                DesignFinding(
                    rule_id="deck_bullet_monotony",
                    severity="warning",
                    path=path,
                    line=first.line,
                    evidence=f"slides {first.number}-{last.number} are pure bullet lists",
                    choice_key=_CHOICE_DECK_ARCHETYPE_MONOTONY,
                    message=(
                        f"Pure bullet-list monotony across slides {first.number}-{last.number}. "
                        "Vary the run with imagery, section, quote, chart, or comparison slides."
                    ),
                )
            )
        run_start = None
    if run_start is not None and len(slides) - run_start >= 3:
        first = slides[run_start]
        last = slides[-1]
        findings.append(
            DesignFinding(
                rule_id="deck_bullet_monotony",
                severity="warning",
                path=path,
                line=first.line,
                evidence=f"slides {first.number}-{last.number} are pure bullet lists",
                choice_key=_CHOICE_DECK_ARCHETYPE_MONOTONY,
                message=(
                    f"Pure bullet-list monotony across slides {first.number}-{last.number}. "
                    "Vary the run with imagery, section, quote, chart, or comparison slides."
                ),
            )
        )
    return findings


def _rule_deck_missing_imagery(path: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    if _deck_image_count(slides) > 0:
        return []
    return [
        DesignFinding(
            rule_id="deck_missing_imagery",
            severity="warning",
            path=path,
            line=slides[0].line if slides else 1,
            evidence="0 images across deck slides",
            choice_key=_CHOICE_DECK_IMAGERY,
            message="no imagery: cover/divider slides should carry generated art",
        )
    ]


def _rule_deck_glass(path: str, text: str, slides: list[_DeckSlide]) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    glass_uses: list[tuple[_DeckSlide | None, int, str]] = []

    for m in _CSS_BLOCK_RE.finditer(text):
        body = m.group(2)
        if not _style_has_backdrop_filter(body):
            continue
        evidence = _backdrop_without_saturate(body)
        if evidence is not None:
            findings.append(
                DesignFinding(
                    rule_id="deck_glass_missing_saturate",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=evidence,
                    choice_key=_CHOICE_DECK_GLASS,
                    message="backdrop-filter without saturate() makes deck glass read as gray mud",
                )
            )
        glass_uses.append((None, m.start(), evidence or "backdrop-filter"))

    for slide in slides:
        for style, offset in _style_attrs(slide):
            if not _style_has_backdrop_filter(style):
                continue
            evidence = _backdrop_without_saturate(style)
            if evidence is not None:
                findings.append(
                    DesignFinding(
                        rule_id="deck_glass_missing_saturate",
                        severity="warning",
                        path=path,
                        line=_line_of(text, offset),
                        evidence=evidence,
                        choice_key=_CHOICE_DECK_GLASS,
                        message=(
                            "backdrop-filter without saturate() makes deck glass "
                            "read as gray mud"
                        ),
                    )
                )
            glass_uses.append((slide, offset, evidence or "backdrop-filter"))

    if not glass_uses:
        return findings

    global_flat = _global_deck_backdrop_is_flat(text)
    deck_has_colorful_backdrop = any(
        _has_colorful_backdrop(f"{slide.attrs}\n{slide.html}") for slide in slides
    )
    for slide, offset, evidence in glass_uses:
        if slide is not None:
            raw = f"{slide.attrs}\n{slide.html}"
            if _has_colorful_backdrop(raw):
                continue
            if _slide_has_flat_backdrop(slide) or global_flat:
                findings.append(
                    DesignFinding(
                        rule_id="deck_glass_flat_backdrop",
                        severity="warning",
                        path=path,
                        line=_line_of(text, offset),
                        evidence=evidence,
                        choice_key=_CHOICE_DECK_GLASS,
                        message=(
                            f"glass on slide {slide.number} sits over a flat single-color "
                            "backdrop; glass needs a colorful or image backdrop"
                        ),
                    )
                )
                break
            continue
        if global_flat and not deck_has_colorful_backdrop:
            findings.append(
                DesignFinding(
                    rule_id="deck_glass_flat_backdrop",
                    severity="warning",
                    path=path,
                    line=_line_of(text, offset),
                    evidence=evidence,
                    choice_key=_CHOICE_DECK_GLASS,
                    message=(
                        "glass sits over a flat single-color deck backdrop; glass needs a "
                        "colorful or image backdrop"
                    ),
                )
            )
            break
    return findings


# ---- the pure engine ----------------------------------------------------------


def lint_design(
    files: Mapping[str, str],
    design_spec: DesignSpec | None,
    *,
    spec_present: bool,
    spec_valid: bool,
) -> dict[str, Any]:
    """Scan an in-memory `{path: text}` map and return the structured verdict.

    `design_spec` is the parsed spec (None when absent OR invalid). `spec_present`
    / `spec_valid` are reported in the verdict so the agent knows WHY rules fired
    (a missing/invalid spec suppresses nothing — intent can't be claimed)."""
    justified = _justified_keys(design_spec)
    findings: list[DesignFinding] = []
    markup: list[tuple[str, str]] = []
    workspace_has_focus_visible = any(
        ":focus-visible" in text.lower() or "focus-visible:" in text.lower()
        for text in files.values()
    )

    for path in sorted(files):
        text = files[path]
        ext = _ext_of(path)
        is_css = ext in _CSS_EXTS
        is_markup = ext in _MARKUP_EXTS
        if not (is_css or is_markup):
            continue
        if is_markup:
            markup.append((path, text))
        deck_slides = _extract_deck_slides(text) if is_markup else []
        # rules that apply to any styled text (css + markup w/ inline styles)
        findings += _rule_generic_font(path, text, design_spec, justified)
        if not deck_slides:
            findings += _rule_web_banned_default_font(path, text, design_spec, justified)
            findings += _rule_web_reflexive_hover_scale(path, text)
            findings += _rule_web_hover_only_interactivity(
                path, text, workspace_has_focus_visible=workspace_has_focus_visible
            )
            findings += _rule_web_text_over_image_no_scrim(path, text)
            findings += _rule_web_glass(path, text)
        findings += _rule_ai_purple(path, text, design_spec, justified)
        findings += _rule_gradient_hero_text(path, text, justified)
        findings += _rule_dark_neon_glow(path, text, justified)
        findings += _rule_too_many_animations(path, text, justified)
        findings += _rule_pill_buttons(path, text, justified)
        if is_markup:
            findings += _rule_emoji_icons(path, text, justified)
            if deck_slides:
                findings += _rule_deck_type_floor(path, text, deck_slides)
                findings += _rule_deck_text_budget(path, deck_slides)
                findings += _rule_deck_bullet_monotony(path, deck_slides)
                findings += _rule_deck_missing_imagery(path, deck_slides)
                findings += _rule_deck_glass(path, text, deck_slides)

    findings += _rule_centered_hero_3_cards_cta(markup, justified)

    findings.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.severity, 9),
            _RULE_META.get(f.rule_id, ("info", 999))[1],
            f.path,
            f.line,
        )
    )

    counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    ok = len(findings) == 0
    if ok:
        summary = (
            f"design_lint: clean — no slop across {len(files)} scanned file(s)."
            if files
            else "design_lint: no scannable files found."
        )
    else:
        rule_ids = [f.rule_id for f in findings]
        summary = (
            f"design_lint: {len(findings)} finding(s) "
            f"({counts['error']} error, {counts['warning']} warning, {counts['info']} info) — "
            f"{', '.join(dict.fromkeys(rule_ids))}."
        )

    return {
        "ok": ok,
        "findings": [f.model_dump() for f in findings],
        "counts": counts,
        "summary": summary,
        "scanned_files": len(files),
        "design_spec_present": spec_present,
        "design_spec_valid": spec_valid,
    }


def _render(verdict: dict[str, Any]) -> str:
    lines = [f"DESIGN_LINT: {'PASS' if verdict['ok'] else 'FINDINGS'}", verdict["summary"]]
    if not verdict["design_spec_present"]:
        lines.append("note: no .disco/designspec.json — off-default values can't be justified.")
    elif not verdict["design_spec_valid"]:
        lines.append("note: .disco/designspec.json is invalid — justifications ignored.")
    for f in verdict["findings"][:25]:
        lines.append(
            f"  [{f['severity']}] {f['rule_id']} ({f['choice_key']}) "
            f"{f['path']}:{f['line']} — {f['evidence']}"
        )
    return "\n".join(lines)


# ---- the tool wrapper ---------------------------------------------------------


class DesignLintArgs(BaseModel):
    root: str = Field(
        default=".",
        description="Workspace-relative directory to scan (defaults to the workspace root).",
    )


class DesignLintTool:
    """[CONTRACT boundary] READ-ONLY design-slop scanner. Walks the workspace's
    CSS + HTML/JSX and `.disco/designspec.json` and returns a structured verdict
    of ranked findings. Observes only — makes no change — so it is safe for the
    planner and counts as a verification probe (not productive work)."""

    definition = ToolDef(
        name="design_lint",
        description=(
            "Scan the generated web app for design SLOP (the LLM-median tells: Inter/Geist "
            "fonts, AI-purple primary, gradient-clipped hero text, dark-neon-glow, pill-button "
            "monoculture, emoji-as-icons, animation soup, centered-hero/3-cards/CTA) and return "
            "a STRUCTURED verdict. For static sites, also checks banned default first fonts on "
            "body/headings, reflexive hover scale, hover-only interactivity without focus-visible, "
            "text over imagery without a scrim, and glass sanity. For HTML deck artifacts, also "
            "checks the slide type floor, text budget, bullet-list monotony, missing imagery, and "
            "glass sanity. Findings are ranked rows with rule_id, severity, path, line, evidence, "
            "choice_key, and a fix message. Web off-default values are suppressed only when "
            ".disco/designspec.json carries a substantive justification for the choice_key. "
            "Read-only — run it to CHECK a build, not to change it. Scans the workspace root "
            "unless `root` is given."
        ),
        args_model=DesignLintArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner + a non-productive probe
    )

    async def run(self, args: DesignLintArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            root = (args.root or ".").strip() or "."
            files = await self._collect_files(ctx, root)
            design_spec, spec_present, spec_valid = await self._load_design_spec(ctx)
            verdict = lint_design(
                files,
                design_spec,
                spec_present=spec_present,
                spec_valid=spec_valid,
            )
            return ToolOutcome(
                success=True,  # the scan ran; pass/findings live in `structured`
                content=_render(verdict),
                structured=verdict,
            )
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a scan error
            return ToolOutcome(success=False, content="", error=f"design_lint error: {e}")

    async def _collect_files(self, ctx: ToolContext, root: str) -> dict[str, str]:
        """Walk the workspace via the sandbox `list_dir`, reading only scannable
        files (bounded by file/depth/size caps). Directories are distinguished
        without relying on backend-specific error types: an entry whose name has
        a non-scannable extension is treated as a leaf file and skipped; an
        extension-less entry is probed as a directory (a failed `list_dir` →
        it was a leaf, skip it)."""
        assert ctx.sandbox is not None
        sandbox = ctx.sandbox
        files: dict[str, str] = {}

        async def walk(rel: str, depth: int) -> None:
            if depth > _MAX_DEPTH or len(files) >= _MAX_FILES:
                return
            try:
                entries = await sandbox.list_dir(rel)
            except Exception:  # noqa: BLE001 — unreadable/non-dir → nothing to scan here
                return
            for name in sorted(entries):
                if len(files) >= _MAX_FILES:
                    return
                # Skip dep/build dirs and ALL dotfiles/dotdirs (incl. `.disco`,
                # which is loaded separately as the spec — never scanned for slop).
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                child = name if rel in (".", "") else f"{rel.rstrip('/')}/{name}"
                ext = _ext_of(name)
                if ext in SCAN_EXTS:
                    # Size-check IN the sandbox before reading: an over-cap (or
                    # unmeasurable) source file is skipped without ever pulling its
                    # bytes into this process — no read-then-discard memory bomb.
                    data = await _bounded_read(sandbox, child, _MAX_FILE_BYTES)
                    if data is None:
                        continue
                    files[child] = data.decode("utf-8", errors="replace")
                elif "." not in name:
                    await walk(child, depth + 1)

        await walk(root, 0)
        return files

    async def _load_design_spec(
        self, ctx: ToolContext
    ) -> tuple[DesignSpec | None, bool, bool]:
        """Read + parse `.disco/designspec.json`. Returns (spec, present, valid).
        Absent → (None, False, False); present-but-bad → (None, True, False)."""
        assert ctx.sandbox is not None
        path = ".disco/designspec.json"
        try:
            exists = await ctx.sandbox.file_exists(path)
        except Exception:  # noqa: BLE001 — treat a probe failure as absent
            exists = False
        if not exists:
            return None, False, False
        # Size-check IN the sandbox BEFORE read: an over-cap (or unmeasurable)
        # `.disco/designspec.json` is never pulled into this process — `_bounded_read`
        # returns None, which we treat as present-but-invalid (rules fire, nothing
        # suppressed). A multi-GB hostile spec is therefore not a memory/time bomb.
        data = await _bounded_read(ctx.sandbox, path, _MAX_SPEC_BYTES)
        if data is None:
            return None, True, False
        try:
            # Defense-in-depth: the Epic C airtight loader re-enforces the same
            # `MAX_DESIGNSPEC_BYTES` cap before json.loads and validates the schema.
            spec = load_design_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — present but malformed/invalid
            return None, True, False
        return spec, True, True


__all__ = [
    "SUPPRESSIBLE_CHOICE_KEYS",
    "DesignFinding",
    "DesignLintArgs",
    "DesignLintTool",
    "lint_design",
]

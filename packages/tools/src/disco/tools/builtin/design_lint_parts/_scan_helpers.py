"""Small, generic AST-light scanning helpers shared across `design_lint` rule
families: line/offset math, color parsing, font-family extraction, selector
matching, and flat-background/backdrop-filter probes. Extracted from
`design_lint.py` verbatim (logic unchanged) — every rule module imports the
pieces it needs from here instead of each owning its own copy."""

from __future__ import annotations

import html as html_lib
import re

from ._constants import (
    _BACKDROP_DECL_RE,
    _BACKGROUND_DECL_RE,
    _COMMENT_RE,
    _DECK_BASE_WIDTH_PX,
    _DIRECTION_GENERIC_FONTS,
    _SCRIPT_STYLE_RE,
    _TAG_RE,
    _WEB_HEADING_SELECTOR_RE,
    _WEB_INTERACTIVE_SELECTOR_RE,
    _WORD_RE,
)


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


def _rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _rgb_saturation(rgb: tuple[int, int, int]) -> int:
    return max(rgb) - min(rgb)


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


def _has_colorful_backdrop(raw: str) -> bool:
    return (
        re.search(
            r"(?:linear|radial|conic)-gradient|url\(|<\s*(?:img|picture|video)\b",
            raw,
            re.I,
        )
        is not None
    )


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
    return any(
        _is_flat_background_value(m.group("value")) for m in _BACKGROUND_DECL_RE.finditer(style)
    )


def _backdrop_without_saturate(style: str) -> str | None:
    for m in _BACKDROP_DECL_RE.finditer(style):
        value = m.group("value")
        if "saturate(" not in value.lower():
            return m.group(0).strip()
    return None


def _style_has_backdrop_filter(style: str) -> bool:
    return _BACKDROP_DECL_RE.search(style) is not None


def _has_background_image_url(raw: str) -> bool:
    return re.search(r"background(?:-image)?\s*:[^;{}]*url\(", raw, re.I) is not None


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
    for m in re.finditer(r"(?P<name>--[a-z0-9-]*font[a-z0-9-]*)\s*:\s*([^;{}]+)", low):
        if re.search(
            r"font-(?:size|weight|style|feature|variation|variant|smoothing)|"
            r"(?:size|weight|style)$",
            m.group("name"),
        ):
            continue
        out.append(_first(m.group(2)))
    for m in re.finditer(r"--(?:display|ui|reading|mono)\s*:\s*([^;{}]+)", low):
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
    return any(
        token.startswith("focus-visible:") or ":focus-visible:" in token for token in raw.split()
    )


def _norm_font_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().strip("'\"").lower())


def _is_generic_font_family(token: str) -> bool:
    return token in _DIRECTION_GENERIC_FONTS or token.startswith("ui-") or "var(" in token


def _inside_css_guard(text: str, offset: int) -> bool:
    prefix = text[:offset]
    starts = [prefix.rfind("@media"), prefix.rfind("@supports")]
    last = max(starts)
    if last < 0:
        return False
    segment = text[last:offset]
    return segment.count("{") > segment.count("}")

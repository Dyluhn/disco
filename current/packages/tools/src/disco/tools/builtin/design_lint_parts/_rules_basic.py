"""Typography/palette/basic-metric rule family: generic fonts, web-default
fonts, AI-purple, font-family count, font size floor, brand color count, and
line-height. Extracted from `design_lint.py` verbatim (logic unchanged)."""

from __future__ import annotations

import re

from disco.core.appkit import (
    CHOICE_PALETTE_PRIMARY,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
)
from disco.core.appkit.spec import DesignSpec

from ._constants import (
    _AI_PURPLE_HEXES,
    _AI_PURPLE_RGB,
    _CHOICE_WEB_COLOR_COUNT,
    _CHOICE_WEB_FONT_COUNT,
    _CHOICE_WEB_FONT_SIZE,
    _CHOICE_WEB_LINE_HEIGHT,
    _COLOR_MERGE_DISTANCE,
    _COLOR_SATURATION_FLOOR,
    _CSS_BLOCK_RE,
    _FONT_CONTEXT_RE,
    _FONT_SIZE_DECL_RE,
    _GENERIC_FONTS,
    _LINE_HEIGHT_DECL_RE,
    _MAX_BRAND_COLORS,
    _MAX_FONT_FAMILIES,
    _MIN_FONT_SIZE_PX,
    _MIN_LINE_HEIGHT,
    _WEB_BANNED_DEFAULT_FONTS,
    _WEB_HEADING_TARGET_RE,
)
from ._model import DesignFinding
from ._scan_helpers import (
    _first_font_family,
    _font_size_px,
    _is_generic_font_family,
    _line_of,
    _norm_color,
    _norm_font_name,
    _primary_families,
    _resolve_rgb,
    _rgb_distance,
    _rgb_saturation,
    _selector_targets_body_or_heading,
)
from ._suppression import _font_justified, _justified_palette_rgbs


def _rule_generic_font(
    path: str, text: str, design_spec: DesignSpec | None, justified: set[str]
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    seen: set[str] = set()
    body_font = design_spec.typography.body_font.strip().lower() if design_spec is not None else ""
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


def _rule_too_many_fonts(path: str, text: str) -> list[DesignFinding]:
    families: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if (
            not _FONT_CONTEXT_RE.search(line)
            and re.search(r"--(?:display|ui|reading|mono)\s*:", line, re.I) is None
        ):
            continue
        for token in _primary_families(line):
            family = _norm_font_name(token)
            if not family or _is_generic_font_family(family):
                continue
            families.setdefault(family, lineno)

    if len(families) <= _MAX_FONT_FAMILIES:
        return []

    listed = ", ".join(sorted(families))
    first_line = min(families.values())
    return [
        DesignFinding(
            rule_id="too_many_fonts",
            severity="warning",
            path=path,
            line=first_line,
            evidence=f"{len(families)} primary font families: {listed}",
            choice_key=_CHOICE_WEB_FONT_COUNT,
            message=(
                f"{len(families)} distinct primary font families (> {_MAX_FONT_FAMILIES}). "
                "Use a focused heading/body/mono system unless the brand truly needs more."
            ),
        )
    ]


def _rule_font_size_too_small(path: str, text: str) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    seen_values: set[str] = set()
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        body = m.group(2)
        for decl in _FONT_SIZE_DECL_RE.finditer(body):
            if decl.start() > 0 and body[decl.start() - 1] == "-":
                continue
            value = decl.group("value")
            unit = decl.group("unit")
            if (unit or "").lower() not in {"px", "pt"}:
                continue
            px = _font_size_px(value, unit)
            if px is None or px == 0 or px >= _MIN_FONT_SIZE_PX:
                continue
            evidence = decl.group(0).strip()
            key = evidence.lower()
            if key in seen_values:
                continue
            seen_values.add(key)
            findings.append(
                DesignFinding(
                    rule_id="font_size_too_small",
                    severity="info",
                    path=path,
                    line=_line_of(text, m.start(2) + decl.start()),
                    evidence=f"{selector} {{ {evidence} }}",
                    choice_key=_CHOICE_WEB_FONT_SIZE,
                    message=(
                        f"{evidence} resolves to {px:.1f}px, below the "
                        f"{_MIN_FONT_SIZE_PX:.0f}px readable web floor."
                    ),
                )
            )
    return findings


def _rule_too_many_colors(path: str, text: str) -> list[DesignFinding]:
    colors: list[tuple[tuple[int, int, int], str, int]] = []
    low = text.lower()
    for m in re.finditer(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?|rgb\([^)]*\)", low):
        val = _norm_color(m.group(0))
        rgb = _resolve_rgb(val)
        if rgb is None or _rgb_saturation(rgb) < _COLOR_SATURATION_FLOOR:
            continue
        if any(_rgb_distance(rgb, kept) <= _COLOR_MERGE_DISTANCE for kept, _, _ in colors):
            continue
        colors.append((rgb, val, m.start()))

    if len(colors) <= _MAX_BRAND_COLORS:
        return []

    listed = ", ".join(color for _, color, _ in colors[:10])
    return [
        DesignFinding(
            rule_id="too_many_colors",
            severity="info",
            path=path,
            line=_line_of(text, colors[0][2]),
            evidence=f"{len(colors)} saturated brand colors: {listed}",
            choice_key=_CHOICE_WEB_COLOR_COUNT,
            message=(
                f"{len(colors)} distinct saturated brand colors (> {_MAX_BRAND_COLORS}). "
                "Normal palettes should cluster around a small set of intentional hues."
            ),
        )
    ]


def _rule_tight_line_height(path: str, text: str) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        selector_parts = [part.strip() for part in selector.split(",") if part.strip()]
        if selector_parts and all(
            _WEB_HEADING_TARGET_RE.search(part) is not None for part in selector_parts
        ):
            continue
        body = m.group(2)
        for decl in _LINE_HEIGHT_DECL_RE.finditer(body):
            if decl.start() > 0 and body[decl.start() - 1] == "-":
                continue
            try:
                value = float(decl.group(1))
            except ValueError:
                continue
            if value >= _MIN_LINE_HEIGHT:
                continue
            findings.append(
                DesignFinding(
                    rule_id="tight_line_height",
                    severity="info",
                    path=path,
                    line=_line_of(text, m.start(2) + decl.start()),
                    evidence=f"{selector} {{ {decl.group(0).strip()} }}",
                    choice_key=_CHOICE_WEB_LINE_HEIGHT,
                    message=(
                        f"Unitless line-height {value:g} is below {_MIN_LINE_HEIGHT:g}; "
                        "body copy below this tends to read cramped."
                    ),
                )
            )
            break
    return findings

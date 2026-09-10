"""Effects rule family: gradient-clipped hero text, dark-neon-glow, and
animation soup. Extracted from `design_lint.py` verbatim (logic unchanged)."""

from __future__ import annotations

import re

from disco.core.appkit import (
    CHOICE_EFFECTS_GLOW,
    CHOICE_EFFECTS_GRADIENT_TEXT,
    CHOICE_MOTION_DENSITY,
)

from ._constants import _ANIMATION_THRESHOLD, _CSS_BLOCK_RE, _HEADING_SELECTOR_TOKENS, _NEON_HEXES
from ._model import DesignFinding
from ._scan_helpers import _line_of, _luminance, _norm_color


def _rule_gradient_hero_text(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
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
            "linear-gradient" in body or "conic-gradient" in body or "radial-gradient" in body
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


def _rule_dark_neon_glow(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
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
    glow = bool(re.search(r"box-shadow|text-shadow|drop-shadow|filter\s*:[^;{}]*blur", low))
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

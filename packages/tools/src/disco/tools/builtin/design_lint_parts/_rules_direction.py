"""Direction-conformance rule family (advisory: the committed design direction
is ground truth, never suppressed by a designspec justification). Extracted
from `design_lint.py`.

`_rule_direction_conformance` itself is DECOMPOSED (not just relocated) versus
the original monolith: the font-mismatch scan and the palette-drift scan were
two independent loops inside one 18-mccabe function. They are now two
separately-named callables (`_direction_font_mismatches` /
`_direction_palette_drifts`), each with its own small predicate helper
(`_is_direction_conforming_font` / `_color_near_committed`) pulled out of their
`if` conditions. Every finding field, message, and iteration order is
unchanged — this is pure decomposition, not a behavior change."""

from __future__ import annotations

import re

from disco.core.appkit import CHOICE_PALETTE_PRIMARY, CHOICE_TYPOGRAPHY_HEADING
from disco.core.design import DesignDirection

from ._constants import (
    _DIRECTION_DRIFT_MIN_DISTANCE,
    _DIRECTION_GENERIC_FONTS,
    _DIRECTION_MIN_SATURATION,
    _FONT_CONTEXT_RE,
)
from ._model import DesignFinding
from ._scan_helpers import (
    _line_of,
    _norm_color,
    _norm_font_name,
    _primary_families,
    _resolve_rgb,
    _rgb_distance,
    _rgb_saturation,
)


def _direction_committed_families(direction: DesignDirection) -> set[str]:
    fp = direction.font_pairing
    return {
        _norm_font_name(fp.heading.family),
        _norm_font_name(fp.body.family),
        _norm_font_name(fp.mono.family),
    }


def _direction_committed_rgbs(direction: DesignDirection) -> list[tuple[int, int, int]]:
    raw = [direction.palette_seed, *(a.hex for a in direction.accents)]
    return [rgb for rgb in (_resolve_rgb(v) for v in raw) if rgb is not None]


def _is_direction_conforming_font(token: str, committed_families: set[str]) -> bool:
    return (
        not token
        or token in _DIRECTION_GENERIC_FONTS
        or token in committed_families
        or token.startswith("ui-")
        or "var(" in token
    )


def _direction_font_mismatches(
    path: str, text: str, direction: DesignDirection, committed_families: set[str]
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    seen_fonts: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if not _FONT_CONTEXT_RE.search(line):
            continue
        for token in _primary_families(line):
            if token in seen_fonts:
                continue
            seen_fonts.add(token)
            if _is_direction_conforming_font(token, committed_families):
                continue  # generic fallback / committed family / unresolved var
            findings.append(
                DesignFinding(
                    rule_id="direction_font_mismatch",
                    severity="warning",
                    path=path,
                    line=lineno,
                    evidence=f"font '{token}' in: {line.strip()[:140]}",
                    choice_key=CHOICE_TYPOGRAPHY_HEADING,
                    message=(
                        f"Font '{token}' is not in the committed design direction "
                        f"({direction.id}: heading={direction.font_pairing.heading.family}, "
                        f"body={direction.font_pairing.body.family}, "
                        f"mono={direction.font_pairing.mono.family}). Use the committed "
                        "families or re-commit the direction."
                    ),
                )
            )
    return findings


def _color_near_committed(
    rgb: tuple[int, int, int], committed_rgbs: list[tuple[int, int, int]]
) -> bool:
    return min(_rgb_distance(rgb, c) for c in committed_rgbs) <= _DIRECTION_DRIFT_MIN_DISTANCE


def _direction_palette_drifts(
    path: str,
    text: str,
    direction: DesignDirection,
    committed_rgbs: list[tuple[int, int, int]],
) -> list[DesignFinding]:
    if not committed_rgbs:
        return []
    findings: list[DesignFinding] = []
    low = text.lower()
    seen_colors: set[str] = set()
    for m in re.finditer(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?|rgb\([^)]*\)", low):
        val = _norm_color(m.group(0))
        if val in seen_colors:
            continue
        seen_colors.add(val)
        rgb = _resolve_rgb(val)
        if rgb is None or _rgb_saturation(rgb) < _DIRECTION_MIN_SATURATION:
            continue  # unresolved / neutral-grey / tint-or-shade → stay quiet
        if _color_near_committed(rgb, committed_rgbs):
            continue  # near a committed color (incl. same-hue variation)
        findings.append(
            DesignFinding(
                rule_id="direction_palette_drift",
                severity="info",
                path=path,
                line=_line_of(low, m.start()),
                evidence=m.group(0),
                choice_key=CHOICE_PALETTE_PRIMARY,
                message=(
                    f"Color {m.group(0)} is far from the committed direction palette "
                    f"({direction.id}: seed={direction.palette_seed}, accents "
                    f"{', '.join(a.hex for a in direction.accents)}). Derive brand "
                    "colors from the committed seed/accents."
                ),
            )
        )
    return findings


def _rule_direction_conformance(
    path: str, text: str, direction: DesignDirection
) -> list[DesignFinding]:
    """ADVISORY conformance — flag built fonts/colors that CONTRADICT the committed
    design direction. Ground truth is the committed DIRECTION itself, so these are
    NEVER run through designspec justifications. Conservative: only intentional,
    prominent choices fire; generic fallbacks, unresolved `var()` references and
    neutral/tint colors are skipped so the rule stays quiet rather than false-fire."""
    committed_families = _direction_committed_families(direction)
    committed_rgbs = _direction_committed_rgbs(direction)
    return _direction_font_mismatches(
        path, text, direction, committed_families
    ) + _direction_palette_drifts(path, text, direction, committed_rgbs)

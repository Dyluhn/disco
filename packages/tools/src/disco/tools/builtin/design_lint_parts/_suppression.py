"""Justification-aware, value-aware suppression logic — whether a finding's
`choice_key` is substantively justified by `.disco/designspec.json`, and (for
the two rules with a concrete DesignSpec field) whether the justified value
actually matches the flagged one. Extracted from `design_lint.py` verbatim."""

from __future__ import annotations

from disco.core.appkit import (
    CHOICE_PALETTE_ACCENT,
    CHOICE_PALETTE_PRIMARY,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
)
from disco.core.appkit.spec import DesignSpec

from ._model import _MIN_REASON_LEN
from ._scan_helpers import _resolve_rgb


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

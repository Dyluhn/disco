"""The `design_lint` finding model + rule metadata (severity/sort priority) +
the suppressible choice-key vocabulary. Extracted from `design_lint.py`
verbatim — this is data/schema, not scanning logic."""

from __future__ import annotations

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
)
from pydantic import BaseModel, ConfigDict

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
    "direction_font_mismatch": ("warning", 25),
    "gradient_hero_text": ("warning", 30),
    "dark_neon_glow": ("warning", 40),
    "deck_text_budget": ("warning", 45),
    "deck_bullet_monotony": ("warning", 46),
    "deck_missing_imagery": ("warning", 47),
    "deck_glass_missing_saturate": ("warning", 48),
    "deck_glass_flat_backdrop": ("warning", 49),
    "deck_orphan_slide": ("warning", 50),
    "deck_handwritten_html": ("warning", 51),
    "web_banned_default_font": ("warning", 50),
    "web_reflexive_hover_scale": ("warning", 51),
    "web_hover_only_interactivity": ("warning", 52),
    "web_text_over_image_no_scrim": ("warning", 53),
    "web_glass_missing_saturate": ("warning", 54),
    "web_glass_flat_backdrop": ("warning", 55),
    "web_default_hidden_content": ("warning", 56),
    "too_many_fonts": ("warning", 57),
    "centered_hero_3_cards_cta": ("warning", 50),
    "too_many_animations": ("info", 60),
    "direction_palette_drift": ("info", 65),
    "too_many_colors": ("info", 66),
    "font_size_too_small": ("info", 67),
    "tight_line_height": ("info", 68),
    "pill_button_monoculture": ("info", 70),
    "emoji_as_icons": ("info", 80),
}
_SEVERITY_RANK: dict[str, int] = {"error": 0, "warning": 1, "info": 2}

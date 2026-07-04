"""C1 design-direction library: pure records, picker, and rendered contract."""

from __future__ import annotations

import re

from disco.core.design import (
    BANNED_PRIMARY_FONTS,
    DIRECTION_BY_ID,
    DIRECTION_IDS,
    DIRECTIONS,
    pick_direction,
    render_design_direction,
)

EXPECTED_IDS = {
    "editorial-magazine",
    "swiss-international",
    "brutalist",
    "soft-depth-saas",
    "terminal-mono",
    "luxury-serif",
    "playful-geometric",
    "dark-glass",
    "warm-craft",
}


def test_all_nine_directions_are_valid_records() -> None:
    assert set(DIRECTION_IDS) == EXPECTED_IDS
    assert len(DIRECTIONS) == 9
    assert set(DIRECTION_BY_ID) == EXPECTED_IDS

    for direction in DIRECTIONS:
        assert re.match(r"^#[0-9A-Fa-f]{6}$", direction.palette_seed)
        assert len(direction.accents) == 3
        assert direction.summary
        assert direction.image_art_direction
        assert direction.density
        assert direction.surface_treatment.tokens.radius
        assert direction.surface_treatment.tokens.shadow_level
        assert direction.surface_treatment.tokens.border_style
        primaries = (
            direction.font_pairing.heading.family,
            direction.font_pairing.body.family,
            direction.font_pairing.mono.family,
        )
        assert not ({name.lower() for name in primaries} & BANNED_PRIMARY_FONTS)
        assert all(stack.fallbacks for stack in (
            direction.font_pairing.heading,
            direction.font_pairing.body,
            direction.font_pairing.mono,
        ))


def test_dark_glass_matches_h2_defaults() -> None:
    dark = DIRECTION_BY_ID["dark-glass"]
    assert dark.surface_treatment.treatment == "glass"
    glass = dark.surface_treatment.glass
    assert glass is not None
    assert glass.blur_px == 14
    assert glass.saturate_pct == 160
    assert glass.light_fill_lane == "rgba(255,255,255,.08-.20)"
    assert glass.dark_fill_lane == "rgba(17,25,40,.45-.65)"
    assert glass.border_highlight_required is True
    assert glass.light_border_alpha_range == (0.25, 0.35)
    assert glass.dark_border_alpha_range == (0.10, 0.15)
    assert glass.shadow == "0 8px 32px rgba(0,0,0,.15-.30)"
    assert glass.radius_px_range == (12, 24)
    assert len(glass.rulebook) == 12


def test_pick_direction_is_deterministic_and_keyword_sensitive() -> None:
    brief = "Build a dark glass AI command center dashboard"
    assert pick_direction(brief, "conv-1") == pick_direction(brief, "conv-1")
    assert pick_direction(brief, "conv-1").id == "dark-glass"
    assert pick_direction("Luxury jewelry maison storefront", "conv-1").id == "luxury-serif"
    assert pick_direction("CLI logs and developer API docs", "conv-1").id == "terminal-mono"
    assert pick_direction("A local artisan bakery website", "conv-1").id == "warm-craft"


def test_render_design_direction_is_stable_and_carries_anti_slop_bans() -> None:
    direction = DIRECTION_BY_ID["dark-glass"]
    first = render_design_direction(direction)
    second = render_design_direction(direction)
    assert first == second
    assert first.startswith("## Design Direction: Dark Glass")
    assert "- ID: dark-glass" in first
    assert "blur=14px; saturate=160%" in first
    assert "border-highlight is mandatory" in first
    assert "DO:" in first
    assert "DON'T:" in first
    for banned in (
        "Inter, Roboto, or Arial",
        "purple-blue gradient default",
        "indigo-600 CTA",
        "hover:scale-105",
        "three identical feature cards",
        "generic FAQs",
        "five-column footer soup",
    ):
        assert banned in first

"""C1 design-direction library: pure records, picker, and rendered contract."""

from __future__ import annotations

import re

from disco.core.brand import theme_css_vars
from disco.core.design import (
    BANNED_PRIMARY_FONTS,
    DIRECTION_BY_ID,
    DIRECTION_IDS,
    DIRECTIONS,
    direction_from_markdown,
    direction_tokens_css,
    pick_direction,
    render_design_direction,
    to_brand_tokens,
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
    "console-dense",
    "enterprise-navy",
    "clinical-calm",
    "trust-fintech",
    "premium-consumer",
    "signal-noir",
    "lab-precise",
    "literate-docs",
    "noir-deco",
    "inkline-sketch",
    "controlled-maximalism",
    "gradient-mesh-warm",
    "pressed-botanical",
}


def test_all_directions_are_valid_records() -> None:
    assert set(DIRECTION_IDS) == EXPECTED_IDS
    assert len(DIRECTIONS) == 22
    assert set(DIRECTION_BY_ID) == EXPECTED_IDS

    for direction in DIRECTIONS:
        assert re.match(r"^#[0-9A-Fa-f]{6}$", direction.palette_seed)
        assert len(direction.accents) == 3
        assert direction.summary
        assert direction.image_art_direction
        assert direction.art_guidance
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
        assert all(
            stack.fallbacks
            for stack in (
                direction.font_pairing.heading,
                direction.font_pairing.body,
                direction.font_pairing.mono,
            )
        )


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


def test_functional_rbac_and_visual_direction_are_independent() -> None:
    # RBAC is not a visual keyword, so it must not change the chosen direction
    # relative to the same functional request without that requirement.
    assert pick_direction("Build a forum with RBAC", "conv-rbac") == pick_direction(
        "Build a forum", "conv-rbac"
    )
    assert pick_direction("Build a brutalist forum", "conv-style").id == "brutalist"


def test_render_design_direction_is_stable_and_carries_anti_slop_bans() -> None:
    direction = DIRECTION_BY_ID["dark-glass"]
    first = render_design_direction(direction)
    second = render_design_direction(direction)
    assert first == second
    assert first.startswith("## Design Direction: Dark Glass")
    assert "- ID: dark-glass" in first
    assert "blur=14px; saturate=160%" in first
    assert "border-highlight is mandatory" in first
    assert "- Art guidance:" in first
    assert "IMAGE-GEN-PREFERRED" in first
    assert "bespoke inline <svg>" in first
    assert "never use external stock URLs" in first
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


def test_art_guidance_sets_svg_first_and_image_gen_fallback_postures() -> None:
    rich_photographic = {
        "dark-glass",
        "warm-craft",
        "premium-consumer",
        "controlled-maximalism",
        "gradient-mesh-warm",
        "pressed-botanical",
    }
    svg_first = EXPECTED_IDS - rich_photographic

    for direction_id in svg_first:
        guidance = DIRECTION_BY_ID[direction_id].art_guidance
        assert guidance.startswith("SVG-FIRST")
        assert "inline <svg>" in guidance
        assert 'aria-hidden="true"' in guidance
        assert "external stock URLs" in guidance

    for direction_id in rich_photographic:
        guidance = DIRECTION_BY_ID[direction_id].art_guidance
        assert guidance.startswith("IMAGE-GEN-PREFERRED")
        assert "image_generate when configured" in guidance
        assert "if it fails or is unavailable" in guidance
        assert "bespoke inline <svg>" in guidance
        assert "Spot icons" in guidance and "stay SVG" in guidance


def test_direction_contract_roundtrips_to_brand_theme_shape() -> None:
    direction = DIRECTION_BY_ID["playful-geometric"]
    markdown = render_design_direction(direction)

    assert direction_from_markdown(markdown) == direction

    theme = to_brand_tokens(direction)
    assert theme.name == "direction-playful-geometric"
    assert theme.mode == "light"
    assert theme.accent == direction.accents[0].hex.lower()
    assert theme.verify_supported == direction.accents[1].hex.lower()
    assert theme.verify_weak == direction.accents[2].hex.lower()
    assert theme.font_display.startswith("'Space Grotesk'")
    assert theme.font_ui.startswith("'Nunito Sans'")
    assert theme.font_mono.startswith("'Fira Code'")
    assert theme.branded is True


def test_direction_tokens_css_is_deterministic_and_names_the_id() -> None:
    direction = DIRECTION_BY_ID["editorial-magazine"]
    css = direction_tokens_css(direction)

    # names the direction id in a header comment
    assert css.startswith("/* disco direction tokens — editorial-magazine */")
    # a real :root{…} custom-property block
    assert ":root{" in css
    # carries the committed fonts + palette vars
    assert "--display:Newsreader,Georgia,serif" in css
    assert "--mono:'IBM Plex Mono'" in css
    assert "--accent:#4b3f72" in css
    # pure composition of the two existing bridges
    assert css == (
        "/* disco direction tokens — editorial-magazine */\n"
        + theme_css_vars(to_brand_tokens(direction))
    )
    # deterministic: same input → byte-identical output
    assert direction_tokens_css(direction) == css

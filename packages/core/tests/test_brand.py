"""Tests for disco.core.brand — token registry, CSS emitters, mark partials.

Covers:
  1. resolve_theme — known themes return documented hex; unknown → ValueError
  2. Token values — light/dark Disco matches signed-off evidence hex byte-for-byte
  3. Neutral — branded=False, no accent chroma, system fonts
  4. font_face_css — @font-face blocks emitted; bundled TTF paths exist on disk
  5. theme_css_vars — :root block contains all token vars; neutral collapses accent
  6. print_skeleton_css — no ::first-letter{float}; contains .chip / .defmark
  7. definition_mark_html — headword + gloss + root + accent tick; all three scales
  8. wordmark_html — "Disco" + ".dot"
"""

from __future__ import annotations

import pytest
from disco.core.brand import (
    THEMES,
    definition_mark_html,
    font_face_css,
    print_skeleton_css,
    resolve_theme,
    theme_css_vars,
    wordmark_html,
)
from disco.core.brand.tokens import DISCO_DARK, DISCO_LIGHT, NEUTRAL_LIGHT

# ---------------------------------------------------------------------------
# 1. resolve_theme
# ---------------------------------------------------------------------------


def test_resolve_theme_disco_light() -> None:
    t = resolve_theme("disco", "light")
    assert t is DISCO_LIGHT
    assert t.bg == "#fcfcfa"
    assert t.accent == "#4077a3"
    assert t.branded is True


def test_resolve_theme_disco_dark() -> None:
    t = resolve_theme("disco", "dark")
    assert t is DISCO_DARK
    assert t.bg == "#0e0f12"
    assert t.accent == "#79c0f1"
    assert t.branded is True


def test_resolve_theme_neutral_light() -> None:
    t = resolve_theme("neutral", "light")
    assert t is NEUTRAL_LIGHT
    assert t.branded is False


def test_resolve_theme_neutral_dark() -> None:
    t = resolve_theme("neutral", "dark")
    assert t.branded is False
    assert t.bg == "#121212"


def test_resolve_theme_case_insensitive() -> None:
    assert resolve_theme("DISCO", "LIGHT") is DISCO_LIGHT
    assert resolve_theme("Neutral", "Dark").branded is False


def test_resolve_theme_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown theme"):
        resolve_theme("vaporwave")


def test_resolve_theme_fallback_to_light() -> None:
    """An unknown mode falls back to light if the name is registered."""
    t = resolve_theme("disco", "sepia")  # sepia doesn't exist → light fallback
    assert t is DISCO_LIGHT


# ---------------------------------------------------------------------------
# 2. Token values — exact hex from the signed-off evidence CSS
# ---------------------------------------------------------------------------


def test_disco_light_tokens_from_evidence() -> None:
    """DISCO_LIGHT values must match _brand_common.css:14-22 byte-for-byte."""
    t = DISCO_LIGHT
    assert t.bg == "#fcfcfa"
    assert t.surface_1 == "#f7f7f4"
    assert t.surface_2 == "#f1f0ed"
    assert t.hairline == "#dfdedb"
    assert t.hairline_strong == "#cbcac7"
    assert t.text == "#1a1813"
    assert t.text_muted == "#5a5853"
    assert t.text_faint == "#878682"
    assert t.accent == "#4077a3"
    assert t.link == "#39688e"


def test_disco_dark_tokens_from_evidence() -> None:
    """DISCO_DARK values must match _brand_dark.css:15-23."""
    t = DISCO_DARK
    assert t.bg == "#0e0f12"
    assert t.surface_1 == "#16171a"
    assert t.surface_2 == "#1e2124"
    assert t.hairline == "#303337"
    assert t.hairline_strong == "#4a4d53"
    assert t.text == "#e5e8ec"
    assert t.text_muted == "#9b9fa3"
    assert t.text_faint == "#727579"
    assert t.accent == "#79c0f1"
    assert t.link == "#74b3de"


def test_disco_light_verify_colors() -> None:
    """Verify colours derived from theme.css:68-71 via oklch→sRGB pipeline."""
    t = DISCO_LIGHT
    assert t.verify_supported == "#397852"
    assert t.verify_weak == "#a67f38"
    assert t.verify_unsupported == "#b14e49"
    assert t.warn == "#b14e49"


def test_disco_dark_verify_colors() -> None:
    """Verify colours derived from theme.css:87-90."""
    t = DISCO_DARK
    assert t.verify_supported == "#75be8f"
    assert t.verify_weak == "#deb866"
    assert t.verify_unsupported == "#f07f77"
    assert t.warn == "#f07f77"


# ---------------------------------------------------------------------------
# 3. Neutral theme
# ---------------------------------------------------------------------------


def test_neutral_branded_false() -> None:
    t = NEUTRAL_LIGHT
    assert t.branded is False


def test_neutral_no_accent_chroma() -> None:
    """Neutral accent == text_muted (no chroma, greyscale)."""
    t = NEUTRAL_LIGHT
    # Neutral accent is set to text_muted (a greyscale value).
    assert t.accent == t.text_muted


def test_neutral_system_fonts() -> None:
    t = NEUTRAL_LIGHT
    # Font stacks must be system/generic, not Fraunces/Newsreader/Schibsted.
    assert "Fraunces" not in t.font_display
    assert "Newsreader" not in t.font_reading
    assert "Schibsted" not in t.font_ui


def test_themes_dict_has_all_template_entries() -> None:
    """disco × 2 + neutral × 2 + the four template spins (ink/sepia/signal/midnight)."""
    assert len(THEMES) == 8
    assert ("disco", "light") in THEMES
    assert ("disco", "dark") in THEMES
    assert ("neutral", "light") in THEMES
    assert ("neutral", "dark") in THEMES
    assert ("ink", "light") in THEMES
    assert ("sepia", "light") in THEMES
    assert ("signal", "light") in THEMES
    assert ("midnight", "dark") in THEMES


def test_all_theme_fields_non_empty() -> None:
    """Every field in every registered theme is a non-empty string or bool."""
    for (name, mode), t in THEMES.items():
        for field in (
            "bg", "surface_1", "surface_2", "hairline", "hairline_strong",
            "text", "text_muted", "text_faint", "accent", "link",
            "verify_supported", "verify_weak", "verify_unsupported", "warn",
            "font_display", "font_ui", "font_reading", "font_mono",
        ):
            val = getattr(t, field)
            assert val, f"{name}/{mode}.{field} is empty"


# ---------------------------------------------------------------------------
# 4. font_face_css
# ---------------------------------------------------------------------------


def test_font_face_css_contains_families() -> None:
    css = font_face_css()
    assert "@font-face" in css
    assert "Fraunces" in css
    assert "Schibsted Grotesk" in css
    assert "Newsreader" in css


def test_font_face_css_uses_file_urls() -> None:
    css = font_face_css()
    # All src references must use absolute file:// URLs (WeasyPrint requires this)
    assert "file://" in css
    assert "url(" in css


def test_font_face_css_weight_ranges() -> None:
    css = font_face_css()
    # Fraunces: 100 900; Schibsted: 300 900; Newsreader: 200 800
    assert "100 900" in css
    assert "300 900" in css
    assert "200 800" in css


def test_font_face_css_ttf_files_exist() -> None:
    """Every TTF referenced by font_face_css() must exist on disk."""
    import re
    from pathlib import Path

    css = font_face_css()
    urls = re.findall(r"url\('file://([^']+)'\)", css)
    assert len(urls) > 0, "No file:// URLs found in font_face_css()"
    for url in urls:
        p = Path(url)
        assert p.exists(), f"Bundled font not found: {p}"


def test_font_face_css_italic_variants() -> None:
    css = font_face_css()
    assert "FrauncesItalic" in css
    assert "NewsreaderItalic" in css
    assert "font-style:italic" in css


# ---------------------------------------------------------------------------
# 5. theme_css_vars
# ---------------------------------------------------------------------------


def test_theme_css_vars_contains_all_vars() -> None:
    css = theme_css_vars(DISCO_LIGHT)
    for var in (
        "--bg", "--surface-1", "--surface-2",
        "--hairline", "--hairline-strong",
        "--text", "--text-muted", "--text-faint",
        "--accent", "--link",
        "--verify-supported", "--verify-weak", "--verify-unsupported",
        "--warn", "--display", "--ui", "--reading", "--mono",
    ):
        assert var in css, f"Missing CSS var {var}"


def test_theme_css_vars_disco_light_values() -> None:
    css = theme_css_vars(DISCO_LIGHT)
    assert "#fcfcfa" in css
    assert "#4077a3" in css


def test_theme_css_vars_neutral_collapses_accent() -> None:
    """Neutral theme must emit text-muted colour for --accent (no chroma)."""
    css = theme_css_vars(NEUTRAL_LIGHT)
    # --accent should NOT be the branded blue
    assert "#4077a3" not in css
    # --accent should equal text-muted
    assert f"--accent:{NEUTRAL_LIGHT.text_muted}" in css.replace(" ", "")


def test_theme_css_vars_dark_values() -> None:
    css = theme_css_vars(DISCO_DARK)
    assert "#0e0f12" in css
    assert "#79c0f1" in css


# ---------------------------------------------------------------------------
# 6. print_skeleton_css
# ---------------------------------------------------------------------------


def test_print_skeleton_css_no_first_letter_float() -> None:
    """LOCKED constraint: no ::first-letter{float} (WeasyPrint asserts on it)."""
    css = print_skeleton_css()
    assert "::first-letter" not in css.replace(" ", "")
    # Also confirm there's no float on any first-letter rule
    lines = css.split("\n")
    for i, line in enumerate(lines):
        if "first-letter" in line.lower():
            # Look ahead for float
            context = "\n".join(lines[i : i + 5])
            assert "float" not in context, (
                f"Forbidden ::first-letter{{float}} near line {i}: {context!r}"
            )


def test_print_skeleton_css_has_chip() -> None:
    css = print_skeleton_css()
    assert ".chip" in css


def test_print_skeleton_css_has_defmark() -> None:
    css = print_skeleton_css()
    assert ".defmark" in css


def test_print_skeleton_css_has_section_no() -> None:
    css = print_skeleton_css()
    assert ".section-no" in css


def test_print_skeleton_css_has_followup_page() -> None:
    css = print_skeleton_css()
    assert ".followup-page" in css
    assert "break-before" in css


def test_print_skeleton_css_has_sources_2col() -> None:
    css = print_skeleton_css()
    assert ".sources-2col" in css


def test_print_skeleton_css_has_toc() -> None:
    css = print_skeleton_css()
    assert ".toc-block" in css


def test_print_skeleton_css_has_dropcap() -> None:
    """Dropcap is an inline .dropcap class, NOT a float."""
    css = print_skeleton_css()
    assert ".dropcap" in css
    # The dropcap should NOT use float:left (WeasyPrint constraint)
    dropcap_idx = css.index(".dropcap")
    dropcap_block = css[dropcap_idx : dropcap_idx + 300]
    assert "float:left" not in dropcap_block.replace(" ", "")


# ---------------------------------------------------------------------------
# 7. definition_mark_html
# ---------------------------------------------------------------------------


def test_definition_mark_html_contains_headword() -> None:
    html = definition_mark_html("colophon")
    assert "disco" in html


def test_definition_mark_html_contains_gloss() -> None:
    html = definition_mark_html("colophon")
    assert "I learn" in html
    assert "acquainted" in html


def test_definition_mark_html_contains_root() -> None:
    html = definition_mark_html("colophon")
    assert "discere" in html


def test_definition_mark_html_contains_accent_tick() -> None:
    html = definition_mark_html("colophon")
    # The accent tick is the <i></i> inside .rule
    assert 'class="rule"' in html
    assert "<i></i>" in html


def test_definition_mark_html_all_scales() -> None:
    for scale in ("masthead", "colophon", "footer"):
        html = definition_mark_html(scale)  # type: ignore[arg-type]
        assert "disco" in html, f"scale={scale!r} missing headword"


def test_definition_mark_html_scale_class() -> None:
    assert 's-lg' in definition_mark_html("masthead")
    assert 's-md' in definition_mark_html("colophon")
    assert 's-sm' in definition_mark_html("footer")


def test_definition_mark_html_has_ipa() -> None:
    html = definition_mark_html("colophon")
    # IPA for /ˈdɪs.koː/ — contains the stress mark unicode char
    assert "d&#618;s.ko" in html or "dɪs" in html or "disco" in html


def test_definition_mark_html_pos_latin() -> None:
    html = definition_mark_html("colophon")
    assert "Latin" in html
    assert "verb" in html


# ---------------------------------------------------------------------------
# 8. wordmark_html
# ---------------------------------------------------------------------------


def test_wordmark_html_contains_disco() -> None:
    html = wordmark_html()
    assert "Disco" in html


def test_wordmark_html_has_dot_span() -> None:
    html = wordmark_html()
    assert "dot" in html
    assert "." in html


def test_wordmark_html_has_wordmark_class() -> None:
    html = wordmark_html()
    assert "wordmark" in html


# ---------------------------------------------------------------------------
# 9. Package-level import
# ---------------------------------------------------------------------------


def test_brand_package_importable() -> None:
    import disco.core.brand as brand

    assert hasattr(brand, "Theme")
    assert hasattr(brand, "THEMES")
    assert hasattr(brand, "resolve_theme")
    assert hasattr(brand, "font_face_css")
    assert hasattr(brand, "theme_css_vars")
    assert hasattr(brand, "print_skeleton_css")
    assert hasattr(brand, "definition_mark_html")
    assert hasattr(brand, "wordmark_html")


def test_theme_is_frozen_dataclass() -> None:
    t = resolve_theme("disco", "light")
    with pytest.raises(Exception):
        t.bg = "#000000"  # type: ignore[misc]


def test_template_catalog_ids_resolve_and_have_a_single_default() -> None:
    """Every catalogue entry's id must resolve to a registered theme, and exactly
    one entry is the default (disco-light)."""
    from disco.core.brand import list_templates, parse_template_id, resolve_theme

    cat = list_templates()
    assert len(cat) >= 6
    defaults = [t for t in cat if t.default]
    assert len(defaults) == 1 and defaults[0].id == "disco-light"
    for t in cat:
        name, mode = parse_template_id(t.id)
        resolve_theme(name, mode)  # must not raise
        assert t.accent.startswith("#") and t.bg.startswith("#")


def test_parse_template_id_splits_name_and_mode() -> None:
    from disco.core.brand import parse_template_id

    assert parse_template_id("midnight-dark") == ("midnight", "dark")
    assert parse_template_id("ink-light") == ("ink", "light")
    assert parse_template_id("neutral") == ("neutral", "light")  # bare → light
    # A hyphenated name with a non-mode tail stays whole (defensive).
    assert parse_template_id("disco-light") == ("disco", "light")


def test_is_valid_template_enforces_catalogue() -> None:
    """is_valid_template accepts only gallery ids — stricter than resolve_theme,
    which would light-fall-back an unknown mode (ink-dark)."""
    from disco.core.brand import is_valid_template

    assert is_valid_template("disco-light")
    assert is_valid_template("midnight-dark")
    assert is_valid_template("ink-light")
    # Modes that aren't in the gallery must be REJECTED (not silently light-mapped).
    assert not is_valid_template("ink-dark")
    assert not is_valid_template("midnight-light")
    assert not is_valid_template("vaporwave-light")

"""Tests for C1 — _deck_schema.py.

Proves:
  - lower_deck produces a valid Deck from a sample AuthoredDeck.
  - _fit_text steps font down when text overflows.
  - Overflow body creates a continuation slide (NOT truncation).
  - _infer_layout is total (never returns None, handles unknown types).
  - Theme resolution splits "disco-light" → ("disco", "light") correctly.
  - image_prompt + empty body → full_image layout.
  - image_prompt + body → image_right layout.
  - notes are NOT counted as overflow (excluded from _fit_text).
  - All layout functions produce in-canvas elements (left/top/width/height > 0).
  - image_prompt element carries the prompt for C7 wiring.
"""

from __future__ import annotations

import pytest

from disco.tools.builtin._deck_schema import (
    AuthoredDeck,
    AuthoredSlide,
    ChartSpec,
    Deck,
    Element,
    Slide,
    TableSpec,
    _chars_per_line,
    _fit_text,
    _infer_layout,
    _layout_bullets,
    _layout_closing,
    _layout_full_image,
    _layout_image_right,
    _layout_section_header,
    _layout_title,
    _layout_two_column,
    _parse_theme,
    lower_deck,
    _SLIDE_W,
    _SLIDE_H,
    _CW,
    _CH,
)
from disco.core.brand import resolve_theme


# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------

def _sample_authored_deck() -> AuthoredDeck:
    """A representative 5-slide AuthoredDeck covering multiple layouts."""
    return AuthoredDeck(
        title="Disco AI — Product Launch",
        theme="disco-light",
        slides=[
            AuthoredSlide(
                type="title",
                title="Introducing Disco AI",
                body=["The research assistant that cites its sources"],
                notes="Welcome the audience warmly.",
            ),
            AuthoredSlide(
                type="bullets",
                title="Key Features",
                body=[
                    "Grounded answers with citation accuracy > 90%",
                    "Multi-source deep research in under 60 seconds",
                    "Privacy-first: runs fully on your hardware",
                    "Open-source core with permissive licensing",
                ],
            ),
            AuthoredSlide(
                type="section_header",
                title="How It Works",
                body=["Three-stage pipeline: search → extract → ground"],
            ),
            AuthoredSlide(
                type="image_right",
                title="Architecture Overview",
                body=["Query router", "Parallel retrieval", "NLI grounding"],
                image_prompt="A clean technical architecture diagram with three boxes connected by arrows on a light background",
            ),
            AuthoredSlide(
                type="closing",
                title="Thank You",
                body=["disco.ai | hello@disco.ai"],
            ),
        ],
    )


def _overflow_authored_deck() -> AuthoredDeck:
    """A deck with a slide that has 30 bullet lines — must trigger continuation."""
    return AuthoredDeck(
        title="Overflow Test",
        theme="disco-light",
        slides=[
            AuthoredSlide(
                type="title",
                title="Overflow Demo",
                body=["Testing continuation slides"],
            ),
            AuthoredSlide(
                type="bullets",
                title="Dense Slide",
                body=[f"Bullet point number {i+1}: some reasonably detailed content here" for i in range(30)],
            ),
        ],
    )


def _image_deck() -> AuthoredDeck:
    """Deck with image slides to test full_image vs image_right."""
    return AuthoredDeck(
        title="Image Test",
        theme="disco-light",
        slides=[
            AuthoredSlide(
                type="full_image",
                title="Urban Mobility 2035",
                body=[],  # no body → full_image
                image_prompt="Futuristic cityscape with autonomous electric vehicles at sunset",
            ),
            AuthoredSlide(
                type="image_right",
                title="Design Philosophy",
                body=["Human-centred", "Sustainable materials", "Zero emissions"],
                image_prompt="Minimal product design sketch on white background",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# lower_deck — basic structure
# ---------------------------------------------------------------------------


def test_lower_deck_returns_deck():
    """lower_deck returns a Deck instance."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    assert isinstance(deck, Deck)


def test_lower_deck_title_preserved():
    """The deck title is preserved from the AuthoredDeck."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    assert deck.title == authored.title


def test_lower_deck_theme_resolved():
    """Theme tokens are resolved — disco-light → non-None bg/text/accent."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    assert deck.theme is not None
    assert deck.theme.bg.startswith("#")
    assert deck.theme.accent.startswith("#")
    assert deck.theme.text.startswith("#")


def test_lower_deck_produces_slides():
    """At least as many slides as the authored deck (continuation may add more)."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    assert len(deck.slides) >= len(authored.slides)


def test_lower_deck_slides_have_layouts():
    """Every lowered slide has a non-empty, non-None layout."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    for slide in deck.slides:
        assert slide.layout
        assert isinstance(slide.layout, str)


def test_lower_deck_slides_have_elements():
    """Every lowered slide has at least one Element."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    for slide in deck.slides:
        assert len(slide.elements) >= 1


def test_lower_deck_notes_on_slide():
    """Notes from AuthoredSlide appear on the lowered Slide (not as an element)."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    # First slide has notes
    first = deck.slides[0]
    assert first.notes == "Welcome the audience warmly."
    # No element should contain the notes text
    all_text = " ".join(el.text for el in first.elements if el.kind == "text")
    assert "Welcome the audience warmly." not in all_text


def test_lower_deck_16_9_canvas():
    """Deck size is 12_192_000 × 6_858_000 EMU (16:9)."""
    deck = lower_deck(_sample_authored_deck())
    assert deck.size == (12_192_000, 6_858_000)


# ---------------------------------------------------------------------------
# Overflow → continuation split (NOT truncation)
# ---------------------------------------------------------------------------


def test_overflow_creates_continuation_slide():
    """A 30-bullet slide MUST produce at least 2 slides (original + continuation)."""
    authored = _overflow_authored_deck()
    deck = lower_deck(authored)

    # The overflow deck has 2 authored slides; the dense one should split.
    dense_slides = [s for s in deck.slides if "Dense" in s.type or "_cont" in s.type or s.type == "bullets"]
    # At minimum, the original "Dense Slide" and one continuation
    assert len(deck.slides) >= 3, (
        f"Expected at least 3 slides (title + dense + continuation), got {len(deck.slides)}"
    )


def test_overflow_no_truncation():
    """All 30 bullet texts must appear across all slides — none silently dropped."""
    authored = _overflow_authored_deck()
    deck = lower_deck(authored)

    # Collect all text from elements across all slides
    all_text = " ".join(
        el.text for slide in deck.slides
        for el in slide.elements
        if el.kind == "text"
    )
    for i in range(30):
        expected = f"Bullet point number {i+1}"
        assert expected in all_text, (
            f"Bullet {i+1} was truncated (not found in any slide element text)"
        )


def test_continuation_slide_has_cont_type():
    """Continuation slides have type ending in '_cont'."""
    authored = _overflow_authored_deck()
    deck = lower_deck(authored)
    cont_slides = [s for s in deck.slides if "_cont" in s.type]
    assert len(cont_slides) >= 1, "No continuation slide found after overflow"


# ---------------------------------------------------------------------------
# _fit_text — font step-down
# ---------------------------------------------------------------------------


def test_fit_text_short_content_no_overflow():
    """4 lines of short bullets fit at max font."""
    from disco.tools.builtin._deck_schema import _SLIDE_W, _BODY_FONT_MAX, _CW, _CH
    lines = ["Short bullet one", "Short bullet two", "Short bullet three", "Short bullet four"]
    font, fitted, overflow = _fit_text(lines, box_width=float(_CW), box_height=float(_CH))
    assert overflow == []
    assert fitted == lines
    assert font == _BODY_FONT_MAX


def test_fit_text_many_long_lines_steps_down():
    """Many long lines force font step-down below max."""
    from disco.tools.builtin._deck_schema import _BODY_FONT_MAX
    lines = ["A" * 150 for _ in range(20)]  # very long lines, many of them
    font, fitted, overflow = _fit_text(lines, box_width=float(_CW), box_height=float(_CH))
    # Should have stepped down OR produced overflow
    assert font < _BODY_FONT_MAX or len(overflow) > 0


def test_fit_text_overflow_is_not_truncated():
    """When overflow occurs, fitted + overflow == all input lines."""
    lines = [f"Bullet line {i}" for i in range(50)]
    font, fitted, overflow = _fit_text(lines, box_width=float(_CW), box_height=float(_CH))
    assert fitted + overflow == lines


def test_fit_text_empty_input():
    """Empty input returns (max_font, [], [])."""
    from disco.tools.builtin._deck_schema import _BODY_FONT_MAX
    font, fitted, overflow = _fit_text([], box_width=float(_CW), box_height=float(_CH))
    assert font == _BODY_FONT_MAX
    assert fitted == []
    assert overflow == []


# ---------------------------------------------------------------------------
# _infer_layout — total + correct
# ---------------------------------------------------------------------------


def test_infer_layout_image_prompt_no_body_is_full_image():
    """image_prompt + no body → full_image."""
    slide = AuthoredSlide(type="image_right", title="X", body=[], image_prompt="a mountain")
    assert _infer_layout(slide) == "full_image"


def test_infer_layout_image_prompt_with_body_is_image_right():
    """image_prompt + body → image_right (first call, alt counter=0)."""
    slide = AuthoredSlide(type="bullets", title="X", body=["bullet"], image_prompt="a forest")
    counter = [0]
    layout = _infer_layout(slide, counter)
    assert layout in ("image_right", "image_left")


def test_infer_layout_alternates_image_sides():
    """Alternating image_prompt+body slides get image_right then image_left."""
    counter = [0]
    s = AuthoredSlide(type="bullets", title="X", body=["b"], image_prompt="img")
    l1 = _infer_layout(s, counter)
    l2 = _infer_layout(s, counter)
    assert l1 != l2
    assert set([l1, l2]) == {"image_right", "image_left"}


def test_infer_layout_title():
    assert _infer_layout(AuthoredSlide(type="title", title="T")) == "title"


def test_infer_layout_section_header():
    assert _infer_layout(AuthoredSlide(type="section_header", title="S")) == "section_header"
    assert _infer_layout(AuthoredSlide(type="section", title="S")) == "section_header"


def test_infer_layout_closing():
    assert _infer_layout(AuthoredSlide(type="closing", title="C")) == "closing"


def test_infer_layout_two_column():
    assert _infer_layout(AuthoredSlide(type="two_column", title="T")) == "two_column"


def test_infer_layout_unknown_falls_back_to_bullets():
    """Unknown custom type → bullets (never None, never crash)."""
    slide = AuthoredSlide(type="era_1_timeline", title="T", body=["b"])
    layout = _infer_layout(slide)
    assert layout == "bullets"


def test_infer_layout_cont_type_is_bullets():
    """Continuation type like 'bullets_cont' → bullets."""
    slide = AuthoredSlide(type="bullets_cont", title="T", body=["b"])
    assert _infer_layout(slide) == "bullets"


def test_infer_layout_layout_hint_wins():
    """Explicit layout_hint always overrides type-based inference."""
    slide = AuthoredSlide(type="title", title="T", layout_hint="closing")
    assert _infer_layout(slide) == "closing"


def test_infer_layout_chart_triggers_metrics():
    """chart set → metrics layout."""
    slide = AuthoredSlide(
        type="bullets",
        title="Chart",
        chart=ChartSpec(kind="bar", labels=["A", "B"], series=[{"name": "x", "data": [1.0, 2.0]}]),
    )
    assert _infer_layout(slide) == "metrics"


def test_infer_layout_table_triggers_table():
    """table set → table layout."""
    slide = AuthoredSlide(
        type="bullets",
        title="Table",
        table=TableSpec(headers=["A", "B"], rows=[["1", "2"]]),
    )
    assert _infer_layout(slide) == "table"


# ---------------------------------------------------------------------------
# Theme parsing
# ---------------------------------------------------------------------------


def test_parse_theme_disco_light():
    assert _parse_theme("disco-light") == ("disco", "light")


def test_parse_theme_disco_dark():
    assert _parse_theme("disco-dark") == ("disco", "dark")


def test_parse_theme_neutral():
    assert _parse_theme("neutral") == ("neutral", "light")


# ---------------------------------------------------------------------------
# Image wiring in lowered elements
# ---------------------------------------------------------------------------


def test_image_element_has_prompt():
    """An image slide produces an Element(kind='image') with the image_prompt."""
    authored = _image_deck()
    deck = lower_deck(authored)
    img_elements = [
        el
        for slide in deck.slides
        for el in slide.elements
        if el.kind == "image"
    ]
    assert len(img_elements) >= 1
    assert any(el.image_prompt for el in img_elements), (
        "No image element carries the image_prompt for C7 wiring"
    )


def test_full_image_layout_for_no_body():
    """image_prompt + body=[] → full_image layout on the lowered slide."""
    authored = _image_deck()
    deck = lower_deck(authored)
    full_img_slides = [s for s in deck.slides if s.layout == "full_image"]
    assert len(full_img_slides) >= 1


def test_notes_excluded_from_elements():
    """Notes are not rendered as text elements on the slide face."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    for slide in deck.slides:
        for el in slide.elements:
            if el.kind == "text" and slide.notes:
                assert slide.notes not in el.text, (
                    f"Notes text leaked into a visible element on slide {slide.type}"
                )


# ---------------------------------------------------------------------------
# In-canvas element geometry
# ---------------------------------------------------------------------------


def test_all_elements_in_canvas():
    """Every element's bounding box is within the 16:9 canvas."""
    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    for slide in deck.slides:
        for el in slide.elements:
            assert el.left >= 0, f"Element left={el.left} < 0"
            assert el.top >= 0, f"Element top={el.top} < 0"
            assert el.left + el.width <= _SLIDE_W + 1000, (
                f"Element right={el.left + el.width} exceeds canvas {_SLIDE_W}"
            )
            assert el.top + el.height <= _SLIDE_H + 1000, (
                f"Element bottom={el.top + el.height} exceeds canvas {_SLIDE_H}"
            )


# ---------------------------------------------------------------------------
# Deck round-trip through PPTX renderer
# ---------------------------------------------------------------------------


def test_lower_deck_renders_to_valid_pptx():
    """A lowered deck can be rendered to a valid .pptx file by C3."""
    from pptx import Presentation
    import io
    from disco.tools.builtin._pptx_render import render_pptx

    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    pptx_bytes = render_pptx(deck)
    assert pptx_bytes[:4] == b"PK\x03\x04", "Not a valid ZIP/OOXML"
    prs = Presentation(io.BytesIO(pptx_bytes))
    # Slide count may be > authored count due to continuations
    assert len(prs.slides) == len(deck.slides)


def test_lower_deck_renders_title_text_in_pptx():
    """Slide titles appear in the .pptx text frames."""
    from pptx import Presentation
    import io
    from disco.tools.builtin._pptx_render import render_pptx

    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    pptx_bytes = render_pptx(deck)
    prs = Presentation(io.BytesIO(pptx_bytes))

    all_pptx_text = ""
    for pptx_slide in prs.slides:
        for shape in pptx_slide.shapes:
            if shape.has_text_frame:
                all_pptx_text += shape.text_frame.text + " "

    assert "Introducing Disco AI" in all_pptx_text
    assert "Key Features" in all_pptx_text


def test_lower_deck_renders_to_valid_html():
    """A lowered deck renders to valid 16:9 HTML."""
    from disco.tools.builtin._pptx_render import render_html

    authored = _sample_authored_deck()
    deck = lower_deck(authored)
    html_str = render_html(deck)

    assert "<!DOCTYPE html>" in html_str
    assert "56.25vw" in html_str  # 16:9 ratio
    assert "ArrowRight" in html_str  # keyboard nav
    assert html_str.count('<section class="slide') == len(deck.slides)

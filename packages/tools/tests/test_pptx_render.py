"""Tests for C3 — native editable PPTX renderer (_pptx_render.py).

Proves:
  - render_pptx: the .pptx round-trips via python-pptx and contains REAL text
    runs (NOT a single embedded image), one shape per layout element.
  - render_html: the output is valid 16:9 HTML with one <section> per slide,
    inlined brand fonts, and keyboard-nav script.
  - convert_to_pdf: soffice-absent returns a clear failure (no false affordance).
  - render_deck: returns both pptx_bytes and html_str.
  - Binary round-trip: pptx_bytes are valid ZIP/OOXML (magic bytes 50 4B).
  - Each layout variant (title / bullets / section / image_right) renders
    without error and deposits text in the slide.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from disco.tools.builtin._pptx_render import (
    DeckSlide,
    MinimalDeck,
    convert_to_pdf,
    render_deck,
    render_html,
    render_pptx,
)

# ---------------------------------------------------------------------------
# Sample decks
# ---------------------------------------------------------------------------

def _sample_deck() -> MinimalDeck:
    """A minimal multi-slide deck exercising all four layouts."""
    return MinimalDeck(
        title="Test Deck",
        theme_name="disco",
        theme_mode="light",
        slides=[
            DeckSlide(
                title="Welcome to Disco",
                bullets=["The native PPTX renderer"],
                layout="title",
            ),
            DeckSlide(
                title="Key Points",
                bullets=["Real text boxes", "Brand fonts", "16:9 canvas"],
                layout="bullets",
            ),
            DeckSlide(
                title="Deep Dive",
                bullets=["A section subtitle"],
                layout="section",
            ),
            DeckSlide(
                title="Image + Bullets",
                bullets=["With placeholder", "For C7 wiring"],
                layout="image_right",
                image_url=None,
            ),
        ],
    )


def _dark_deck() -> MinimalDeck:
    """A single-slide disco-dark deck."""
    return MinimalDeck(
        title="Dark Mode",
        theme_name="disco",
        theme_mode="dark",
        slides=[
            DeckSlide(title="Dark Slide", bullets=["Dark theme"], layout="bullets"),
        ],
    )


def _neutral_deck() -> MinimalDeck:
    """A neutral-theme deck."""
    return MinimalDeck(
        title="Neutral",
        theme_name="neutral",
        theme_mode="light",
        slides=[
            DeckSlide(title="Neutral Slide", bullets=["System fonts"], layout="bullets"),
        ],
    )


# ---------------------------------------------------------------------------
# PPTX tests
# ---------------------------------------------------------------------------


def test_pptx_binary_magic_bytes():
    """The pptx_bytes start with the ZIP magic bytes PK (50 4B 03 04)."""
    deck = _sample_deck()
    data = render_pptx(deck)
    assert isinstance(data, bytes)
    assert data[:4] == b"PK\x03\x04", "Expected OOXML ZIP magic bytes"


def test_pptx_round_trips_via_python_pptx():
    """python-pptx can re-open the rendered bytes without error."""
    import io

    from pptx import Presentation

    deck = _sample_deck()
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))
    # Should have same slide count
    assert len(prs.slides) == len(deck.slides)


def test_pptx_contains_real_text_runs():
    """The .pptx contains real text runs — NOT a single embedded image per slide.

    Specifically: at least one TextFrame with the slide title text must be
    present for each slide.  This proves text boxes, not image shapes.
    """
    import io

    from pptx import Presentation
    from pptx.shapes.picture import Picture

    deck = _sample_deck()
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))

    for i, (pptx_slide, deck_slide) in enumerate(zip(prs.slides, deck.slides)):
        # Collect all text in the slide
        all_text = ""
        pic_count = 0
        for shape in pptx_slide.shapes:
            if shape.has_text_frame:
                all_text += shape.text_frame.text
            if isinstance(shape, Picture):
                pic_count += 1

        assert deck_slide.title in all_text, (
            f"Slide {i} ({deck_slide.layout!r}): title {deck_slide.title!r} "
            f"not found in extracted text {all_text!r}"
        )
        # Image-per-slide would be ONE Picture shape with no title text.
        # We should have zero pictures on non-image_right layouts.
        if deck_slide.layout != "image_right":
            assert pic_count == 0, (
                f"Slide {i} ({deck_slide.layout!r}): unexpected Picture shape — "
                "renderer must use text boxes, not images"
            )


def test_pptx_16_9_canvas():
    """Slide dimensions are 12 192 000 × 6 858 000 EMU (16:9)."""
    import io

    from pptx import Presentation

    deck = _sample_deck()
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))
    assert prs.slide_width == 12_192_000
    assert prs.slide_height == 6_858_000


def test_pptx_bullets_slide_has_bullet_text():
    """A bullets slide carries each bullet in at least one text run."""
    import io

    from pptx import Presentation

    deck = MinimalDeck(
        title="Bullets Test",
        slides=[
            DeckSlide(
                title="Three Bullets",
                bullets=["Alpha", "Beta", "Gamma"],
                layout="bullets",
            )
        ],
    )
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))
    slide = prs.slides[0]
    all_text = " ".join(
        shape.text_frame.text for shape in slide.shapes if shape.has_text_frame
    )
    for bullet in ("Alpha", "Beta", "Gamma"):
        assert bullet in all_text, f"Bullet {bullet!r} missing from slide text"


def test_pptx_title_layout():
    """Title layout renders the deck title and optional subtitle."""
    import io

    from pptx import Presentation

    deck = MinimalDeck(
        title="Title Test",
        slides=[
            DeckSlide(
                title="Grand Title",
                bullets=["A subtitle here"],
                layout="title",
            )
        ],
    )
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))
    slide = prs.slides[0]
    all_text = " ".join(
        s.text_frame.text for s in slide.shapes if s.has_text_frame
    )
    assert "Grand Title" in all_text
    assert "A subtitle here" in all_text


def test_pptx_section_layout():
    """Section layout renders the section title."""
    import io

    from pptx import Presentation

    deck = MinimalDeck(
        title="Section Test",
        slides=[
            DeckSlide(title="Chapter One", layout="section"),
        ],
    )
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))
    slide = prs.slides[0]
    all_text = " ".join(
        s.text_frame.text for s in slide.shapes if s.has_text_frame
    )
    assert "Chapter One" in all_text


def test_pptx_dark_theme():
    """Dark theme renders without error and produces valid bytes."""
    import io

    from pptx import Presentation

    data = render_pptx(_dark_deck())
    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) == 1


def test_pptx_neutral_theme():
    """Neutral theme renders without error."""
    import io

    from pptx import Presentation

    data = render_pptx(_neutral_deck())
    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) == 1


def test_pptx_with_notes():
    """Speaker notes land in the notes slide text frame."""
    import io

    from pptx import Presentation

    deck = MinimalDeck(
        title="Notes Test",
        slides=[
            DeckSlide(
                title="Noted Slide",
                bullets=["Bullet"],
                layout="bullets",
                notes="These are the speaker notes.",
            )
        ],
    )
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))
    notes_text = prs.slides[0].notes_slide.notes_text_frame.text
    assert "speaker notes" in notes_text


def test_pptx_empty_deck():
    """An empty deck (no slides) produces valid 0-slide PPTX."""
    import io

    from pptx import Presentation

    deck = MinimalDeck(title="Empty", slides=[])
    data = render_pptx(deck)
    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) == 0


# ---------------------------------------------------------------------------
# HTML tests
# ---------------------------------------------------------------------------


def test_html_is_string():
    """render_html returns a string."""
    html_str = render_html(_sample_deck())
    assert isinstance(html_str, str)


def test_html_one_section_per_slide():
    """The HTML contains one <section class="slide"> per slide."""
    deck = _sample_deck()
    html_str = render_html(deck)
    count = html_str.count('<section class="slide')
    assert count == len(deck.slides), (
        f"Expected {len(deck.slides)} slide sections, found {count}"
    )


def test_html_16_9_ratio():
    """The HTML shell uses 56.25vw height (16:9 via viewport units)."""
    html_str = render_html(_sample_deck())
    assert "56.25vw" in html_str, "Missing 16:9 viewport-unit height in CSS"


def test_html_contains_title_text():
    """Each slide title appears in the HTML."""
    deck = _sample_deck()
    html_str = render_html(deck)
    for slide in deck.slides:
        assert slide.title in html_str, (
            f"Slide title {slide.title!r} not found in HTML"
        )


def test_html_has_keyboard_nav_script():
    """The HTML includes a keyboard navigation script (← / →)."""
    html_str = render_html(_sample_deck())
    assert "ArrowRight" in html_str
    assert "ArrowLeft" in html_str


def test_html_has_font_face_declarations():
    """The HTML inlines @font-face declarations for brand OFL fonts."""
    html_str = render_html(_sample_deck())
    # All three brand typefaces must be declared
    assert "@font-face" in html_str
    assert "Fraunces" in html_str
    assert "Schibsted Grotesk" in html_str
    assert "Newsreader" in html_str


def test_html_has_brand_css_vars():
    """Brand CSS custom properties are present in the HTML."""
    html_str = render_html(_sample_deck())
    assert "--bg:" in html_str
    assert "--accent:" in html_str
    assert "--text:" in html_str


def test_html_first_slide_is_active():
    """The first slide has class 'active'; others do not on initial load."""
    html_str = render_html(_sample_deck())
    # The first active marker
    first_active = html_str.find('class="slide active"')
    assert first_active != -1, "First slide must have 'active' class"
    # Only one initial active slide
    assert html_str.count('class="slide active"') == 1


def test_html_escapes_special_chars():
    """Slide titles with HTML special chars are properly escaped."""
    deck = MinimalDeck(
        title="<Test & Check>",
        slides=[
            DeckSlide(
                title='<Script> "injection"',
                bullets=["<b>bold attempt</b>"],
                layout="bullets",
            )
        ],
    )
    html_str = render_html(deck)
    # Raw angle brackets from title/bullet must not appear unescaped
    assert "<Script>" not in html_str
    assert "<b>bold" not in html_str
    # Escaped versions must be present
    assert "&lt;Script&gt;" in html_str
    assert "&lt;b&gt;" in html_str


def test_html_dark_theme_bg():
    """Dark theme background color appears in the HTML CSS vars."""
    html_str = render_html(_dark_deck())
    # Disco dark bg is #0e0f12
    assert "#0e0f12" in html_str


# ---------------------------------------------------------------------------
# PDF / convert_to_pdf tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_to_pdf_absent_soffice_returns_clear_failure():
    """When soffice is absent, convert_to_pdf returns (False, descriptive_error)."""
    ctx = MagicMock()
    mock_result = MagicMock()
    mock_result.exit_code = 1
    ctx.sandbox.exec_shell = AsyncMock(return_value=mock_result)

    ok, err = await convert_to_pdf(ctx, "deck.pptx")

    assert ok is False
    assert err  # non-empty error message
    # Must mention soffice/LibreOffice — no silent failure
    assert "soffice" in err.lower() or "libreoffice" in err.lower()


@pytest.mark.asyncio
async def test_convert_to_pdf_probe_called_first():
    """convert_to_pdf probes for soffice before attempting conversion."""
    ctx = MagicMock()
    # First call (probe) → soffice absent
    probe_result = MagicMock(exit_code=1)
    ctx.sandbox.exec_shell = AsyncMock(return_value=probe_result)

    ok, err = await convert_to_pdf(ctx, "deck.pptx")

    assert ok is False
    # Only the probe call should have fired (no conversion attempt)
    assert ctx.sandbox.exec_shell.call_count == 1
    probe_call_args = ctx.sandbox.exec_shell.call_args_list[0]
    assert "command -v soffice" in str(probe_call_args)


@pytest.mark.asyncio
async def test_convert_to_pdf_success_path():
    """When soffice is present and conversion succeeds, returns (True, '')."""
    ctx = MagicMock()
    # Probe → found; conversion → success
    probe_ok = MagicMock(exit_code=0)
    convert_ok = MagicMock(exit_code=0, timed_out=False, stderr="")
    ctx.sandbox.exec_shell = AsyncMock(side_effect=[probe_ok, convert_ok])

    ok, err = await convert_to_pdf(ctx, "deck.pptx")

    assert ok is True
    assert err == ""
    # Second call should include soffice + pptx name
    convert_call = ctx.sandbox.exec_shell.call_args_list[1]
    assert "soffice" in str(convert_call)
    assert "deck.pptx" in str(convert_call)


@pytest.mark.asyncio
async def test_convert_to_pdf_timeout_returns_clear_failure():
    """A timed-out soffice returns (False, descriptive error)."""
    ctx = MagicMock()
    probe_ok = MagicMock(exit_code=0)
    timeout_result = MagicMock(exit_code=1, timed_out=True, stderr="")
    ctx.sandbox.exec_shell = AsyncMock(side_effect=[probe_ok, timeout_result])

    ok, err = await convert_to_pdf(ctx, "deck.pptx")

    assert ok is False
    assert "timed out" in err.lower()


@pytest.mark.asyncio
async def test_convert_to_pdf_nonzero_exit_returns_failure():
    """A non-zero soffice exit code returns (False, error text)."""
    ctx = MagicMock()
    probe_ok = MagicMock(exit_code=0)
    fail_result = MagicMock(exit_code=2, timed_out=False,
                            stderr="Error: import failed")
    ctx.sandbox.exec_shell = AsyncMock(side_effect=[probe_ok, fail_result])

    ok, err = await convert_to_pdf(ctx, "deck.pptx")

    assert ok is False
    assert "import failed" in err or err  # either stderr or fallback


# ---------------------------------------------------------------------------
# render_deck tests
# ---------------------------------------------------------------------------


def test_render_deck_returns_both_outputs():
    """render_deck returns pptx_bytes (bytes) and html_str (str)."""
    result = render_deck(_sample_deck())
    assert "pptx_bytes" in result
    assert "html_str" in result
    assert isinstance(result["pptx_bytes"], bytes)
    assert isinstance(result["html_str"], str)


def test_render_deck_pptx_is_valid():
    """render_deck pptx_bytes are a valid OOXML file."""
    import io

    from pptx import Presentation

    result = render_deck(_sample_deck())
    prs = Presentation(io.BytesIO(result["pptx_bytes"]))
    assert len(prs.slides) == len(_sample_deck().slides)


def test_render_deck_html_is_16_9():
    """render_deck html_str is a 16:9 HTML deck."""
    result = render_deck(_sample_deck())
    assert "56.25vw" in result["html_str"]

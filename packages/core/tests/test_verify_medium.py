"""Medium detection for bounded visual verification."""

from __future__ import annotations

from disco.core.verify_medium import detect_html_medium, html_manifest_hrefs


def test_deck_html_detects_slide_medium() -> None:
    html = """
    <html><body>
      <section class="slide active" data-slide-id="slide-0" data-layout="title"></section>
    </body></html>
    """

    hint = detect_html_medium(html, manifest_present=False)

    assert hint is not None
    assert hint.kind == "deck"
    assert "presentation distance" in hint.review_guidance
    assert "Bottom whitespace is correct" in hint.review_guidance


def test_pwa_html_detects_mobile_medium_when_manifest_exists() -> None:
    html = """
    <html><head>
      <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
      <link rel="manifest" href="/manifest.json">
    </head><body>App</body></html>
    """

    assert html_manifest_hrefs(html) == ("/manifest.json",)
    hint = detect_html_medium(html, manifest_present=True)

    assert hint is not None
    assert hint.kind == "mobile"
    assert hint.viewport_width == 390
    assert hint.viewport_height == 844
    assert "44px" in hint.review_guidance
    assert "safe areas" in hint.review_guidance


def test_plain_site_medium_is_default_none() -> None:
    html = "<html><head><title>Site</title></head><body><main>Hello</main></body></html>"

    assert detect_html_medium(html, manifest_present=False) is None

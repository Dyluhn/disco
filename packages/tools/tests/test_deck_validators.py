"""W14a-deck — deck provenance validators (procedural-image + raw-html-default detection).

Drives the W18 negative fixtures: a deck embedding the AUTHENTIC procedural placeholder PNG
must be flagged, and a deck deliverable that defaulted to raw html must be flagged — while a
deck with a real (high-entropy) image is NOT flagged.
"""

from __future__ import annotations

import base64
import io
import json
import pathlib

from disco.tools.verify.artifact_validators import (
    _looks_procedural,
    validate_deck_deliverable,
    validate_deck_file,
)

_REPO = pathlib.Path(__file__).resolve().parents[4]
_NEG = _REPO / "current/packages/agent-server/tests/fixtures/negative"


def test_detects_procedural_image_in_html_deck():
    problems = validate_deck_file(str(_NEG / "procedural_image_deck.html"))
    assert "procedural_placeholder_image" in problems


def test_procedural_png_fixture_is_detected_directly():
    img = (_NEG / "procedural_placeholder.png").read_bytes()
    assert _looks_procedural(img) is True


def test_rejects_raw_html_default_deliverable():
    descriptor = json.loads((_NEG / "raw_html_deck_deliverable.json").read_text())
    assert "raw_html_default" in validate_deck_deliverable(descriptor)


def test_pptx_deliverable_is_accepted():
    assert validate_deck_deliverable({"format": "pptx", "path": "deck.pptx"}) == []


def test_real_image_is_not_flagged_as_procedural():
    """A high-entropy (noise) image must NOT trip the procedural heuristic."""
    from PIL import Image

    rng = bytes((i * 73 + i * i * 11) & 0xFF for i in range(64 * 64 * 3))
    img = Image.frombytes("RGB", (64, 64), rng)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert _looks_procedural(buf.getvalue()) is False

    # and a deck embedding it produces no procedural flag
    deck = (
        '<html><body><img src="data:image/png;base64,'
        + base64.b64encode(buf.getvalue()).decode()
        + '"></body></html>'
    )
    p = _REPO / "current/packages/tools/tests" / "_tmp_real_deck.html"
    try:
        p.write_text(deck)
        assert validate_deck_file(str(p)) == []
    finally:
        p.unlink(missing_ok=True)


def test_looks_procedural_no_false_positive_on_flat_art():
    """codex round-4: the heuristic must NOT flag ordinary flat art (a logo/chart has a small
    palette + a dominant background but NO thin corner-diagonal accent line). Byte detection
    is PNG-reliable; format-agnostic procedural detection is via PROVENANCE (the image_generate
    `placeholder` field the disco-verify runner checks), not this heuristic."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 300), (240, 240, 235))
    draw = ImageDraw.Draw(img)
    draw.rectangle((50, 50, 350, 250), fill=(70, 110, 180))  # a big centred shape
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert _looks_procedural(buf.getvalue()) is False

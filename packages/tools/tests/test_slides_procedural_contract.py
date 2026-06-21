"""W3 — slides/artifact contract.

The runthru bug: a "make slides" request shipped a deck whose image slots rendered as
garish procedural placeholder graphics (nested rectangles + diagonal), because the deck
pipeline auto-generates an image per ``image_prompt`` slide and the image provider defaulted
to the keyless ``pil-procedural`` backend. A presentable deck must NOT embed procedural
placeholders — when no real image provider is configured, omit the image (text slide). Also:
the slides tool default format must be a presentable deck (``pptx``), not raw ``html``.
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin._deck_schema import AuthoredDeck, AuthoredSlide
from disco.tools.builtin._slides_pipeline import _stage_assets
from disco.tools.builtin.image_gen import _PILProceduralBackend

pytestmark = pytest.mark.boundary_contract


def _deck_with_image() -> AuthoredDeck:
    return AuthoredDeck(
        title="History of Three.js",
        slides=[
            AuthoredSlide(
                type="title", title="Hero", body=[],
                image_prompt="a hero image of the three.js logo",
            ),
        ],
    )


class _RealBackend:
    """A stand-in for a real (connected) image provider — NOT the placeholder one."""

    name = "fake-real"
    is_remote = True

    def generate(self, *, prompt, width, height, seed, fmt) -> bytes:  # noqa: ANN001
        # a minimal valid 1x1 PNG
        import base64

        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )


async def test_procedural_backend_omits_deck_images():
    """The placeholder backend must NOT embed images in the deck (no garish boxes)."""
    ctx = ToolContext.model_construct(sandbox=None)
    assets = await _stage_assets(_deck_with_image(), ctx, _PILProceduralBackend(), "f")
    assert assets == {}, "procedural placeholder backend must not embed images into a deck"


async def test_real_backend_still_stages_images():
    """A connected provider DOES embed images (the fix must not break real image decks)."""
    ctx = ToolContext.model_construct(sandbox=None)
    assets = await _stage_assets(_deck_with_image(), ctx, _RealBackend(), "f")
    assert 0 in assets, "a real backend must still stage images"
    assert assets[0].startswith(b"\x89PNG")


def test_slides_default_format_is_pptx():
    """The slides tool must default to a presentable deck format, not raw html."""
    from disco.tools.builtin.slides import SlidesGenerateArgs

    assert SlidesGenerateArgs.model_fields["format"].default == "pptx"

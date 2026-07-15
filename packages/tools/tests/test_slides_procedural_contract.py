"""W3 / W-50 — slides/artifact contract.

The runthru bug: a "make slides" request shipped a deck whose image slots rendered as
garish procedural placeholder graphics, because the deck pipeline auto-generated an image
per ``image_prompt`` slide and the image provider defaulted to a keyless procedural
backend. W-50 removed that backend entirely: when no real image provider is configured,
``select_image_backend()`` raises and the slides tool passes ``backend=None`` so the deck
DEGRADES to text-only (image omitted) — never a placeholder, never a crash. Also: the
slides tool default format must be a presentable deck (``pptx``), not raw ``html``.
"""

from __future__ import annotations

import pytest
from disco.tools.anatomy import ToolContext
from disco.tools.builtin._deck_schema import AuthoredDeck, AuthoredSlide
from disco.tools.builtin._slides_pipeline import _stage_assets

pytestmark = pytest.mark.boundary_contract


def _deck_with_image() -> AuthoredDeck:
    return AuthoredDeck(
        title="History of Three.js",
        slides=[
            AuthoredSlide(
                type="title",
                title="Hero",
                body=[],
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


async def test_unconfigured_backend_omits_deck_images():
    """W-50: no configured image backend (backend=None) → the deck degrades to
    text-only (no images embedded), never a placeholder, never a crash."""
    ctx = ToolContext.model_construct(sandbox=None)
    assets, _ = await _stage_assets(_deck_with_image(), ctx, None, "f")
    assert assets == {}, "an unconfigured image backend must omit images, not crash"


async def test_real_backend_still_stages_images():
    """A connected provider DOES embed images (the fix must not break real image decks)."""
    ctx = ToolContext.model_construct(sandbox=None)
    assets, _ = await _stage_assets(_deck_with_image(), ctx, _RealBackend(), "f")
    assert 0 in assets, "a real backend must still stage images"
    assert assets[0].startswith(b"\x89PNG")


def test_slides_default_format_is_pptx():
    """The slides tool must default to a presentable deck format, not raw html."""
    from disco.tools.builtin.slides import SlidesGenerateArgs

    assert SlidesGenerateArgs.model_fields["format"].default == "pptx"

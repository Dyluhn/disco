"""``lower_deck`` — the main C1 entry point (AuthoredDeck → Deck).

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from disco.core.brand import resolve_theme
from disco.core.brand.tokens import Theme

from ._authoring_schema import AuthoredDeck
from ._expand import _expand_slides
from ._helpers import _parse_theme, _uid
from ._layout_dispatch import _LAYOUT_FNS
from ._layouts_basic import _layout_bullets
from ._precise_schema import Deck, Slide


def lower_deck(
    authored: AuthoredDeck,
    *,
    theme_override: str | None = None,
    brand_override: Theme | None = None,
    image_assets: dict[int, bytes] | None = None,
) -> Deck:
    """Lower an AuthoredDeck to a Deck of precisely-positioned Elements.

    This is a PURE function: same input → structurally equivalent output
    (ids differ because they are UUIDs, but all positions / text / layout
    decisions are deterministic).

    ``theme_override`` ("{name}-{mode}" template id) re-themes the deck at render
    time WITHOUT mutating the authored sidecar — this is the slide-deck template
    selector's render-on-demand path. None → use the deck's authored theme.

    ``brand_override`` is an already-resolved Theme snapshot (for example from a
    committed design direction). When supplied it wins over registry lookup while
    preserving the rest of the lowering/rendering path.

    ``image_assets`` (C7 wire) maps an AUTHORED-slide index → generated image bytes.
    The image element lowered from that slide carries the bytes so the renderer
    embeds the real picture instead of the ``[image]`` placeholder. None → no
    images generated yet (the re-theme / editor paths), so placeholders render as
    before.

    Overflow rules:
      - ``_fit_text`` steps font from max to min.
      - If body still overflows at min font, overflow lines become a new
        ``AuthoredSlide`` with ``type=<original>_cont`` inserted immediately
        after the current slide.
      - ``notes`` and ``image_prompt`` are NEVER passed to ``_fit_text``
        (they do not appear on the visible slide face).
      - Maximum continuation depth: 3 (prevents catastrophic infinite split).
    """
    if brand_override is None:
        theme_name, theme_mode = _parse_theme(theme_override or authored.theme)
        theme = resolve_theme(theme_name, theme_mode)
    else:
        theme = brand_override

    # Shared overflow expansion — identical to the editor's, so counts match (BW-13).
    image_alt: list[int] = [0]  # alternating image side counter
    eff_slides = _expand_slides(authored.slides, theme, image_alt)

    deck_slides: list[Slide] = []
    for eff in eff_slides:
        aslide = eff.aslide
        layout = eff.layout
        layout_fn = _LAYOUT_FNS.get(layout, _layout_bullets)
        # The layout fn drops this fragment's own overflow internally; the
        # expansion already created the matching continuation _EffSlide for it.
        elements, _overflow = layout_fn(aslide, theme)  # type: ignore[operator]

        # C7 wire: attach generated image bytes to this slide's image element(s).
        # Only ORIGINAL (non-cont) slides carry an image_prompt; continuations are
        # text-only, so nothing is attached for them.
        if image_assets and not eff.is_cont and eff.orig_index in image_assets:
            for el in elements:
                if el.kind == "image":
                    el.image_bytes = image_assets[eff.orig_index]

        deck_slides.append(
            Slide(
                id=_uid(),
                type=aslide.type,
                layout=layout,
                archetype=aslide.archetype,
                title=aslide.title,
                elements=elements,
                notes=aslide.notes,
                chart=aslide.chart,
                table=aslide.table,
                # Carry the SHARED expansion's index map so render_html stamps the same
                # authored-body pointer the editor's lower_deck_for_editor model uses (BW-13).
                body_index_map=list(eff.body_index_map),
            )
        )

    return Deck(
        id=_uid(),
        title=authored.title,
        theme=theme,
        slides=deck_slides,
    )

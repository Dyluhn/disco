"""Theme/craft defaults, safe post-parse coercions, and density/image-slot
post-processing for the C2 staged deck-generation pipeline.

Extracted from ``_slides_pipeline.py``. ``_coerce_known_theme_aliases`` and
``_prepare_filled_deck`` are re-exported as module-level attributes of
``_slides_pipeline`` (the former imported directly by tests); the rest are only
called from within this module or from functions that stay in
``_slides_pipeline.py`` (``_stage_outline``/``_stage_fill``/``generate_deck``),
which resolve them at call time through the re-imported names in that module's
own globals.
"""

from __future__ import annotations

import re
from typing import get_args

from disco.tools.builtin._deck_schema import (
    AccentSpec,
    AuthoredDeck,
    AuthoredSlide,
    FontPairingSpec,
    LightDarkTokenPair,
    SlideArchetype,
)

_IMAGE_SLOT_ARCHETYPES = frozenset({"full_bleed_image", "photo_grid"})
_SECTION_ARCHETYPES = frozenset({"section_divider"})
_BANNED_TITLE_PATTERNS = (
    re.compile(r"\bit'?s\s+not\s+.+?\bit'?s\s+.+", re.IGNORECASE),
    re.compile(r"\bthe\s+magic\s+moment\b", re.IGNORECASE),
    re.compile(r"^(verdict|punchline)\s*:", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# Theme alias coercion (SAFE — known shorthands only)
# ---------------------------------------------------------------------------

# Only these canonical shorthands are coerced.  Unknown/invalid values (e.g.
# ``"dark-research"``, ``"corporate"``) are left as-is so pydantic rejects them
# and the retry (which lists the full enum) corrects the model.
_THEME_ALIASES: dict[str, str] = {
    "dark": "disco-dark",
    "light": "disco-light",
}


def _coerce_known_theme_aliases(data: dict) -> dict:
    """Coerce a known legacy theme shorthand to the canonical Literal value.

    Returns a shallow copy with ``theme`` remapped when the value is a key in
    ``_THEME_ALIASES``; otherwise returns ``data`` unchanged.
    """
    theme = data.get("theme")
    if isinstance(theme, str) and theme in _THEME_ALIASES:
        data = {**data, "theme": _THEME_ALIASES[theme]}
    return data


_VALID_LAYOUT_HINTS: frozenset[str] = frozenset(
    get_args(get_args(AuthoredSlide.model_fields["layout_hint"].annotation)[0])
)


def _null_invalid_layout_hints(data: dict) -> dict:
    """Null out off-enum ``layout_hint`` values instead of failing the deck.

    ``layout_hint`` is OPTIONAL — the lowering infers a layout from ``type``/
    ``archetype`` when it is None. Gauntlet run-1: MiniMax M3 authored creative
    hints ("hero", "big_number") and the WHOLE outline hard-failed schema
    validation over a nullable field, degrading the deck to the plain fallback.
    Unknown hints now become None (a per-slide note in the retry already lists
    the enum for fields that MUST be exact)."""
    slides = data.get("slides")
    if not isinstance(slides, list):
        return data
    fixed = []
    changed = False
    for s in slides:
        if isinstance(s, dict):
            hint = s.get("layout_hint")
            if hint is not None and hint not in _VALID_LAYOUT_HINTS:
                s = {**s, "layout_hint": None}
                changed = True
        fixed.append(s)
    return {**data, "slides": fixed} if changed else data


# ---------------------------------------------------------------------------
# Craft metadata + density/image-slot post-processing
# ---------------------------------------------------------------------------


def _theme_defaults(
    theme: str,
) -> tuple[list[AccentSpec], FontPairingSpec, LightDarkTokenPair, str]:
    """Return conservative craft metadata for legacy/partial model output."""
    by_theme: dict[str, tuple[list[AccentSpec], FontPairingSpec, LightDarkTokenPair, str]] = {
        "disco-light": (
            [
                AccentSpec(name="cerulean", value="#4077a3", role="primary emphasis"),
                AccentSpec(name="moss", value="#397852", role="supportive proof"),
                AccentSpec(name="paper", value="#d8b26e", role="warm section contrast"),
            ],
            FontPairingSpec(display="Fraunces", body="Newsreader", ui="Schibsted Grotesk"),
            LightDarkTokenPair(
                light_bg="#fcfcfa",
                light_text="#1a1813",
                dark_bg="#0d1017",
                dark_text="#e9e6df",
            ),
            "grainy editorial risograph, cerulean and warm paper palette, "
            "soft print grain, no text",
        ),
        "signal-light": (
            [
                AccentSpec(name="cobalt", value="#2f5fd0", role="primary emphasis"),
                AccentSpec(name="mint", value="#2f7d54", role="positive signal"),
                AccentSpec(name="amber", value="#a07a30", role="threshold warning"),
            ],
            FontPairingSpec(display="Schibsted Grotesk", body="Newsreader", ui="Schibsted Grotesk"),
            LightDarkTokenPair(
                light_bg="#fbfcfd",
                light_text="#13161c",
                dark_bg="#0d1017",
                dark_text="#e9e6df",
            ),
            "clean technical editorial illustration, cobalt and mint palette, "
            "precise linework, no text",
        ),
        "ink-light": (
            [
                AccentSpec(name="oxblood", value="#9d2b2b", role="primary emphasis"),
                AccentSpec(name="charcoal", value="#15140f", role="linework"),
                AccentSpec(name="warm gray", value="#c3beb6", role="secondary fields"),
            ],
            FontPairingSpec(display="Fraunces", body="Newsreader", ui="Schibsted Grotesk"),
            LightDarkTokenPair(
                light_bg="#ffffff",
                light_text="#15140f",
                dark_bg="#15140f",
                dark_text="#f5f4f2",
            ),
            "stark ink editorial engraving, oxblood and charcoal palette, paper texture, no text",
        ),
        "sepia-light": (
            [
                AccentSpec(name="terracotta", value="#a85d2e", role="primary emphasis"),
                AccentSpec(name="olive", value="#5a6b3a", role="supporting proof"),
                AccentSpec(name="parchment", value="#d9cfb4", role="section field"),
            ],
            FontPairingSpec(display="Fraunces", body="Newsreader", ui="Schibsted Grotesk"),
            LightDarkTokenPair(
                light_bg="#f7f1e3",
                light_text="#2c2417",
                dark_bg="#2c2417",
                dark_text="#f7f1e3",
            ),
            "warm archival collage, terracotta and parchment palette, soft paper grain, no text",
        ),
        "midnight-dark": (
            [
                AccentSpec(name="amber", value="#d9a441", role="primary emphasis"),
                AccentSpec(name="sky", value="#76a9d8", role="cool contrast"),
                AccentSpec(name="deep navy", value="#151926", role="background field"),
            ],
            FontPairingSpec(display="Fraunces", body="Newsreader", ui="Schibsted Grotesk"),
            LightDarkTokenPair(
                light_bg="#fbfcfd",
                light_text="#13161c",
                dark_bg="#0d1017",
                dark_text="#e9e6df",
            ),
            "nocturne editorial illustration, amber and deep navy palette, "
            "soft cinematic grain, no text",
        ),
    }
    accents, fonts, tokens, art_direction = by_theme.get(theme, by_theme["disco-light"])
    return (
        [accent.model_copy() for accent in accents],
        fonts.model_copy(),
        tokens.model_copy(),
        art_direction,
    )


def _infer_archetype(slide: AuthoredSlide, index: int, total: int) -> SlideArchetype:
    if index == 0:
        return "title"
    if index == total - 1 or slide.type.lower() == "closing":
        return "closing"
    typ = slide.type.lower()
    if typ in ("section_header", "section"):
        return "section_divider"
    if typ in ("full_image", "image_left", "image_right") and not slide.body:
        return "full_bleed_image"
    if typ in ("metrics", "metrics_grid"):
        return "big_number"
    if typ in ("comparison", "table"):
        return "comparison_table"
    if typ == "two_column":
        return "two_by_two"
    return "bullets"


def _with_craft_defaults(deck: AuthoredDeck) -> AuthoredDeck:
    accents, fonts, tokens, art_direction = _theme_defaults(deck.theme)
    if not deck.accent_palette:
        deck.accent_palette = accents
    if deck.font_pairing is None:
        deck.font_pairing = fonts
    if deck.token_pair is None:
        deck.token_pair = tokens
    if not deck.art_direction:
        deck.art_direction = art_direction

    total = len(deck.slides)
    for i, slide in enumerate(deck.slides):
        if slide.archetype is None:
            slide.archetype = _infer_archetype(slide, i, total)
    return deck


def _clip_words(line: str, max_words: int) -> str:
    words = line.split()
    if len(words) <= max_words:
        return line
    return " ".join(words[:max_words])


def _scrub_title(title: str) -> str:
    out = title.strip()
    if _BANNED_TITLE_PATTERNS[0].search(out):
        parts = re.split(r"\bit'?s\s+", out, flags=re.IGNORECASE)
        if parts:
            out = parts[-1].strip().strip(".")
    if _BANNED_TITLE_PATTERNS[1].search(out):
        out = re.sub(r"\bthe\s+magic\s+moment\b", "Critical moment", out, flags=re.IGNORECASE)
    if _BANNED_TITLE_PATTERNS[2].search(out):
        out = re.sub(r"^(verdict|punchline)\s*:\s*", "", out, flags=re.IGNORECASE)
    return out or title


def _enforce_density_budgets(deck: AuthoredDeck) -> AuthoredDeck:
    for slide in deck.slides:
        slide.title = _scrub_title(slide.title)
        archetype = slide.archetype or "bullets"
        if archetype == "bullets":
            slide.body = [_clip_words(line, 9) for line in slide.body[:5]]
        elif archetype == "big_number":
            slide.body = [_clip_words(line, 9) for line in slide.body[:2]]
        elif archetype in ("full_bleed_image", "photo_grid"):
            slide.body = [_clip_words(line, 9) for line in slide.body[:2]]
        elif archetype in ("title", "section_divider", "closing"):
            slide.body = [_clip_words(line, 12) for line in slide.body[:1]]
        elif archetype == "quote":
            slide.body = [_clip_words(line, 14) for line in slide.body[:2]]
    return deck


def _image_slot_type(slide: AuthoredSlide, index: int) -> str | None:
    archetype = slide.archetype
    if index == 0:
        return "full-bleed background"
    if archetype in _SECTION_ARCHETYPES:
        return "divider art"
    if archetype in _IMAGE_SLOT_ARCHETYPES:
        return "full-bleed background"
    if slide.image_prompt:
        return "spot illustration"
    return None


def _compose_image_prompt(art_direction: str, subject: str, slot_type: str) -> str:
    subject_clean = " ".join(subject.split())
    return f"{art_direction}; subject: {subject_clean}; slot: {slot_type}; no words, no lettering"


def _ensure_image_slot_prompts(deck: AuthoredDeck) -> AuthoredDeck:
    art_direction = deck.art_direction or _theme_defaults(deck.theme)[3]
    for i, slide in enumerate(deck.slides):
        slot_type = _image_slot_type(slide, i)
        if slot_type is None:
            continue
        subject = slide.image_prompt or slide.title
        if slide.body:
            subject = f"{subject} — {slide.body[0]}"
        slide.image_prompt = _compose_image_prompt(art_direction, subject, slot_type)
        if (
            i == 0
            or slide.archetype in _SECTION_ARCHETYPES
            or slide.archetype in _IMAGE_SLOT_ARCHETYPES
        ):
            slide.layout_hint = "full_image"
    return deck


def _prepare_outline_deck(deck: AuthoredDeck) -> AuthoredDeck:
    return _with_craft_defaults(deck)


def _demote_tableless_comparisons(deck: AuthoredDeck) -> AuthoredDeck:
    """Gauntlet s-arxiv 2026-07-07: a comparison_table slide with NO table
    payload and a thin body (3 lines) rendered as two column headers + one
    lonely bullet — a mostly-empty slide. A comparison NEEDS a real table (or
    at least enough body lines for two balanced columns); anything thinner
    reads better as plain bullets. Demote in place (render-time normalization —
    the authored sidecar keeps the model's original)."""
    for slide in deck.slides:
        if (
            slide.archetype == "comparison_table"
            and slide.table is None
            and slide.chart is None
            and len(slide.body or []) < 4
        ):
            slide.archetype = "bullets"
            slide.layout_hint = "bullets"
    return deck


def _prepare_filled_deck(deck: AuthoredDeck) -> AuthoredDeck:
    deck = _with_craft_defaults(deck)
    deck = _demote_tableless_comparisons(deck)
    deck = _enforce_density_budgets(deck)
    return _ensure_image_slot_prompts(deck)

"""C2 — Staged deck generation pipeline.

Pipeline stages:
  1. Outline (LLM): Generate {type, title} for all slides (structural skeleton).
  2. Fill (LLM):   Fill body, image_prompt, notes, chart/table per slide.
  3. Assets:       Call image_generate backend for each slide with image_prompt.
  4. Lower:        lower_deck(AuthoredDeck) → Deck via C1.
  5. Render:       Deck → .pptx + .html via C3.

Defensive parse: JSON failure → ONE retry with "return ONLY valid JSON" →
second failure → fall back to the Marp/fallback path.

Tier-aware:
  ctx.assist == False (capable): compact schema instructions.
  ctx.assist == True  (weak):    full worked-example AuthoredDeck JSON.

Image wiring: each slide with image_prompt is passed to the C7 ImageBackend
to generate image bytes; these are written to the sandbox and the element's
image_path is set.

Image-prompt discipline (verdict §7 p08 fix): the C2 prompt explicitly
instructs: "if image_prompt set, body=[] or at most one caption line."

Layering: disco.tools → disco.core (legal downward import).
          disco.tools ≠→ disco.agent_server (upward import — never here).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, get_args

import httpx
from disco.tools.builtin._deck_schema import (
    AccentSpec,
    AuthoredDeck,
    AuthoredSlide,
    Deck,
    FontPairingSpec,
    LightDarkTokenPair,
    SlideArchetype,
    lower_deck,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from disco.tools.anatomy import ToolContext
    from disco.tools.builtin.image_gen import ImageBackend

_LOG = logging.getLogger("disco.tools.slides_pipeline")

# Derive the valid theme set from the single source of truth — the Literal on
# AuthoredDeck.theme.  Every prompt and the retry message reference this tuple
# so they stay in sync with the schema automatically.
_VALID_THEMES: tuple[str, ...] = get_args(AuthoredDeck.model_fields["theme"].annotation)
_VALID_THEMES_STR = " | ".join(f'"{t}"' for t in _VALID_THEMES)
_ARCHETYPES: tuple[str, ...] = get_args(SlideArchetype)
_ARCHETYPES_STR = " | ".join(f'"{a}"' for a in _ARCHETYPES)

_IMAGE_SLOT_ARCHETYPES = frozenset({"full_bleed_image", "photo_grid"})
_SECTION_ARCHETYPES = frozenset({"section_divider"})
_BANNED_TITLE_PATTERNS = (
    re.compile(r"\bit'?s\s+not\s+.+?\bit'?s\s+.+", re.IGNORECASE),
    re.compile(r"\bthe\s+magic\s+moment\b", re.IGNORECASE),
    re.compile(r"^(verdict|punchline)\s*:", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# LLM endpoint resolution (ConfigStore-based; no agent_server import)
# ---------------------------------------------------------------------------


def _resolve_slides_llm() -> tuple[str, str, str | None]:
    """Return (base_url, model_id, api_key) for the slides generation LLM.

    Uses the AGENT_DRIVER model from ConfigStore.  Falls back to env vars
    LLM_URL / LLM_MODEL, then to a hardcoded local default. The api_key is
    resolved from the entry's `api_key_env` (encrypted SecretStore first, then
    os.environ) so a REMOTE driver (OpenRouter / any paid OpenAI-compatible
    endpoint) authenticates — without it the deck author silently 401s on every
    non-local driver. Local keyless endpoints resolve to None (no auth header).

    This mirrors audio_config.py's approach without importing from agent_server.
    """
    try:
        from disco.core.llm import ConfigStore
        from disco.core.llm.types import ModelRole

        cfg = ConfigStore().load()
        model_key = cfg.model_for(ModelRole.AGENT_DRIVER)
        entry = cfg.models.get(model_key)
        if entry and entry.base_url:
            return entry.base_url.rstrip("/"), entry.model_id, _resolve_llm_key(entry.api_key_env)
    except Exception:  # noqa: BLE001
        pass
    url = os.environ.get("LLM_URL", "http://localhost:18080/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "local-model")
    # An explicit env override may still want a key (e.g. LLM_API_KEY_ENV names it).
    return url, model, _resolve_llm_key(os.environ.get("LLM_API_KEY_ENV"))


def _resolve_llm_key(api_key_env: str | None) -> str | None:
    """Resolve the named secret for the deck-author LLM. Never logs the value.

    Mirrors the agent-server's canonical resolution (runtime._overlay_stored_secrets):
    the OpenRouter driver key is stored in the RESERVED "openrouter" SecretStore slot
    (set by the dedicated /api OpenRouter route), NOT under its env-var name — so a
    plain get_secret(api_key_env) misses it. Order: exact named secret → reserved
    openrouter slot (when api_key_env is the OpenRouter var) → exact env → legacy
    PMX_OPENROUTER_API_KEY env. Returns None if nothing is configured."""
    if not api_key_env:
        return None
    try:
        from disco.core.llm.secrets import (
            OPENROUTER_API_KEY_ENV,
            OPENROUTER_API_KEY_ENV_LEGACY,
            SecretStore,
        )

        store = SecretStore()
        key = store.get_secret(api_key_env)
        if key:
            return key
        if api_key_env in (OPENROUTER_API_KEY_ENV, OPENROUTER_API_KEY_ENV_LEGACY):
            key = store.get_openrouter_key()  # the reserved "openrouter" slot
            if key:
                return key
        env_val = os.environ.get(api_key_env)
        if env_val:
            return env_val
        if api_key_env == OPENROUTER_API_KEY_ENV:
            return os.environ.get(OPENROUTER_API_KEY_ENV_LEGACY)
        return None
    except Exception:  # noqa: BLE001
        return os.environ.get(api_key_env)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

# Capable-model system prompt (compact — C4 verdict: loose hybrid wins)
_CAPABLE_SYSTEM = """\
You are an expert slide-deck designer. Generate a slide deck as valid JSON matching \
the AuthoredDeck schema. Output ONLY valid JSON — no markdown fences, no prose before \
or after.

AuthoredDeck schema:
{
  "title": "Deck title",
  "theme": "disco-light" | "disco-dark" | "ink-light" | "sepia-light"
           | "signal-light" | "midnight-dark" | "neutral" | "neutral-light",
  "accent_palette": [{"name": "teal", "value": "#1f9a8a", "role": "section accent"}],
  "font_pairing": {"display": "Fraunces", "body": "Newsreader", "ui": "Schibsted Grotesk"},
  "token_pair": {"light_bg": "#fcfcfa", "light_text": "#1a1813", "dark_bg": "#0d1017", "dark_text": "#e9e6df"},
  "art_direction": "grainy risograph, two-tone teal/sand, soft grain, no text",
  "slides": [AuthoredSlide, ...]
}

AuthoredSlide schema:
{
  "type": "title"|"bullets"|"section_header"|"two_column"|"comparison"|"metrics"|"image_right"|"image_left"|"full_image"|"table"|"closing",
  "archetype": "title"|"section_divider"|"big_number"|"full_bleed_image"|"quote"|"comparison_table"|"timeline"|"diagram"|"two_by_two"|"photo_grid"|"bullets"|"closing",
  "title": "Slide heading",
  "body": ["bullet or content line", ...],
  "layout_hint": null | "title"|"bullets"|...,
  "image_prompt": null | "prompt for image generation",
  "chart": null,
  "table": null,
  "notes": null | "speaker notes (never shown on slide)"
}

Deck craft rules:
- Write the full title sequence first and keep ONE grammatical style for every title: \
  either short topic noun-phrases OR brief declarative action titles. Never mix them. \
  Titles alone must tell the story.
- Choose an archetype for every slide from the listed set. Never use more than two \
  consecutive "bullets" archetypes. Every section opens with section_divider or \
  full_bleed_image. Include at least one big_number or quote when the content supports it. \
  HARD RULE for decks of 8+ slides: at least TWO slides must be image-bearing \
  (section_divider, full_bleed_image, or photo_grid) — a long deck with zero imagery \
  slides is invalid.
- Commit a theme with 3-4 named accents, a non-default font pairing (never Inter, \
  Roboto, or Arial), a light/dark token pair, and one project-wide art_direction.
- Image prompts are slots, not decoration: cover + section dividers + full_bleed_image \
  and photo_grid slides need image_prompt. Compose them as art direction + slide subject \
  + slot type + "no words, no lettering". Target at least one image per 3-4 slides; do \
  not add decorative spam.
- Density: bullets <= 5 lines and <= 9 words each; big_number = 1 number + 1 line; \
  full_bleed_image/photo_grid <= 2 lines. Keep repeated elements parallel.
- Banned title patterns: "It's not X. It's Y.", punchline/verdict titles, \
  "The magic moment". Avoid web-density reflexes; bottom whitespace is correct on slides.
- Think in slide pixels: 36pt = 48px, and visible type should not imply less than a \
  24px floor.
- notes and image_prompt are NEVER shown on the visible slide face.
"""

# Worked-example system prompt for weak models (ctx.assist=True)
_WEAK_SYSTEM = """\
You are an expert slide-deck designer. Generate a slide deck as valid JSON.
Output ONLY valid JSON — no markdown fences, no prose.

The "theme" field MUST be one of these exact strings:
"disco-light" | "disco-dark" | "ink-light" | "sepia-light"
| "signal-light" | "midnight-dark" | "neutral" | "neutral-light"

Use these craft rules:
- First decide the whole title sequence. Use one style only: short topic noun-phrases OR \
  brief declarative action titles. Titles alone must tell the story.
- Every slide needs an "archetype" from this exact set: title, section_divider, \
  big_number, full_bleed_image, quote, comparison_table, timeline, diagram, two_by_two, \
  photo_grid, bullets, closing.
- Never use more than two bullets slides in a row. Every section starts with \
  section_divider or full_bleed_image.
- Commit 3-4 named accents, a font pairing that is NOT Inter/Roboto/Arial, a light/dark \
  token pair, and one art_direction.
- Cover, every section_divider, every full_bleed_image, and every photo_grid slide need \
  image_prompt. Prompt shape: art_direction + slide subject + slot type + \
  "no words, no lettering".
- Density: bullets <= 5 lines <= 9 words each; big_number = 1 number + 1 line; \
  full_bleed_image/photo_grid <= 2 lines. Keep repeated items parallel.
- Banned title patterns: "It's not X. It's Y.", punchline/verdict titles, \
  "The magic moment". Use a 24px visible type floor mindset.

Copy this exact structure and fill it in with the requested content:

{
  "title": "DECK TITLE HERE",
  "theme": "disco-light",
  "accent_palette": [
    {"name": "teal", "value": "#1f9a8a", "role": "primary section accent"},
    {"name": "sand", "value": "#d8b26e", "role": "warm contrast"},
    {"name": "ink", "value": "#20211c", "role": "text and linework"}
  ],
  "font_pairing": {"display": "Fraunces", "body": "Newsreader", "ui": "Schibsted Grotesk"},
  "token_pair": {"light_bg": "#fcfcfa", "light_text": "#1a1813", "dark_bg": "#0d1017", "dark_text": "#e9e6df"},
  "art_direction": "grainy editorial risograph, teal and sand palette, soft paper grain, no text",
  "slides": [
    {
      "type": "title",
      "archetype": "title",
      "title": "MAIN TITLE",
      "body": ["One-line subtitle or tagline"],
      "layout_hint": "full_image",
      "image_prompt": "grainy editorial risograph, teal and sand palette, soft paper grain, no text; subject: MAIN TITLE; slot: full-bleed background; no words, no lettering",
      "chart": null,
      "table": null,
      "notes": "Optional speaker notes"
    },
    {
      "type": "bullets",
      "archetype": "bullets",
      "title": "SLIDE HEADING",
      "body": [
        "First parallel point",
        "Second parallel point",
        "Third parallel point",
        "Fourth parallel point"
      ],
      "layout_hint": null,
      "image_prompt": null,
      "chart": null,
      "table": null,
      "notes": null
    },
    {
      "type": "closing",
      "archetype": "closing",
      "title": "Thank You",
      "body": ["Contact: name@example.com"],
      "layout_hint": null,
      "image_prompt": null,
      "chart": null,
      "table": null,
      "notes": null
    }
  ]
}
"""

_OUTLINE_USER_TMPL = """\
Goal: {goal}

Generate an outline of {n} slides for this deck.

Before choosing any body copy, write the full slide title sequence in one grammatical \
style for the entire deck. Pick either short topic noun-phrases OR brief declarative \
action titles; do not mix styles. The titles alone must tell the story.

Output a minimal AuthoredDeck JSON:
- Deck-level fields must include title, theme, accent_palette, font_pairing, token_pair, \
  art_direction, and slides.
- accent_palette must contain 3-4 named accents suitable for OKLCH-friendly section \
  derivation.
- font_pairing must avoid Inter, Roboto, and Arial.
- art_direction must be a reusable image style contract with style keywords, palette \
  descriptors, medium/texture/lighting, and no-text guidance.
- Each slide must include type, archetype, title, body, layout_hint, image_prompt, \
  chart, table, and notes.
- At outline stage, body must be [], notes null, chart null, table null.
- Set image_prompt null at outline stage unless the slot subject is already essential; \
  the fill stage will compose final prompts from art_direction.

Archetype set: {archetypes}.
Mix rules: never more than two consecutive "bullets"; every section opens with \
section_divider or full_bleed_image; include at least one big_number or quote when \
the material supports it.
Use existing type values for renderer compatibility: title, bullets, section_header, \
two_column, comparison, metrics, image_right, image_left, full_image, table, closing.
"""

_FILL_USER_TMPL = """\
Here is the slide outline:
{outline_json}

Now fill in the complete content for each slide.  For each slide:
- Keep type, archetype, title, theme, accent_palette, font_pairing, token_pair, and \
  art_direction from the outline.
- Preserve the outline's one-style title sequence. Do not introduce banned title \
  patterns: "It's not X. It's Y.", punchline/verdict titles, or "The magic moment".
- Apply density budgets by archetype:
  * bullets: <= 5 body lines, <= 9 words per line.
  * big_number: body is exactly 2 lines: one number, then one short interpretation line.
  * full_bleed_image/photo_grid: <= 2 body lines.
  * section_divider/title/closing: <= 1 body line unless speaker notes carry detail.
- Make repeated elements parallel in grammar and length.
- Use a 24px visible type floor mindset for any inline size guidance; 36pt equals 48px.
- image_prompt is REQUIRED for the cover slide, every section_divider, every \
  full_bleed_image slide, and every photo_grid slide. Compose it exactly as: \
  art_direction + slide subject + slot type (full-bleed background | spot illustration \
  | divider art) + "no words, no lettering".
- Image prompts are capped to cover + dividers + explicit visual slides; do not add \
  decorative image prompts to ordinary bullets slides.
- If image_prompt is set and the slide is visual, set layout_hint to "full_image" and \
  keep body at the visual-slide budget.
- notes: optional 1–3 sentence speaker notes.

Return the COMPLETE AuthoredDeck JSON with ALL fields filled in.
Output ONLY valid JSON.
"""

def _retry_msg(err: str) -> str:
    """Build a targeted retry prompt that names the specific parse error and
    lists ALL valid theme values.  The theme enum is the most common mismatch
    (models hallucinate values like ``"dark-research"`` when only shown 3 of the
    8 valid strings) so we call it out explicitly on every retry."""
    return (
        "Your previous response was not valid JSON or did not match the AuthoredDeck schema.\n\n"
        f"Error detail: {err}\n\n"
        f'The "theme" field MUST be exactly one of: {_VALID_THEMES_STR}\n\n'
        "Please fix all errors and output ONLY valid JSON matching the AuthoredDeck schema.\n"
        "No markdown, no prose — only the raw JSON object."
    )

# ---------------------------------------------------------------------------
# LLM call helper
# ---------------------------------------------------------------------------


async def _call_llm(
    messages: list[dict[str, str]],
    llm_url: str,
    model: str,
    *,
    api_key: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 8192,
) -> str:
    """Call the OpenAI-compatible /chat/completions endpoint.  Returns raw text.

    Sends a Bearer Authorization header when `api_key` is provided so a remote
    driver (OpenRouter / paid endpoint) authenticates; local keyless endpoints
    pass api_key=None and send no auth header (unchanged)."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


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


# ---------------------------------------------------------------------------
# Craft metadata + density/image-slot post-processing
# ---------------------------------------------------------------------------


def _theme_defaults(theme: str) -> tuple[list[AccentSpec], FontPairingSpec, LightDarkTokenPair, str]:
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
            "grainy editorial risograph, cerulean and warm paper palette, soft print grain, no text",
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
            "clean technical editorial illustration, cobalt and mint palette, precise linework, no text",
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
            "nocturne editorial illustration, amber and deep navy palette, soft cinematic grain, no text",
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
    return (
        f"{art_direction}; subject: {subject_clean}; slot: {slot_type}; "
        "no words, no lettering"
    )


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
        if i == 0 or slide.archetype in _SECTION_ARCHETYPES or slide.archetype in _IMAGE_SLOT_ARCHETYPES:
            slide.layout_hint = "full_image"
    return deck


def _prepare_outline_deck(deck: AuthoredDeck) -> AuthoredDeck:
    return _with_craft_defaults(deck)


def _prepare_filled_deck(deck: AuthoredDeck) -> AuthoredDeck:
    deck = _with_craft_defaults(deck)
    deck = _enforce_density_budgets(deck)
    return _ensure_image_slot_prompts(deck)


# ---------------------------------------------------------------------------
# JSON extraction + AuthoredDeck validation
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> str:
    """Extract the first {...} JSON object from LLM output that may have prose."""
    text = text.strip()
    # Direct JSON object
    if text.startswith("{"):
        depth = 0
        end = 0
        for i, ch in enumerate(text):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            return text[:end]
    # Markdown code fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Last-resort: first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _parse_authored_deck(raw: str) -> tuple[AuthoredDeck | None, str]:
    """Parse raw LLM output as an AuthoredDeck.  Returns (deck, error_msg).

    Applies ``_coerce_known_theme_aliases`` BEFORE pydantic validation to
    silently fix short-hand aliases (``"dark"`` → ``"disco-dark"`` etc.).
    Unknown invalid theme strings are left for pydantic to reject; the caller
    should then pass ``_retry_msg(err)`` so the model sees the full enum.
    """
    extracted = _extract_json_object(raw)
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    data = _coerce_known_theme_aliases(data)
    try:
        deck = AuthoredDeck.model_validate(data)
    except (ValidationError, Exception) as e:
        return None, f"Schema validation error: {e}"
    return deck, ""


# ---------------------------------------------------------------------------
# Stage 1: Outline
# ---------------------------------------------------------------------------


async def _stage_outline(
    goal: str,
    slide_count: int,
    system_prompt: str,
    llm_url: str,
    model: str,
    api_key: str | None = None,
) -> tuple[AuthoredDeck | None, str, str]:
    """Generate a minimal outline deck (type+title per slide).

    Returns (deck_or_None, raw_response, error_msg).
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _OUTLINE_USER_TMPL.format(
            goal=goal, n=slide_count, archetypes=_ARCHETYPES_STR
        )},
    ]
    try:
        raw = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, "", f"LLM outline call failed: {e}"

    deck, err = _parse_authored_deck(raw)
    if deck is not None:
        return _prepare_outline_deck(deck), raw, ""

    # One retry — include the specific error + full theme enum so the model
    # can self-correct a theme mismatch (the most common parse failure).
    messages.append({"role": "assistant", "content": raw})
    messages.append({"role": "user", "content": _retry_msg(err)})
    try:
        raw2 = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, raw, f"Outline retry failed: {e}"

    deck2, err2 = _parse_authored_deck(raw2)
    if deck2 is not None:
        return _prepare_outline_deck(deck2), raw2, ""
    return None, raw2, f"Outline parse failed after retry: {err2}"


# ---------------------------------------------------------------------------
# Stage 2: Fill
# ---------------------------------------------------------------------------


async def _stage_fill(
    outline: AuthoredDeck,
    system_prompt: str,
    llm_url: str,
    model: str,
    api_key: str | None = None,
) -> tuple[AuthoredDeck | None, str]:
    """Fill body/notes/image_prompt for each slide in the outline.

    Returns (filled_deck_or_None, error_msg).
    """
    outline_json = outline.model_dump_json(indent=2)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _FILL_USER_TMPL.format(outline_json=outline_json)},
    ]
    try:
        raw = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, f"LLM fill call failed: {e}"

    deck, err = _parse_authored_deck(raw)
    if deck is not None:
        return _prepare_filled_deck(deck), ""

    # One retry — include the specific error + full theme enum so the model
    # can self-correct a theme mismatch (the most common parse failure).
    messages.append({"role": "assistant", "content": raw})
    messages.append({"role": "user", "content": _retry_msg(err)})
    try:
        raw2 = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, f"Fill retry failed: {e}"

    deck2, err2 = _parse_authored_deck(raw2)
    if deck2 is not None:
        return _prepare_filled_deck(deck2), ""
    return None, f"Fill parse failed after retry: {err2}"


# ---------------------------------------------------------------------------
# Stage 3: Asset generation (image_prompt → image bytes)
# ---------------------------------------------------------------------------


def _is_raster_bytes(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") or data[:3] == b"\xff\xd8\xff"


async def _stage_assets(
    authored: AuthoredDeck,
    ctx: ToolContext,
    backend: ImageBackend | None,
    filename_base: str,
) -> dict[int, bytes]:
    """For each slide with image_prompt, generate the image, write it to the sandbox
    as a standalone artifact, AND return {authored_slide_index: image_bytes}.

    The returned map is handed to ``lower_deck(image_assets=...)`` so the generated
    picture is embedded directly into the rendered deck (C7 wire) — previously the
    bytes were written to disk but never reached the renderer, so every image slide
    fell back to the ``[image]`` placeholder. The on-disk copy is kept too (a
    browsable artifact + back-compat for any path-based consumer).
    """
    import hashlib

    assets: dict[int, bytes] = {}
    # W-50: image generation is OPTIONAL for a deck. When no real image backend is
    # configured (select_image_backend() raised → caller passed None), DEGRADE to
    # text-only slides — omit images rather than crash. Configure ComfyUI/OpenAI/
    # OpenRouter in Settings → Image generation to include real images.
    if backend is None:
        _LOG.info("no image backend configured — generating image-less (text-only) slides")
        return assets
    for i, slide in enumerate(authored.slides):
        if not slide.image_prompt:
            continue
        img_name = f"{filename_base}_img_{i}.png"
        meta_name = f"{filename_base}_img_{i}.sha256"
        h = hashlib.sha256(slide.image_prompt.encode("utf-8"))
        h_hex = h.hexdigest()
        seed = int.from_bytes(h.digest()[:4], "big", signed=False)

        if ctx.sandbox is not None:
            try:
                cached_hash = await ctx.sandbox.read_file(meta_name)
                cached_img = await ctx.sandbox.read_file(img_name)
                if cached_hash.decode("utf-8").strip() == h_hex and _is_raster_bytes(cached_img):
                    assets[i] = cached_img
                    continue
            except Exception:  # noqa: BLE001
                pass

        try:
            img_bytes = backend.generate(
                prompt=slide.image_prompt,
                # 16:9 landscape, both dims >= the ComfyUI 512 floor so a real
                # diffusion backend doesn't snap 384 up to 1024 and hand back a
                # distorted portrait for a slide that wants a wide image.
                width=1024,
                height=576,
                seed=seed,
                fmt="png",
            )
            # Only embed real raster bytes — a backend that returns something other
            # than PNG/JPEG (mis-config, error blob) must fall back to the [image]
            # placeholder, not a broken data-URI / corrupt add_picture.
            if not _is_raster_bytes(img_bytes):
                _LOG.warning("Slide %d image is not PNG/JPEG — skipping embed", i)
                continue
            assets[i] = img_bytes
            if ctx.sandbox is not None:
                await ctx.sandbox.write_file(img_name, img_bytes)
                await ctx.sandbox.write_file(meta_name, h_hex.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            _LOG.warning("Image generation failed for slide %d: %s", i, e)
    return assets


# ---------------------------------------------------------------------------
# Fallback markdown generator (used when fill stage fails)
# ---------------------------------------------------------------------------


def _outline_to_markdown(outline: AuthoredDeck | None, goal: str) -> str:
    """Convert an outline (or None) to minimal Marp markdown as a fallback."""
    if outline is None or not outline.slides:
        # Bare minimum fallback
        return f"# {goal}\n\n---\n\n*Deck generation failed — please try again.*"

    parts: list[str] = []
    for slide in outline.slides:
        title = slide.title or "Slide"
        body = "\n".join(f"- {b}" for b in slide.body) if slide.body else ""
        parts.append(f"# {title}\n\n{body}".strip())
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# generate_deck — main pipeline entry point
# ---------------------------------------------------------------------------


async def generate_deck(
    goal: str,
    filename: str,
    ctx: ToolContext,
    backend: ImageBackend | None,
    *,
    slide_count: int = 5,
) -> tuple[Deck | None, str | None, str | None, str | None]:
    """Run the full C2 generation pipeline.

    Returns:
        (c1_deck, fallback_markdown, error_msg, authored_sidecar)

    ``authored_sidecar`` is the ``{filename}.authored.json`` path IFF this run wrote
    it successfully (else None) — the caller advertises the in-app editor only on a
    real, fresh sidecar, never a stale leftover from a prior run whose write failed.

    On success: (Deck, None, None, sidecar-or-None)
    On fill-stage failure: (None, fallback_markdown_str, error_msg, None)
    """
    # ROOT-5: honor the CONVERSATION's effective (override-aware) driver model when the
    # executor supplied it — so a deck authored in a conversation that picked a specific
    # model (e.g. a remote DeepSeek) is generated by THAT model, not the global default
    # AGENT_DRIVER. The key env-var name is resolved here (ctx never carries raw secrets).
    if ctx.driver_llm is not None:
        base_url, model, api_key_env = ctx.driver_llm
        llm_url, api_key = base_url.rstrip("/"), _resolve_llm_key(api_key_env)
    else:
        llm_url, model, api_key = _resolve_slides_llm()
    system = _WEAK_SYSTEM if ctx.assist else _CAPABLE_SYSTEM

    # Stage 1: Outline
    outline, _raw_outline, outline_err = await _stage_outline(
        goal, slide_count, system, llm_url, model, api_key
    )

    if outline is None:
        _LOG.warning("Outline stage failed: %s", outline_err)
        fallback_md = _outline_to_markdown(None, goal)
        return None, fallback_md, outline_err, None

    # Stage 2: Fill
    filled, fill_err = await _stage_fill(outline, system, llm_url, model, api_key)

    if filled is None:
        _LOG.warning("Fill stage failed: %s — using outline as fallback", fill_err)
        fallback_md = _outline_to_markdown(outline, goal)
        return None, fallback_md, fill_err, None

    # Stage 3: Assets (image_prompt → image bytes + sandbox files)
    image_assets = await _stage_assets(filled, ctx, backend, filename)

    # Stage 4: Lower to C1 Deck (carry generated image bytes so they embed — C7)
    try:
        from disco.tools.builtin._direction_brand import direction_brand_override

        deck = lower_deck(
            filled,
            image_assets=image_assets,
            brand_override=await direction_brand_override(ctx),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.warning("lower_deck failed: %s — falling back to Marp", e)
        fallback_md = _outline_to_markdown(filled, goal)
        return None, fallback_md, str(e), None

    # A2.0: persist the editable AuthoredDeck source alongside the renders so the
    # in-app deck editor (+ deck_patch tool) can read it back. Best-effort: a write
    # failure here must not sink an otherwise-successful generation — but we report
    # the sidecar path ONLY when the write actually succeeded, so the caller never
    # advertises the editor against a stale leftover sidecar from an earlier run.
    authored_sidecar: str | None = None
    if ctx.sandbox is not None:
        sidecar = f"{filename}.authored.json"
        try:
            await ctx.sandbox.write_file(
                sidecar, filled.model_dump_json(indent=2).encode()
            )
            authored_sidecar = sidecar
        except Exception as e:  # noqa: BLE001
            _LOG.warning("Failed to persist authored.json for %s: %s", filename, e)

    return deck, None, None, authored_sidecar

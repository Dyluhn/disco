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
from disco.tools.builtin._deck_schema import AuthoredDeck, Deck, lower_deck
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
You are an expert slide-deck designer.  Generate a slide deck as valid JSON matching \
the AuthoredDeck schema.  Output ONLY valid JSON — no markdown fences, no prose before \
or after.

AuthoredDeck schema:
{
  "title": "Deck title",
  "theme": "disco-light" | "disco-dark" | "ink-light" | "sepia-light"
           | "signal-light" | "midnight-dark" | "neutral" | "neutral-light",
  "slides": [AuthoredSlide, ...]
}

AuthoredSlide schema:
{
  "type": "title"|"bullets"|"section_header"|"two_column"|"comparison"|"metrics"|"image_right"|"image_left"|"full_image"|"table"|"closing",
  "title": "Slide heading",
  "body": ["bullet or content line", ...],
  "layout_hint": null | "title"|"bullets"|...,
  "image_prompt": null | "prompt for image generation",
  "chart": null,
  "table": null,
  "notes": null | "speaker notes (never shown on slide)"
}

Rules:
- body: 4–6 lines suggested (no hard cap — overflow is handled by the lowerer).
- If image_prompt is set and the slide is primarily visual, set body=[] or at most one \
  caption line.  Do NOT fill body with text descriptions of what the image shows.
- notes and image_prompt are NEVER shown on the visible slide face.
- Use "type": "title" for the opening slide, "closing" for the last.
- Well-known type values: title, bullets, section_header, two_column, comparison, \
  metrics, image_right, image_left, full_image, table, closing.
"""

# Worked-example system prompt for weak models (ctx.assist=True)
_WEAK_SYSTEM = """\
You are an expert slide-deck designer.  Generate a slide deck as valid JSON.
Output ONLY valid JSON — no markdown fences, no prose.

The "theme" field MUST be one of these exact strings:
"disco-light" | "disco-dark" | "ink-light" | "sepia-light"
| "signal-light" | "midnight-dark" | "neutral" | "neutral-light"

Copy this exact structure and fill it in with the requested content:

{
  "title": "DECK TITLE HERE",
  "theme": "disco-light",
  "slides": [
    {
      "type": "title",
      "title": "MAIN TITLE",
      "body": ["One-line subtitle or tagline"],
      "layout_hint": null,
      "image_prompt": null,
      "chart": null,
      "table": null,
      "notes": "Optional speaker notes"
    },
    {
      "type": "bullets",
      "title": "SLIDE HEADING",
      "body": [
        "First bullet point",
        "Second bullet point",
        "Third bullet point",
        "Fourth bullet point"
      ],
      "layout_hint": null,
      "image_prompt": null,
      "chart": null,
      "table": null,
      "notes": null
    },
    {
      "type": "closing",
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

IMPORTANT: If image_prompt is set, body must be [] or at most one caption line.
"""

_OUTLINE_USER_TMPL = """\
Goal: {goal}

Generate an outline of {n} slides for this deck.

Output a minimal AuthoredDeck JSON where each slide has ONLY "type" and "title" filled in.
Body, notes, and image_prompt should be [] or null at this stage.
Deck title should be short and professional.
"""

_FILL_USER_TMPL = """\
Here is the slide outline:
{outline_json}

Now fill in the complete content for each slide.  For each slide:
- "body": 4–6 bullet points or content lines.
- "notes": optional 1–3 sentence speaker notes.
- "image_prompt": if the slide is primarily visual, set a descriptive image generation \
  prompt AND set body=[] or at most one caption line.
- Keep type and title from the outline (do not change them).

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
            goal=goal, n=slide_count
        )},
    ]
    try:
        raw = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, "", f"LLM outline call failed: {e}"

    deck, err = _parse_authored_deck(raw)
    if deck is not None:
        return deck, raw, ""

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
        return deck2, raw2, ""
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
        return deck, ""

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
        return deck2, ""
    return None, f"Fill parse failed after retry: {err2}"


# ---------------------------------------------------------------------------
# Stage 3: Asset generation (image_prompt → image bytes)
# ---------------------------------------------------------------------------


async def _stage_assets(
    authored: AuthoredDeck,
    ctx: ToolContext,
    backend: ImageBackend,
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
    for i, slide in enumerate(authored.slides):
        if not slide.image_prompt:
            continue
        img_name = f"{filename_base}_img_{i}.png"
        h = hashlib.sha256(slide.image_prompt.encode("utf-8")).digest()
        seed = int.from_bytes(h[:4], "big", signed=False)

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
            if not (img_bytes.startswith(b"\x89PNG\r\n\x1a\n") or img_bytes[:3] == b"\xff\xd8\xff"):
                _LOG.warning("Slide %d image is not PNG/JPEG — skipping embed", i)
                continue
            assets[i] = img_bytes
            if ctx.sandbox is not None:
                await ctx.sandbox.write_file(img_name, img_bytes)
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
    backend: ImageBackend,
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
    image_assets: dict[int, bytes] = {}
    if ctx.sandbox is not None:
        image_assets = await _stage_assets(filled, ctx, backend, filename)

    # Stage 4: Lower to C1 Deck (carry generated image bytes so they embed — C7)
    try:
        deck = lower_deck(filled, image_assets=image_assets)
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

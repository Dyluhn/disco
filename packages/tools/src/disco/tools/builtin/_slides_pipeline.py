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
from typing import TYPE_CHECKING

import httpx
from disco.tools.builtin._deck_schema import AuthoredDeck, Deck, lower_deck
from pydantic import ValidationError

if TYPE_CHECKING:
    from disco.tools.anatomy import ToolContext
    from disco.tools.builtin.image_gen import ImageBackend

_LOG = logging.getLogger("disco.tools.slides_pipeline")

# ---------------------------------------------------------------------------
# LLM endpoint resolution (ConfigStore-based; no agent_server import)
# ---------------------------------------------------------------------------


def _resolve_slides_llm() -> tuple[str, str]:
    """Return (base_url, model_id) for the slides generation LLM.

    Uses the AGENT_DRIVER model from ConfigStore.  Falls back to env vars
    LLM_URL / LLM_MODEL, then to a hardcoded local default.

    This mirrors audio_config.py's approach without importing from agent_server.
    """
    try:
        from disco.core.llm import ConfigStore
        from disco.core.llm.types import ModelRole

        cfg = ConfigStore().load()
        model_key = cfg.model_for(ModelRole.AGENT_DRIVER)
        entry = cfg.models.get(model_key)
        if entry and entry.base_url:
            return entry.base_url.rstrip("/"), entry.model_id
    except Exception:  # noqa: BLE001
        pass
    url = os.environ.get("LLM_URL", "http://localhost:18080/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "local-model")
    return url, model


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
  "theme": "disco-light" | "disco-dark" | "neutral",
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

_RETRY_MSG = (
    "Your previous response was not valid JSON or did not match the AuthoredDeck schema.\n\n"
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
    temperature: float = 0.7,
    max_tokens: int = 8192,
) -> str:
    """Call the OpenAI-compatible /chat/completions endpoint.  Returns raw text."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            f"{llm_url}/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


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
    """Parse raw LLM output as an AuthoredDeck.  Returns (deck, error_msg)."""
    extracted = _extract_json_object(raw)
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
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
        raw = await _call_llm(messages, llm_url, model)
    except Exception as e:  # noqa: BLE001
        return None, "", f"LLM outline call failed: {e}"

    deck, err = _parse_authored_deck(raw)
    if deck is not None:
        return deck, raw, ""

    # One retry
    messages.append({"role": "assistant", "content": raw})
    messages.append({"role": "user", "content": _RETRY_MSG})
    try:
        raw2 = await _call_llm(messages, llm_url, model)
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
        raw = await _call_llm(messages, llm_url, model)
    except Exception as e:  # noqa: BLE001
        return None, f"LLM fill call failed: {e}"

    deck, err = _parse_authored_deck(raw)
    if deck is not None:
        return deck, ""

    # One retry
    messages.append({"role": "assistant", "content": raw})
    messages.append({"role": "user", "content": _RETRY_MSG})
    try:
        raw2 = await _call_llm(messages, llm_url, model)
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
) -> AuthoredDeck:
    """For each slide with image_prompt, generate image bytes and store in sandbox.

    Returns a new AuthoredDeck with image_prompt-bearing slides unchanged
    (the image path is stored on the lowered Element in lower_deck output);
    here we write the images to the sandbox so the renderer can find them.

    The image file name is stored in a parallel dict returned for use in the
    render step.  Because AuthoredDeck is immutable at the Pydantic level,
    we don't mutate slides — the renderer picks up the file by convention:
    "{filename_base}_img_{i}.png" where i is the slide index.
    """
    for i, slide in enumerate(authored.slides):
        if not slide.image_prompt:
            continue
        img_name = f"{filename_base}_img_{i}.png"
        import hashlib
        h = hashlib.sha256(slide.image_prompt.encode("utf-8")).digest()
        seed = int.from_bytes(h[:4], "big", signed=False)

        try:
            img_bytes = backend.generate(
                prompt=slide.image_prompt,
                width=512,
                height=384,
                seed=seed,
                fmt="png",
            )
            if ctx.sandbox is not None:
                await ctx.sandbox.write_file(img_name, img_bytes)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("Image generation failed for slide %d: %s", i, e)
    return authored


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
) -> tuple[Deck | None, str | None, str | None]:
    """Run the full C2 generation pipeline.

    Returns:
        (c1_deck, fallback_markdown, error_msg)

    On success: (Deck, None, None)
    On fill-stage failure: (None, fallback_markdown_str, error_msg)
    """
    llm_url, model = _resolve_slides_llm()
    system = _WEAK_SYSTEM if ctx.assist else _CAPABLE_SYSTEM

    # Stage 1: Outline
    outline, _raw_outline, outline_err = await _stage_outline(
        goal, slide_count, system, llm_url, model
    )

    if outline is None:
        _LOG.warning("Outline stage failed: %s", outline_err)
        fallback_md = _outline_to_markdown(None, goal)
        return None, fallback_md, outline_err

    # Stage 2: Fill
    filled, fill_err = await _stage_fill(outline, system, llm_url, model)

    if filled is None:
        _LOG.warning("Fill stage failed: %s — using outline as fallback", fill_err)
        fallback_md = _outline_to_markdown(outline, goal)
        return None, fallback_md, fill_err

    # Stage 3: Assets (image_prompt → image files in sandbox)
    if ctx.sandbox is not None:
        filled = await _stage_assets(filled, ctx, backend, filename)

    # Stage 4: Lower to C1 Deck
    try:
        deck = lower_deck(filled)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("lower_deck failed: %s — falling back to Marp", e)
        fallback_md = _outline_to_markdown(filled, goal)
        return None, fallback_md, str(e)

    return deck, None, None

"""C2 — Staged deck generation pipeline.

Pipeline stages:
  1. Outline (LLM): Generate {type, title} for all slides (structural skeleton).
  2. Fill (LLM):   Fill body, image_prompt, notes, chart/table per slide.
  3. Assets:       Call image_generate backend for each slide with image_prompt.
  4. Lower:        lower_deck(AuthoredDeck) → Deck via C1.
  5. Render:       Deck → .pptx + .html via C3.

Defensive parse: a completed but malformed response gets ONE correction retry;
a second completed malformed response falls back to the Marp path. Provider-truncated
responses fail explicitly and never enter that malformed-output fallback.

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

The implementation (prompt templates, LLM endpoint resolution, the raw
chat-completions transport, theme/craft defaults, JSON extraction/validation)
lives in the sibling ``_slides_pipeline_parts`` package; this module re-imports
and re-exports every one of those names unchanged so that
``disco.tools.builtin._slides_pipeline.<name>`` keeps resolving exactly as it
did before the split — including for ``find_and_edit.py``'s imports and for
test ``patch``/``patch.object`` targets, since every real caller of these names
(``_stage_outline``, ``_stage_fill``, ``_stage_assets``, ``generate_deck``)
still lives physically in this module and therefore still resolves them at
call time through this module's globals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from disco.tools.builtin._deck_schema import AuthoredDeck, AuthoredSlide, Deck, lower_deck

from ._slides_pipeline_parts._constants import (
    _ARCHETYPES as _ARCHETYPES,
)
from ._slides_pipeline_parts._constants import (
    _ARCHETYPES_STR,
)
from ._slides_pipeline_parts._constants import (
    _VALID_THEMES as _VALID_THEMES,
)
from ._slides_pipeline_parts._constants import (
    _VALID_THEMES_STR as _VALID_THEMES_STR,
)

# --- compatibility re-exports (PKG-10-MEDIA) --------------------------------
# Names this module exposed BEFORE the parts split. Consumers and the test
# suite reach several of them through this module object (including via
# monkeypatch), so the facade must keep exposing every one. Restored
# programmatically by diffing this module's top-level names against its
# parent commit; PKG-13-FACADES owns their eventual deletion.
from ._slides_pipeline_parts._constants import (
    SlideArchetype as SlideArchetype,
)
from ._slides_pipeline_parts._constants import (
    get_args as get_args,
)
from ._slides_pipeline_parts._craft import (
    _BANNED_TITLE_PATTERNS as _BANNED_TITLE_PATTERNS,
)
from ._slides_pipeline_parts._craft import (
    _IMAGE_SLOT_ARCHETYPES as _IMAGE_SLOT_ARCHETYPES,
)
from ._slides_pipeline_parts._craft import (
    _SECTION_ARCHETYPES as _SECTION_ARCHETYPES,
)
from ._slides_pipeline_parts._craft import (
    _THEME_ALIASES as _THEME_ALIASES,
)
from ._slides_pipeline_parts._craft import (
    _VALID_LAYOUT_HINTS as _VALID_LAYOUT_HINTS,
)
from ._slides_pipeline_parts._craft import (
    AccentSpec as AccentSpec,
)
from ._slides_pipeline_parts._craft import (
    FontPairingSpec as FontPairingSpec,
)
from ._slides_pipeline_parts._craft import (
    LightDarkTokenPair as LightDarkTokenPair,
)
from ._slides_pipeline_parts._craft import (
    _clip_words as _clip_words,
)
from ._slides_pipeline_parts._craft import (
    _coerce_known_theme_aliases as _coerce_known_theme_aliases,
)
from ._slides_pipeline_parts._craft import (
    _compose_image_prompt as _compose_image_prompt,
)
from ._slides_pipeline_parts._craft import (
    _demote_tableless_comparisons as _demote_tableless_comparisons,
)
from ._slides_pipeline_parts._craft import (
    _enforce_density_budgets as _enforce_density_budgets,
)
from ._slides_pipeline_parts._craft import (
    _ensure_image_slot_prompts as _ensure_image_slot_prompts,
)
from ._slides_pipeline_parts._craft import (
    _image_slot_type as _image_slot_type,
)
from ._slides_pipeline_parts._craft import (
    _infer_archetype as _infer_archetype,
)
from ._slides_pipeline_parts._craft import (
    _null_invalid_layout_hints as _null_invalid_layout_hints,
)
from ._slides_pipeline_parts._craft import (
    _prepare_filled_deck,
    _prepare_outline_deck,
)
from ._slides_pipeline_parts._craft import (
    _scrub_title as _scrub_title,
)
from ._slides_pipeline_parts._craft import (
    _theme_defaults as _theme_defaults,
)
from ._slides_pipeline_parts._craft import (
    _with_craft_defaults as _with_craft_defaults,
)
from ._slides_pipeline_parts._craft import (
    re as re,
)
from ._slides_pipeline_parts._json_deck_parse import (
    ValidationError as ValidationError,
)
from ._slides_pipeline_parts._json_deck_parse import (
    _extract_json_object as _extract_json_object,
)
from ._slides_pipeline_parts._json_deck_parse import (
    _parse_authored_deck,
)
from ._slides_pipeline_parts._json_deck_parse import (
    json as json,
)
from ._slides_pipeline_parts._llm_client import (
    LLMResponse as LLMResponse,
)
from ._slides_pipeline_parts._llm_client import _call_llm, _complete_response_content, _retry_msg
from ._slides_pipeline_parts._llm_client import (
    httpx as httpx,
)
from ._slides_pipeline_parts._llm_endpoint import (
    Any as Any,
)
from ._slides_pipeline_parts._llm_endpoint import (
    _purpose_for_model_endpoint,
    _resolve_llm_key,
    _resolve_slides_llm,
)
from ._slides_pipeline_parts._prompts import (
    _CAPABLE_SYSTEM,
    _FILL_USER_TMPL,
    _OUTLINE_USER_TMPL,
    _WEAK_SYSTEM,
)
from ._slides_pipeline_parts._types import (
    LLMResponseLike as LLMResponseLike,
)
from ._slides_pipeline_parts._types import (
    SlidesGenerationIncompleteError,
)

if TYPE_CHECKING:
    from disco.tools.anatomy import ToolContext
    from disco.tools.builtin.image_gen import ImageBackend

_LOG = logging.getLogger("disco.tools.slides_pipeline")


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
        {
            "role": "user",
            "content": _OUTLINE_USER_TMPL.format(
                goal=goal, n=slide_count, archetypes=_ARCHETYPES_STR
            ),
        },
    ]
    try:
        response = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, "", f"LLM outline call failed: {type(e).__name__}: {e}"
    raw = _complete_response_content(response, stage="outline")

    deck, err = _parse_authored_deck(raw)
    if deck is not None:
        return _prepare_outline_deck(deck), raw, ""

    # One retry — include the specific error + full theme enum so the model
    # can self-correct a theme mismatch (the most common parse failure).
    messages.append({"role": "assistant", "content": raw})
    messages.append({"role": "user", "content": _retry_msg(err)})
    try:
        response2 = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, raw, f"Outline retry failed: {e}"
    raw2 = _complete_response_content(response2, stage="outline correction")

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
        response = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, f"LLM fill call failed: {type(e).__name__}: {e}"
    raw = _complete_response_content(response, stage="fill")

    deck, err = _parse_authored_deck(raw)
    if deck is not None:
        return _prepare_filled_deck(deck), ""

    # One retry — include the specific error + full theme enum so the model
    # can self-correct a theme mismatch (the most common parse failure).
    messages.append({"role": "assistant", "content": raw})
    messages.append({"role": "user", "content": _retry_msg(err)})
    try:
        response2 = await _call_llm(messages, llm_url, model, api_key=api_key)
    except Exception as e:  # noqa: BLE001
        return None, f"Fill retry failed: {e}"
    raw2 = _complete_response_content(response2, stage="fill correction")

    deck2, err2 = _parse_authored_deck(raw2)
    if deck2 is not None:
        return _prepare_filled_deck(deck2), ""
    return None, f"Fill parse failed after retry: {err2}"


# ---------------------------------------------------------------------------
# Stage 3: Asset generation (image_prompt → image bytes)
# ---------------------------------------------------------------------------


def _is_raster_bytes(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") or data[:3] == b"\xff\xd8\xff"


@dataclass
class ImageGenStats:
    """Honest per-deck image-generation outcome, surfaced all the way to the
    slides_generate ToolOutcome (gauntlet 2026-07-07: a mis-configured flux
    model failed EVERY slide image and the agent had no way to know — the
    failures died in a server-side log warning)."""

    configured: bool = False
    wanted: int = 0
    generated: int = 0
    failed: list[int] = field(default_factory=list)
    sample_error: str | None = None

    def note(self) -> str:
        """One-line human/agent-facing summary for the tool result content."""
        if self.wanted == 0:
            return ""
        if not self.configured:
            return (
                f"\nImages: 0/{self.wanted} — image generation is NOT configured; "
                "themed art fallback used for every image slot. Configure "
                "Settings → Image generation (and use its Test button) for real images."
            )
        if self.failed:
            reason = f" (first error: {self.sample_error})" if self.sample_error else ""
            return (
                f"\nImages: {self.generated}/{self.wanted} generated — "
                f"{len(self.failed)} FAILED{reason}; themed art fallback used for the "
                "failed slots. The configured image model may not support image "
                "output — verify it with Settings → Image generation → Test."
            )
        return f"\nImages: {self.generated}/{self.wanted} generated."


async def _cached_slide_image(
    ctx: ToolContext, meta_name: str, img_name: str, expected_hash: str
) -> bytes | None:
    """Return the sandbox's previously-generated image for this slide if its
    cached hash still matches the current prompt and the bytes are real
    raster data, else None (no usable cache entry)."""
    if ctx.sandbox is None:
        return None
    try:
        cached_hash = await ctx.sandbox.read_file(meta_name)
        cached_img = await ctx.sandbox.read_file(img_name)
        if cached_hash.decode("utf-8").strip() == expected_hash and _is_raster_bytes(cached_img):
            return cached_img
    except Exception:  # noqa: BLE001
        pass
    return None


async def _generate_one_slide_image(
    ctx: ToolContext,
    backend: ImageBackend,
    slide: AuthoredSlide,
    seed: int,
    img_name: str,
    meta_name: str,
    h_hex: str,
    index: int,
) -> tuple[bytes | None, str | None]:
    """Generate + cache one slide's image.

    Returns ``(image_bytes, None)`` on success, ``(None, None)`` when the
    backend returned something that isn't real raster data (not an error, just
    not embeddable), or ``(None, error_summary)`` when generation raised.
    """
    prompt = slide.image_prompt or ""
    try:
        img_bytes = backend.generate(
            prompt=prompt,
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
            _LOG.warning("Slide %d image is not PNG/JPEG — skipping embed", index)
            return None, None
        if ctx.sandbox is not None:
            await ctx.sandbox.write_file(img_name, img_bytes)
            await ctx.sandbox.write_file(meta_name, h_hex.encode("utf-8"))
        return img_bytes, None
    except Exception as e:  # noqa: BLE001
        _LOG.warning("Image generation failed for slide %d: %s", index, e)
        return None, f"{type(e).__name__}: {e}"[:200]


async def _stage_assets(
    authored: AuthoredDeck,
    ctx: ToolContext,
    backend: ImageBackend | None,
    filename_base: str,
) -> tuple[dict[int, bytes], ImageGenStats]:
    """For each slide with image_prompt, generate the image, write it to the sandbox
    as a standalone artifact, AND return {authored_slide_index: image_bytes}.

    The returned map is handed to ``lower_deck(image_assets=...)`` so the generated
    picture is embedded directly into the rendered deck (C7 wire) — previously the
    bytes were written to disk but never reached the renderer, so every image slide
    fell back to the ``[image]`` placeholder. The on-disk copy is kept too (a
    browsable artifact + back-compat for any path-based consumer).

    Also returns ImageGenStats so the caller can put the REAL image outcome in
    the tool result instead of burying failures in a log line.
    """
    import hashlib

    assets: dict[int, bytes] = {}
    stats = ImageGenStats(
        configured=backend is not None,
        wanted=sum(1 for s in authored.slides if s.image_prompt),
    )
    # W-50: image generation is OPTIONAL for a deck. When no real image backend is
    # configured (select_image_backend() raised → caller passed None), DEGRADE to
    # art-fallback slides — omit real images rather than crash. Configure ComfyUI/
    # OpenAI/OpenRouter in Settings → Image generation to include real images.
    if backend is None:
        _LOG.info("no image backend configured — generating image-less (text-only) slides")
        return assets, stats
    wanted = 0  # slides that requested an image
    failed: list[int] = []  # slide indices whose image generation errored/was rejected
    for i, slide in enumerate(authored.slides):
        if not slide.image_prompt:
            continue
        wanted += 1
        img_name = f"{filename_base}_img_{i}.png"
        meta_name = f"{filename_base}_img_{i}.sha256"
        h = hashlib.sha256(slide.image_prompt.encode("utf-8"))
        h_hex = h.hexdigest()
        seed = int.from_bytes(h.digest()[:4], "big", signed=False)

        cached = await _cached_slide_image(ctx, meta_name, img_name, h_hex)
        if cached is not None:
            assets[i] = cached
            continue

        img_bytes, err = await _generate_one_slide_image(
            ctx, backend, slide, seed, img_name, meta_name, h_hex, i
        )
        if img_bytes is not None:
            assets[i] = img_bytes
        else:
            failed.append(i)
            if err is not None and stats.sample_error is None:
                stats.sample_error = err
    if failed:
        # One aggregate signal instead of scattered per-slide lines — a backend that
        # is configured but erroring (out of credits, rate-limited) otherwise produced
        # an image-less deck invisibly.
        _LOG.warning(
            "Deck image generation: %d of %d requested slide image(s) failed (slides %s) "
            "— those slides fall back to the themed art placeholder.",
            len(failed),
            wanted,
            ", ".join(str(x) for x in failed),
        )
    stats.wanted = wanted
    stats.failed = failed
    stats.generated = len(assets)
    return assets, stats


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
) -> tuple[Deck | None, str | None, str | None, str | None, ImageGenStats | None]:
    """Run the full C2 generation pipeline.

    Returns:
        (c1_deck, fallback_markdown, error_msg, authored_sidecar, image_stats)

    ``authored_sidecar`` is the ``{filename}.authored.json`` path IFF this run wrote
    it successfully (else None) — the caller advertises the in-app editor only on a
    real, fresh sidecar, never a stale leftover from a prior run whose write failed.
    ``image_stats`` carries the honest image-generation outcome (configured /
    generated / failed + first error) for the tool result.

    On success: (Deck, None, None, sidecar-or-None, stats)
    On fill-stage failure: (None, fallback_markdown_str, error_msg, None, None)
    """
    # ROOT-5: honor the CONVERSATION's effective (override-aware) driver model when the
    # executor supplied it — so a deck authored in a conversation that picked a specific
    # model (e.g. a remote DeepSeek) is generated by THAT model, not the global default
    # AGENT_DRIVER. The key env-var name is resolved here (ctx never carries raw secrets).
    if ctx.driver_llm is not None:
        base_url, model, api_key_env = ctx.driver_llm
        from disco.core.llm import ConfigStore
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin

        llm_url = base_url.rstrip("/")
        store = ConfigStore()
        cfg = store.load()
        purpose = _purpose_for_model_endpoint(cfg, llm_url, api_key_env)
        if not store.origin_approved(llm_url, purpose, api_key_env):
            return None, _outline_to_markdown(None, goal), "LLM origin not approved", None, None
        if not secret_ref_allowed_for_origin(api_key_env, llm_url):
            return None, _outline_to_markdown(None, goal), "LLM secret_ref not allowed", None, None
        api_key = _resolve_llm_key(api_key_env)
    else:
        llm_url, model, api_key = _resolve_slides_llm()
    if not llm_url:
        return None, _outline_to_markdown(None, goal), "LLM origin not approved", None, None
    system = _WEAK_SYSTEM if ctx.assist else _CAPABLE_SYSTEM

    # Stage 1: Outline. A provider-declared incomplete response is not ordinary
    # malformed JSON: do not replay it at the same cap and do not substitute a
    # plain deck that could be mistaken for the requested authored deliverable.
    try:
        outline, _raw_outline, outline_err = await _stage_outline(
            goal, slide_count, system, llm_url, model, api_key
        )
    except SlidesGenerationIncompleteError as exc:
        _LOG.error("Slide generation stopped explicitly: %s", exc)
        return None, None, str(exc), None, None

    if outline is None:
        _LOG.warning("Outline stage failed: %s", outline_err)
        fallback_md = _outline_to_markdown(None, goal)
        return None, fallback_md, outline_err, None, None

    # Stage 2: Fill uses the same explicit classification. The valid outline is
    # kept in memory only; no sidecar or rendered artifact is written.
    try:
        filled, fill_err = await _stage_fill(outline, system, llm_url, model, api_key)
    except SlidesGenerationIncompleteError as exc:
        _LOG.error("Slide generation stopped explicitly: %s", exc)
        return None, None, str(exc), None, None

    if filled is None:
        _LOG.warning("Fill stage failed: %s — using outline as fallback", fill_err)
        fallback_md = _outline_to_markdown(outline, goal)
        return None, fallback_md, fill_err, None, None

    # Stage 3: Assets (image_prompt → image bytes + sandbox files)
    image_assets, image_stats = await _stage_assets(filled, ctx, backend, filename)

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
        return None, fallback_md, str(e), None, None

    # A2.0: persist the editable AuthoredDeck source alongside the renders so the
    # in-app deck editor (+ deck_patch tool) can read it back. Best-effort: a write
    # failure here must not sink an otherwise-successful generation — but we report
    # the sidecar path ONLY when the write actually succeeded, so the caller never
    # advertises the editor against a stale leftover sidecar from an earlier run.
    authored_sidecar: str | None = None
    if ctx.sandbox is not None:
        sidecar = f"{filename}.authored.json"
        try:
            await ctx.sandbox.write_file(sidecar, filled.model_dump_json(indent=2).encode())
            authored_sidecar = sidecar
        except Exception as e:  # noqa: BLE001
            _LOG.warning("Failed to persist authored.json for %s: %s", filename, e)

    return deck, None, None, authored_sidecar, image_stats

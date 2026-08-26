"""Static vision-capability table and the canonical tri-state resolver.

V3 (§2) — pure, no httpx: importable from config.py without pulling network
dependencies.  Fills the gaps left by providers that don't expose capability
metadata (OpenAI, Anthropic) so the overlay in apply_runtime_capabilities
has a sensible default even without a live probe.

Probe-order integration (config.py):
  (1) per-model `vision` pin   → overrides everything
  (2) runtime probe result     → from wiring.py, beats table
  (3) provider-declared modality → preserved from the provider catalogue
  (4) this table               → static best-effort
  (5) unknown → unknown       ← fail-closed for image requests

Rules:
  Anthropic   — all claude ≥ v3 are vision-capable → True.
                Reuses the same detection logic as openai_provider._is_anthropic
                ("claude" in id OR starts with "anthropic/") so the two stay
                consistent without a circular import.
  OpenAI      — known vision models: gpt-4o/4.1/4.5, gpt-5*, o3/o4 → True.
                Non-visual specializations (embeddings, Whisper, TTS) → False.
  Gemini      — all gemini-* models are multimodal → True.
  Unknown     — unknown (fail-closed for image requests; never silently text-only).

The table is intentionally small and documented so every entry is auditable.
Do NOT add speculative entries — unknown remains unknown until a higher-authority
source or the required operator confirmation resolves it.
"""

from __future__ import annotations

from typing import Literal

VisionStatus = Literal["vision", "text-only", "unknown"]

# Keywords that conclusively identify a NON-VISION model regardless of family.
# Checked before vision patterns so an "embedding-4o" edge-case stays False.
_NON_VISION_KEYWORDS: tuple[str, ...] = (
    "embedding",  # text-embedding-*, *-embed-*, etc.
    "whisper",  # audio transcription models
    "-tts",  # TTS generation: tts-1, tts-1-hd, *-tts
    "rerank",  # cross-encoder rerankers
)

# Substring patterns that identify a known-vision model (applied after non-vision check).
# Keep these specific enough that an accidental substring match is unlikely.
_VISION_SUBSTRINGS: tuple[str, ...] = (
    "gpt-4o",  # gpt-4o, gpt-4o-mini, gpt-4o-2024-*
    "gpt-4.1",  # gpt-4.1, gpt-4.1-mini, gpt-4.1-nano
    "gpt-4.5",  # gpt-4.5, gpt-4.5-preview
    "gpt-5",  # gpt-5 and anticipated variants
    "gemini",  # gemini-*, google/gemini-* — all Gemini are multimodal
    # Open / multilingual vision-language models. "-vl" is the universal
    # vision-language naming convention (Qwen-VL, InternVL, …); the rest are
    # named multimodal families. Conclusive enough that an accidental substring
    # match is highly unlikely in a real model id.
    "-vl",  # qwen-2.5-vl, qwen2-vl, qwen3-vl, *-vl-*
    "internvl",  # InternVL (written as one word, no hyphen before vl)
    "llava",  # LLaVA family
    "vision",  # explicit *-vision-* tags
    "glm-4v",  # GLM-4V (Zhipu/THUDM multimodal)
    "glm-4.5v",  # GLM-4.5V
    "glm-4.6v",  # GLM-4.6V
)


def table_vision_status(model_id: str, family: str | None = None) -> bool | None:
    """Return the static table's tri-state answer.

    Parameters
    ----------
    model_id:
        The provider's model identifier string (may include a vendor prefix
        like "anthropic/claude-3-opus" or "openai/gpt-4o").
    family:
        Optional explicit family tag from ``ModelEntry.family``.  When
        provided it short-circuits to the family rule before inspecting
        ``model_id``.  Pass ``None`` if the family is unknown.

    Returns
    -------
    bool | None
        ``True`` = vision, ``False`` = conclusively text-only, ``None`` = unknown.
    """
    lower = model_id.lower()

    # ---- 1. Anthropic family rule -------------------------------------------
    # All Claude ≥ v3 are vision-capable.  The family tag takes precedence over
    # substring matching so an operator who explicitly sets family="anthropic"
    # gets the rule even if the id is opaque.
    if family == "anthropic" or "claude" in lower or lower.startswith("anthropic/"):
        return True

    # ---- 2. Known non-vision patterns (exclusions) --------------------------
    for kw in _NON_VISION_KEYWORDS:
        if kw in lower:
            return False

    # ---- 3. Known OpenAI / Gemini vision patterns ---------------------------
    for substring in _VISION_SUBSTRINGS:
        if substring in lower:
            return True

    # O-series: o3, o3-mini, o4, o4-mini — matched on the BASE id (after any
    # "vendor/" prefix) so a model like "voice-o3" (hypothetical) is not falsely
    # included.
    base_id = lower.split("/")[-1]
    if base_id.startswith(("o3", "o4")):
        return True

    # ---- 4. Unknown ----------------------------------------------------------
    return None


def table_vision(model_id: str, family: str | None = None) -> bool:
    """Back-compatible boolean view of the static table.

    Callers that need to distinguish an unknown model from a confirmed
    text-only model must use :func:`table_vision_status` or
    :func:`resolve_vision_status`.
    """

    return table_vision_status(model_id, family) is True


def resolve_vision_status(
    *,
    model_id: str,
    family: str | None = None,
    explicit: bool | None = None,
    live_probe: bool | None = None,
    provider_declared: bool | None = None,
) -> VisionStatus:
    """Resolve image understanding capability using one ordered authority.

    The order is intentionally short and deterministic:
    operator pin → current live probe → provider-declared modality →
    conclusive static catalogue rule → unknown.  ``None`` means *not known*;
    it is never converted to text-only here.
    """

    value = next(
        (
            candidate
            for candidate in (explicit, live_probe, provider_declared)
            if candidate is not None
        ),
        table_vision_status(model_id, family),
    )
    if value is True:
        return "vision"
    if value is False:
        return "text-only"
    return "unknown"

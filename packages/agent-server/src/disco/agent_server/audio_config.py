"""Audio overview config — voice assignments + service URLs.

VOICE_A / VOICE_B are the two Kokoro voices used for the two-host dialogue.
Dylan ratified af_heart + af_bella on 2026-06-10 (the two highest-graded
voices, A and A-). These are BINDING constants; the brief's pre-emptive
warning about `am_michael` did not materialise — the plan body (§2 RP-09 in
next-fix-set-plan.md) also specifies af_heart + af_bella, matching the
ratified decision. No discrepancy to report.

SPEACHES_URL points at the Speaches HTTP service (Kokoro TTS via an
OpenAI-compatible `/v1/audio/speech` endpoint). Default assumes Speaches
runs on the homelab mini-PC at port 8000.

The turn-script LLM is resolved at execution time through ConfigStore so a
poisoned config/env cannot trigger import-time host egress.
"""

from __future__ import annotations

import os

# ---- voice assignments (RATIFIED — do not change) --------------------------

VOICE_A: str = "af_heart"
VOICE_B: str = "af_bella"

# ---- Speaches endpoint -----------------------------------------------------

_SPEACHES_DEFAULT = "http://localhost:8000"
SPEACHES_URL: str = os.environ.get("SPEACHES_URL", _SPEACHES_DEFAULT).rstrip("/")

# ---- LLM endpoint (turn-script generation) ---------------------------------

LLM_URL: str = ""
LLM_MODEL: str = "local-model"
LLM_API_KEY_ENV: str | None = None


def resolve_llm_config() -> tuple[str, str, str | None]:
    from disco.core.llm import ConfigStore, ModelRole

    cfg = ConfigStore().load()
    key = cfg.assignments.get(ModelRole.RAG_ANSWERER) or cfg.default_model
    entry = cfg.models.get(key)
    if entry is None or not entry.base_url:
        return "", LLM_MODEL, None
    return entry.base_url.rstrip("/"), entry.model_id, entry.api_key_env

# ---- inter-turn silence range (ms) -----------------------------------------

SILENCE_MS_MIN: int = 300
SILENCE_MS_MAX: int = 600
SILENCE_MS_DEFAULT: int = 500

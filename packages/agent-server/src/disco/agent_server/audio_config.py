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

LLM_URL / LLM_MODEL configure the OpenAI-compatible chat completions
endpoint used to generate the turn-script JSON. Defaults to a local
llama.cpp server; override LLM_URL + LLM_MODEL via env vars.
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

import json


def _resolve_llm_config() -> tuple[str, str, str | None]:
    try:
        config_path = os.environ.get("DISCO_CONFIG") or os.environ.get("PMX_CONFIG") or "disco-config.json"
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        key = config.get("assignments", {}).get("rag_answerer") or config.get("default_model")
        model_info = config["models"][key]
        return model_info["base_url"], model_info["model_id"], model_info.get("api_key_env")
    except Exception:
        return "http://192.168.1.231:18080/v1", "llama-3-8b", None

_cfg_url, _cfg_model, _cfg_key_env = _resolve_llm_config()

LLM_URL: str = os.environ.get("LLM_URL", _cfg_url).rstrip("/")
LLM_MODEL: str = os.environ.get("LLM_MODEL", _cfg_model)
LLM_API_KEY_ENV: str | None = _cfg_key_env

# ---- inter-turn silence range (ms) -----------------------------------------

SILENCE_MS_MIN: int = 300
SILENCE_MS_MAX: int = 600
SILENCE_MS_DEFAULT: int = 500

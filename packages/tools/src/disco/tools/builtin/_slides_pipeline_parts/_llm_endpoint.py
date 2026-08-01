"""LLM endpoint resolution (ConfigStore-based; no agent_server import).

Extracted from ``_slides_pipeline.py``. Layering: disco.tools -> disco.core
(legal downward import). disco.tools =/=> disco.agent_server (upward import —
never here). This mirrors ``audio_config.py``'s approach without importing
from ``agent_server`` — preserve that if you touch this file.

``_resolve_slides_llm``, ``_resolve_llm_key`` and ``_purpose_for_model_endpoint``
are re-exported as module-level attributes of ``_slides_pipeline`` and imported
directly by ``find_and_edit.py`` and tests (including via
``patch.object(slides, "_resolve_slides_llm", ...)``, which relies on the real
caller — ``generate_deck``, which stays physically in ``_slides_pipeline.py`` —
resolving the name at call time through that module's own globals).
"""

from __future__ import annotations

from typing import Any


def _resolve_slides_llm() -> tuple[str, str, str | None]:
    """Return (base_url, model_id, api_key) for the slides generation LLM.

    Uses the AGENT_DRIVER model from ConfigStore. The api_key is resolved from
    the entry's `api_key_env` SecretStore ref so a REMOTE driver
    (OpenRouter / any paid OpenAI-compatible
    endpoint) authenticates — without it the deck author silently 401s on every
    non-local driver. Local keyless endpoints resolve to None (no auth header).

    This mirrors audio_config.py's approach without importing from agent_server.
    """
    try:
        from disco.core.llm import ConfigStore
        from disco.core.llm.secret_refs import secret_ref_allowed_for_origin
        from disco.core.llm.types import ModelRole

        store = ConfigStore()
        cfg = store.load()
        model_key = cfg.model_for(ModelRole.AGENT_DRIVER)
        entry = cfg.models.get(model_key)
        if (
            entry
            and entry.base_url
            and store.origin_approved(entry.base_url, f"model:{entry.provider}", entry.api_key_env)
            and secret_ref_allowed_for_origin(entry.api_key_env, entry.base_url)
        ):
            return entry.base_url.rstrip("/"), entry.model_id, _resolve_llm_key(entry.api_key_env)
    except Exception:  # noqa: BLE001
        pass
    return "", "local-model", None


def _resolve_llm_key(api_key_env: str | None) -> str | None:
    """Resolve the named secret for the deck-author LLM. Never logs the value.

    Mirrors the agent-server's canonical SecretStore-only resolution:
    the OpenRouter driver key is stored in the RESERVED "openrouter" SecretStore slot
    (set by the dedicated /api OpenRouter route), NOT under its env-var name — so a
    plain get_secret(api_key_env) misses it. Returns None if nothing is configured."""
    if not api_key_env:
        return None
    try:
        from disco.core.llm.secret_refs import resolve_provider_secret
        from disco.core.llm.secrets import SecretStore

        return resolve_provider_secret(api_key_env, SecretStore())
    except Exception:  # noqa: BLE001
        return None


def _purpose_for_model_endpoint(cfg: Any, llm_url: str, api_key_env: str | None) -> str:
    for entry in cfg.models.values():
        if not entry.base_url:
            continue
        # Normalize BOTH sides: a keyless local entry stores api_key_env as "" while
        # ctx.driver_llm carries it verbatim — `None == ""` must not knock a
        # self-hosted driver down to model:unknown (unapproved).
        if entry.base_url.rstrip("/") == llm_url and (entry.api_key_env or None) == (
            api_key_env or None
        ):
            return f"model:{entry.provider}"
    return "model:unknown"

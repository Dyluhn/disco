"""Build the live `ModelProvider` map from a `RouterConfig` (live-wiring).

One `OpenAIProvider` per distinct endpoint (`entry.provider`), pointed at that
entry's `base_url`, with the API key read from `entry.api_key_env` (env var) if
the server needs one. Entries without a `base_url` (the NLI cross-encoder, etc.)
are skipped — they aren't chat backends. Kept in its own module so importing
`config` (widely imported) doesn't pull `httpx`.

V2 (§2): async vision probes for providers that advertise modality metadata.
  - llama.cpp / self-host: GET {base_url − /v1}/props → modalities.vision bool.
  - OpenRouter: GET /api/v1/models → architecture.input_modalities includes "image".
  - Other: return None (defer to the static table in config.vision_table).
Results are memoized per (base_url, model_id) for the process lifetime.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

import httpx

from .config import ROLE_FALLBACK_PROVIDER_KEY, RouterConfig
from .openai_provider import OpenAIProvider
from .provider import ModelProvider
from .types import Requirement

_LOG = logging.getLogger("disco.wiring")

# V2 (§2): process-lifetime memo cache for vision probe results.
# Key: (base_url, model_id) → bool | None (True/False/unknown).
_VISION_PROBE_CACHE: dict[tuple[str, str], bool | None] = {}


async def probe_vision(
    base_url: str,
    model_id: str,
    client: httpx.AsyncClient,
) -> bool | None:
    """V2 (§2): probe one endpoint for vision capability.

    Provider detection (by URL):
    - openrouter.ai → GET /api/v1/models, match model id, check input_modalities.
    - else (llama.cpp / self-host) → GET {base_url − /v1}/props, read modalities.vision.

    Returns:
      True   — definitively vision-capable.
      False  — definitively not vision-capable.
      None   — unknown (network error, missing field, model not found); caller falls
               back to the static table.

    Fail-soft: never raises. Result memoized for the process lifetime.
    """
    cache_key = (base_url, model_id)
    if cache_key in _VISION_PROBE_CACHE:
        return _VISION_PROBE_CACHE[cache_key]

    result: bool | None = None
    try:
        if "openrouter.ai" in base_url:
            # OpenRouter advertises per-model input_modalities in /api/v1/models.
            resp = await client.get(f"{base_url}/models", timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                for m in data.get("data", []):
                    if m.get("id") == model_id:
                        modalities = m.get("architecture", {}).get("input_modalities", [])
                        result = "image" in modalities
                        break
                # If model not found in the list, result stays None.
        else:
            # llama.cpp / self-host: /props lives at the SERVER ROOT, not under /v1.
            # Strip "/v1" (or "/v1/") suffix to reach the server root.
            if base_url.endswith("/v1/"):
                server_url = base_url[:-4]
            elif base_url.endswith("/v1"):
                server_url = base_url[:-3]
            else:
                server_url = base_url
            resp = await client.get(f"{server_url}/props", timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                modalities = data.get("modalities", {})
                if isinstance(modalities, dict) and "vision" in modalities:
                    result = bool(modalities["vision"])
    except Exception:  # noqa: BLE001 — probe is fail-soft by contract
        result = None

    _VISION_PROBE_CACHE[cache_key] = result
    return result


async def probe_all_vision(config: RouterConfig) -> dict[str, bool | None]:
    """V2 (§2): probe all live model entries for vision capability.

    Skips entries with ``base_url=None`` (NLI cross-encoders etc.).
    Returns a dict keyed by model catalogue key → probe result.
    Uses a single AsyncClient for all probes; results are memoized in
    _VISION_PROBE_CACHE for the process lifetime.
    """
    results: dict[str, bool | None] = {}
    async with httpx.AsyncClient() as client:
        for key, entry in config.models.items():
            if entry.base_url is None:
                continue
            result = await probe_vision(entry.base_url, entry.model_id, client)
            results[key] = result
    return results


def build_providers(
    config: RouterConfig,
    *,
    env: Mapping[str, str] | None = None,
    enable_thinking: bool | None = None,
) -> dict[str, ModelProvider]:
    """Map endpoint key → live provider. Keyed by `entry.provider`, matching how
    the router resolves a provider (`self._providers[entry.provider]`)."""
    environ = os.environ if env is None else env
    providers: dict[str, ModelProvider] = {}
    if any(entry.provider == ROLE_FALLBACK_PROVIDER_KEY for entry in config.models.values()):
        raise ValueError(f"{ROLE_FALLBACK_PROVIDER_KEY!r} is reserved for role fallback")
    for entry in config.models.values():
        if entry.base_url is None or entry.provider in providers:
            continue
        # advisory capability union across all models on this endpoint
        caps = set().union(
            *(e.capabilities for e in config.models.values() if e.provider == entry.provider)
        )
        api_key = environ.get(entry.api_key_env) if entry.api_key_env else None
        if api_key is None and entry.api_key_env and entry.api_key_env.startswith("DISCO_"):
            # back-compat: honor a legacy PMX_<X> key var when DISCO_<X> is unset
            api_key = environ.get("PMX_" + entry.api_key_env[len("DISCO_") :])
        providers[entry.provider] = OpenAIProvider(
            entry.base_url,
            name=entry.provider,
            api_key=api_key,
            capabilities=caps,
            enable_thinking=enable_thinking,
        )
    fallback = config.role_fallback
    if fallback.enabled and fallback.base_url.strip():
        api_key_env = fallback.api_key_env.strip()
        api_key = environ.get(api_key_env) if api_key_env else None
        if api_key is None and api_key_env.startswith("DISCO_"):
            api_key = environ.get("PMX_" + api_key_env[len("DISCO_") :])
        providers[ROLE_FALLBACK_PROVIDER_KEY] = OpenAIProvider(
            fallback.base_url.strip(),
            name="role_fallback",
            api_key=api_key,
            capabilities=frozenset({Requirement.JSON_MODE}),
            enable_thinking=enable_thinking,
        )
    return providers

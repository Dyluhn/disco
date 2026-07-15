"""Build the live `ModelProvider` map from a `RouterConfig` (live-wiring).

One `OpenAIProvider` per distinct endpoint (`entry.provider`), pointed at an
operator-approved `base_url`, with the API key read from SecretStore by
`entry.api_key_env` secret-ref if the server needs one. Entries without a
`base_url` (the NLI cross-encoder, etc.) are skipped — they aren't chat
backends. Kept in its own module so importing `config` (widely imported)
doesn't pull `httpx`.

V2 (§2): async vision probes for providers that advertise modality metadata.
  - llama.cpp / self-host: GET {base_url − /v1}/props → modalities.vision bool.
  - OpenRouter: GET /api/v1/models → architecture.input_modalities includes "image".
  - Other: return None (defer to the static table in config.vision_table).
Results are memoized per (probe protocol, base_url, model_id) for the process lifetime.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable, Mapping
from typing import Literal
from urllib.parse import urlsplit

import httpx

from .config import ROLE_FALLBACK_PROVIDER_KEY, RouterConfig
from .openai_provider import OpenAIProvider
from .provider import ModelProvider
from .secret_refs import resolve_provider_secret, secret_ref_allowed_for_origin
from .secrets import SecretStore
from .types import Requirement

_LOG = logging.getLogger("disco.wiring")

# V2 (§2): process-lifetime memo cache for vision probe results.
# Key: (probe kind, base_url, model_id) → bool | None (True/False/unknown).
# Include the selected provider protocol so reclassifying an endpoint cannot reuse
# an `unknown` result cached while the same URL was configured as a generic cloud.
_VISION_PROBE_CACHE: dict[tuple[str, str, str], bool | None] = {}

_VisionProbeKind = Literal["llamacpp", "openrouter"]


def _is_self_hosted_endpoint(base_url: str) -> bool:
    """Return whether an endpoint is plausibly on the operator's local network.

    Generic OpenAI-compatible cloud APIs do not advertise vision capability, and
    probing an invented ``/props`` route causes noisy, unsupported requests.  Keep
    llama.cpp's runtime-authoritative probe to loopback, LAN, Tailscale/CGNAT, and
    explicitly local hostnames.  A public llama.cpp deployment can still opt in by
    naming its provider ``llamacpp`` (handled by `_vision_probe_kind`).
    """
    host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host.endswith((".ts.net", ".local", ".lan", ".home.arpa", ".internal")):
        return True
    if "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip in ipaddress.ip_network("100.64.0.0/10")
    )


def _vision_probe_kind(base_url: str, provider: str | None) -> _VisionProbeKind | None:
    """Select only provider protocols with a documented capability endpoint."""
    normalized_provider = (provider or "").strip().lower().replace("_", "-")
    host = (urlsplit(base_url).hostname or "").lower().rstrip(".")

    if (
        normalized_provider == "openrouter"
        or host == "openrouter.ai"
        or host.endswith(".openrouter.ai")
    ):
        return "openrouter"
    if normalized_provider in {"llama.cpp", "llama-cpp", "llamacpp"}:
        return "llamacpp"
    if _is_self_hosted_endpoint(base_url):
        return "llamacpp"
    return None


async def probe_vision(
    base_url: str,
    model_id: str,
    client: httpx.AsyncClient,
    *,
    provider: str | None = None,
) -> bool | None:
    """V2 (§2): probe one endpoint for vision capability.

    Provider detection:
    - OpenRouter provider/host → GET /api/v1/models, match model id, check
      input_modalities.
    - llama.cpp provider or local/LAN endpoint → GET {base_url − /v1}/props,
      read modalities.vision.
    - other cloud providers → no request; return None and use the static table.

    Returns:
      True   — definitively vision-capable.
      False  — definitively not vision-capable.
      None   — unknown (network error, missing field, model not found); caller falls
               back to the static table.

    Fail-soft: never raises. Result memoized for the process lifetime.
    """
    probe_kind = _vision_probe_kind(base_url, provider)
    if probe_kind is None:
        return None

    cache_key = (probe_kind, base_url, model_id)
    if cache_key in _VISION_PROBE_CACHE:
        return _VISION_PROBE_CACHE[cache_key]

    result: bool | None = None
    try:
        if probe_kind == "openrouter":
            # OpenRouter advertises per-model input_modalities in /api/v1/models.
            resp = await client.get(f"{base_url.rstrip('/')}/models", timeout=10.0)
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
    return await probe_all_vision_with_approvals(config, origin_approved=lambda *_: False)


async def probe_all_vision_with_approvals(
    config: RouterConfig,
    *,
    origin_approved: Callable[[str, str, str | None], bool],
) -> dict[str, bool | None]:
    results: dict[str, bool | None] = {}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        for key, entry in config.models.items():
            if entry.base_url is None:
                continue
            purpose = _model_purpose(entry.provider)
            if not origin_approved(entry.base_url, purpose, entry.api_key_env):
                results[key] = None
                continue
            result = await probe_vision(
                entry.base_url,
                entry.model_id,
                client,
                provider=entry.provider,
            )
            results[key] = result
    return results


def build_providers(
    config: RouterConfig,
    *,
    env: Mapping[str, str] | None = None,
    enable_thinking: bool | None = None,
    origin_approved: Callable[[str, str, str | None], bool] | None = None,
) -> dict[str, ModelProvider]:
    """Map endpoint key → live provider. Keyed by `entry.provider`, matching how
    the router resolves a provider (`self._providers[entry.provider]`)."""
    del env  # provider keys resolve from SecretStore by secret-ref id only.
    secret_store = SecretStore()
    approved = origin_approved or (lambda *_: False)
    providers: dict[str, ModelProvider] = {}
    if any(entry.provider == ROLE_FALLBACK_PROVIDER_KEY for entry in config.models.values()):
        raise ValueError(f"{ROLE_FALLBACK_PROVIDER_KEY!r} is reserved for role fallback")
    for entry in config.models.values():
        if entry.base_url is None or entry.provider in providers:
            continue
        purpose = _model_purpose(entry.provider)
        if not approved(entry.base_url, purpose, entry.api_key_env):
            _LOG.warning("provider %s origin is not operator-approved; skipping", entry.provider)
            continue
        if not secret_ref_allowed_for_origin(entry.api_key_env, entry.base_url):
            _LOG.warning(
                "provider %s secret_ref is not allowed for this origin; skipping",
                entry.provider,
            )
            continue
        # advisory capability union across all models on this endpoint
        caps = set().union(
            *(e.capabilities for e in config.models.values() if e.provider == entry.provider)
        )
        api_key = resolve_provider_secret(entry.api_key_env, secret_store)
        if entry.api_key_env and not api_key:
            _LOG.warning("provider %s secret_ref is not decryptable; skipping", entry.provider)
            continue
        providers[entry.provider] = OpenAIProvider(
            entry.base_url,
            name=entry.provider,
            api_key=api_key,
            capabilities=caps,
            enable_thinking=enable_thinking,
        )
    fallback = config.role_fallback
    if fallback.enabled and fallback.base_url.strip():
        if not approved(fallback.base_url.strip(), "role_fallback", fallback.api_key_env.strip()):
            _LOG.warning("role fallback origin is not operator-approved; skipping")
            return providers
        if not secret_ref_allowed_for_origin(
            fallback.api_key_env.strip(), fallback.base_url.strip()
        ):
            _LOG.warning("role fallback secret_ref is not allowed for this origin; skipping")
            return providers
        api_key = resolve_provider_secret(fallback.api_key_env.strip(), secret_store)
        if fallback.api_key_env.strip() and not api_key:
            _LOG.warning("role fallback secret_ref is not decryptable; skipping")
            return providers
        providers[ROLE_FALLBACK_PROVIDER_KEY] = OpenAIProvider(
            fallback.base_url.strip(),
            name="role_fallback",
            api_key=api_key,
            capabilities=frozenset({Requirement.JSON_MODE}),
            enable_thinking=enable_thinking,
        )
    return providers


def _model_purpose(provider: str) -> str:
    return f"model:{provider or 'unknown'}"

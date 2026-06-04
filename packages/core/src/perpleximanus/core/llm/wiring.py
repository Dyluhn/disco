"""Build the live `ModelProvider` map from a `RouterConfig` (live-wiring).

One `OpenAIProvider` per distinct endpoint (`entry.provider`), pointed at that
entry's `base_url`, with the API key read from `entry.api_key_env` (env var) if
the server needs one. Entries without a `base_url` (the NLI cross-encoder, etc.)
are skipped — they aren't chat backends. Kept in its own module so importing
`config` (widely imported) doesn't pull `httpx`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .config import RouterConfig
from .openai_provider import OpenAIProvider
from .provider import ModelProvider


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
    for entry in config.models.values():
        if entry.base_url is None or entry.provider in providers:
            continue
        # advisory capability union across all models on this endpoint
        caps = set().union(
            *(e.capabilities for e in config.models.values() if e.provider == entry.provider)
        )
        api_key = environ.get(entry.api_key_env) if entry.api_key_env else None
        providers[entry.provider] = OpenAIProvider(
            entry.base_url,
            name=entry.provider,
            api_key=api_key,
            capabilities=caps,
            enable_thinking=enable_thinking,
        )
    return providers

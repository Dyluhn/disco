"""Regression (live bug, 2026-07-06): when build_providers SKIPS a provider (its
origin isn't operator-approved / secret-ref not allowed / key undecryptable), the
router must resolve it into a TERMINAL, named ``NoEligibleModel`` — not a raw
``KeyError`` from ``self._providers[entry.provider]`` that escapes the caller and
crashes the run (deep research's uncaught-KeyError trap). ``_preflight_driver`` and
the loop already classify ``NoEligibleModel`` as a clean StatusEvent(ERROR)."""

from __future__ import annotations

import pytest
from disco.core.llm import DefaultLLMRouter, ModelEntry, RouterConfig
from disco.core.llm.errors import NoEligibleModel
from disco.core.llm.wiring import build_providers
from llm_fakes import simple_config


def test_provider_for_missing_raises_named_error_not_keyerror():
    # Empty providers dict == every configured model was skipped by build_providers.
    router = DefaultLLMRouter(simple_config(), {})
    ghost = ModelEntry(
        model_id="deepseek/x",
        provider="or-deepseek-x",
        context_window=8192,
        base_url="https://openrouter.ai/api/v1",
    )
    with pytest.raises(NoEligibleModel):
        router._provider_for(ghost)


def test_provider_for_present_returns_the_provider():
    router, _sink, providers = _router_with_providers()
    entry = ModelEntry(
        model_id="local.gguf",
        provider="ollama",
        context_window=8192,
        base_url="http://x/v1",
    )
    assert router._provider_for(entry) is providers["ollama"]


def test_keyless_provider_wires_even_with_an_unresolved_ownership_ref():
    entry = ModelEntry(
        model_id="minimax-m2.5",
        provider="ollama",
        context_window=196_608,
        base_url="http://host.docker.internal:11434/v1",
        api_key_env="provider_ollama",
        requires_api_key=False,
    )
    config = RouterConfig(models={"ollama-model": entry}, default_model="ollama-model")

    providers = build_providers(config, origin_approved=lambda *_args: True)

    assert "ollama" in providers


def _router_with_providers():
    from llm_fakes import build_router

    return build_router()

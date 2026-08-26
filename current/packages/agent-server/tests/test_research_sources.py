from __future__ import annotations

from types import SimpleNamespace

import pytest
from disco.agent_server.deep_research_provider import DeepResearchProvider
from disco.retrieval.live import ProviderConfigError
from disco.retrieval.models import SearchHit
from disco.retrieval.source_adapters import MultiSearchProvider


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def search(self, query: str, **kwargs: object) -> list[SearchHit]:
        del query, kwargs
        return []


def test_additional_sources_compose_without_rebuilding_other_dependencies() -> None:
    configured = _Provider("searxng")
    extraction = object()
    reranker = object()
    embedder = object()
    nli = object()
    providers = {
        "search": configured,
        "extraction": extraction,
        "reranker": reranker,
        "embedder": embedder,
        "nli": nli,
    }
    provider = DeepResearchProvider(
        object(), object(), object(), injected_providers=providers
    )

    baseline = provider.research()
    assert baseline is providers
    assert provider.research(search_override=None) is baseline

    composed = provider.research(search_override=_Provider("news"))
    assert isinstance(composed["search"], MultiSearchProvider)
    assert composed["search"]._providers[0] is configured
    assert composed["extraction"] is extraction
    assert composed["reranker"] is reranker
    assert composed["embedder"] is embedder
    assert composed["nli"] is nli


def test_legacy_baseline_source_is_ignored_but_unknown_source_is_rejected() -> None:
    provider = DeepResearchProvider(object(), object(), object(), injected_providers={})
    assert provider.search_override_for_sources(["ddgs", "searxng"]) is None
    with pytest.raises(ProviderConfigError, match="unsupported additional"):
        provider.search_override_for_sources(["unknown"])


def test_provider_cache_rebuilds_when_secret_value_rotates_under_same_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Secrets:
        values = {
            "TAVILY_API_KEY": "tavily-key-one",
            "FIRECRAWL_API_KEY": "firecrawl-key-one",
        }

        def get_secret(self, name: str, **_kwargs: object) -> str | None:
            return self.values.get(name)

    class _Approvals:
        def origin_approved(self, *_args: object, **_kwargs: object) -> bool:
            return True

        def approval_store(self, **_kwargs: object):
            return SimpleNamespace(verified=lambda: ("approval-revision",))

    cfg = SimpleNamespace(
        encoders=SimpleNamespace(
            remote=False,
            reranker_url="",
            embedder_url="",
            nli_url="",
        ),
        search=SimpleNamespace(
            provider="tavily",
            base_url="https://api.tavily.com",
            api_key_env="TAVILY_API_KEY",
        ),
        extraction=SimpleNamespace(
            provider="firecrawl",
            base_url="https://api.firecrawl.dev",
            api_key_env="FIRECRAWL_API_KEY",
        ),
    )
    config = SimpleNamespace(load=lambda: cfg, approvals=_Approvals())
    secrets = _Secrets()
    builds: list[dict[str, object]] = []

    def build_live_retrieval(**kwargs: object) -> dict[str, object]:
        builds.append(kwargs)
        return {
            "search": SimpleNamespace(key=kwargs["search_api_key"]),
            "extraction": SimpleNamespace(key=kwargs["extraction_api_key"]),
        }

    import disco.retrieval.live as live

    monkeypatch.setattr(live, "build_live_retrieval", build_live_retrieval)
    provider = DeepResearchProvider(config, secrets, object())

    first = provider.research()
    assert provider.research() is first
    assert len(builds) == 1

    secrets.values["TAVILY_API_KEY"] = "tavily-key-two"
    secrets.values["FIRECRAWL_API_KEY"] = "firecrawl-key-two"
    second = provider.research()

    assert second is not first
    assert len(builds) == 2
    assert second["search"].key == "tavily-key-two"
    assert second["extraction"].key == "firecrawl-key-two"


def test_additional_provider_approval_uses_its_own_origin_not_configured_searxng() -> None:
    calls: list[tuple[str, str, str | None]] = []

    class _Secrets:
        def get_secret(self, name: str, **_kwargs: object) -> str | None:
            return "semantic-key" if name == "SEMANTIC_SCHOLAR_API_KEY" else None

    class _Approvals:
        def origin_approved(
            self,
            url: str,
            purpose: str,
            secret_ref: str | None = None,
            **_kwargs: object,
        ) -> bool:
            calls.append((url, purpose, secret_ref))
            return True

    cfg = SimpleNamespace(
        search=SimpleNamespace(
            provider="searxng",
            base_url="http://search.internal:8888",
            api_key_env=None,
        )
    )
    provider = DeepResearchProvider(
        SimpleNamespace(load=lambda: cfg, approvals=_Approvals()),
        _Secrets(),
        object(),
    )

    assert provider.search_override_for_sources(["semantic_scholar"]) is not None
    assert (
        "https://api.semanticscholar.org",
        "search:semantic_scholar",
        "SEMANTIC_SCHOLAR_API_KEY",
    ) in calls
    assert not any(
        url == "http://search.internal:8888" and purpose == "search:semantic_scholar"
        for url, purpose, _ref in calls
    )

"""The bundled keyless search tier (L21): the composite, Wikipedia, and the
migration off the removed `ddgs` provider.

The three things this file pins down are the three ways the old tier hurt runs:

1. A leg that gets refused has to be REMEMBERED, even when its siblings carried
   the turn — otherwise every subsequent turn re-hammers the engine that already
   said no, which is how a five-minute cooldown became an hour-long one.
2. "Degraded" has to mean the whole tier, not one leg, or the hold/exhaustion
   machinery upstream gives up while four engines are still answering.
3. A config that still names `ddgs` has to LOAD — with one line saying what
   changed and how to choose otherwise. Not a crash (which discards the user's
   whole catalogue through ConfigStore's seed fallback) and not a silent swap.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from disco.core.llm.config import SearchSettings
from disco.retrieval._search_wiring import build_keyless_search, build_multi_search
from disco.retrieval._transport_retry import (
    OUTCOME_KEY,
    OUTCOME_RATE_LIMITED,
    classify_search_response,
    engines_cooling,
    search_with_degradation_retry,
)
from disco.retrieval._wikipedia_search import WikipediaSearchProvider
from disco.retrieval.models import SearchHit
from disco.retrieval.source_adapters import MultiSearchProvider

# ---- Wikipedia (verbatim live capture, 2026-09-02) --------------------------

WIKIPEDIA_BODY = {
    "batchcomplete": "",
    "query": {
        "searchinfo": {"totalhits": 82},
        "search": [
            {
                "ns": 0,
                "title": "RDNA 4",
                "pageid": 79348668,
                "snippet": (
                    '<span class="searchmatch">RDNA</span> '
                    '<span class="searchmatch">4</span> is a GPU '
                    '<span class="searchmatch">microarchitecture</span> designed by AMD'
                ),
                "timestamp": "2026-08-07T11:59:55Z",
            },
            {
                "ns": 0,
                "title": "RDNA (microarchitecture)",
                "pageid": 61016619,
                "snippet": "RDNA (Radeon DNA) is a graphics processing unit (GPU)",
                "timestamp": "2026-08-19T00:40:29Z",
            },
        ],
    },
}


def _wiki_transport(response: httpx.Response) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "action=query" in str(request.url)
        assert "list=search" in str(request.url)
        # Wikimedia's API etiquette: a descriptive agent, not a browser lie.
        assert request.headers["user-agent"].startswith("disco/")
        return response

    return httpx.MockTransport(handler)


async def test_wikipedia_maps_search_rows_to_hits_without_inventing_dates():
    provider = WikipediaSearchProvider(
        transport=_wiki_transport(httpx.Response(200, json=WIKIPEDIA_BODY))
    )
    hits, diagnostic = await provider.search_detailed("rdna 4", limit=5)

    assert [hit.url for hit in hits] == [
        "https://en.wikipedia.org/wiki/RDNA_4",
        "https://en.wikipedia.org/wiki/RDNA_(microarchitecture)",
    ]
    assert hits[0].title == "RDNA 4"
    assert "<span" not in hits[0].snippet and "RDNA 4 is a GPU" in hits[0].snippet
    # `timestamp` is the page's LAST EDIT. Treating it as a publication date
    # would make every article look days old to the recency filter.
    assert all(hit.published_at is None for hit in hits)
    assert diagnostic["result_count"] == 2


async def test_wikipedia_rate_limit_is_a_named_marker_not_an_empty_world():
    provider = WikipediaSearchProvider(
        transport=_wiki_transport(httpx.Response(429, text="too many requests"))
    )
    hits, diagnostic = await provider.search_detailed("q", limit=5)
    assert hits == []
    assert diagnostic["unresponsive_engines"] == ["wikipedia: rate limit (http 429)"]


async def test_wikipedia_api_level_error_is_named_too():
    body = {"error": {"code": "srsearch-error", "info": "bad params"}}
    provider = WikipediaSearchProvider(
        transport=_wiki_transport(httpx.Response(200, json=body))
    )
    hits, diagnostic = await provider.search_detailed("q", limit=5)
    assert hits == []
    assert "srsearch-error" in str(diagnostic.get("provider_error"))


# ---- the composite ----------------------------------------------------------


class _Leg:
    """A composite member with a scripted (hits, diagnostic) answer."""

    def __init__(self, name: str, hits: list[SearchHit], diagnostic: dict) -> None:
        self.name = name
        self._hits = hits
        self._diagnostic = diagnostic
        self.calls = 0

    async def search_detailed(self, query: str, **_kwargs):
        self.calls += 1
        return list(self._hits), dict(self._diagnostic)

    async def search(self, query: str, **kwargs):
        hits, _diagnostic = await self.search_detailed(query, **kwargs)
        return hits


def _hit(url: str, engine: str, rank: int = 0) -> SearchHit:
    return SearchHit(url=url, title=url, snippet="", source_engine=engine, rank=rank)


def _cooling(name: str) -> dict:
    return {"unresponsive_engines": [f"{name}: rate limit (http 429)"], "result_count": 0}


async def test_a_refused_leg_cools_even_when_the_composite_returned_hits():
    """The gap the old code left: `search_with_degradation_retry` only cools from
    an EMPTY response, and a five-leg composite is rarely empty — so the refused
    leg was re-hammered every turn while its siblings carried the result."""
    refused = _Leg("exa", [], _cooling("exa"))
    healthy = _Leg("parallel", [_hit("https://a.test/1", "parallel")], {"result_count": 1})
    composite = MultiSearchProvider((refused, healthy))

    hits, diagnostic = await composite.search_detailed("q", limit=5)

    assert [hit.url for hit in hits] == ["https://a.test/1"]
    assert "exa" in engines_cooling()
    # It is not degraded: one leg answered, so the query WAS tested.
    assert "composite_degraded" not in diagnostic
    assert classify_search_response(len(hits), diagnostic) is None


async def test_the_composite_is_degraded_only_when_every_leg_is_refused():
    legs = (_Leg("exa", [], _cooling("exa")), _Leg("parallel", [], _cooling("parallel")))
    composite = MultiSearchProvider(legs)

    hits, diagnostic = await composite.search_detailed("q", limit=5)

    assert hits == []
    assert diagnostic["composite_degraded"] is True
    assert diagnostic["members_total"] == 2
    assert set(diagnostic["members_cooling"]) == {"exa", "parallel"}

    degradation = classify_search_response(len(hits), diagnostic)
    assert degradation is not None and degradation.rate_limited


async def test_one_refused_leg_beside_an_honest_zero_is_not_a_degraded_tier():
    """A leg that genuinely found nothing is not a refusal, so the tier is not
    declared down — the alternative turns every empty query into a fake outage."""
    legs = (_Leg("exa", [], _cooling("exa")), _Leg("arxiv", [], {"result_count": 0}))
    composite = MultiSearchProvider(legs)

    hits, diagnostic = await composite.search_detailed("q", limit=5)
    assert hits == []
    assert "composite_degraded" not in diagnostic


async def test_a_leg_that_raises_is_named_rather_than_reported_as_bare_httperror():
    class _Broken:
        name = "parallel"

        async def search(self, query: str, **_kwargs):
            raise RuntimeError("boom")

    composite = MultiSearchProvider(
        (_Broken(), _Leg("exa", [_hit("https://a.test/1", "exa")], {}))
    )
    _hits, diagnostic = await composite.search_detailed("q", limit=5)
    assert "parallel" in str(diagnostic["providers"]["parallel"]["provider_error"])


async def test_the_composite_dedupes_by_url_and_keeps_every_engine_label():
    shared = "https://a.test/shared"
    parallel_rows = [_hit(shared, "parallel", 3), _hit("https://a.test/p", "parallel", 1)]
    legs = (
        _Leg("parallel", parallel_rows, {}),
        _Leg("exa", [_hit(shared, "exa", 0)], {}),
    )
    composite = MultiSearchProvider(legs)

    hits, _diagnostic = await composite.search_detailed("q", limit=10)

    assert [hit.url for hit in hits] == [shared, "https://a.test/p"]
    assert set(hits[0].source_engine.split("+")) == {"parallel", "exa"}
    assert [hit.rank for hit in hits] == [0, 1]


async def test_a_fully_refused_composite_is_not_retried_into():
    legs = (_Leg("exa", [], _cooling("exa")), _Leg("parallel", [], _cooling("parallel")))
    composite = MultiSearchProvider(legs)

    async def attempt():
        return await composite.search_detailed("q", limit=5)

    hits, record = await search_with_degradation_retry(attempt)
    assert hits == []
    assert record[OUTCOME_KEY] == OUTCOME_RATE_LIMITED
    assert legs[0].calls == 1 and legs[1].calls == 1


# ---- composition + the removed provider -------------------------------------


def test_the_bundled_default_is_the_five_keyless_legs_in_merge_order():
    provider = build_keyless_search()
    assert isinstance(provider, MultiSearchProvider)
    assert [leg.name for leg in provider._providers] == [
        "parallel",
        "exa",
        "wikipedia",
        "arxiv",
        "semantic_scholar",
    ]


def test_a_config_naming_the_removed_ddgs_provider_loads_with_one_radiant_line(caplog):
    """A wall with an angle: why it changed, what runs now, how to choose else."""
    with caplog.at_level(logging.WARNING, logger="disco.config"):
        settings = SearchSettings.model_validate(
            {"provider": "ddgs", "base_url": "http://192.168.1.202:8888"}
        )

    assert settings.provider == "bundled"
    assert settings.base_url == ""  # a searxng URL must not ride along

    lines = [r.getMessage() for r in caplog.records if "ddgs" in r.getMessage()]
    assert len(lines) == 1, lines
    message = lines[0]
    assert "removed" in message  # why
    assert "bundled" in message and "Parallel" in message  # what runs now
    assert "SearXNG" in message and "Settings" in message  # how to choose another
    assert "rate limit" in message  # the reason it could not do the work


def test_a_valid_provider_loads_without_the_migration_line(caplog):
    with caplog.at_level(logging.WARNING, logger="disco.config"):
        settings = SearchSettings.model_validate({"provider": "searxng", "base_url": "http://x"})
    assert settings.provider == "searxng" and settings.base_url == "http://x"
    assert not [r for r in caplog.records if "ddgs" in r.getMessage()]


@pytest.mark.parametrize(
    "provider",
    ["bundled", "searxng", "tavily", "brave", "exa", "parallel", "wikipedia", "arxiv",
     "news", "semantic_scholar", "site_scoped"],
)
def test_every_configurable_provider_round_trips_and_constructs(provider: str):
    """No false affordances: every id the settings surface offers must both
    survive a config round-trip and actually build a provider."""
    settings = SearchSettings(provider=provider)  # pyright: ignore[reportArgumentType]
    restored = SearchSettings.model_validate(json.loads(settings.model_dump_json()))
    assert restored.provider == provider

    from disco.retrieval._search_wiring import make_search

    base_url = "http://searx:8888" if provider == "searxng" else ""
    assert make_search(provider, base_url, "key") is not None


def test_searxng_categories_round_trip_and_default_to_general_science():
    settings = SearchSettings(provider="searxng", base_url="http://x", categories="general,it")
    restored = SearchSettings.model_validate(json.loads(settings.model_dump_json()))
    assert restored.categories == "general,it"

    provider = build_multi_search(
        ["searxng"], searxng_url="http://x", searxng_categories="general,it"
    )
    assert provider._build_params("q", None)["categories"] == "general,it"

    default = build_multi_search(["searxng"], searxng_url="http://x")
    assert default._build_params("q", None)["categories"] == "general,science"


# ---- origin posture ---------------------------------------------------------


def test_keyless_mcp_search_needs_no_operator_approval():
    """The bundled tier has to work on a fresh install. Keyless, these origins
    receive a query and no secret — the same posture the removed tier had — so
    demanding an approval click before the first search would be a wall with
    nothing behind it."""
    from disco.retrieval._provider_wiring import _resolve_search_wiring

    for provider in ("exa", "parallel", "bundled", "wikipedia", "arxiv"):
        assert _resolve_search_wiring(provider, "", "", "", {}, lambda *_a: False)[0] == provider


def test_attaching_a_key_makes_an_mcp_origin_need_approval_like_any_paid_tier():
    """A key changes what crosses the boundary, so it changes the posture."""
    from disco.retrieval._provider_wiring import ProviderConfigError, _resolve_search_wiring

    with pytest.raises(ProviderConfigError, match="approved trust origin"):
        _resolve_search_wiring("exa", "", "k", "exa", {}, lambda *_a: False)

    seen: list[tuple[str, str, str | None]] = []

    def approve(url: str, purpose: str, ref: str | None = "") -> bool:
        seen.append((url, purpose, ref))
        return True

    _resolve_search_wiring("parallel", "", "k", "parallel", {}, approve)
    assert seen == [("https://search.parallel.ai", "search:parallel", "parallel")]

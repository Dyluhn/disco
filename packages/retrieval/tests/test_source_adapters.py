from __future__ import annotations

import httpx
from disco.retrieval.models import SearchHit
from disco.retrieval.providers import SearchProvider
from disco.retrieval.source_adapters import (
    ArxivSearchProvider,
    MultiSearchProvider,
    SemanticScholarSearchProvider,
    SiteScopedSearchProvider,
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_arxiv_maps_atom_hits_and_satisfies_protocol():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>https://arxiv.org/abs/2401.00001</id>
        <title>  First
          Paper  </title>
        <summary> First abstract. </summary>
      </entry>
      <entry>
        <id>https://arxiv.org/abs/2401.00002</id>
        <title>Second Paper</title>
        <summary>Second abstract.</summary>
      </entry>
    </feed>"""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["search_query"] == "all:agent memory"
        assert req.url.params["max_results"] == "2"
        return httpx.Response(200, content=xml)

    provider = ArxivSearchProvider(transport=_transport(handler))
    assert isinstance(provider, SearchProvider)

    hits = await provider.search("agent memory", limit=2)

    assert [h.url for h in hits] == [
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/abs/2401.00002",
    ]
    assert hits[0].title == "First Paper"
    assert hits[0].snippet == "First abstract."
    assert hits[0].source_engine == "arxiv"
    assert hits[0].rank == 0


async def test_arxiv_follows_redirects_before_parsing_atom_hits():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>https://arxiv.org/abs/2401.00003</id>
        <title>Redirected Paper</title>
        <summary>Redirected abstract.</summary>
      </entry>
    </feed>"""
    seen_schemes: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_schemes.append(req.url.scheme)
        if req.url.scheme == "http":
            return httpx.Response(
                301,
                headers={"Location": str(req.url.copy_with(scheme="https"))},
            )
        assert req.url.scheme == "https"
        assert req.url.params["search_query"] == "all:redirect test"
        return httpx.Response(200, content=xml)

    provider = ArxivSearchProvider(
        base_url="http://export.arxiv.org/api/query",
        transport=_transport(handler),
    )

    hits = await provider.search("redirect test", limit=1)

    assert seen_schemes == ["http", "https"]
    assert [h.url for h in hits] == ["https://arxiv.org/abs/2401.00003"]
    assert hits[0].title == "Redirected Paper"
    assert hits[0].snippet == "Redirected abstract."


async def test_arxiv_malformed_xml_degrades_to_empty():
    provider = ArxivSearchProvider(
        transport=_transport(lambda req: httpx.Response(200, content=b"<feed>"))
    )

    assert await provider.search("bad") == []


async def test_semantic_scholar_maps_json_and_sends_api_key():
    seen_headers: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers.append(req.headers.get("x-api-key"))
        assert req.url.path == "/graph/v1/paper/search"
        assert req.url.params["query"] == "retrieval"
        assert req.url.params["fields"] == "title,abstract,url,year,externalIds"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "title": "Dense Retrieval",
                        "abstract": "An abstract.",
                        "url": "https://www.semanticscholar.org/paper/abc",
                        "paperId": "abc",
                    },
                    {
                        "title": "Fallback URL",
                        "abstract": None,
                        "paperId": "def",
                    },
                ]
            },
        )

    provider = SemanticScholarSearchProvider(api_key="sekret", transport=_transport(handler))
    assert isinstance(provider, SearchProvider)

    hits = await provider.search("retrieval", limit=2)

    assert seen_headers == ["sekret"]
    assert [h.url for h in hits] == [
        "https://www.semanticscholar.org/paper/abc",
        "https://www.semanticscholar.org/paper/def",
    ]
    assert hits[0].title == "Dense Retrieval"
    assert hits[0].snippet == "An abstract."
    assert hits[0].source_engine == "semantic_scholar"
    assert hits[1].snippet == ""


async def test_semantic_scholar_http_error_degrades_to_empty():
    provider = SemanticScholarSearchProvider(
        transport=_transport(lambda req: httpx.Response(500, text="nope"))
    )

    assert await provider.search("q") == []


class FakeMultiProvider:
    def __init__(self, name: str, hits: list[SearchHit] | None = None, *, raises: bool = False):
        self.name = name
        self._hits = hits or []
        self._raises = raises

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        del query, domains_allow, domains_deny, time_filter
        if self._raises:
            raise RuntimeError("provider down")
        return self._hits[:limit]


async def test_multi_search_fans_out_merges_dedupes_and_ignores_failures():
    provider = MultiSearchProvider(
        (
            FakeMultiProvider(
                "ddgs",
                [
                    SearchHit(
                        url="https://example.com/a",
                        title="A",
                        snippet="a",
                        source_engine="ddgs",
                        rank=0,
                    ),
                    SearchHit(
                        url="https://example.com/shared",
                        title="Shared weak",
                        snippet="weak",
                        source_engine="ddgs",
                        rank=5,
                    ),
                    SearchHit(
                        url="https://example.com/c",
                        title="C",
                        snippet="c",
                        source_engine="ddgs",
                        rank=2,
                    ),
                ],
            ),
            FakeMultiProvider(
                "arxiv",
                [
                    SearchHit(
                        url="https://example.com/shared",
                        title="Shared best",
                        snippet="best",
                        source_engine="arxiv",
                        rank=0,
                    ),
                    SearchHit(
                        url="https://example.com/b",
                        title="B",
                        snippet="b",
                        source_engine="arxiv",
                        rank=1,
                    ),
                ],
            ),
            FakeMultiProvider("empty", []),
            FakeMultiProvider("broken", raises=True),
        )
    )

    hits = await provider.search("q", limit=3)

    assert [h.url for h in hits] == [
        "https://example.com/a",
        "https://example.com/shared",
        "https://example.com/b",
    ]
    assert [h.rank for h in hits] == [0, 1, 2]
    shared = hits[1]
    assert shared.title == "Shared best"
    assert shared.source_engine == "arxiv+ddgs"


class FakeInnerSearchProvider:
    name = "fake"

    def __init__(self) -> None:
        self.query = ""
        self.domains_allow: frozenset[str] | None = None
        self.domains_deny: frozenset[str] | None = None
        self.time_filter: str | None = None

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        domains_allow: frozenset[str] | None = None,
        domains_deny: frozenset[str] | None = None,
        time_filter: str | None = None,
    ) -> list[SearchHit]:
        self.query = query
        self.domains_allow = domains_allow
        self.domains_deny = domains_deny
        self.time_filter = time_filter
        return [
            SearchHit(
                url="https://example.com/a",
                title="A",
                snippet="snip",
                source_engine="fake",
                rank=3,
            )
        ][:limit]


async def test_site_scoped_injects_site_operators_and_domains_allow():
    inner = FakeInnerSearchProvider()
    provider = SiteScopedSearchProvider(sites="example.com, docs.example.org", inner=inner)
    assert isinstance(provider, SearchProvider)

    hits = await provider.search(
        "vector stores",
        limit=1,
        domains_allow=frozenset({"already.test"}),
        domains_deny=frozenset({"deny.test"}),
        time_filter="week",
    )

    assert inner.query == "vector stores (site:example.com OR site:docs.example.org)"
    assert inner.domains_allow == frozenset(
        {"example.com", "docs.example.org", "already.test"}
    )
    assert inner.domains_deny == frozenset({"deny.test"})
    assert inner.time_filter == "week"
    assert hits == [
        SearchHit(
            url="https://example.com/a",
            title="A",
            snippet="snip",
            source_engine="site_scoped",
            rank=3,
        )
    ]


async def test_site_scoped_empty_sites_passthrough():
    inner = FakeInnerSearchProvider()
    provider = SiteScopedSearchProvider(sites="", inner=inner)

    hits = await provider.search("plain query", domains_allow=frozenset({"example.com"}))

    assert inner.query == "plain query"
    assert inner.domains_allow == frozenset({"example.com"})
    assert hits == [
        SearchHit(
            url="https://example.com/a",
            title="A",
            snippet="snip",
            source_engine="fake",
            rank=3,
        )
    ]

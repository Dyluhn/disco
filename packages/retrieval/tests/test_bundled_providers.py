"""Universal data providers (§B1/B2): the three tiers for search + extraction, and
the bundled (keyless) defaults. The bundled local extractor is tested offline; the
provider SELECTION is tested by type."""

from __future__ import annotations

import asyncio

from perpleximanus.retrieval.bundled_providers import (
    DdgsSearchProvider,
    FirecrawlExtractionProvider,
    LocalExtractionProvider,
    TavilySearchProvider,
    _MarkdownExtractor,
)
from perpleximanus.retrieval.live import _make_extraction, _make_search

# ---- provider selection (config → instance) ---------------------------------


def test_search_defaults_to_bundled_ddgs():
    assert isinstance(_make_search("ddgs", "", ""), DdgsSearchProvider)
    assert isinstance(_make_search("", "", ""), DdgsSearchProvider)  # unknown → bundled


def test_search_tiers():
    assert type(_make_search("searxng", "http://h:8888", "")).__name__ == "SearxngSearchProvider"
    assert isinstance(_make_search("tavily", "", "key"), TavilySearchProvider)


def test_extraction_defaults_to_bundled_local():
    assert isinstance(_make_extraction("local", "", ""), LocalExtractionProvider)
    assert isinstance(_make_extraction("", "", ""), LocalExtractionProvider)


def test_extraction_tiers():
    crawl = _make_extraction("crawl4ai", "http://h", "")
    assert type(crawl).__name__ == "Crawl4aiExtractionProvider"
    assert isinstance(_make_extraction("firecrawl", "", "key"), FirecrawlExtractionProvider)


# ---- bundled local extractor (stdlib HTML→markdown, offline) -----------------


def test_local_extractor_emits_markdown_and_drops_noise():
    html = (
        "<html><head><title>My Page</title><style>a{}</style></head>"
        "<body><nav>menu</nav><h1>Title</h1>"
        '<p>A <a href="https://x.test">link</a>.</p>'
        "<ul><li>one</li><li>two</li></ul>"
        "<script>evil()</script></body></html>"
    )
    p = _MarkdownExtractor()
    p.feed(html)
    md = p.markdown()
    assert p.title.strip() == "My Page"
    assert "# Title" in md
    assert "[link](https://x.test)" in md
    assert "- one" in md and "- two" in md
    assert "evil()" not in md and "a{}" not in md  # script + style dropped


def test_ddgs_degrades_to_empty_on_failure(monkeypatch):
    """A rate-limit / network error returns [] — it never crashes the run."""
    prov = DdgsSearchProvider()
    monkeypatch.setattr(prov, "_blocking_search", lambda q, n: [])
    assert asyncio.run(prov.search("anything", limit=3)) == []


def test_paid_providers_no_key_is_safe():
    # no key → empty/failed, never a crash or a leaked request
    assert asyncio.run(TavilySearchProvider("").search("x")) == []
    doc = asyncio.run(FirecrawlExtractionProvider("").extract("https://x.test"))
    assert not doc.fetched_ok and doc.status == "error"


def test_local_extractor_populates_citable_passages():
    """The research pipeline cites PASSAGES, not raw content — an extractor that
    returns content but no passages makes a run report 'couldn't read any'. So the
    local extractor must chunk (regression for the in-app extraction failure)."""
    from perpleximanus.retrieval.bundled_providers import chunk_passages

    para = "Paragraph with plenty of words so it counts as a real passage of content here."
    body = "\n\n".join(f"{i}. {para}" for i in range(60))  # ~5k chars → several passages
    ps = chunk_passages("https://x.test", "Title", body)
    assert len(ps) >= 3
    assert all(p.source_url == "https://x.test" and p.text for p in ps)
    assert len({p.id for p in ps}) == len(ps)  # stable, unique ids


def test_local_extractor_drops_empty_anchor_nav():
    """Empty-anchor links (`[](url)` nav menus) are dropped — passages get real
    content, not menu noise."""
    html = (
        '<html><body><ul><li><a href="/a"></a></li>'
        '<li><a href="/b">Real text</a></li></ul></body></html>'
    )
    p = _MarkdownExtractor()
    p.feed(html)
    md = p.markdown()
    assert "[](/a)" not in md  # empty anchor gone
    assert "[Real text](/b)" in md  # anchor with text kept

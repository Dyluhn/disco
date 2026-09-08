"""Real observed discovery/extraction boundaries, with transport under test control."""

import httpx
import pytest
from disco.retrieval._crawl4ai import Crawl4aiExtractionProvider
from disco.retrieval.live import SearxngSearchProvider
from disco.retrieval.url_policy import query_domain_scopes


@pytest.mark.parametrize(
    "success, final_status, expected",
    [
        (True, 200, "ok"),
        (False, 200, "error"),
        (True, 404, "not_found"),
        (True, None, "error"),
        (True, 301, "error"),
    ],
)
def test_redirected_crawl_uses_final_response_without_accepting_unfinished_redirect(
    success, final_status, expected
):
    original = "https://example.com/old"
    provider = Crawl4aiExtractionProvider("http://crawler")
    doc = provider._to_doc(
        original,
        {
            "url": original,
            "redirected_url": "https://example.com/new",
            "status_code": 301,
            "redirected_status_code": final_status,
            "success": success,
            "markdown": {
                "raw_markdown": "A protocol specification with meaningful retained content."
            },
        },
    )
    assert doc.status == expected
    assert doc.fetched_ok == (expected == "ok")
    assert doc.url == original
    if doc.fetched_ok:
        assert doc.content == doc.passages[0].text
        assert doc.passages[0].source_url == original


async def test_search_enforces_site_scope_before_limit_and_keeps_host_policy():
    urls = [
        "https://unrelated.test/a",
        "https://docs.example.com.evil.test/a",
        "https://docs.example.com/Reference/Headers/Cache-Control",
        "https://other.example.com/a",
    ]

    def reply(request):
        return httpx.Response(
            200, json={"results": [{"url": u, "title": u, "content": "response"} for u in urls]}
        )

    search = SearxngSearchProvider("http://search", transport=httpx.MockTransport(reply))
    hits, diagnostic = await search.search_detailed(
        "site:docs.example.com/Headers/Cache-Control", limit=1
    )
    assert [h.url for h in hits] == [urls[2]]
    assert diagnostic["query_scope_dropped"] == 3
    hits, _ = await search.search_detailed(
        "site:docs.example.com", domains_deny=frozenset({"example.com"})
    )
    assert hits == []


@pytest.mark.parametrize(
    "query, allow, deny",
    [
        ("ordinary research", None, frozenset()),
        (
            'site:"example.com" -site:"bad.example.com"',
            frozenset({"example.com"}),
            frozenset({"bad.example.com"}),
        ),
        ('explain "site:example.com" syntax', None, frozenset()),
        (
            "cache (site:example.com OR site:docs.example.org) -site:bad.example.com",
            frozenset({"example.com", "docs.example.org"}),
            frozenset({"bad.example.com"}),
        ),
        ("SITE:https://example.com/path", frozenset({"example.com"}), frozenset()),
    ],
)
def test_explicit_site_operators_are_host_scopes(query, allow, deny):
    assert query_domain_scopes(query) == (allow, deny)

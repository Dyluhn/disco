"""arXiv search semantics: unrelated common-word OR matches must not leak in."""

import httpx
import pytest
from disco.retrieval.source_adapters import ArxivSearchProvider


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("quantum entanglement", 'all:"quantum" AND all:"entanglement"'),
        ('"Attention Is All You Need"', 'all:"Attention Is All You Need"'),
        ('transformer "positional encoding"', 'all:"transformer" AND all:"positional encoding"'),
        ("  naïve\nBayes?  ", 'all:"naïve" AND all:"Bayes"'),
        ("O'Connor (C++)", 'all:"O\'Connor" AND all:"C++"'),
        ('"say \\"hello\\""', 'all:"say \\"hello\\""'),
        (r'"C:\\papers" learning', r'all:"C:\\papers" AND all:"learning"'),
        ("AND OR", 'all:"AND" AND all:"OR"'),
        ('ti:"quantum criticality" AND au:Smith', 'ti:"quantum criticality" AND au:Smith'),
        (
            '(ti:"graph networks" OR abs:transformer) ANDNOT au:Smith',
            '(ti:"graph networks" OR abs:transformer) ANDNOT au:Smith',
        ),
        (r'ti:"say \"hello\"" AND au:Smith', r'ti:"say \"hello\"" AND au:Smith'),
        (
            "cat:cs.CL AND submittedDate:[20170101 TO 20171231]",
            "cat:cs.CL AND submittedDate:[20170101 TO 20171231]",
        ),
    ],
)
async def test_plain_terms_conjoin_and_native_fielded_syntax_survives(query, expected):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["search_query"] == expected
        assert request.url.params["sortBy"] == "relevance"
        assert request.url.params["sortOrder"] == "descending"
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')

    provider = ArxivSearchProvider(transport=httpx.MockTransport(handler))
    assert await provider.search(query) == []
    assert len(requests) == 1


@pytest.mark.parametrize(
    "query",
    [
        "",
        "  ",
        "?!",
        '"unfinished',
        'ti:"unfinished',
        "ti:quantum AND (au:Smith",
        "ti:quantum)",
        "quantum\\",
    ],
)
async def test_empty_or_incomplete_query_never_broadens_to_an_unconstrained_request(query):
    def forbidden(_request):
        pytest.fail("Malformed/empty query must not send a broad arXiv request")

    provider = ArxivSearchProvider(transport=httpx.MockTransport(forbidden))
    assert await provider.search(query) == []


async def test_quoted_phrase_remains_one_field_and_preserves_hits_filters_and_recency():
    import datetime

    recent = datetime.date.today().isoformat()
    xml = f"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>https://arxiv.org/abs/2609.10001</id><title>Graph Networks</title>
      <summary>Graph networks and a separate search result.</summary>
      <published>{recent}T00:00:00Z</published></entry>
      <entry><id>https://arxiv.org/abs/1701.00001</id><title>Old Graph Networks</title>
      <published>2017-01-01T00:00:00Z</published></entry>
    </feed>"""

    def handler(request):
        assert request.url.params["search_query"] == 'all:"graph networks" AND all:"learning"'
        assert request.url.params["max_results"] == "8"
        return httpx.Response(200, text=xml)

    provider = ArxivSearchProvider(transport=httpx.MockTransport(handler))
    hits = await provider.search('"graph networks" learning', limit=2, time_filter="week")
    assert [(hit.title, hit.source_engine) for hit in hits] == [("Graph Networks", "arxiv")]
    assert (
        await provider.search(
            '"graph networks" learning',
            limit=2,
            time_filter="week",
            domains_deny=frozenset({"arxiv.org"}),
        )
        == []
    )


async def test_full_question_keeps_scope_terms_without_implicit_or_or_invented_keywords():
    question = "What did the 2017 transformer paper introduce?"

    def handler(request):
        expression = request.url.params["search_query"]
        assert (
            expression == 'all:"What" AND all:"did" AND all:"the" AND all:"2017" '
            'AND all:"transformer" AND all:"paper" AND all:"introduce"'
        )
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')

    provider = ArxivSearchProvider(transport=httpx.MockTransport(handler))
    assert await provider.search(question) == []

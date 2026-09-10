"""Following a known original source must not depend on search ranking."""

import json

import pytest
from disco.retrieval import DefaultRetrievalEngine, LexicalReranker, RetrievalRequest
from disco.retrieval.bundled_providers import LocalExtractionProvider
from disco.retrieval.deep_research._search_outcomes import partition_fresh_queries
from disco.retrieval.deep_research._turn_accounting import turn_accounting_rollup
from research_fakes import FakeExtractionProvider, FakeSearchProvider, hit
from test_research_agent import _bound, _run, _TurnRouter

PRIMARY = "https://original.example/reports/2026/measurement"
SECONDARY = "https://summary.example/overview"
BODY = "Measured output is 12 units, under the documented laboratory conditions only."


def engine(*, failing=None):
    search = FakeSearchProvider([hit(SECONDARY)])
    extraction = FakeExtractionProvider(
        {PRIMARY: BODY, SECONDARY: "A secondary summary omits the laboratory restriction."},
        failing=failing,
    )
    return search, DefaultRetrievalEngine(search, extraction, LexicalReranker())


async def test_known_source_is_read_without_search_and_keeps_exact_evidence():
    search, retrieval = engine()
    result = await retrieval.retrieve(RetrievalRequest(query=PRIMARY, distinct_sources=True))
    assert search.queries == []
    assert result.issued_queries == []
    assert [(p.source_url, p.text) for p in result.passages] == [(PRIMARY, BODY)]
    assert result.extracted[0].content == BODY
    trace = result.notes["retrieval_trace"]
    assert trace["acquisition"] == "direct_url"
    assert trace["raw_discovered_hit_count"] == 0 and trace["queries"] == []
    assert trace["direct_sources"][0]["url"] == PRIMARY
    assert result.all_hits[0].status == "ok"


async def test_failed_original_is_not_replaced_by_a_search_summary():
    search, retrieval = engine(failing={PRIMARY: "paywalled"})
    result = await retrieval.retrieve(RetrievalRequest(query=PRIMARY))
    assert search.queries == [] and result.passages == []
    assert result.all_hits[0].url == PRIMARY and result.all_hits[0].status == "paywalled"
    assert result.extracted[0].fetched_ok is False


@pytest.mark.parametrize(
    "retrieval_request",
    [
        RetrievalRequest(query=PRIMARY, domains_deny=frozenset({"original.example"})),
        RetrievalRequest(query=PRIMARY, domains_allow=frozenset({"other.example"})),
        RetrievalRequest(query=PRIMARY, use_web=False),
    ],
)
async def test_direct_read_preserves_web_and_domain_policies(retrieval_request):
    search, retrieval = engine()
    result = await retrieval.retrieve(retrieval_request)
    assert search.queries == [] and result.extracted == [] and result.passages == []


async def test_local_address_is_still_rejected_by_the_real_extraction_guard():
    search = FakeSearchProvider([])
    retrieval = DefaultRetrievalEngine(search, LocalExtractionProvider(), LexicalReranker())
    result = await retrieval.retrieve(RetrievalRequest(query="http://127.0.0.1/private"))
    assert search.queries == [] and result.passages == []
    assert len(result.extracted) == 1 and not result.extracted[0].fetched_ok
    assert result.extracted[0].error


async def test_url_inside_search_terms_remains_an_ordinary_search():
    search, retrieval = engine()
    query = "independent replication of " + PRIMARY
    result = await retrieval.retrieve(RetrievalRequest(query=query))
    assert search.queries == [query] and result.issued_queries == [query]
    assert result.passages[0].source_url == SECONDARY


def test_distinct_source_paths_are_not_near_duplicate_searches():
    other = PRIMARY + "-limitations"
    case_sensitive = PRIMARY.replace("measurement", "Measurement")
    trail = [{"kind": "search", "query": PRIMARY, "admitted": 1, "turn": 0}]
    fresh, refused, _ = partition_fresh_queries(
        trail, [other, case_sensitive, PRIMARY + "?utm_source=ref#results"]
    )
    assert fresh == [other, case_sensitive]
    assert len(refused) == 1 and refused[0].earlier.query == PRIMARY


async def test_research_can_follow_a_primary_link_and_resume_with_its_full_evidence():
    search, retrieval = engine()
    turn = json.dumps(
        {
            "brief": "Verify the measurement and its applicability in the original report.",
            "decision_summary": "Read the linked original directly.",
            "coverage": {
                "covered": [],
                "open": ["measurement limitations"],
                "contradictions_checked": [],
            },
            "queries": [PRIMARY],
            "ready_to_write": False,
        }
    )
    outcome, _ = await _run(_TurnRouter([turn]), retrieval, bound=_bound(max_research_turns=1))
    assert search.queries == [] and len(outcome.passages) == 1
    assert outcome.passages[0].source_url == PRIMARY and outcome.passages[0].text == BODY
    assert turn_accounting_rollup(outcome.trail, total_turns=1)["model_turns"] == 1
    repeated, _ = await _run(
        _TurnRouter([turn]),
        retrieval,
        bound=_bound(max_research_turns=1),
        resume_passages=outcome.passages,
        resume_trail=outcome.trail,
    )
    assert search.queries == []
    assert [(p.id, p.text) for p in repeated.passages] == [(p.id, p.text) for p in outcome.passages]
    assert any(row.get("kind") == "query_rejected" for row in repeated.trail)

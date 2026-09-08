"""Discovery is useful work, but only selected source reads become evidence."""

from disco.retrieval import DefaultRetrievalEngine, LexicalReranker
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._discovery_context import discovery_context
from disco.retrieval.deep_research._recovery_state import AgentCheckpoint
from disco.retrieval.deep_research._search_outcomes import zero_admission_feedback
from disco.retrieval.deep_research._search_turn import execute_search_turn
from disco.retrieval.deep_research._subquestion_ledger import build_ledger
from disco.retrieval.deep_research._turn_context import _upstream_zero_yield_warning
from research_fakes import FakeExtractionProvider, FakeSearchProvider, hit
from test_research_agent import _bound


async def issue(state, retrieval, query, turn=0):
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    result = await execute_search_turn(
        state,
        [query],
        turn,
        retrieval_engine=retrieval,
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(turn + 1, 8),
    )
    return result, events


async def test_discovery_survives_resume_and_only_the_selected_page_spends_a_source_slot():
    original = "https://original.example/study"
    overview = "https://overview.example/summary"
    search = FakeSearchProvider([hit(overview), hit(original, rank=1)])

    class Extraction(FakeExtractionProvider):
        calls = []

        async def extract_many(self, urls):
            self.calls.append(list(urls))
            return await super().extract_many(urls)

    extraction = Extraction({original: "The study measured twelve units in a laboratory."})
    retrieval = DefaultRetrievalEngine(search, extraction, LexicalReranker())
    state = _AgentState(budget=SourceBudget(3))
    discovered, events = await issue(state, retrieval, "original measurement study")
    assert discovered.charge.counted
    assert not state.pool and state.budget.used == 0 and not extraction.calls
    assert discovered.outcomes[0].outcome == "discovered"
    assert discovered.outcomes[0].new_discovered == 2
    assert events[-1][1]["added"] == 0 and events[-1][1]["new_discovered"] == 2
    assert not state.reissue.queries
    saved = AgentCheckpoint.model_validate_json(AgentCheckpoint.capture(state).model_dump_json())
    restored = saved.restore(_bound(max_sources=3))
    read, _ = await issue(restored, retrieval, original, 1)
    assert read.charge.counted and read.admitted == 1
    assert restored.budget.used == 1
    assert extraction.calls == [[original]]
    assert search.queries == ["original measurement study"]
    assert [p.source_url for p in restored.pool] == [original]


def test_three_discoveries_do_not_close_an_angle_or_order_a_pivot():
    trail = []
    for turn in range(3):
        trail.extend(
            [
                {
                    "kind": "search",
                    "turn": turn,
                    "query": f"measurement study institution{turn}",
                    "admitted": 0,
                    "yield_reason": "discovered",
                    "result": "discovery",
                },
                {
                    "kind": "search_turn_summary",
                    "turn": turn,
                    "queries": 1,
                    "new_admitted": 0,
                    "new_discovered": 2,
                    "yield_reasons": ["discovered"],
                },
            ]
        )
    coverage = {"covered": [], "open": ["measurement study"], "contradictions_checked": []}
    ledger, _ = build_ledger(coverage, trail, frozenset())
    assert ledger[0].state == "open" and not ledger[0].exhausted
    state = _AgentState(budget=SourceBudget(10), trail=trail, coverage=coverage)
    assert _upstream_zero_yield_warning(state) == ""
    feedback = zero_admission_feedback([("measurement study", "discovered")], [])
    assert "complete URLs" in feedback
    assert "Pivot by changing" not in feedback and "infrastructure failed" not in feedback


async def test_repeated_leads_do_not_claim_new_discovery_progress():
    retrieval = DefaultRetrievalEngine(
        FakeSearchProvider([hit("https://original.example/study")]),
        FakeExtractionProvider({}),
        LexicalReranker(),
    )
    state = _AgentState(budget=SourceBudget(3))
    first, _ = await issue(state, retrieval, "measurement study")
    second, events = await issue(state, retrieval, "independent experiment", 1)
    assert first.outcomes[0].new_discovered == 1
    assert second.outcomes[0].new_discovered == 0
    assert events[-1][1]["new_discovered"] == 0
    assert not state.pool and state.budget.used == 0


def test_bounded_discovery_preserves_provider_rank_before_recency():
    state = _AgentState(
        budget=SourceBudget(3),
        all_hits=[
            hit("https://original.example/rank1", rank=1),
            hit("https://overview.example/rank8", rank=8),
        ],
    )
    context = discovery_context(state)
    assert context.index("https://original.example/rank1") < context.index(
        "https://overview.example/rank8"
    )


async def test_unread_leads_at_the_work_limit_are_not_reported_as_absence_of_an_answer():
    import pytest
    from disco.retrieval.deep_research.agent import ResearchAgentError
    from test_research_agent import _run, _TurnRouter
    from test_unread_discovery_context import turn

    search = FakeSearchProvider([hit("https://original.example/study")])
    retrieval = DefaultRetrievalEngine(search, FakeExtractionProvider({}), LexicalReranker())
    with pytest.raises(ResearchAgentError) as caught:
        await _run(
            _TurnRouter([turn("measurement study")]), retrieval, bound=_bound(max_research_turns=1)
        )
    message = str(caught.value)
    assert "search found source leads" in message
    assert "work limit" in message
    assert "it had nothing usable" not in message
    assert "source URL" in message


async def test_requested_url_is_not_counted_as_a_discovered_search_result():
    url = "https://original.example/known-study"
    search = FakeSearchProvider([])
    retrieval = DefaultRetrievalEngine(
        search,
        FakeExtractionProvider(
            {url: "The original study directly measured twelve units in the laboratory."}
        ),
        LexicalReranker(),
    )
    result, events = await issue(_AgentState(budget=SourceBudget(3)), retrieval, url)
    assert result.admitted == 1 and result.outcomes[0].new_discovered == 0
    assert events[-1][1]["new_discovered"] == 0
    assert search.queries == []

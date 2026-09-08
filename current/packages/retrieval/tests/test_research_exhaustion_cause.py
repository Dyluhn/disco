"""A run that ends empty must name the layer that emptied it.

The live defect: crawl4ai's browser driver wedged, 59 of 60 extractions came
back ``upstream_http_500``, the run correctly ERRORED with an empty pool — and
the terminal error said ``research exhausted its turn budget without usable
evidence``. Every word of that is true and none of it is the finding. The
operator had to reconstruct the outage from the trace before they could act.

These pin the repaired contract: the same exception type and the same funnel
outcome, with the failing provider, the failure share, and the dominant error
class in the message; the plain message preserved when no provider dominates;
and the extraction outage folded into the untested/re-issuable query class
beside the search one, with feedback that names it a host fault instead of
sending the model to pivot around a broken fetcher.

The doubles are the ones `test_research_agent.py` already scripts the loop
with, so an outage fixture differs from a healthy run in exactly one way.
"""

from __future__ import annotations

from typing import Any

import pytest
from disco.retrieval.deep_research import _exhaustion
from disco.retrieval.deep_research import agent as agent_mod
from disco.retrieval.deep_research._exhaustion import (
    ExtractionOutcomes,
    starved_provider_clause,
)
from disco.retrieval.deep_research._search_outcomes import (
    EXTRACTION_FAILURE,
    semantically_tested_queries,
    zero_admission_feedback,
    zero_yield_detail,
)
from disco.retrieval.deep_research.agent import ResearchAgentError
from disco.retrieval.models import ExtractedDoc, RetrievalRequest, RetrievalResult, SearchHit
from test_research_agent import (
    _TURN0,
    _bound,
    _DegradedRetrieval,
    _FakeRetrieval,
    _run,
    _TurnRouter,
)

# ---- doubles ----------------------------------------------------------------


def _failed_doc(index: int, *, error: str, status: str = "error") -> ExtractedDoc:
    return ExtractedDoc(
        url=f"https://example.com/article/{index}",
        title=f"Source {index}",
        content="",
        fetched_ok=False,
        error=error,
        status=status,  # pyright: ignore[reportArgumentType]
    )


class _WedgedExtractor:
    """The live shape: search works, every fetch of what it finds fails.

    Real hits are discovered (so the query WAS put to the world), and not one
    of them yields a passage — the wedged browser driver returns HTTP 500 for
    every URL.
    """

    def __init__(self, *, per_turn: int = 5, error: str = "http status 500 from upstream") -> None:
        self._per_turn = per_turn
        self._error = error
        self.calls = 0

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        base = self.calls * self._per_turn
        self.calls += 1
        docs = [
            _failed_doc(base + index, error=self._error) for index in range(self._per_turn)
        ]
        return RetrievalResult(
            passages=[],
            all_hits=[
                SearchHit(url=doc.url, title=doc.title, snippet="s", rank=index)
                for index, doc in enumerate(docs)
            ],
            extracted=docs,
            issued_queries=[req.query],
        )


class _BarrenSearch:
    """A clean zero: the provider answered, named no outage, and had nothing.

    This is a RESEARCH result. It must never acquire a provider-failure clause.
    """

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        return RetrievalResult(
            passages=[], all_hits=[], extracted=[], issued_queries=[req.query]
        )


async def _exhaust(retrieval: Any, *, turns: int = 3) -> str:
    """Drive the loop to an empty-pool turn-budget wall; return the message."""
    router = _TurnRouter([_TURN0] * (turns + 1))
    with pytest.raises(ResearchAgentError) as raised:
        await _run(router, retrieval, bound=_bound(max_research_turns=turns))
    return str(raised.value)


# ---- the error names the provider -------------------------------------------


async def test_an_extraction_outage_names_the_extraction_provider() -> None:
    """The reproduction. Same exception, same funnel — a message that acts."""
    message = await _exhaust(_WedgedExtractor())

    assert message.startswith("research exhausted its turn budget without usable evidence")
    assert "extraction provider failing" in message
    assert "attempts failed (upstream_http_500)" in message


async def test_the_named_share_is_the_real_count_of_attempts() -> None:
    """The numbers are measured, never rounded into a slogan."""
    message = await _exhaust(_WedgedExtractor(per_turn=4), turns=3)

    # Two fresh queries per turn over three turns, four URLs each.
    assert "24/24 attempts failed" in message


async def test_a_search_outage_names_the_search_provider_instead() -> None:
    """Upstream first: when discovery is what is down, the extractor is not
    the service to go look at."""
    message = await _exhaust(_DegradedRetrieval())

    assert message.startswith("research exhausted its turn budget without usable evidence")
    assert "search provider failing" in message
    assert "queries degraded" in message
    assert "yahoo" in message  # the engines the provider named as unresponsive
    assert "extraction provider" not in message


async def test_a_world_that_really_had_nothing_keeps_the_plain_message() -> None:
    """Blaming infrastructure for a genuine research result is the same defect
    pointed the other way."""
    message = await _exhaust(_BarrenSearch())

    assert message.startswith("research exhausted its turn budget without usable evidence.")
    assert "provider failing" not in message
    # …and it still says what to do about a run that really found nothing.
    assert "it had nothing usable" in message and "not an outage" in message


async def test_a_healthy_run_never_reaches_the_clause_at_all() -> None:
    """The guard: a run with evidence bounds honestly and does not raise."""
    router = _TurnRouter([_TURN0, _TURN0])
    outcome, _ = await _run(router, _FakeRetrieval(), bound=_bound(max_research_turns=1))

    assert outcome.bounded_by == "turns" and outcome.passages


# ---- the thresholds ---------------------------------------------------------


def test_too_few_attempts_is_a_bad_run_not_an_outage() -> None:
    """Nine failures out of nine URLs is a hard day, not a provider outage."""
    outcomes = ExtractionOutcomes()
    outcomes.record(
        [_failed_doc(index, error="http status 500 from upstream") for index in range(9)]
    )

    assert outcomes.attempted < _exhaustion.EXTRACTION_MIN_ATTEMPTS
    assert not outcomes.dominant
    assert starved_provider_clause(outcomes, []) == ""


def test_a_minority_of_failures_is_not_an_outage() -> None:
    ok = ExtractedDoc(url="https://example.com/a", title="A", content="text")
    outcomes = ExtractionOutcomes()
    outcomes.record([ok] * 10)
    outcomes.record([_failed_doc(index, error="timed out") for index in range(5)])

    assert outcomes.attempted == 15 and outcomes.failed == 5
    assert not outcomes.dominant


def test_the_dominant_error_class_wins_and_ties_break_by_name() -> None:
    """Deterministic: the same run must always print the same class."""
    outcomes = ExtractionOutcomes()
    outcomes.record([_failed_doc(index, error="timed out") for index in range(6)])
    outcomes.record(
        [_failed_doc(20 + index, error="captcha wall") for index in range(6)]
    )

    assert outcomes.failures == {"timeout": 6, "anti_bot": 6}
    assert outcomes.top_error_class == "timeout"  # tie broken by name, not order
    assert outcomes.dominant


def test_a_clean_extraction_run_records_attempts_and_no_failures() -> None:
    outcomes = ExtractionOutcomes()
    outcomes.record(
        [ExtractedDoc(url=f"https://example.com/{i}", title="T", content="c") for i in range(12)]
    )

    assert outcomes.attempted == 12 and outcomes.failed == 0 and not outcomes.dominant


# ---- an extraction outage is an untested query, not a dead end --------------


def test_queries_that_died_in_extraction_were_never_semantically_tested() -> None:
    """Same rule as a degraded search, one layer down: the pool did not move
    because nothing could be READ, which says nothing about the query."""
    trail = [
        {"kind": "search", "query": "tested query", "result": "empty"},
        {"kind": "search", "query": "unreadable", "yield_reason": EXTRACTION_FAILURE},
    ]

    assert semantically_tested_queries(trail) == {"tested query"}


def test_a_query_that_died_in_extraction_may_be_re_issued_verbatim() -> None:
    """The freshness wall must not forbid the only correct response to an outage."""
    state = agent_mod._AgentState(budget=agent_mod.SourceBudget(10))
    state.trail.append(
        {"kind": "search", "turn": 0, "query": "X overview", "yield_reason": EXTRACTION_FAILURE}
    )

    fresh, refused, _narrowed = agent_mod._fresh_queries(state, ("X overview",))

    assert fresh == ["X overview"] and refused == []


def test_a_query_tested_once_and_unreadable_once_stays_blocked() -> None:
    """The unchanged half of the rule: a real result about a query is a result."""
    state = agent_mod._AgentState(budget=agent_mod.SourceBudget(10))
    state.trail.extend(
        [
            {
                "kind": "search",
                "turn": 0,
                "query": "X overview",
                "yield_reason": EXTRACTION_FAILURE,
            },
            {
                "kind": "search",
                "turn": 1,
                "query": "X overview",
                "admitted": 2,
                "result": "evidence",
            },
        ]
    )

    fresh, refused, _narrowed = agent_mod._fresh_queries(state, ("X overview",))

    assert fresh == [] and [refusal.query for refusal in refused] == ["X overview"]


# ---- the feedback names the fault -------------------------------------------


def test_the_extraction_outage_feedback_names_it_a_host_fault() -> None:
    feedback = zero_admission_feedback([("X overview", EXTRACTION_FAILURE)], [])

    assert "EXTRACTION INFRASTRUCTURE FAILING" in feedback
    assert "a host/provider fault, not a research result" in feedback
    assert '"X overview"' in feedback  # the query the host took over
    # L19: retrying is the host's job. The clause that told the model to do it
    # is gone, and the message says who owns the work instead.
    assert "re-issue them verbatim" not in feedback
    assert "do not run or paraphrase these queries again" in feedback
    assert "the host refuses a query it has already queued" in feedback
    # …and it never tells the model to work around the outage.
    assert "Pivot by changing" not in feedback
    assert "Do not paraphrase those queries" not in feedback


def test_a_tested_zero_still_gets_the_pivot_instruction() -> None:
    """The split is preserved: a real empty result is still a pivot."""
    feedback = zero_admission_feedback([("X overview", "no_hits")], [])

    assert "Pivot by changing" in feedback
    assert "EXTRACTION INFRASTRUCTURE" not in feedback


def test_a_mixed_turn_carries_both_messages_without_blending_them() -> None:
    feedback = zero_admission_feedback(
        [("unreadable", EXTRACTION_FAILURE), ("barren", "no_hits")], []
    )

    assert "EXTRACTION INFRASTRUCTURE FAILING" in feedback
    assert "Pivot by changing" in feedback
    assert "no_hits" in feedback and "extraction_failure" not in feedback


def test_the_observation_detail_says_untested_not_pivot() -> None:
    detail = zero_yield_detail(EXTRACTION_FAILURE, None)

    assert "Extraction infrastructure failed" in detail
    assert "never semantically tested" in detail
    assert "Pivot the query" not in detail


async def test_an_extraction_outage_turn_does_not_feed_the_exhausted_lead_streak() -> None:
    """Two empty turns normally earn a pivot warning. An outage is not evidence
    that the lead is exhausted; it is not evidence about the lead at all."""
    router = _TurnRouter([_TURN0] * 4)
    with pytest.raises(ResearchAgentError):
        await _run(router, _WedgedExtractor(), bound=_bound(max_research_turns=3))

    prompts = [request.messages[-1].content for request in router.requests]
    assert not any("Two distinct research turns chasing an upstream lead" in p for p in prompts)
    # …and the model was told what actually broke on the turn that followed it.
    # (It is not the LAST prompt any more: an outage turn no longer consumes the
    # model's turn budget, so the run reaches its wall through refused repeats.)
    assert any("EXTRACTION INFRASTRUCTURE FAILING" in prompt for prompt in prompts)

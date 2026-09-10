"""Hermetic tests for the v2 agentic research loop (`deep_research.agent`).

Scripted router + fake retrieval engine (the same doubles pattern as
`test_deep_research.py`): the turn protocol, search execution, readiness,
malformed-JSON re-ask with precise feedback, the live budget line and wrap-up
warning, steer/inject drains, and the Stop checkpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from disco.core.inspect import registry
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import agent as agent_mod
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._search_outcomes import (
    NEAR_DUPLICATE_JACCARD,
    narrowing_trail_rows,
    queries_are_near_duplicates,
    query_tokens,
    refusal_feedback,
    refusal_trail_rows,
    semantically_tested_queries,
)
from disco.retrieval.deep_research._search_turn import execute_search_turn
from disco.retrieval.deep_research.agent import (
    ResearchAgentError,
    ResearchOutcome,
    run_research_agent,
)
from disco.retrieval.deep_research.depth import DepthBound
from disco.retrieval.models import (
    Passage,
    RetrievalRequest,
    RetrievalResult,
    SearchHit,
)

# ---- doubles ----------------------------------------------------------------


class _TurnRouter(LLMRouter):
    """Returns queued scripted turn responses and records every request so
    tests can assert on the exact prompts the agent built."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.requests: list[CompletionRequest] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        self.requests.append(request)
        text = (
            self._responses.pop(0)
            if self._responses
            else '{"action": "done", "reason": "script exhausted"}'
        )
        return CompletionResponse(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="fake",
            request_id=request.request_id,
            routing=None,
        )

    async def stream_complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> AsyncIterator[StreamChunk]:
        async def gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(done=True, final=await self.complete(request, context=context))

        return gen()


def _passage(index: int, *, domain: str = "example.com") -> Passage:
    return Passage(
        id=f"p{index}",
        source_url=f"https://{domain}/article/{index}",
        source_title=f"Source {index}",
        text=(
            "Substantive evidence about the research subject with measured "
            f"figures and reported findings, item {index}."
        ),
    )


class _FakeRetrieval:
    """RetrievalEngine double: returns a scripted batch of passages per call
    (round-robin over `batches`) and records every request."""

    def __init__(self, batches: list[list[Passage]] | None = None) -> None:
        self.batches = batches
        self.requests: list[RetrievalRequest] = []
        self._calls = 0

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        self.requests.append(req)
        if self.batches is None:
            passages = [_passage(self._calls * 10 + i) for i in range(2)]
        else:
            passages = self.batches[self._calls % len(self.batches)]
        self._calls += 1
        hits = [
            SearchHit(url=p.source_url, title=p.source_title, snippet="s", rank=i)
            for i, p in enumerate(passages)
        ]
        return RetrievalResult(
            passages=passages, all_hits=hits, extracted=[], issued_queries=[req.query]
        )


class _FailingRetrieval:
    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        del req
        raise ConnectionError("provider down")


def _provider_notes(outcome: str, *, attempts: int = 3) -> dict[str, Any]:
    """The retrieval layer's record of its own below-the-model retries."""
    outcomes = ["degraded"] * attempts
    diagnostic: dict[str, Any] = {
        "status_code": 200,
        "search_attempts": attempts,
        "search_attempt_outcomes": outcomes,
        "search_outcome": outcome,
    }
    if outcome == "degraded":
        diagnostic["unresponsive_engines"] = ["yahoo", "brave"]
    else:
        outcomes[-1] = "ok"  # the winning attempt was clean, and named nothing
    return {"retrieval_trace": {"provider": "searxng", "provider_diagnostics": [diagnostic]}}


class _DegradedRetrieval:
    """Search infrastructure that stayed degraded through every retry: HTTP 200,
    zero hits, engines named unresponsive. NOT a semantic miss."""

    def __init__(self) -> None:
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        self.requests.append(req)
        return RetrievalResult(
            passages=[],
            all_hits=[],
            extracted=[],
            issued_queries=[req.query],
            notes=_provider_notes("degraded"),
        )


class _RecoveredRetrieval(_FakeRetrieval):
    """The provider flapped and the retrieval layer's retry won — the model must
    see an ordinary successful search, with no trace of the outage."""

    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        result = await super().retrieve(req)
        return result.model_copy(update={"notes": _provider_notes("recovered", attempts=2)})


class _TracedRetrieval(_FakeRetrieval):
    async def retrieve(self, req: RetrievalRequest) -> RetrievalResult:
        result = await super().retrieve(req)
        return result.model_copy(
            update={
                "notes": {
                    "retrieval_trace": {
                        "planned_query": req.query,
                        "issued_queries": [req.query + " compressed"],
                        "raw_discovered_hit_count": len(result.all_hits),
                        "extraction": {"attempted": 1, "success": 1, "failure": 0},
                        "reranked_passage_count": len(result.passages),
                    }
                }
            }
        )


def test_admission_dedupes_same_batch_url_variants_without_double_charge() -> None:
    clean = _passage(1)
    tracked = clean.model_copy(
        update={
            "id": "tracked-copy",
            "source_url": f"{clean.source_url}?msockid=abc",
        }
    )
    retrieval = RetrievalResult(
        passages=[clean, tracked],
        all_hits=[
            SearchHit(
                url=clean.source_url,
                title="Clean",
                source_engine="yahoo",
                status=None,
            ),
            SearchHit(
                url=tracked.source_url,
                title="Tracked",
                source_engine="bing",
                status="ok",
            ),
        ],
        extracted=[],
        issued_queries=["q"],
    )
    state = agent_mod._AgentState(budget=SourceBudget(5))

    assert state.admit_retrieved(retrieval) == 1
    assert state.budget.used == 1
    assert [passage.id for passage in state.pool] == [clean.id]
    assert len(state.all_hits) == 1
    assert state.all_hits[0].source_engine == "yahoo+bing"
    assert state.all_hits[0].status == "ok"


def _collect_events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        captured.append((kind, payload))

    return captured, emit


def _bound(**overrides: Any) -> DepthBound:
    values: dict[str, Any] = dict(
        max_sources=10,
        max_rounds_per_subq=2,
        max_research_turns=8,
        max_subquestions=4,
        discover_limit=5,
        extract_cap=4,
        rerank_top_k=4,
    )
    values.update(overrides)
    return DepthBound(**values)


_TURN0 = (
    '{"brief": "The question asks about X. I will map the players, the '
    'numbers, and independent criticism.", "decision_summary": "Map the '
    'question broadly before checking load-bearing claims.", "coverage": '
    '{"covered": [], "open": ["players", "numbers", "criticism"], '
    '"contradictions_checked": []}, "queries": ["X overview", '
    '"X criticism"], "ready_to_write": false}'
)
_READY = (
    '{"brief": "The question asks about X.", "decision_summary": '
    '"The evidence now covers the requested angles.", "coverage": '
    '{"covered": [{"angle": "overview", "evidence_ids": ["p0", "p1", '
    '"p10", "p11"]}], "open": [], "contradictions_checked": ["X"]}, '
    '"queries": [], "ready_to_write": true}'
)
_PIVOT = (
    '{"brief": "The question asks about X.", "decision_summary": '
    '"Pivot to economics.", "coverage": {"covered": [], "open": [], '
    '"contradictions_checked": []}, "queries": ["X economics"], '
    '"ready_to_write": false}'
)
_DONE = _READY  # legacy test alias; the runtime no longer accepts action=done
_READY_P99 = _READY.replace('"p0", "p1", "p10", "p11"', '"p99", "p99", "p99", "p99"')


async def _run(
    router: _TurnRouter,
    retrieval: Any,
    *,
    bound: DepthBound | None = None,
    **kwargs: Any,
) -> tuple[ResearchOutcome, list[tuple[str, dict[str, Any]]]]:
    captured, emit = _collect_events()
    outcome = await run_research_agent(
        "the state of X",
        router=router,
        retrieval_engine=retrieval,
        bound=bound or _bound(),
        namespace="conv_test",
        emit=emit,
        **kwargs,
    )
    return outcome, captured


# ---- turn protocol ----------------------------------------------------------


async def test_brief_parses_streams_and_search_executes() -> None:
    router = _TurnRouter([_TURN0, _DONE])
    retrieval = _FakeRetrieval()
    outcome, captured = await _run(router, retrieval)

    # Turn 0's brief is parsed, recorded, and emitted immediately.
    assert outcome.brief.startswith("The question asks about X.")
    kinds = [k for k, _ in captured]
    assert kinds[0] == "phase" and captured[0][1]["phase"] == "gather"
    assert ("brief", {"text": outcome.brief}) in captured

    # Both queries were retrieved with the tier's caps + admitted to the pool.
    assert [r.query for r in retrieval.requests] == ["X overview", "X criticism"]
    assert retrieval.requests[0].discover_limit == 5
    assert retrieval.requests[0].top_k == 4
    assert len(outcome.passages) == 4
    assert outcome.all_hits

    # The UI event contract: search before, observation after, per query.
    searches = [p for k, p in captured if k == "search"]
    observations = [p for k, p in captured if k == "observation"]
    assert {s["query"] for s in searches} == {"X overview", "X criticism"}
    assert all({"subquestion", "query", "round"} <= set(s) for s in searches)
    assert all(o["ok"] and "added" in o and "remaining_budget" in o for o in observations)

    # Readiness is host-gated; the trail carries searches and coverage.
    assert outcome.bounded_by is None
    search_entries = [e for e in outcome.trail if e.get("kind") == "search"]
    assert [e["query"] for e in search_entries] == ["X overview", "X criticism"]
    assert all(e["admitted"] == 2 for e in search_entries)
    assert any(e.get("kind") == "ready" for e in outcome.trail)


async def test_search_trail_carries_backend_trace_and_admission_outcome() -> None:
    router = _TurnRouter([_TURN0, _DONE])
    outcome, captured = await _run(router, _TracedRetrieval())

    entries = [entry for entry in outcome.trail if entry.get("kind") == "search"]
    assert entries and all(entry["final_admitted_count"] == 2 for entry in entries)
    assert all(entry["retrieval_trace"]["raw_discovered_hit_count"] == 2 for entry in entries)
    assert all(entry["retrieval_trace"]["extraction"]["success"] == 1 for entry in entries)
    assert all("yield_reason" not in entry for entry in entries)
    observations = [payload for kind, payload in captured if kind == "observation"]
    assert observations and all("retrieval_trace" in payload for payload in observations)


async def test_budget_countdown_line_present_every_turn() -> None:
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval())
    del outcome
    first_user = router.requests[0].messages[-1].content
    assert "BUDGET:" in first_user
    # The countdown is denominated in WORK (turns + source slots), never in
    # wall-clock seconds — a slow local model must not be charged for its
    # hardware.
    assert "8 research turns remaining of 8" in first_user
    assert "research time remaining" not in first_user
    assert "10 of 10 source slots remaining" in first_user
    assert "0 searches issued so far" in first_user
    # Second turn: a turn + slots consumed, searches counted, digest shown.
    second_user = router.requests[1].messages[-1].content
    assert "7 research turns remaining of 8" in second_user
    assert "6 of 10 source slots remaining" in second_user
    assert "2 searches issued so far" in second_user
    assert "EVIDENCE POOL (4 sources" in second_user
    assert "[p0]" in second_user and "example.com" in second_user


async def test_system_prompt_requires_primary_source_pivots() -> None:
    router = _TurnRouter([_TURN0, _DONE])
    await _run(router, _FakeRetrieval())

    system = router.requests[0].messages[0].content
    assert "broad coverage does not mean generic queries" in system
    assert "Start this on the first turn" in system
    assert "use its very next query to find the original" in system
    assert "exact title or DOI with PDF, preprint, author" in system
    assert "do not replace it with an SEO summary" in system
    assert "TREAT WEAK SECONDARY PAGES AS LEADS, NOT PROOF" in system
    assert "pivot with a targeted site: query" in system
    assert "one domain are one source, not independent corroboration" in system


async def test_wrap_up_warning_when_budget_nearly_spent() -> None:
    # The warning is work-based and fires on EITHER arm. Here the source arm:
    # a 5-slot budget with turn 0 admitting 4 leaves 1 of 5 (20% ≤ 25%), so the
    # second turn's message carries the warning and the first does not.
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval(), bound=_bound(max_sources=5))
    assert outcome.bounded_by is None
    assert "WRAP-UP WARNING" not in router.requests[0].messages[-1].content
    assert "WRAP-UP WARNING" in router.requests[1].messages[-1].content


def test_wrap_up_warning_fires_on_the_turn_arm() -> None:
    """The turn arm of the same single warning: ≤25% of the turn budget left."""
    bound = _bound(max_sources=100)
    assert "WRAP-UP WARNING" not in agent_mod._budget_line(3, 8, bound, 100, 4)
    assert "WRAP-UP WARNING" in agent_mod._budget_line(2, 8, bound, 100, 4)


async def test_wall_clock_exhaustion_bounds_the_run() -> None:
    """Budget exhaustion with an EMPTY pool is an error, not a report.

    The budget is now denominated in research turns (the name is kept for
    continuity with the persisted evidence); a zero-turn allowance is spent
    before the model is ever asked.
    """
    router = _TurnRouter([_TURN0])
    with pytest.raises(
        ResearchAgentError, match="(without usable evidence|could not sustain the turn protocol)"
    ):
        await _run(router, _FakeRetrieval(), bound=_bound(max_research_turns=0))
    assert router.requests == []  # never even asked the model


async def test_turn_budget_exhaustion_bounds_a_run_with_evidence() -> None:
    """Turns spent with a non-empty pool bounds the run honestly as "turns"."""
    router = _TurnRouter([_TURN0, _PIVOT])
    outcome, _ = await _run(router, _FakeRetrieval(), bound=_bound(max_research_turns=1))
    assert outcome.bounded_by == "turns"
    assert outcome.passages  # turn 0's evidence survived the bound
    assert len(router.requests) == 1  # the budget stopped it before turn 1


# ---- malformed JSON ---------------------------------------------------------


async def test_malformed_turn_reasks_once_with_exact_error() -> None:
    router = _TurnRouter(["this is not json at all", _TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval())
    # The re-ask carried the parse error + the required schema, then the
    # corrected response drove a normal run.
    reask = router.requests[1].messages[-1].content
    assert "could not be used" in reask and "not valid JSON" in reask
    assert '"brief"' in reask  # first-turn schema repeated verbatim
    assert outcome.brief and outcome.bounded_by is None
    assert len(outcome.passages) == 4


async def test_three_consecutive_malformed_turns_raise() -> None:
    router = _TurnRouter(["nope"] * 6)  # 3 turns × (attempt + re-ask)
    with pytest.raises(ResearchAgentError):
        await _run(router, _FakeRetrieval())


# ---- budgets, steers, injection, stop --------------------------------------


async def test_source_budget_exhaustion_bounds_the_run() -> None:
    bound = _bound(max_sources=2)
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval(), bound=bound)
    assert outcome.bounded_by == "sources"
    assert len(outcome.passages) == 2
    # The researcher sees the last admissions and closes coverage within its turn cap.
    assert len(router.requests) == 2
    assert "Queries will not be issued" in router.requests[-1].messages[-1].content


async def test_steers_are_priority_lines_and_recorded() -> None:
    steers = [["focus on recalls in Europe"], []]

    def pop_steers() -> list[str]:
        return steers.pop(0) if steers else []

    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval(), pop_steers=pop_steers)
    first_user = router.requests[0].messages[-1].content
    assert "USER STEER" in first_user
    assert "focus on recalls in Europe" in first_user
    assert {"kind": "steer", "text": "focus on recalls in Europe"} in outcome.trail


async def test_injected_sources_bypass_quality_filter() -> None:
    # Text this short would fail the report-usable filter; injection is exempt.
    queue = [[Passage(id="user1", source_url="", source_title="Note", text="short")]]

    def pop_injected() -> list[Passage]:
        return queue.pop(0) if queue else []

    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval(), pop_injected_sources=pop_injected)
    assert any(p.id == "user1" for p in outcome.passages)
    assert {"kind": "injected", "count": 1} in outcome.trail


async def test_stop_returns_checkpoint_with_pool() -> None:
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval(), should_cancel=lambda: bool(router.requests))
    assert outcome.bounded_by == "stopped"
    assert len(outcome.passages) == 4  # turn 0's admissions are checkpointed


async def test_retrieval_failure_is_observed_and_loop_continues() -> None:
    router = _TurnRouter([_TURN0, _READY])
    with pytest.raises(
        ResearchAgentError, match="(without usable evidence|could not sustain the turn protocol)"
    ):
        await _run(router, _FailingRetrieval())


async def test_empty_retrieval_is_an_error_not_a_dead_end_report() -> None:
    router = _TurnRouter([_TURN0, _READY])
    with pytest.raises(
        ResearchAgentError, match="(without usable evidence|could not sustain the turn protocol)"
    ):
        await _run(router, _FakeRetrieval(batches=[[]]))


async def test_resume_coverage_and_history_seed_the_run() -> None:
    router = _TurnRouter([_READY_P99])
    prior = [
        {"kind": "brief", "text": "The prior brief."},
        {
            "kind": "coverage",
            "turn": 0,
            "coverage": {
                "covered": [{"angle": "prior", "evidence_ids": ["p99"]}],
                "open": [],
                "contradictions_checked": [],
            },
        },
        {"kind": "search", "turn": 0, "query": "old query", "admitted": 1, "result": "evidence"},
    ]
    outcome, _ = await _run(
        router,
        _FakeRetrieval(batches=[[]]),
        resume_passages=[_passage(99)],
        resume_trail=prior,
    )
    assert outcome.coverage["covered"][0]["evidence_ids"] == ["p99"] * 4
    assert "old query" in router.requests[0].messages[-1].content
    assert "FULL QUERY OUTCOME HISTORY" in router.requests[0].messages[-1].content
    assert '"angle": "prior"' in router.requests[0].messages[-1].content


async def test_resume_effort_counts_unique_search_turns() -> None:
    bound = _bound(minimum_research_turns=2, minimum_useful_sources=0)
    router = _TurnRouter([_READY_P99, _PIVOT, _READY_P99])
    prior = [
        {"kind": "search", "turn": 3, "query": "old query", "admitted": 0},
        {"kind": "search", "turn": 3, "query": "old query two", "admitted": 0},
    ]
    outcome, _ = await _run(
        router,
        _FakeRetrieval(batches=[[]]),
        bound=bound,
        resume_passages=[_passage(99)],
        resume_trail=prior,
    )
    rejected = [entry for entry in outcome.trail if entry.get("kind") == "ready_rejected"]
    assert rejected and "fresh pivot" in rejected[0]["reason"]
    search_turns = {
        entry["turn"]
        for entry in outcome.trail
        if entry.get("kind") == "search" and isinstance(entry.get("turn"), int)
    }
    assert search_turns == {3, 5}


async def test_resume_trail_and_passages_seed_the_run() -> None:
    router = _TurnRouter([_TURN0, _READY])
    prior = [{"kind": "search", "query": "old query", "resumed": True}]
    outcome, _ = await _run(
        router,
        _FakeRetrieval(),
        resume_passages=[_passage(50, domain="carried.example")],
        resume_trail=prior,
    )
    assert outcome.trail[0] == prior[0]
    assert any(p.id == "p50" for p in outcome.passages)
    # The carried passage appears in the model's evidence digest.
    assert "[p50]" in router.requests[0].messages[-1].content


async def test_resumed_run_emits_turn_1_of_the_full_budget_with_the_pool_carried() -> None:
    """A resumed run's FIRST ``turn`` event says turn 1 of the whole budget.

    ``_AgentState.turns_charged`` starts at zero on every call, so the
    continuation gets its own full turn allowance while the checkpoint's
    passages are seeded exempt from the new source budget. The run UI reads
    both numbers off these events, so a change here would silently make the
    strip's "Turn 1 of 8" over a carried pool a lie rather than a fact.
    """
    router = _TurnRouter([_TURN0, _READY])
    outcome, captured = await _run(
        router,
        _FakeRetrieval(),
        resume_passages=[_passage(50, domain="carried.example"), _passage(51)],
        resume_trail=[{"kind": "search", "query": "old query", "resumed": True}],
    )
    turns = [payload for kind, payload in captured if kind == "turn"]
    assert turns[0]["n"] == 1
    assert turns[0]["of"] == 8
    assert {p.id for p in outcome.passages} >= {"p50", "p51"}


async def test_done_action_is_a_model_contract_error() -> None:
    router = _TurnRouter(['{"action": "done", "reason": "enough"}'] * 6)
    with pytest.raises(ResearchAgentError, match="could not sustain the turn protocol"):
        await _run(router, _FakeRetrieval())


async def test_ready_is_held_until_depth_floor() -> None:
    bound = _bound(minimum_research_turns=2, minimum_useful_sources=4)
    router = _TurnRouter([_READY, _TURN0, _PIVOT, _READY])
    outcome, _ = await _run(router, _FakeRetrieval(), bound=bound)
    assert outcome.bounded_by is None
    assert any(entry.get("kind") == "ready_rejected" for entry in outcome.trail)
    assert len([entry for entry in outcome.trail if entry.get("kind") == "ready"]) == 1


async def test_malformed_turn_does_not_satisfy_research_turn_floor() -> None:
    bound = _bound(minimum_research_turns=2, minimum_useful_sources=0)
    router = _TurnRouter(["{}", _READY, _TURN0, _PIVOT, _READY])
    outcome, _ = await _run(
        router,
        _FakeRetrieval(),
        bound=bound,
        resume_passages=[_passage(0), _passage(1), _passage(10), _passage(11)],
    )
    rejected = [entry for entry in outcome.trail if entry.get("kind") == "ready_rejected"]
    assert rejected and "fresh pivot" in rejected[0]["reason"]
    assert len([entry for entry in outcome.trail if entry.get("kind") == "ready"]) == 1


async def test_queryless_ready_turn_cannot_satisfy_effort_floor() -> None:
    bound = _bound(minimum_research_turns=2, minimum_useful_sources=0)
    router = _TurnRouter([_TURN0, _READY, _PIVOT, _READY])
    outcome, _ = await _run(router, _FakeRetrieval(), bound=bound)
    rejected = [entry for entry in outcome.trail if entry.get("kind") == "ready_rejected"]
    assert rejected and "fresh pivot" in rejected[0]["reason"]
    search_turns = {
        entry["turn"]
        for entry in outcome.trail
        if entry.get("kind") == "search" and isinstance(entry.get("turn"), int)
    }
    assert len(search_turns) == 2


async def test_open_coverage_gap_blocks_readiness() -> None:
    ready_with_gap = _READY.replace(
        '"open": [], "contradictions_checked"',
        '"open": ["uncorroborated claim"], "contradictions_checked"',
    ).replace('"queries": [], "ready_to_write"', '"queries": ["X gap check"], "ready_to_write"')
    router = _TurnRouter([ready_with_gap, _READY])
    outcome, _ = await _run(
        router,
        _FakeRetrieval(),
        resume_passages=[_passage(0), _passage(1), _passage(10), _passage(11)],
    )
    assert any(entry.get("kind") == "ready_rejected" for entry in outcome.trail)
    assert "critical open coverage gaps" in router.requests[1].messages[-1].content
    assert outcome.bounded_by is None


async def test_repeated_query_is_rejected_and_feedback_is_visible() -> None:
    repeat = (
        '{"brief": "The question asks about X.", "decision_summary": "Pivot.", '
        '"coverage": {"covered": [], "open": ["gap"], "contradictions_checked": []}, '
        '"queries": [" x   overview "], "ready_to_write": false}'
    )
    router = _TurnRouter([_TURN0, repeat, _READY])
    outcome, _ = await _run(router, _FakeRetrieval())
    assert any(entry.get("kind") == "query_rejected" for entry in outcome.trail)
    assert "already attempted" in router.requests[2].messages[-1].content
    assert outcome.bounded_by is None


def test_later_turn_brief_is_optional() -> None:
    """The first turn establishes the brief; later turns may omit it."""
    later = _READY.replace('"brief": "The question asks about X.", ', "")
    parsed, error = agent_mod._parse_turn(later, expect_brief=False)
    assert error is None
    assert parsed is not None and parsed.brief == ""


def test_coverage_without_evidence_ids_is_unresolved_not_malformed() -> None:
    """An angle can be declared before the model maps its supporting passages."""
    response = _READY.replace(
        '"evidence_ids": ["p0", "p1", "p10", "p11"]',
        '"evidence_ids": null',
    )
    parsed, error = agent_mod._parse_turn(response, expect_brief=False)
    assert error is None
    assert parsed is not None
    assert parsed.coverage["covered"] == [{"angle": "overview", "evidence_ids": []}]


def test_coverage_topic_strings_are_unresolved_and_blank_topics_remain_malformed() -> None:
    response = _READY.replace(
        '{"angle": "overview", "evidence_ids": ["p0", "p1", "p10", "p11"]}',
        '"overview"',
    )
    parsed, error = agent_mod._parse_turn(response, expect_brief=False)
    assert error is None and parsed is not None
    assert parsed.coverage["covered"] == [{"angle": "overview", "evidence_ids": []}]

    blank = response.replace('"overview"', '"  "')
    parsed, error = agent_mod._parse_turn(blank, expect_brief=False)
    assert parsed is None
    assert error == 'each covered item must have a non-empty string "angle"'


async def test_unresolved_coverage_still_cannot_pass_readiness() -> None:
    response = _READY.replace(
        '"evidence_ids": ["p0", "p1", "p10", "p11"]',
        '"evidence_ids": null',
    )
    parsed, error = agent_mod._parse_turn(response, expect_brief=False)
    assert error is None and parsed is not None

    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.admit_exempt([_passage(0)])
    _, emit = _collect_events()
    done = await agent_mod._handle_parsed_turn(
        state,
        parsed,
        0,
        retrieval_engine=_FakeRetrieval(batches=[[]]),
        bound=_bound(minimum_research_turns=0, minimum_useful_sources=1, min_evidence_themes=1),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
    )
    assert not done
    assert "evidence-backed major angles" in state.feedback


_ZERO_YIELD_READY = _READY.replace(
    '"covered": [{"angle": "overview", "evidence_ids": ["p0", "p1", "p10", "p11"]}]',
    '"covered": []',
)


async def _zero_yield_run(retrieval: Any) -> tuple[_TurnRouter, ResearchOutcome]:
    router = _TurnRouter([_TURN0, _ZERO_YIELD_READY])
    outcome, _ = await _run(
        router,
        retrieval,
        bound=_bound(minimum_useful_sources=1),
        resume_passages=[_passage(99)],
        resume_trail=[{"kind": "search", "turn": 0, "query": "old", "admitted": 1}],
    )
    return router, outcome


async def test_zero_yield_reason_reaches_the_next_turn() -> None:
    """A CLEAN zero gives the lead a concrete pivot signal in its next prompt; a
    zero caused by degraded search infrastructure never does — the two outcomes
    are separate classes and only one of them is about the query."""
    router, outcome = await _zero_yield_run(_FakeRetrieval(batches=[[]]))

    assert outcome.bounded_by is None
    next_prompt = router.requests[1].messages[-1].content
    assert "no_hits" in next_prompt
    assert "Do not paraphrase those queries" in next_prompt
    assert "SEARCH INFRASTRUCTURE DEGRADED" not in next_prompt
    summary = [entry for entry in outcome.trail if entry.get("kind") == "search_turn_summary"]
    assert summary and summary[-1]["yield_reasons"] == ["no_hits"]

    # Same empty response, but the provider named an outage and the retrieval
    # layer's retries were exhausted → a distinct class, never "no_hits".
    router, outcome = await _zero_yield_run(_DegradedRetrieval())

    assert outcome.bounded_by is None
    summary = [entry for entry in outcome.trail if entry.get("kind") == "search_turn_summary"]
    assert summary and summary[-1]["yield_reasons"] == ["provider_degraded"]
    searches = [entry for entry in outcome.trail if entry.get("kind") == "search"]
    assert [entry.get("yield_reason") for entry in searches[-2:]] == [
        "provider_degraded",
        "provider_degraded",
    ]


async def test_provider_degraded_feedback_says_what_broke_and_what_is_allowed() -> None:
    """Radiant, specific feedback: what happened, the state now, what to do next,
    what remains allowed. Never a pivot instruction for an outage."""
    router, _outcome = await _zero_yield_run(_DegradedRetrieval())
    prompt = router.requests[1].messages[-1].content

    assert "SEARCH INFRASTRUCTURE DEGRADED" in prompt
    assert "unresponsive search engines: yahoo, brave" in prompt  # named, not generic
    assert "3 attempts with backoff" in prompt  # retried below the model
    assert "NOT SEMANTICALLY TESTED" in prompt
    assert "do not treat it as a dead end" in prompt
    # L19: the host owns re-running them; the model is told the state and the
    # next action, never invited to spend its own turns on the retry.
    assert "re-issue them verbatim" not in prompt
    assert "The host has queued 2 of them to run again by itself" in prompt
    assert "NEXT: spend this turn on different angles" in prompt
    assert "STILL AVAILABLE: every other query" in prompt
    # The pivot instruction belongs to a real semantic miss only.
    assert "Do not paraphrase those queries" not in prompt
    assert "no_hits" not in prompt
    # The model can see WHICH queries were never really tested.
    assert '"yield_reason": "provider_degraded"' in prompt


def test_provider_degraded_queries_stay_reissuable_but_tested_ones_do_not() -> None:
    """_fresh_queries forbids repeating a tested query and permits re-issuing one
    whose every issue died in an outage — including after a repeat rejection."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(
        [
            {
                "kind": "search",
                "turn": 0,
                "query": "X named report",
                "admitted": 0,
                "yield_reason": "provider_degraded",
            },
            {
                "kind": "search",
                "turn": 0,
                "query": "X economics",
                "admitted": 0,
                "yield_reason": "no_hits",
            },
            {"kind": "query_rejected", "turn": 1, "query": "X named report"},
        ]
    )

    fresh, repeated, _narrowed = agent_mod._fresh_queries(state, ("X named report", "X economics"))
    assert fresh == ["X named report"]
    assert [refusal.query for refusal in repeated] == ["X economics"]

    # Once that query is really tested, it is a repeat like any other.
    state.trail.append(
        {
            "kind": "search",
            "turn": 2,
            "query": "X named report",
            "admitted": 0,
            "yield_reason": "no_hits",
        }
    )
    fresh, repeated, _narrowed = agent_mod._fresh_queries(state, ("X named report",))
    assert fresh == [] and [refusal.query for refusal in repeated] == ["X named report"]


def test_transport_failed_queries_stay_reissuable() -> None:
    """A query whose issue ended in a retrieval EXCEPTION (`result="failed"`)
    was never put to the world either, so it is re-issuable — including after a
    repeat rejection. One that also has a real tested issue stays blocked."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(
        [
            {
                "kind": "search",
                "turn": 0,
                "query": "X filing",
                "admitted": 0,
                "provider_error": "TimeoutError",
                "result": "failed",
            },
            {"kind": "query_rejected", "turn": 1, "query": "X filing"},
            {
                "kind": "search",
                "turn": 0,
                "query": "X margins",
                "admitted": 0,
                "result": "empty",
                "yield_reason": "no_hits",
            },
        ]
    )

    fresh, repeated, _narrowed = agent_mod._fresh_queries(state, ("X filing", "X margins"))
    assert fresh == ["X filing"]
    assert [refusal.query for refusal in repeated] == ["X margins"]

    # A query that blew up ONCE but was really tested on another issue stays
    # blocked: the tested issue is a genuine result about it.
    state.trail.append(
        {"kind": "search", "turn": 2, "query": "X filing", "admitted": 2, "result": "evidence"}
    )
    fresh, repeated, _narrowed = agent_mod._fresh_queries(state, ("X filing",))
    assert fresh == [] and [refusal.query for refusal in repeated] == ["X filing"]


def test_semantically_tested_queries_splits_tested_from_untested() -> None:
    """The set the freshness gate reads: both untested classes stay out."""
    assert semantically_tested_queries(
        [
            {"kind": "search", "query": "Tested Query", "result": "empty"},
            {"kind": "search", "query": "broke", "result": "failed"},
            {"kind": "search", "query": "outage", "yield_reason": "provider_degraded"},
            {"kind": "query_rejected", "query": "broke"},
            {"kind": "query_rejected", "query": "outage"},
            {"kind": "query_rejected", "query": "plain repeat"},
        ]
    ) == {"tested query", "plain repeat"}


# ---- the near-duplicate freshness wall --------------------------------------

_LOOPED = "solid-state batteries EVs commercialization pathways"

#: Genuinely different angles that share the topic words. These are what the
#: threshold is tuned against: every one must stay issuable.
_DIFFERENT_ANGLES = [
    (
        "solid-state battery manufacturing cost breakdown",
        "solid-state battery safety failure modes",
    ),
    (
        "grid storage lithium iron phosphate cost per kWh",
        "grid storage lithium iron phosphate recycling regulations",
    ),
    (
        "sodium-ion battery energy density 2026",
        "sodium-ion battery supply chain China",
    ),
    (
        "solid-state battery cathode manufacturing",
        "solid-state battery anode manufacturing",
    ),
]


def _tested_trail(
    query: str, *, turn: int = 2, reason: str = "duplicates_or_filtered"
) -> list[dict[str, Any]]:
    return [
        {
            "kind": "search",
            "turn": turn,
            "query": query,
            "admitted": 0,
            "result": "empty",
            "yield_reason": reason,
        }
    ]


def test_near_duplicate_queries_are_refused_like_exact_repeats() -> None:
    """Word order, hyphens, plurals and stopwords do not make a new query.

    This is the live paraphrase loop: the model rewords a tested query, the
    reworded one retrieves the same results, and the turn ends in
    `duplicates_or_filtered`. Identity, not instruction, has to stop it.
    """
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(_tested_trail(_LOOPED))
    paraphrases = (
        "solid state battery EV commercialization timeline",
        "commercialization pathways for solid-state batteries in EVs",
        "Solid-State Battery EVs Commercialization Pathway",
    )

    fresh, refused, _narrowed = agent_mod._fresh_queries(state, paraphrases)

    assert fresh == []
    assert [refusal.query for refusal in refused] == list(paraphrases)
    assert all(refusal.earlier is not None for refusal in refused)


def test_near_duplicate_refusal_names_the_earlier_query_turn_and_outcome() -> None:
    """A refusal radiates: which query it collides with, when that ran, how it
    ended, and what an actual pivot would change."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(_tested_trail(_LOOPED))
    _fresh, refused, _narrowed = agent_mod._fresh_queries(
        state, ("solid state battery EV commercialization timeline",)
    )

    message = refusal_feedback(refused)
    assert "solid state battery EV commercialization timeline" in message
    assert _LOOPED in message
    assert "turn 2" in message
    assert "duplicates_or_filtered" in message
    assert "angle" in message
    assert "source class" in message
    assert "domain" in message
    assert "specificity" in message

    rows = refusal_trail_rows(3, refused)
    assert [row["kind"] for row in rows] == ["query_rejected"]
    assert rows[0]["rejected"] == "near_duplicate"
    assert rows[0]["duplicates"] == _LOOPED
    assert rows[0]["duplicates_turn"] == 2
    assert rows[0]["duplicates_outcome"] == "duplicates_or_filtered"


def test_an_exact_repeat_refusal_also_names_the_earlier_turn() -> None:
    """The radiant message is the only refusal message; exact repeats get it too."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.append(
        {"kind": "search", "turn": 1, "query": _LOOPED, "admitted": 4, "result": "evidence"}
    )
    _fresh, refused, _narrowed = agent_mod._fresh_queries(state, (f"  {_LOOPED.upper()} ",))

    message = refusal_feedback(refused)
    assert "turn 1" in message
    assert "admitted 4" in message
    assert refusal_trail_rows(2, refused)[0]["rejected"] == "repeat"


def test_a_different_angle_sharing_topic_words_stays_issuable() -> None:
    """The wall refuses reworded queries, never new angles on the same subject."""
    for tested, pivot in _DIFFERENT_ANGLES:
        state = agent_mod._AgentState(budget=SourceBudget(5))
        state.trail.append(
            {"kind": "search", "turn": 0, "query": tested, "admitted": 3, "result": "evidence"}
        )
        fresh, refused, _narrowed = agent_mod._fresh_queries(state, (pivot,))
        assert fresh == [pivot], (tested, pivot)
        assert refused == [], (tested, pivot)


#: Measured on the 13 recorded batch-B runs: the freshness wall refused each of
#: these while telling the model that changing the SPECIFICITY was a real pivot.
#: (tested query, the narrowing of it the wall refused, what it added)
_MEASURED_NARROWINGS = [
    (
        "EIA AEO 2025 levelized cost LCOE LCOS gas combustion turbine peaker vs "
        "battery storage EIA.gov report",
        "EIA AEO Levelized Costs gas combustion turbine peaker vs battery storage "
        "LCOE LCOS site:eia.gov",
        "site:eia.gov",
    ),
    (
        "Nature Human Behaviour 2025 Work time reduction 4-day workweek Fan Schor "
        "methods quasi-experimental pre-registered control companies",
        "Nature Human Behaviour 2025 Fan Schor Gu work time reduction 4-day workweek "
        "quasi-experimental pre-registered control methods PDF",
        "pdf",
    ),
    (
        "solid-state battery manufacturing scalability cost challenges sulfide dry room yield",
        "solid-state battery manufacturing cost scalability sulfide yield dry room "
        "challenges 2026 BNEF",
        "2026",
    ),
    (
        "SMIC Huawei Kirin 9000S 9010 7nm N+2 yield production TechInsights teardown 2024 2025",
        "TechInsights Huawei Kirin 9020 9010 SMIC 7nm N+2 5nm teardown yield capacity 2024 2025",
        "9020",
    ),
]


def test_a_narrowing_of_a_tested_query_is_issued_not_refused() -> None:
    """The wall told the model to change the specificity, then refused it.

    Every pair here is a real refusal from the recorded runs. The narrowing adds
    a scope the tested query does not carry, so it reaches different sources and
    has to be issued.
    """
    for tested, narrowing, added in _MEASURED_NARROWINGS:
        state = agent_mod._AgentState(budget=SourceBudget(5))
        state.trail.extend(_tested_trail(tested))
        assert queries_are_near_duplicates(query_tokens(tested), query_tokens(narrowing)), narrowing

        fresh, refused, narrowed = agent_mod._fresh_queries(state, (narrowing,))

        assert fresh == [narrowing], narrowing
        assert refused == [], narrowing
        assert [row.narrowed_from for row in narrowed] == [tested]
        assert added in narrowed[0].added


def test_a_rewording_that_narrows_nothing_is_still_refused() -> None:
    """The wall stays a wall. A new content word is not a narrowing.

    `methodology` against a query that already says `methods` is the same query
    said differently — and a rule that counted any added token would refuse
    nothing at all.
    """
    tested = (
        "Late Bronze Age collapse evidentiary methods destruction layers Bayesian "
        "radiocarbon dendrochronology limitations"
    )
    reworded = (
        "Late Bronze Age collapse evidentiary methods limitations destruction layers "
        "pollen radiocarbon Bayesian critique methodology"
    )
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(_tested_trail(tested))

    fresh, refused, narrowed = agent_mod._fresh_queries(state, (reworded,))

    assert fresh == [] and narrowed == []
    assert [refusal.query for refusal in refused] == [reworded]


def test_a_repeat_of_a_scoped_query_is_not_a_narrowing_of_itself() -> None:
    """An exact repeat carries the same scopes, so it adds none and stays refused."""
    scoped = 'site:iea.org "Global EV Outlook 2026" solid-state battery forecast PDF'
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(_tested_trail(scoped))

    fresh, refused, narrowed = agent_mod._fresh_queries(state, (f" {scoped.upper()} ",))

    assert fresh == [] and narrowed == []
    assert refusal_trail_rows(2, refused)[0]["rejected"] == "repeat"


def test_a_narrowing_of_one_query_that_repeats_another_is_still_refused() -> None:
    """Measured live (T3 run-02, turns 12 and 13): this exact query ran twice.

    It narrows the broad IRENA query from turn 10 — `filetype:pdf` — so the wall
    matched it there, called it a narrowing and issued it. Next turn the model
    proposed it verbatim, the broad query was still the FIRST thing it matched
    in trail order, and it was issued again. A narrowing has to narrow
    everything it matched, or it reaches exactly what one of them already did.
    """
    broad = "site:irena.org Renewable Power Generation Costs battery storage 2024"
    narrowed = "site:irena.org filetype:pdf Renewable Power Generation Costs 2024 battery storage"
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(_tested_trail(broad, turn=10))

    fresh, refused, narrowing = agent_mod._fresh_queries(state, (narrowed,))
    assert fresh == [narrowed] and [row.added for row in narrowing] == [("filetype:pdf",)]

    # It ran; now it is a tested query of its own, and proposing it again is a
    # repeat no matter what it narrows.
    state.trail.append(
        {"kind": "search", "turn": 12, "query": narrowed, "admitted": 3, "result": "evidence"}
    )
    fresh, refused, narrowing = agent_mod._fresh_queries(state, (narrowed,))

    assert fresh == [] and narrowing == []
    assert [refusal.earlier.query for refusal in refused if refusal.earlier] == [narrowed]
    assert refusal_trail_rows(13, refused)[0]["rejected"] == "repeat"


def test_the_refusal_says_a_narrowing_would_be_issued() -> None:
    """The wall's message has to be true about what it will accept next."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(_tested_trail(_LOOPED))
    _fresh, refused, _narrowed = agent_mod._fresh_queries(
        state, ("solid state battery EV commercialization timeline",)
    )

    message = refusal_feedback(refused)
    assert "STATE:" in message and "NEXT:" in message and "STILL AVAILABLE:" in message
    assert "a NARROWING of a refused query IS issued, not refused" in message
    for lever in ("site:", "filetype:", "DOI", "quote an exact title"):
        assert lever in message


def test_the_trail_records_which_query_a_narrowing_narrowed() -> None:
    """A narrowing is issued, so it looks like any other search on the wire.

    The audit row is the only place a reader can tell that the wall matched it
    and let it through on purpose.
    """
    tested, narrowing, _added = _MEASURED_NARROWINGS[0]
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(_tested_trail(tested))
    _fresh, _refused, narrowed = agent_mod._fresh_queries(state, (narrowing,))

    rows = narrowing_trail_rows(7, narrowed)
    assert [row["kind"] for row in rows] == ["query_narrowed"]
    assert rows[0]["turn"] == 7
    assert rows[0]["query"] == narrowing
    assert rows[0]["narrowed_from"] == tested
    assert rows[0]["narrowing"] == ["site:eia.gov"]


def test_near_duplicate_threshold_sits_between_the_measured_cases() -> None:
    """Pin the number the contrast pairs justify, so drift is visible.

    The looped pair measures 5/7; the tightest different-angle pair measures
    4/6. 0.8 would let the loop through; the threshold lives in between.
    """
    looped = query_tokens(_LOOPED) & query_tokens(
        "solid state battery EV commercialization timeline"
    )
    assert len(looped) == 5
    assert queries_are_near_duplicates(
        query_tokens(_LOOPED), query_tokens("solid state battery EV commercialization timeline")
    )
    for tested, pivot in _DIFFERENT_ANGLES:
        assert not queries_are_near_duplicates(query_tokens(tested), query_tokens(pivot))
    assert 0.667 < NEAR_DUPLICATE_JACCARD <= 5 / 7


def test_provider_degraded_near_duplicate_stays_issuable() -> None:
    """An outage never tested the query, so a rewording of it is not a repeat.
    The settled untested rule governs near-duplicates exactly as it governs
    exact ones."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.trail.extend(
        [
            {
                "kind": "search",
                "turn": 0,
                "query": _LOOPED,
                "admitted": 0,
                "yield_reason": "provider_degraded",
            },
            {"kind": "query_rejected", "turn": 1, "query": _LOOPED},
            {
                "kind": "search",
                "turn": 0,
                "query": "sodium-ion battery cathode chemistry",
                "admitted": 0,
                "provider_error": "TimeoutError",
                "result": "failed",
            },
        ]
    )

    fresh, refused, _narrowed = agent_mod._fresh_queries(
        state,
        (
            "solid state battery EV commercialization timeline",
            "sodium ion batteries cathode chemistries",
        ),
    )
    assert fresh == [
        "solid state battery EV commercialization timeline",
        "sodium ion batteries cathode chemistries",
    ]
    assert refused == []


async def test_duplicates_turn_feedback_names_the_overlap_count() -> None:
    """A turn that retrieved only what the pool already holds says so in counts."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    pool = [_passage(0), _passage(1), _passage(2)]
    assert state.admit_exempt(pool) == pool
    _captured, emit = _collect_events()

    await execute_search_turn(
        state,
        ["an untried angle on the subject"],
        1,
        retrieval_engine=_FakeRetrieval(batches=[pool]),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(1, 8),
    )

    assert "duplicates_or_filtered" in state.feedback
    assert "3 of the results retrieved were sources already in the evidence pool" in state.feedback
    assert "the pool already covers this angle" in state.feedback
    # Counts only — the wall never lists what it already has.
    assert "Source 0" not in state.feedback


async def test_outage_streak_is_not_read_as_an_exhausted_lead() -> None:
    """Two zero-yield turns caused by an outage say nothing about the lead, so the
    'stop chasing this' warning must not fire on them."""
    router = _TurnRouter([_TURN0, _PIVOT, _ZERO_YIELD_READY])
    outcome, _ = await _run(
        router,
        _DegradedRetrieval(),
        bound=_bound(minimum_useful_sources=1, min_evidence_themes=0),
        resume_passages=[_passage(99)],
        resume_trail=[{"kind": "search", "turn": 0, "query": "old", "admitted": 1}],
    )

    assert outcome.bounded_by is None
    third_prompt = router.requests[2].messages[-1].content
    assert "Stop paraphrasing or retrying" not in third_prompt
    assert "SEARCH INFRASTRUCTURE DEGRADED" in third_prompt


async def test_recovered_provider_outage_is_invisible_to_the_model() -> None:
    """Retries are system mechanics. A query the retrieval layer recovered looks
    exactly like an ordinary successful search from the turn loop upward."""
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _RecoveredRetrieval())

    assert outcome.bounded_by is None
    searches = [entry for entry in outcome.trail if entry.get("kind") == "search"]
    assert searches and all("yield_reason" not in entry for entry in searches)
    prompt = router.requests[1].messages[-1].content
    assert "DEGRADED" not in prompt and "provider_degraded" not in prompt


async def test_upstream_zero_yield_warning_starts_after_two_distinct_turns() -> None:
    pivot_one = _PIVOT.replace('"X economics"', '"X named report details"')
    pivot_two = _PIVOT.replace('"X economics"', '"X named report primary source"')
    ready = _READY.replace(
        '"covered": [{"angle": "overview", "evidence_ids": ["p0", "p1", "p10", "p11"]}]',
        '"covered": []',
    )
    router = _TurnRouter([_TURN0, pivot_one, pivot_two, ready])
    retrieval = _FakeRetrieval(batches=[[_passage(0)], [_passage(1)], [], []])
    outcome, _ = await _run(
        router,
        retrieval,
        bound=_bound(min_evidence_themes=0),
    )

    assert outcome.bounded_by is None
    first_retry = router.requests[1].messages[-1].content
    second_retry = router.requests[2].messages[-1].content
    third_retry = router.requests[3].messages[-1].content
    assert "Stop paraphrasing or retrying" not in first_retry
    assert "Stop paraphrasing or retrying" not in second_retry
    assert "Stop paraphrasing or retrying" in third_retry
    assert "different authoritative class" in third_retry


async def test_readiness_counts_distinct_works_not_passages() -> None:
    """Mirrors of one DOI do not satisfy the independent-work floor."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.admit_exempt(
        [
            Passage(
                id="doi-a",
                source_url="https://doi.org/10.1234/example",
                source_title="Original",
                text="Substantive evidence from the original work.",
            ),
            Passage(
                id="doi-b",
                source_url="https://example.org/mirror/10.1234/example",
                source_title="Mirror",
                text="Substantive evidence from a mirror of the work.",
            ),
        ]
    )
    parsed, error = agent_mod._parse_turn(
        _READY.replace(
            '"covered": [{"angle": "overview", "evidence_ids": ["p0", "p1", "p10", "p11"]}]',
            '"covered": []',
        ),
        expect_brief=False,
    )
    assert error is None and parsed is not None
    captured, emit = _collect_events()
    done = await agent_mod._handle_parsed_turn(
        state,
        parsed,
        0,
        retrieval_engine=_FakeRetrieval(batches=[[]]),
        bound=_bound(minimum_research_turns=0, minimum_useful_sources=1, min_evidence_sources=2),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
    )
    assert not done
    assert "distinct works" in state.feedback
    # The turn's own phase event leads; the brief is still the model's first
    # visible output.
    assert captured == [
        ("turn", {"n": 1, "of": 8, "phase": "planning"}),
        ("brief", {"text": "The question asks about X."}),
    ]


async def test_readiness_requires_a_recorded_counterevidence_check() -> None:
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.admit_exempt([_passage(99)])
    ready_without_check = _READY.replace(
        '"covered": [{"angle": "overview", "evidence_ids": ["p0", "p1", "p10", "p11"]}]',
        '"covered": []',
    ).replace('"contradictions_checked": ["X"]', '"contradictions_checked": []')
    parsed, error = agent_mod._parse_turn(ready_without_check, expect_brief=False)
    assert error is None and parsed is not None
    _, emit = _collect_events()
    done = await agent_mod._handle_parsed_turn(
        state,
        parsed,
        0,
        retrieval_engine=_FakeRetrieval(batches=[[]]),
        bound=_bound(minimum_research_turns=0, minimum_useful_sources=1),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
    )
    assert not done
    assert "targeted search" in state.feedback
    assert "contradictions_checked" in state.feedback


async def test_depth_requires_model_owned_evidence_backed_coverage() -> None:
    """The top-down map is a readiness funnel, not prompt-only advice."""
    state = agent_mod._AgentState(budget=SourceBudget(5))
    state.admit_exempt([_passage(99)])
    one_angle = _READY.replace(
        '"covered": [{"angle": "overview", "evidence_ids": ["p0", "p1", "p10", "p11"]}]',
        '"covered": [{"angle": "overview", "evidence_ids": ["p99"]}]',
    )
    parsed, error = agent_mod._parse_turn(one_angle, expect_brief=False)
    assert error is None and parsed is not None
    _, emit = _collect_events()

    done = await agent_mod._handle_parsed_turn(
        state,
        parsed,
        0,
        retrieval_engine=_FakeRetrieval(batches=[[]]),
        bound=_bound(
            minimum_research_turns=0,
            minimum_useful_sources=1,
            min_evidence_themes=2,
        ),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
    )

    assert not done
    assert "top-down coverage pass" in state.feedback
    assert "2 evidence-backed major angles" in state.feedback


async def test_model_io_trace_records_visible_turns_and_declared_queries(monkeypatch) -> None:
    monkeypatch.setenv("DISCO_INSPECT", "1")
    registry().clear()
    router = _TurnRouter([f"<think>private scratch</think>{_TURN0}", _READY])

    await _run(router, _FakeRetrieval())

    snapshot = registry().snapshot("conv_test")
    assert snapshot is not None
    rows = snapshot["model_io"]
    assert len(rows) == 2
    assert rows[0]["stage"] == "research_turn"
    assert router.requests[0].metadata == {
        "conversation_id": "conv_test",
        "inspect_stage": "research_turn",
    }
    assert rows[0]["declared_decision"]["queries"] == ["X overview", "X criticism"]
    assert rows[0]["request"]["messages"][-1]["content"].startswith("QUESTION:")
    assert "private scratch" not in rows[0]["response"]["text"]
    assert rows[0]["parse_error"] is None


async def test_research_decisions_use_provider_neutral_json_without_hidden_thinking() -> None:
    """Research turns preserve provider reasoning defaults and visible cleanliness.

    The generic hint remains unset so each provider's configured default applies.
    Hidden reasoning never reaches the parser because the response is think-stripped.
    """
    router = _TurnRouter(["not json", _TURN0, _READY])

    await _run(router, _FakeRetrieval())

    assert all(request.response_format == "json" for request in router.requests)
    assert all(request.enable_thinking is None for request in router.requests)
    retry = router.requests[1]
    assert [message.content for message in retry.messages if message.role == "assistant"] == [
        "not json"
    ]


async def test_research_turn_wrapped_in_a_think_block_still_parses() -> None:
    """A reasoning model's visible JSON survives its own thinking phase."""
    router = _TurnRouter(
        [
            f"<think>Let me plan the angles first.</think>\n{_TURN0}",
            f"<think>The pool now covers the question.</think>{_READY}",
        ]
    )

    outcome, _ = await _run(router, _FakeRetrieval())

    assert outcome.brief.startswith("The question asks about X.")
    assert len(outcome.passages) == 4
    assert outcome.bounded_by is None
    assert any(entry.get("kind") == "ready" for entry in outcome.trail)

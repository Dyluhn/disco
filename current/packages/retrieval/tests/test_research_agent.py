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
        max_wall_clock_s=600,
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
    assert "research time remaining" in first_user
    assert "10 of 10 source slots remaining" in first_user
    assert "0 searches issued so far" in first_user
    # Second turn: slots consumed + searches counted + evidence digest shown.
    second_user = router.requests[1].messages[-1].content
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


async def test_wrap_up_warning_when_budget_nearly_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Research budget for this bound: 600s wall clock − 80s writing reserve
    # = 520s. Clock jumps to t=500 → 20s left (<25%) → warning appears.
    clock = iter([0.0])

    def fake_monotonic() -> float:
        return next(clock, 500.0)

    monkeypatch.setattr(agent_mod, "_monotonic", fake_monotonic)
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval())
    assert outcome.bounded_by is None
    first_user = router.requests[0].messages[-1].content
    assert "WRAP-UP WARNING" in first_user


async def test_wall_clock_exhaustion_bounds_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([0.0])  # `started` reads 0; every later read is past the cap
    monkeypatch.setattr(agent_mod, "_monotonic", lambda: next(clock, 10_000.0))
    router = _TurnRouter([_TURN0])
    with pytest.raises(
        ResearchAgentError, match="(without usable evidence|malformed research turns)"
    ):
        await _run(router, _FakeRetrieval())
    assert router.requests == []  # never even asked the model


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
    # Only turn 0 ran: the loop stopped before asking for another turn.
    assert len(router.requests) == 1


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
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # cancel at the second turn boundary

    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval(), should_cancel=should_cancel)
    assert outcome.bounded_by == "stopped"
    assert len(outcome.passages) == 4  # turn 0's admissions are checkpointed


async def test_retrieval_failure_is_observed_and_loop_continues() -> None:
    router = _TurnRouter([_TURN0, _READY])
    with pytest.raises(
        ResearchAgentError, match="(without usable evidence|malformed research turns)"
    ):
        await _run(router, _FailingRetrieval())


async def test_empty_retrieval_is_an_error_not_a_dead_end_report() -> None:
    router = _TurnRouter([_TURN0, _READY])
    with pytest.raises(
        ResearchAgentError, match="(without usable evidence|malformed research turns)"
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


async def test_done_action_is_a_model_contract_error() -> None:
    router = _TurnRouter(['{"action": "done", "reason": "enough"}'] * 6)
    with pytest.raises(ResearchAgentError, match="malformed research turns"):
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


async def test_zero_yield_reason_reaches_the_next_turn() -> None:
    """A no-hit turn gives the lead a concrete pivot signal in its next prompt."""
    turn = _TURN0
    ready = _READY.replace(
        '"covered": [{"angle": "overview", "evidence_ids": ["p0", "p1", "p10", "p11"]}]',
        '"covered": []',
    )
    router = _TurnRouter([turn, ready])
    outcome, _ = await _run(
        router,
        _FakeRetrieval(batches=[[]]),
        bound=_bound(minimum_useful_sources=1),
        resume_passages=[_passage(99)],
        resume_trail=[{"kind": "search", "turn": 0, "query": "old", "admitted": 1}],
    )
    assert outcome.bounded_by is None
    assert "no_hits" in router.requests[1].messages[-1].content
    summary = [entry for entry in outcome.trail if entry.get("kind") == "search_turn_summary"]
    assert summary and summary[-1]["yield_reasons"] == ["no_hits"]


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
    assert captured == [("brief", {"text": "The question asks about X."})]


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
    router = _TurnRouter(["not json", _TURN0, _READY])

    await _run(router, _FakeRetrieval())

    assert all(request.response_format == "json" for request in router.requests)
    assert all(request.enable_thinking is False for request in router.requests)
    retry = router.requests[1]
    assert not any(message.role == "assistant" for message in retry.messages)

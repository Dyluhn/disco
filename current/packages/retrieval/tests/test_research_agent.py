"""Hermetic tests for the v2 agentic research loop (`deep_research.agent`).

Scripted router + fake retrieval engine (the same doubles pattern as
`test_deep_research.py`): the turn protocol (brief parse, search execution,
done), malformed-JSON re-ask with precise feedback, the live budget line and
wrap-up warning, steer/inject drains, the Stop checkpoint, and the dead_end
flag semantics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from disco.core.llm import (
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    StreamChunk,
    TokenUsage,
)
from disco.retrieval.deep_research import agent as agent_mod
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
    'numbers, and independent criticism.", "action": "search", '
    '"queries": ["X overview", "X criticism"]}'
)
_DONE = '{"action": "done", "reason": "the evidence covers every angle"}'


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
    assert all(
        {"subquestion", "query", "round", "rounds_max"} <= set(s) for s in searches
    )
    assert all(o["ok"] and "added" in o and "remaining_budget" in o for o in observations)

    # Clean termination: model done → bounded_by None, dead_end False, and
    # the trail carries searches with admitted counts + the done reason.
    assert outcome.bounded_by is None and outcome.dead_end is False
    search_entries = [e for e in outcome.trail if e.get("kind") == "search"]
    assert [e["query"] for e in search_entries] == ["X overview", "X criticism"]
    assert all(e["admitted"] == 2 for e in search_entries)
    assert outcome.trail[-1] == {"kind": "done", "reason": "the evidence covers every angle"}


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
    outcome, _ = await _run(router, _FakeRetrieval())
    assert outcome.bounded_by == "wall_clock"
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
    outcome, _ = await _run(
        router, _FakeRetrieval(), pop_injected_sources=pop_injected
    )
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
    assert outcome.dead_end is False  # stopped is never a dead end


async def test_retrieval_failure_is_observed_and_loop_continues() -> None:
    router = _TurnRouter([_TURN0, _DONE])
    outcome, captured = await _run(router, _FailingRetrieval())
    failures = [p for k, p in captured if k == "observation" and not p["ok"]]
    assert len(failures) == 2
    assert all("ConnectionError" in f["detail"] for f in failures)
    # Model then called done over an empty pool → dead end.
    assert outcome.bounded_by is None
    assert outcome.dead_end is True


async def test_dead_end_only_when_terminal_and_pool_empty() -> None:
    # Empty retrieval results + model done → dead_end True.
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(router, _FakeRetrieval(batches=[[]]))
    assert outcome.passages == []
    assert outcome.dead_end is True
    # Same retrieval, but a non-empty pool via resume seed → not a dead end.
    router = _TurnRouter([_TURN0, _DONE])
    outcome, _ = await _run(
        router,
        _FakeRetrieval(batches=[[]]),
        resume_passages=[_passage(99)],
    )
    assert outcome.dead_end is False


async def test_resume_trail_and_passages_seed_the_run() -> None:
    router = _TurnRouter([_TURN0, _DONE])
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

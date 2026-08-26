"""Whole-execution source and wall-clock bounds for Deep Research.

Two hard budgets bound a run, and both bound RESEARCH only:

  * the SOURCE budget is one shared, atomically-charged allowance for the whole
    execution — never partitioned per query, and never consumed by extraction
    furniture;
  * the research WALL CLOCK is the tier's wall clock minus a tier-proportional
    writing reserve, so research can never starve the writer. When it trips,
    the agent loop stops at its next turn boundary and the report is written
    normally from the evidence already gathered. The writing phase itself
    carries no deadline checks — a slow writer call is never cancelled by it.
"""

from __future__ import annotations

import asyncio
import re
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
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research.agent import research_seconds_left, run_research_agent
from disco.retrieval.deep_research.depth import DepthBound
from disco.retrieval.deep_research.engine import DeepResearchRun
from disco.retrieval.models import Passage, RetrievalRequest, RetrievalResult, SearchHit
from disco.retrieval.vectorstore import InMemoryVectorStore

# ---- doubles ----------------------------------------------------------------

_THREE_QUERIES = (
    '{"brief": "Three angles are worth opening at once.", "decision_summary": '
    '"Open the angles in parallel.", "coverage": {"covered": [], "open": '
    '["angle one", "angle two", "angle three"], "contradictions_checked": '
    '[]}, "queries": ["angle one", "angle two", "angle three"], '
    '"ready_to_write": false}'
)
_ONE_QUERY = (
    '{"brief": "One angle to open.", "decision_summary": "Open the angle.", '
    '"coverage": {"covered": [], "open": ["angle one"], '
    '"contradictions_checked": []}, "queries": ["angle one"], '
    '"ready_to_write": false}'
)
_DONE = (
    '{"brief": "The evidence is sufficient.", "decision_summary": '
    '"The gathered evidence covers the requested angles.", "coverage": '
    '{"covered": [], "open": [], "contradictions_checked": []}, '
    '"queries": [], "ready_to_write": true}'
)
_CLEAN_REVIEW = '{"passes": true, "failures": []}'
_EVIDENCE_ID = re.compile(r"(?m)^\[([\w-]+)\] ")


def _auto_report(prompt: str) -> str:
    """A grounded whole-report draft citing the ids in the writer's own
    evidence block (see `test_deep_research.py` for the same helper)."""
    ids = list(dict.fromkeys(_EVIDENCE_ID.findall(prompt)))[:2]
    if not ids:
        return ""
    body = " ".join(
        "The measured evidence documents the research question with collected "
        f"data and reported figures [[{passage_id}]]."
        for passage_id in ids
    )
    return f"{body} {body}\n\n## Convergent findings\n{body} {body}"


class _RunRouter(LLMRouter):
    """Turn queue for the research loop + auto-grounded report/clean review for
    the writer. `writer_delay` makes every WRITING call slow, so a test can
    prove the writing phase is not deadline-bounded."""

    def __init__(self, turns: list[str], *, writer_delay: float = 0.0) -> None:
        self._turns = list(turns)
        self._writer_delay = writer_delay
        self.calls: list[str] = []

    async def complete(
        self, request: CompletionRequest, *, context: Any = None
    ) -> CompletionResponse:
        del context
        joined = "\n".join(message.content for message in request.messages)
        if "FIXED rubric" in joined:
            kind, text = "review", _CLEAN_REVIEW
        elif "WRITE THE REPORT" in joined:
            kind, text = "report", _auto_report(joined)
        else:
            kind = "turn"
            text = self._turns.pop(0) if self._turns else _DONE
        if kind != "turn" and self._writer_delay:
            await asyncio.sleep(self._writer_delay)
        self.calls.append(kind)
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


def _passage(passage_id: str) -> Passage:
    return Passage(
        id=passage_id,
        text=(
            f"Substantive measured evidence for {passage_id} documents the "
            "research question with collected data and reported figures."
        ),
        source_url=f"https://example.test/{passage_id}",
        source_title=passage_id,
    )


class _BatchEngine:
    """Returns a fresh batch of `per_query` passages for every query, and
    records the requests so a test can inspect what each concurrent query was
    issued."""

    def __init__(self, per_query: int = 3, *, stub_first: bool = False) -> None:
        self.per_query = per_query
        self.stub_first = stub_first
        self.requests: list[RetrievalRequest] = []
        self._calls = 0

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        call = self._calls
        self._calls += 1
        await asyncio.sleep(0)  # let sibling queries of the same turn interleave
        passages = [_passage(f"leg-{call}-{index}") for index in range(self.per_query)]
        if self.stub_first:
            passages.insert(
                0,
                Passage(
                    id=f"stub-{call}",
                    text="Published: 2024",
                    source_url=f"https://example.test/stub-{call}",
                    source_title="stub",
                ),
            )
        hits = [
            SearchHit(url=p.source_url, title=p.source_title, snippet="s", rank=i)
            for i, p in enumerate(passages)
        ]
        return RetrievalResult(
            passages=passages, all_hits=hits, extracted=[], issued_queries=[request.query]
        )


async def _noop_emit(kind: str, payload: dict[str, object]) -> None:
    del kind, payload


class _NLI:
    def entail(self, premise: str, hypothesis: str) -> str:
        if not premise or not hypothesis:
            return "neutral"
        return (
            "entail"
            if set(premise.lower().split()) & set(hypothesis.lower().split())
            else "neutral"
        )

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if self.entail(premise, hypothesis) == "entail" else 0.0


def _bound(**overrides: Any) -> DepthBound:
    values: dict[str, Any] = dict(
        max_sources=12,
        max_rounds_per_subq=2,
        max_wall_clock_s=600,
        max_subquestions=6,
        discover_limit=8,
        extract_cap=4,
        rerank_top_k=6,
        report_min_words=40,
        report_max_words=4_000,
    )
    values.update(overrides)
    return DepthBound(**values)


def _run(router: LLMRouter, engine: Any, bound: DepthBound) -> DeepResearchRun:
    run = DeepResearchRun(
        query="question",
        router=router,
        retrieval_engine=engine,
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_NLI(),
    )
    run._bound = bound
    return run


# ---- the shared source budget ------------------------------------------------


async def test_concurrent_legs_share_one_atomic_source_budget() -> None:
    """One turn's concurrent retrievals charge ONE budget: nine candidate
    passages against a four-source cap admit exactly four, and the run is then
    bounded by sources — not four per query."""
    engine = _BatchEngine(per_query=3)
    outcome = await run_research_agent(
        "question",
        router=_RunRouter([_THREE_QUERIES]),
        retrieval_engine=engine,
        bound=_bound(max_sources=4),
        namespace="run",
        emit=_noop_emit,
    )
    assert len(engine.requests) == 3  # all three queries ran
    assert len({p.id for p in outcome.passages}) == 4  # …into one shared cap
    assert outcome.bounded_by == "sources"


async def test_budget_partitioning() -> None:
    """The source cap is shared, never partitioned: every query in a turn is
    issued against the WHOLE remaining allowance (cap 3 → each request sized 3),
    and global admission is what enforces the total."""
    engine = _BatchEngine(per_query=3)
    outcome = await run_research_agent(
        "question",
        router=_RunRouter([_THREE_QUERIES]),
        retrieval_engine=engine,
        bound=_bound(max_sources=3, rerank_top_k=6, extract_cap=6),
        namespace="run",
        emit=_noop_emit,
    )
    # Each concurrent query saw the full remaining cap (3), not cap/3.
    assert [request.top_k for request in engine.requests] == [3, 3, 3]
    assert [request.extract_cap for request in engine.requests] == [3, 3, 3]
    # …and the shared budget still admitted only three passages in total.
    assert len(outcome.passages) == 3
    assert outcome.bounded_by == "sources"


def test_shared_duplicate_does_not_hide_later_new_source() -> None:
    """A passage the budget already owns costs zero and must not consume the
    caller's local charge allowance."""
    shared = SourceBudget(3)
    already_shared = _passage("shared")
    assert shared.admit([already_shared]) == ([already_shared], 1)

    admitted, charged = shared.admit(
        [already_shared, _passage("new"), _passage("later")], charge_limit=1
    )

    assert [passage.id for passage in admitted] == ["shared", "new"]
    assert charged == 1
    assert shared.remaining == 1


async def test_unusable_extraction_stub_does_not_exhaust_source_budget() -> None:
    """Extraction furniture is filtered BEFORE it can consume source capacity,
    so a one-source budget is spent on the substantive passage."""
    engine = _BatchEngine(per_query=1, stub_first=True)
    outcome = await run_research_agent(
        "question",
        router=_RunRouter([_ONE_QUERY]),
        retrieval_engine=engine,
        bound=_bound(max_sources=1),
        namespace="run",
        emit=_noop_emit,
    )
    assert [passage.id for passage in outcome.passages] == ["leg-0-0"]


# ---- the research wall clock reserves the writing budget ---------------------


def test_gather_deadline_preserves_the_report_writing_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Research seconds are the tier wall clock MINUS the writing reserve, so
    a long research phase can never starve the report writer."""
    bound = _bound(max_wall_clock_s=100, max_subquestions=16, report_min_words=4_000)
    monkeypatch.setattr(agent_mod, "_monotonic", lambda: 100.0)
    # The standard-depth nine-section reserve is capped at 40% of this
    # synthetic 100-second tier, leaving 50 research seconds ten seconds in.
    assert research_seconds_left(bound, started=90.0) == 50


async def test_wall_clock_does_not_cancel_slow_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once writing starts there are no deadline checks: writer calls that run
    many times the whole tier wall clock still complete and their full prose
    reaches the report, with the research bound reported honestly."""
    # `started` and turn 0's check read 0; every later read is far past the cap.
    clock = iter([0.0, 0.0])
    monkeypatch.setattr(agent_mod, "_monotonic", lambda: next(clock, 10_000.0))
    router = _RunRouter([_ONE_QUERY], writer_delay=0.3)  # 6x the whole wall clock
    run = _run(router, _BatchEngine(per_query=2), _bound(max_wall_clock_s=0.05))

    report = await run.run(emit=_noop_emit)

    assert report.bounded_by == "wall_clock"  # research stopped at its deadline
    assert router.calls == ["turn", "report", "review"]  # …writing ran to completion
    assert [section.title for section in report.sections] == ["Convergent findings"]
    assert report.sections[0].markdown.strip()
    assert report.summary.strip()

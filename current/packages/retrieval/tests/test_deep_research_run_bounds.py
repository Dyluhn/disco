"""Whole-execution source and wall-clock bounds for Deep Research."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from disco.core import ReportSection
from disco.core.llm import CallContext
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research.decompose import SubQuestion
from disco.retrieval.deep_research.depth import DepthBound
from disco.retrieval.deep_research.engine import DeepResearchRun
from disco.retrieval.deep_research.gather import (
    GatherLegContext,
    SubQuestionResult,
    _admit_fresh_passages,
    gather_for_subquestion,
)
from disco.retrieval.models import Passage, RetrievalRequest, RetrievalResult
from disco.retrieval.vectorstore import InMemoryVectorStore


def _passage(passage_id: str) -> Passage:
    return Passage(
        id=passage_id,
        text=f"Evidence for {passage_id} is available.",
        source_url=f"https://example.test/{passage_id}",
        source_title=passage_id,
    )


class _BarrierEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        call = self.calls
        self.calls += 1
        await asyncio.sleep(0)
        passages = [_passage(f"leg-{call}-{index}") for index in range(3)]
        return RetrievalResult(
            passages=passages,
            all_hits=[],
            extracted=[],
            issued_queries=[],
        )


async def _noop_emit(kind: str, payload: dict[str, object]) -> None:
    return None


@pytest.mark.asyncio
async def test_concurrent_legs_share_one_atomic_source_budget() -> None:
    budget = SourceBudget(3)
    bound = DepthBound(3, 1, 60, 2, 3, 3, 3)
    engine = _BarrierEngine()

    async def gather(index: int) -> SubQuestionResult:
        subq = SubQuestion(title=f"question {index}")
        return await gather_for_subquestion(
            subq,
            engine=engine,
            router=AsyncMock(),
            embedder=None,
            vector_store=InMemoryVectorStore(),
            namespace=f"run/{index}",
            bound=bound,
            emit=_noop_emit,
            remaining_source_budget=3,
            source_budget=budget,
            leg_context=GatherLegContext(
                subq_id=f"s{index}",
                namespace=f"run/{index}",
                call_context=CallContext(conversation_id=f"run/s{index}"),
            ),
        )

    results = await asyncio.gather(gather(1), gather(2))
    admitted = {passage.id for result in results for passage in result.passages}
    assert budget.used == 3
    assert len(admitted) == 3
    assert any(result.bounded_by_sources for result in results)


def test_shared_duplicate_does_not_hide_later_new_source() -> None:
    """A sibling-owned first result costs zero and must not consume the local slot."""
    shared = SourceBudget(3)
    already_shared = _passage("shared")
    assert shared.admit([already_shared]) == ([already_shared], 1)
    seen_by_leg: set[str] = set()
    retrieval = RetrievalResult(
        passages=[already_shared, _passage("new"), _passage("later")],
        all_hits=[],
        extracted=[],
        issued_queries=[],
    )

    admitted, charged = _admit_fresh_passages(retrieval, seen_by_leg, 1, shared)

    assert [passage.id for passage in admitted] == ["shared", "new"]
    assert charged == 1
    assert seen_by_leg == {"shared", "new"}
    assert shared.remaining == 1


def _run() -> DeepResearchRun:
    return DeepResearchRun(
        query="question",
        router=AsyncMock(),
        retrieval_engine=AsyncMock(),
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=AsyncMock(),
    )


def _tight_deadline(run: DeepResearchRun) -> None:
    run._bound = replace(run.bound, max_wall_clock_s=0.05)


@pytest.mark.asyncio
async def test_wall_clock_cancels_slow_gather_and_returns_partial_report() -> None:
    run = _run()
    _tight_deadline(run)

    async def slow_gather(subq: SubQuestion, **kwargs: object) -> SubQuestionResult:
        await asyncio.sleep(1)
        return SubQuestionResult(subq=subq)

    with patch(
        "disco.retrieval.deep_research.engine.gather_for_subquestion",
        side_effect=slow_gather,
    ):
        report = await run.run(["slow gather"], emit=_noop_emit)

    assert report.bounded_by == "wall_clock"
    assert report.sections == []
    assert "time limit" in report.summary


@pytest.mark.asyncio
async def test_wall_clock_drains_sibling_cleanup_and_overrides_plan_width_bound() -> None:
    run = _run()
    run._bound = replace(run.bound, max_subquestions=2, max_wall_clock_s=0.05)
    sibling_started = asyncio.Event()
    sibling_cleaned = asyncio.Event()

    async def slow_gather(subq: SubQuestion, **kwargs: object) -> SubQuestionResult:
        if subq.title == "second":
            sibling_started.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            if subq.title == "second":
                await asyncio.sleep(0)
                sibling_cleaned.set()
            raise
        return SubQuestionResult(subq=subq)

    with patch(
        "disco.retrieval.deep_research.engine.gather_for_subquestion",
        side_effect=slow_gather,
    ):
        report = await run.run(["first", "second", "trimmed"], emit=_noop_emit)

    assert sibling_started.is_set()
    assert sibling_cleaned.is_set()
    assert report.bounded_by == "wall_clock"


@pytest.mark.asyncio
async def test_wall_clock_cancels_slow_synthesis_and_returns_partial_report() -> None:
    run = _run()
    _tight_deadline(run)

    async def gathered(subq: SubQuestion, **kwargs: object) -> SubQuestionResult:
        return SubQuestionResult(subq=subq)

    async def slow_synthesis(*args: object, **kwargs: object) -> ReportSection:
        await asyncio.sleep(1)
        return ReportSection(id="late", title="late", markdown="late")

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=gathered,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=slow_synthesis,
        ),
    ):
        report = await run.run(["slow synthesis"], emit=_noop_emit)

    assert report.bounded_by == "wall_clock"
    assert report.sections == []
    assert "time limit" in report.summary


@pytest.mark.asyncio
async def test_wall_clock_keeps_completed_section_when_coherence_times_out() -> None:
    run = _run()
    _tight_deadline(run)

    async def gathered(subq: SubQuestion, **kwargs: object) -> SubQuestionResult:
        return SubQuestionResult(subq=subq)

    async def synthesized(*args: object, **kwargs: object) -> ReportSection:
        return ReportSection(id="s1", title="done", markdown="Grounded section.")

    async def slow_coherence(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(1)
        return "late"

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=gathered,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=synthesized,
        ),
        patch(
            "disco.retrieval.deep_research.engine.coherence_pass",
            side_effect=slow_coherence,
        ),
    ):
        report = await run.run(["done"], emit=_noop_emit)

    assert report.bounded_by == "wall_clock"
    assert [section.title for section in report.sections] == ["done"]
    assert "time limit" in report.summary

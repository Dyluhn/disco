"""Whole-execution source and wall-clock bounds for Deep Research.

The research wall clock (with its writing reserve) bounds GATHERING only:
when it trips, still-running gather legs are cancelled, `bounded_by=
"wall_clock"` is recorded, and the report is then written normally from the
evidence already gathered. Writing carries no deadline checks — a slow
writing-phase call is never cancelled by the wall clock.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from disco.core import ReportSection
from disco.core.llm import CallContext
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._engine_parts._drain import seconds_left as drain_seconds_left
from disco.retrieval.deep_research.decompose import SubQuestion
from disco.retrieval.deep_research.depth import DepthBound
from disco.retrieval.deep_research.engine import DeepResearchRun
from disco.retrieval.deep_research.gather import (
    GatherLegContext,
    SubQuestionResult,
    _admit_fresh_passages,
    gather_for_subquestion,
)
from disco.retrieval.deep_research.report_compiler import ReportSectionSpec
from disco.retrieval.models import Passage, RetrievalRequest, RetrievalResult
from disco.retrieval.vectorstore import InMemoryVectorStore


def _passage(passage_id: str) -> Passage:
    return Passage(
        id=passage_id,
        text=f"Substantive source evidence for {passage_id} is available for review.",
        source_url=f"https://example.test/{passage_id}",
        source_title=passage_id,
    )


class _NLI:
    def entail(self, premise: str, hypothesis: str) -> str:
        return "entail" if premise and hypothesis else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if premise and hypothesis else 0.0


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


def test_unusable_extraction_stub_does_not_exhaust_source_budget() -> None:
    shared = SourceBudget(1)
    retrieval = RetrievalResult(
        passages=[
            _passage("good"),
            Passage(
                id="stub",
                text="Published: 2024",
                source_url="https://example.test/stub",
                source_title="stub",
            ),
        ],
        all_hits=[],
        extracted=[],
        issued_queries=[],
    )

    admitted, charged = _admit_fresh_passages(retrieval, set(), 1, shared)

    assert [passage.id for passage in admitted] == ["good"]
    assert charged == 1
    assert shared.used == 1


def _run() -> DeepResearchRun:
    return DeepResearchRun(
        query="question",
        router=AsyncMock(),
        retrieval_engine=AsyncMock(),
        embedder=None,
        vector_store=InMemoryVectorStore(),
        nli=_NLI(),
    )


def _tight_deadline(run: DeepResearchRun) -> None:
    run._bound = replace(run.bound, max_wall_clock_s=0.05)


def test_gather_deadline_preserves_the_report_writing_reserve() -> None:
    run = _run()
    run._bound = replace(
        run.bound,
        max_wall_clock_s=100,
        report_min_words=4_000,
    )

    with patch(
        "disco.retrieval.deep_research._engine_parts._drain.time.monotonic",
        return_value=100,
    ):
        # The standard-depth nine-section reserve is capped at 40% of this
        # synthetic 100-second tier, leaving 50 research seconds ten seconds in.
        assert drain_seconds_left(run, started=90) == 50


# ---- the wall clock bounds gathering only -----------------------------------
#
# The writing stages below are patched at the engine module (where the run
# resolves them late) so these tests exercise the run's control flow: which
# phase the wall clock can cancel, and which it must never touch.


async def _compiled(
    *args: object, **kwargs: object
) -> tuple[list[ReportSectionSpec], list[Passage]]:
    return [ReportSectionSpec("r0", "Finding", ("p0",))], [_passage("p0")]


async def _written_section(*args: object, **kwargs: object) -> ReportSection:
    return ReportSection(
        id="r0",
        title="Finding",
        markdown="Grounded section [[p0]].",
        cited_passage_ids=["p0"],
    )


async def _written_summary(*args: object, **kwargs: object) -> str:
    return "Executive summary [[p0]]."


@pytest.mark.asyncio
async def test_wall_clock_cancels_slow_gather_and_report_still_compiles() -> None:
    """A gather leg that outlives the research deadline is cancelled; the
    report is then compiled normally from the sibling leg's evidence with
    bounded_by='wall_clock' — no degraded or manufactured prose."""
    run = _run()
    _tight_deadline(run)
    cancelled = asyncio.Event()

    async def gather(subq: SubQuestion, **kwargs: object) -> SubQuestionResult:
        if subq.title == "slow":
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return SubQuestionResult(subq=subq)
        return SubQuestionResult(subq=subq, passages=[_passage("p0")])

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=gather,
        ),
        patch(
            "disco.retrieval.deep_research.engine.compile_report_plan",
            side_effect=_compiled,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=_written_section,
        ),
        patch(
            "disco.retrieval.deep_research.engine.coherence_pass",
            side_effect=_written_summary,
        ),
    ):
        report = await run.run(["evidence", "slow"], emit=_noop_emit)

    assert cancelled.is_set()
    assert report.bounded_by == "wall_clock"
    assert [section.title for section in report.sections] == ["Finding"]
    assert report.summary == "Executive summary [[p0]]."


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

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=slow_gather,
        ),
        patch(
            "disco.retrieval.deep_research.engine.compile_report_plan",
            side_effect=_compiled,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=_written_section,
        ),
        patch(
            "disco.retrieval.deep_research.engine.coherence_pass",
            side_effect=_written_summary,
        ),
    ):
        report = await run.run(["first", "second", "trimmed"], emit=_noop_emit)

    assert sibling_started.is_set()
    assert sibling_cleaned.is_set()
    assert report.bounded_by == "wall_clock"


@pytest.mark.asyncio
async def test_wall_clock_does_not_cancel_slow_synthesis() -> None:
    """Once writing starts there are no deadline checks: a section synthesis
    that runs several times the whole tier wall clock still completes and its
    full prose reaches the report."""
    run = _run()
    _tight_deadline(run)

    async def gather(subq: SubQuestion, **kwargs: object) -> SubQuestionResult:
        if subq.title == "slow":
            await asyncio.sleep(1)
            return SubQuestionResult(subq=subq)
        return SubQuestionResult(subq=subq, passages=[_passage("p0")])

    async def slow_synthesis(*args: object, **kwargs: object) -> ReportSection:
        await asyncio.sleep(0.3)  # 6x the entire 0.05s wall clock
        return await _written_section()

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=gather,
        ),
        patch(
            "disco.retrieval.deep_research.engine.compile_report_plan",
            side_effect=_compiled,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=slow_synthesis,
        ),
        patch(
            "disco.retrieval.deep_research.engine.coherence_pass",
            side_effect=_written_summary,
        ),
    ):
        report = await run.run(["evidence", "slow"], emit=_noop_emit)

    assert report.bounded_by == "wall_clock"
    assert [section.title for section in report.sections] == ["Finding"]
    assert report.sections[0].markdown == "Grounded section [[p0]]."
    assert report.summary == "Executive summary [[p0]]."


@pytest.mark.asyncio
async def test_wall_clock_does_not_cancel_slow_coherence() -> None:
    """The coherence summary is a writing-phase call: it runs to completion
    even though the research deadline expired long before, and the completed
    section is kept alongside it."""
    run = _run()
    _tight_deadline(run)

    async def gather(subq: SubQuestion, **kwargs: object) -> SubQuestionResult:
        if subq.title == "slow":
            await asyncio.sleep(1)
            return SubQuestionResult(subq=subq)
        return SubQuestionResult(subq=subq, passages=[_passage("p0")])

    async def slow_coherence(*args: object, **kwargs: object) -> str:
        await asyncio.sleep(0.3)  # 6x the entire 0.05s wall clock
        return "Late but complete summary [[p0]]."

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=gather,
        ),
        patch(
            "disco.retrieval.deep_research.engine.compile_report_plan",
            side_effect=_compiled,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=_written_section,
        ),
        patch(
            "disco.retrieval.deep_research.engine.coherence_pass",
            side_effect=slow_coherence,
        ),
    ):
        report = await run.run(["evidence", "slow"], emit=_noop_emit)

    assert report.bounded_by == "wall_clock"
    assert [section.title for section in report.sections] == ["Finding"]
    assert report.summary == "Late but complete summary [[p0]]."

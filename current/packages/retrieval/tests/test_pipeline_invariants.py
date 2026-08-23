import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from disco.core import ReportSection
from disco.retrieval.deep_research import DeepResearchRun
from disco.retrieval.deep_research.gather import SubQuestionResult
from disco.retrieval.deep_research.report_compiler import (
    ReportCompilationError,
    ReportSectionSpec,
)
from disco.retrieval.models import Passage


class _NLI:
    def entail(self, premise: str, hypothesis: str) -> str:
        return "entail" if premise and hypothesis else "neutral"

    def score(self, premise: str, hypothesis: str) -> float:
        return 1.0 if premise and hypothesis else 0.0


@pytest.mark.asyncio
async def test_pipeline_invariants():
    """RP-04: Verify concurrency, serial synthesis, and race fixes (hash-based IDs)."""

    gather_calls = []
    synth_calls = []

    async def mock_gather(subq, **kwargs):
        start = time.monotonic()
        gather_calls.append((subq.title, "start", start))
        # Wait enough to ensure overlap with other gathers and following synths
        await asyncio.sleep(0.2)
        end = time.monotonic()
        gather_calls.append((subq.title, "end", end))
        return SubQuestionResult(
            subq=subq,
            passages=[
                Passage(
                    id=f"p_{subq.title}",
                    text="Substantive source evidence for the final report section.",
                    source_url="http://example.com",
                    source_title="Source",
                    score=0.9,
                    metadata={},
                )
            ],
            all_hits=[],
            rounds_run=1,
            issued_queries=[subq.title],
            bounded_by_rounds=False,
        )

    async def mock_synth(sub_result, **kwargs):
        subq_title = sub_result.subq.title
        start = time.monotonic()
        synth_calls.append((subq_title, "start", start))
        await asyncio.sleep(0.05)  # Simulate serial LLM work
        end = time.monotonic()
        synth_calls.append((subq_title, "end", end))
        return ReportSection(
            id=kwargs.get("section_id", "s1"),
            title=subq_title,
            markdown=f"Grounded body [[{sub_result.passages[0].id}]].",
            cited_passage_ids=[p.id for p in sub_result.passages],
            unsupported_count=0,
        )

    # Mock dependencies for DeepResearchRun
    router = AsyncMock()
    engine = AsyncMock()
    embedder = AsyncMock()
    vector_store = AsyncMock()
    nli = _NLI()

    run = DeepResearchRun(
        query="test query",
        router=router,
        retrieval_engine=engine,
        embedder=embedder,
        vector_store=vector_store,
        nli=nli,
        conversation_id="test_conv",
    )
    run._bound = replace(
        run._bound,
        min_evidence_passages=3,
        min_evidence_themes=1,
        min_evidence_sources=1,
    )

    plan_steps = ["Q1", "Q2", "Q3"]
    captured_events = []

    async def emit(kind, payload):
        captured_events.append((kind, payload))

    async def mock_compile(_query, results, **_kwargs):
        evidence = [passage for result in results for passage in result.passages]
        specs = [
            ReportSectionSpec(
                id=f"r{index}",
                title=f"Evidence-led finding {index + 1}",
                evidence_ids=(result.passages[0].id,),
            )
            for index, result in enumerate(results)
        ]
        return specs, evidence

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=mock_gather,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=mock_synth,
        ),
        patch(
            "disco.retrieval.deep_research.engine.coherence_pass",
            return_value="summary",
        ),
        patch(
            "disco.retrieval.deep_research.engine.compile_report_plan",
            side_effect=mock_compile,
        ),
    ):
        report = await run.run(plan_steps, emit=emit)

    # 1. Assert Concurrency (Pipeline)
    # All gathers should start nearly at the same time, before Q1 synthesis finishes.
    gather_starts = {title: t for title, event, t in gather_calls if event == "start"}
    first_synth_start = next(
        t
        for title, event, t in synth_calls
        if title == "Evidence-led finding 1" and event == "start"
    )

    # Check that all gathers started before the first synthesis
    # (since they are tasks started upfront)
    for title in ["Q1", "Q2", "Q3"]:
        assert gather_starts[title] < first_synth_start, (
            f"Gather for {title} should have started before report synthesis"
        )

    # 2. Assert Serial Synthesis
    # Synthesis calls should NOT overlap.
    for i in range(len(plan_steps) - 1):
        q_curr = f"Evidence-led finding {i + 1}"
        q_next = f"Evidence-led finding {i + 2}"
        curr_end = next(t for title, event, t in synth_calls if title == q_curr and event == "end")
        next_start = next(
            t for title, event, t in synth_calls if title == q_next and event == "start"
        )
        assert next_start >= curr_end, f"Synthesis for {q_next} started before {q_curr} finished"

    # 3. Report ids come from the post-research compiler, not probe hashes.
    assert [section.id for section in report.sections] == ["r0", "r1", "r2"]

    # 4. Assert section_done events order
    done_events = [p for k, p in captured_events if k == "section_done"]
    assert len(done_events) == 3
    assert [event["title"] for event in done_events] == [
        "Evidence-led finding 1",
        "Evidence-led finding 2",
        "Evidence-led finding 3",
    ]


@pytest.mark.asyncio
async def test_budget_partitioning():
    """Every probe sees the shared cap; SourceBudget performs global admission.

    Every gather leg returns EMPTY evidence, so after research exhausts its
    bounded probes the evidence pool is empty and the run must surface the
    ERROR outcome (`compile_report_plan` raises ReportCompilationError) —
    never a fallback report."""

    partitioned_budgets = {}

    async def mock_gather(subq, **kwargs):
        partitioned_budgets[subq.title] = kwargs.get("remaining_source_budget")
        return SubQuestionResult(
            subq=subq,
            passages=[],
            all_hits=[],
            rounds_run=1,
            issued_queries=[subq.title],
            bounded_by_rounds=False,
        )

    # Mock dependencies
    router = AsyncMock()
    engine = AsyncMock()
    embedder = AsyncMock()
    vector_store = AsyncMock()
    nli = _NLI()

    run = DeepResearchRun(
        query="test query",
        router=router,
        retrieval_engine=engine,
        embedder=embedder,
        vector_store=vector_store,
        nli=nli,
        depth="standard_deep",  # max_sources=90
    )

    plan_steps = ["Q1", "Q2", "Q3"]

    async def emit(kind, payload):
        pass

    with patch(
        "disco.retrieval.deep_research.engine.gather_for_subquestion",
        side_effect=mock_gather,
    ):
        with pytest.raises(ReportCompilationError):
            await run.run(plan_steps, emit=emit)

    assert set(plan_steps) <= set(partitioned_budgets)
    assert {f"{query} primary sources evidence" for query in plan_steps} <= set(
        partitioned_budgets
    )
    assert len(partitioned_budgets) <= run._bound.max_subquestions
    assert not any(
        "primary sources evidence primary sources evidence" in query
        for query in partitioned_budgets
    )
    assert set(partitioned_budgets.values()) == {90}


@pytest.mark.asyncio
async def test_resume_semantics_with_pipeline():
    """RP-04: Resume skips completed probes and recompiles globally.

    A Stop checkpoint carries evidence + probe state, never sections; on
    resume the completed probe is NOT re-gathered and the final report is
    compiled once, globally, from the combined evidence pool (compiler-owned
    headings, not probe titles)."""

    gather_calls = []

    async def mock_gather(subq, **kwargs):
        gather_calls.append(subq.title)
        return SubQuestionResult(
            subq=subq,
            passages=[
                Passage(
                    id=f"p_{subq.title}",
                    text="Substantive source evidence for the resumed report section.",
                    source_url=f"https://example.test/{subq.title}",
                    source_title="Source",
                )
            ],
            all_hits=[],
            rounds_run=1,
            issued_queries=[subq.title],
            bounded_by_rounds=False,
        )

    async def mock_synth(sub_result, **kwargs):
        return ReportSection(
            id=kwargs.get("section_id", "s1"),
            title=sub_result.subq.title,
            markdown=f"Grounded body [[{sub_result.passages[0].id}]].",
            cited_passage_ids=[p.id for p in sub_result.passages],
            unsupported_count=0,
        )

    async def mock_compile(_query, results, **_kwargs):
        evidence = [passage for result in results for passage in result.passages]
        specs = [
            ReportSectionSpec(
                id=f"r{index}",
                title=f"Evidence-led finding {index + 1}",
                evidence_ids=(result.passages[0].id,),
            )
            for index, result in enumerate(results)
        ]
        return specs, evidence

    run = DeepResearchRun(
        query="q",
        router=AsyncMock(),
        retrieval_engine=AsyncMock(),
        embedder=AsyncMock(),
        vector_store=AsyncMock(),
        nli=_NLI(),
    )
    run._bound = replace(
        run._bound,
        min_evidence_passages=2,
        min_evidence_themes=1,
        min_evidence_sources=1,
    )

    plan_steps = ["Q1", "Q2", "Q3"]

    async def emit(kind, payload):
        pass

    with (
        patch(
            "disco.retrieval.deep_research.engine.gather_for_subquestion",
            side_effect=mock_gather,
        ),
        patch(
            "disco.retrieval.deep_research.engine.synthesize_section",
            side_effect=mock_synth,
        ),
        patch(
            "disco.retrieval.deep_research.engine.coherence_pass",
            return_value="summary",
        ),
        patch(
            "disco.retrieval.deep_research.engine.compile_report_plan",
            side_effect=mock_compile,
        ),
    ):
        report = await run.run(
            plan_steps,
            emit=emit,
            # The stop checkpoint carries no sections — only probe state.
            resume_sections=[],
            resume_completed_probes=["Q1"],
        )

    # Should only gather Q2 and Q3
    assert "Q1" not in gather_calls
    assert "Q2" in gather_calls
    assert "Q3" in gather_calls
    # Recompiled globally from the pool: compiler-owned headings, and the
    # checkpointed probe is still recorded as completed.
    assert [section.title for section in report.sections] == [
        "Evidence-led finding 1",
        "Evidence-led finding 2",
    ]
    assert "Q1" in report.completed_probes
    assert report.bounded_by is None

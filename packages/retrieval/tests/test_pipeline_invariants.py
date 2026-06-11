
import asyncio
import time
import pytest
import hashlib
from perpleximanus.retrieval.deep_research import DeepResearchRun
from perpleximanus.retrieval.deep_research.gather import SubQuestionResult
from perpleximanus.retrieval.models import Passage
from perpleximanus.core import ReportSection
from unittest.mock import AsyncMock, patch

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
            passages=[Passage(id=f"p_{subq.title}", text="text", source_url="http://example.com", source_title="Source", score=0.9, metadata={})],
            all_hits=[],
            rounds_run=1,
            issued_queries=[subq.title],
            bounded_by_rounds=False
        )

    async def mock_synth(sub_result, **kwargs):
        subq_title = sub_result.subq.title
        start = time.monotonic()
        synth_calls.append((subq_title, "start", start))
        await asyncio.sleep(0.05) # Simulate serial LLM work
        end = time.monotonic()
        synth_calls.append((subq_title, "end", end))
        return ReportSection(
            id=kwargs.get("section_id", "s1"),
            title=subq_title,
            markdown="body",
            cited_passage_ids=[p.id for p in sub_result.passages],
            unsupported_count=0
        )

    # Mock dependencies for DeepResearchRun
    router = AsyncMock()
    engine = AsyncMock()
    embedder = AsyncMock()
    vector_store = AsyncMock()
    nli = AsyncMock()
    
    run = DeepResearchRun(
        query="test query",
        router=router,
        retrieval_engine=engine,
        embedder=embedder,
        vector_store=vector_store,
        nli=nli,
        conversation_id="test_conv"
    )

    plan_steps = ["Q1", "Q2", "Q3"]
    captured_events = []
    async def emit(kind, payload):
        captured_events.append((kind, payload))

    with patch("perpleximanus.retrieval.deep_research.engine.gather_for_subquestion", side_effect=mock_gather), \
         patch("perpleximanus.retrieval.deep_research.engine.synthesize_section", side_effect=mock_synth), \
         patch("perpleximanus.retrieval.deep_research.engine.coherence_pass", return_value="summary"):
        
        report = await run.run(plan_steps, emit=emit)

    # 1. Assert Concurrency (Pipeline)
    # All gathers should start nearly at the same time, before Q1 synthesis finishes.
    gather_starts = {title: t for title, event, t in gather_calls if event == "start"}
    q1_synth_start = next(t for title, event, t in synth_calls if title == "Q1" and event == "start")
    
    # Check that all gathers started before the first synthesis (since they are tasks started upfront)
    for title in ["Q1", "Q2", "Q3"]:
        assert gather_starts[title] < q1_synth_start, f"Gather for {title} should have started before Q1 synthesis"

    # 2. Assert Serial Synthesis
    # Synthesis calls should NOT overlap.
    for i in range(len(plan_steps) - 1):
        q_curr = plan_steps[i]
        q_next = plan_steps[i+1]
        curr_end = next(t for title, event, t in synth_calls if title == q_curr and event == "end")
        next_start = next(t for title, event, t in synth_calls if title == q_next and event == "start")
        assert next_start >= curr_end, f"Synthesis for {q_next} started before {q_curr} finished"

    # 3. Assert section_id is derived from hash
    for s in report.sections:
        expected_hash = hashlib.sha256(s.title.encode()).hexdigest()[:8]
        assert s.id == f"s{expected_hash}", f"Section ID mismatch for {s.title}"

    # 4. Assert section_done events order
    done_events = [p for k, p in captured_events if k == "section_done"]
    assert len(done_events) == 3
    assert done_events[0]["title"] == "Q1"
    assert done_events[1]["title"] == "Q2"
    assert done_events[2]["title"] == "Q3"

@pytest.mark.asyncio
async def test_budget_partitioning():
    """RP-04: Verify that the source budget is partitioned N-ways upfront."""
    
    partitioned_budgets = {}
    
    async def mock_gather(subq, **kwargs):
        partitioned_budgets[subq.title] = kwargs.get("remaining_source_budget")
        return SubQuestionResult(
            subq=subq,
            passages=[],
            all_hits=[],
            rounds_run=1,
            issued_queries=[subq.title],
            bounded_by_rounds=False
        )

    # Mock dependencies
    router = AsyncMock()
    engine = AsyncMock()
    embedder = AsyncMock()
    vector_store = AsyncMock()
    nli = AsyncMock()
    
    run = DeepResearchRun(
        query="test query",
        router=router,
        retrieval_engine=engine,
        embedder=embedder,
        vector_store=vector_store,
        nli=nli,
        depth="standard_deep" # max_sources=20
    )

    plan_steps = ["Q1", "Q2", "Q3"] # 40 // 3 = 13, with 1 leftover → Q1 gets 14, Q2 gets 13, Q3 gets 13
    
    async def emit(kind, payload): pass

    with patch("perpleximanus.retrieval.deep_research.engine.gather_for_subquestion", side_effect=mock_gather), \
         patch("perpleximanus.retrieval.deep_research.engine.synthesize_section"), \
         patch("perpleximanus.retrieval.deep_research.engine.coherence_pass"):
        
        await run.run(plan_steps, emit=emit)

    assert partitioned_budgets["Q1"] == 14
    assert partitioned_budgets["Q2"] == 13
    assert partitioned_budgets["Q3"] == 13

@pytest.mark.asyncio
async def test_resume_semantics_with_pipeline():
    """RP-04: Verify that resume still works correctly (skips done sections)."""
    
    gather_calls = []
    
    async def mock_gather(subq, **kwargs):
        gather_calls.append(subq.title)
        return SubQuestionResult(subq=subq, passages=[], all_hits=[], rounds_run=1, issued_queries=[], bounded_by_rounds=False)

    # Mock dependencies
    run = DeepResearchRun(query="q", router=AsyncMock(), retrieval_engine=AsyncMock(), embedder=AsyncMock(), vector_store=AsyncMock(), nli=AsyncMock())

    plan_steps = ["Q1", "Q2", "Q3"]
    resume_sections = [
        ReportSection(id="s_old", title="Q1", markdown="b", cited_passage_ids=[], unsupported_count=0)
    ]
    
    async def emit(kind, payload): pass

    with patch("perpleximanus.retrieval.deep_research.engine.gather_for_subquestion", side_effect=mock_gather), \
         patch("perpleximanus.retrieval.deep_research.engine.synthesize_section"), \
         patch("perpleximanus.retrieval.deep_research.engine.coherence_pass"):
        
        await run.run(plan_steps, emit=emit, resume_sections=resume_sections)

    # Should only gather Q2 and Q3
    assert "Q1" not in gather_calls
    assert "Q2" in gather_calls
    assert "Q3" in gather_calls

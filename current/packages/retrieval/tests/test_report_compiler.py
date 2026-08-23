"""Focused contracts for post-research report planning."""

from __future__ import annotations

import json
from typing import Any

import pytest
from disco.core import ReportSection
from disco.core.llm import CompletionResponse, TokenUsage
from disco.retrieval.deep_research.decompose import SubQuestion
from disco.retrieval.deep_research.gather import SubQuestionResult
from disco.retrieval.deep_research.report_compiler import (
    ReportCompilationError,
    collect_claim_ledger,
    collect_global_evidence,
    compile_report_plan,
)
from disco.retrieval.models import Passage


def _passage(identifier: str, text: str) -> Passage:
    return Passage(
        id=identifier,
        source_url=f"https://example.test/{identifier}",
        source_title=f"Source {identifier}",
        text=text,
    )


class _PlannerRouter:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests = []

    async def complete(self, request):  # noqa: ANN001
        self.requests.append(request)
        return CompletionResponse(
            text=json.dumps(self.payload),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="planner-test",
            request_id=request.request_id,
        )


class _SequencedPlannerRouter:
    """Replays one payload per call: the plan call, then the repair call."""

    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.requests = []

    async def complete(self, request):  # noqa: ANN001
        self.requests.append(request)
        return CompletionResponse(
            text=json.dumps(self.payloads.pop(0)),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used="planner-test",
            request_id=request.request_id,
        )


def test_global_evidence_deduplicates_and_omits_scrape_furniture() -> None:
    useful = _passage(
        "p1",
        "The product entered limited commercial availability in August 2026.",
    )
    junk = _passage("junk", "Last verified: August 19, 2026.")
    result = SubQuestionResult(
        subq=SubQuestion(title="Initial probe"),
        passages=[useful, junk, useful],
    )

    assert collect_global_evidence([result]) == [useful]


@pytest.mark.asyncio
async def test_plan_uses_global_evidence_and_not_probe_titles() -> None:
    evidence = _passage(
        "p1",
        "The product entered limited commercial availability in August 2026.",
    )
    result = SubQuestionResult(
        subq=SubQuestion(title="Initial probe: history"),
        passages=[evidence],
    )
    router = _PlannerRouter(
        [{"title": "Commercial availability remains limited", "evidence_ids": ["p1"]}]
    )

    specs, corpus = await compile_report_plan(
        "Is the product commercially available?",
        [result],
        router=router,  # type: ignore[arg-type]
    )

    assert [spec.title for spec in specs] == ["Commercial availability remains limited"]
    assert specs[0].evidence_ids == ("p1",)
    assert corpus == [evidence]
    assert "Initial probe: history" not in router.requests[0].messages[-1].content


@pytest.mark.asyncio
async def test_plan_carries_editor_thesis_purpose_outline_and_word_allocation() -> None:
    evidence = _passage(
        "p1",
        "The product entered limited commercial availability in August 2026.",
    )
    result = SubQuestionResult(subq=SubQuestion(title="Initial probe"), passages=[evidence])
    router = _PlannerRouter(
        {
            "report_thesis": "Availability is real but remains limited.",
            "sections": [
                {
                    "title": "Commercial availability remains limited",
                    "purpose": (
                        "Establish the current shipping status and its practical significance."
                    ),
                    "evidence_ids": ["p1"],
                    "target_words": [300, 500],
                }
            ],
        }
    )

    specs, _ = await compile_report_plan(
        "Is the product commercially available?",
        [result],
        router=router,  # type: ignore[arg-type]
        report_spec={"min_words": 300, "max_words": 500},
    )

    assert specs[0].purpose.startswith("Establish the current")
    assert specs[0].target_words == (300, 500)
    assert specs[0].report_thesis == "Availability is real but remains limited."
    assert specs[0].report_outline == "1. Commercial availability remains limited"


@pytest.mark.asyncio
async def test_unusable_plan_after_one_repair_raises_compilation_error() -> None:
    """A planner that never yields usable sections is a run failure: one plan
    call, exactly one repair call, then ReportCompilationError — never a
    synthetic outline."""
    evidence = _passage(
        "p1",
        "The product entered limited commercial availability in August 2026.",
    )
    result = SubQuestionResult(
        subq=SubQuestion(title="Initial probe"),
        passages=[evidence],
    )
    router = _PlannerRouter("not json")

    with pytest.raises(
        ReportCompilationError,
        match="no usable sections after one repair",
    ):
        await compile_report_plan(
            "Is the product commercially available?",
            [result],
            router=router,  # type: ignore[arg-type]
        )

    assert len(router.requests) == 2  # the plan call + exactly one repair call
    assert "Repair that response" in router.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_malformed_first_plan_is_recovered_by_the_single_repair_call() -> None:
    """A malformed plan call still reaches the report outcome when the one
    bounded repair call returns a usable plan."""
    evidence = _passage(
        "p1",
        "The product entered limited commercial availability in August 2026.",
    )
    result = SubQuestionResult(
        subq=SubQuestion(title="Initial probe"),
        passages=[evidence],
    )
    router = _SequencedPlannerRouter(
        [
            "not json",
            [{"title": "Commercial availability remains limited", "evidence_ids": ["p1"]}],
        ]
    )

    specs, corpus = await compile_report_plan(
        "Is the product commercially available?",
        [result],
        router=router,  # type: ignore[arg-type]
    )

    assert len(router.requests) == 2
    assert [spec.title for spec in specs] == ["Commercial availability remains limited"]
    assert specs[0].evidence_ids == ("p1",)
    assert corpus == [evidence]


@pytest.mark.asyncio
async def test_empty_evidence_pool_raises_without_calling_the_planner() -> None:
    """Zero admissible evidence is an ERROR outcome, not an empty plan."""
    result = SubQuestionResult(subq=SubQuestion(title="Initial probe"))
    router = _PlannerRouter([])

    with pytest.raises(
        ReportCompilationError,
        match="research admitted no report-worthy evidence",
    ):
        await compile_report_plan(
            "What happened?",
            [result],
            router=router,  # type: ignore[arg-type]
        )

    assert router.requests == []


def test_claim_ledger_retains_verdicts_outside_rendered_markdown() -> None:
    passage = _passage(
        "p1",
        "The product entered limited commercial availability in August 2026.",
    )
    section = ReportSection(
        id="r0",
        title="Availability",
        markdown="The product entered limited commercial availability in August 2026 [[p1]].",
    )

    class _NLI:
        def entail(self, premise: str, hypothesis: str) -> str:
            return "entail" if premise and hypothesis else "neutral"

        def score(self, premise: str, hypothesis: str) -> float:
            return 1.0

    ledger = collect_claim_ledger([section], [passage], _NLI())

    assert len(ledger) == 1
    assert ledger[0].section_id == "r0"
    assert ledger[0].cited_passage_ids == ("p1",)
    assert ledger[0].verdict == "supported"

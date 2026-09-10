"""Settled source-address failures are model work, not infrastructure outages."""

from __future__ import annotations

from typing import Any, Literal

import pytest
from disco.retrieval.deep_research._agent_state import _AgentState
from disco.retrieval.deep_research._budget import SourceBudget
from disco.retrieval.deep_research._search_outcomes import (
    EXTRACTION_FAILURE,
    SOURCE_UNAVAILABLE,
    zero_admission_feedback,
    zero_yield_detail,
)
from disco.retrieval.deep_research._search_turn import execute_search_turn
from disco.retrieval.deep_research.depth import DepthBound
from disco.retrieval.models import ExtractedDoc, RetrievalRequest, RetrievalResult, SearchHit

URL = "https://example.test/original-study"


def _bound() -> DepthBound:
    return DepthBound(
        max_sources=10,
        max_rounds_per_subq=2,
        max_research_turns=8,
        max_subquestions=4,
        discover_limit=5,
        extract_cap=4,
        rerank_top_k=4,
    )


def _result(statuses: list[Literal["not_found", "paywalled", "error"]]) -> RetrievalResult:
    docs = [
        ExtractedDoc(
            url=f"{URL}/{index}",
            title="Original study",
            content="",
            fetched_ok=False,
            status=status,
            error=status,
        )
        for index, status in enumerate(statuses)
    ]
    hits = [SearchHit(url=doc.url, title=doc.title, status=doc.status) for doc in docs]
    return RetrievalResult(
        passages=[], all_hits=hits, extracted=docs, issued_queries=["study source"]
    )


class _ScriptedRetrieval:
    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return self.result


def _events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    captured: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        captured.append((kind, payload))

    return captured, emit


@pytest.mark.parametrize("statuses", [["not_found"], ["paywalled"], ["not_found", "paywalled"]])
async def test_settled_unavailable_sources_count_as_work_and_skip_reissue_queue(
    statuses: list[Literal["not_found", "paywalled", "error"]],
) -> None:
    state = _AgentState(budget=SourceBudget(10))
    captured, emit = _events()
    result = await execute_search_turn(
        state,
        ["study source"],
        0,
        retrieval_engine=_ScriptedRetrieval(_result(statuses)),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(1, 8),
    )

    assert result.outcomes[0].outcome == SOURCE_UNAVAILABLE
    assert result.charge.counted is True
    assert not state.reissue.queries
    assert "SOURCE UNAVAILABLE" in state.feedback
    assert "does not disprove the research angle" in state.feedback
    assert "correct address or an accessible primary version" in state.feedback
    trail = [row for row in state.trail if row.get("kind") == "search"][-1]
    assert trail["yield_reason"] == SOURCE_UNAVAILABLE
    observations = [payload for kind, payload in captured if kind == "observation"]
    assert observations[0]["yield_reason"] == SOURCE_UNAVAILABLE


def test_unavailable_detail_is_actionable_without_outage_language() -> None:
    detail = zero_yield_detail(SOURCE_UNAVAILABLE, None)
    assert "not found or paywalled" in detail
    assert "does not disprove the research angle" in detail
    assert "correct address or an accessible primary version" in detail
    assert "infrastructure" not in detail
    feedback = zero_admission_feedback([("study source", SOURCE_UNAVAILABLE)], [])
    assert "SOURCE UNAVAILABLE" in feedback


@pytest.mark.parametrize("statuses", [["error"], ["not_found", "error"]])
async def test_mixed_settled_and_transient_failures_keep_infrastructure_queue_behavior(
    statuses: list[Literal["not_found", "paywalled", "error"]],
) -> None:
    state = _AgentState(budget=SourceBudget(10))
    _captured, emit = _events()
    result = await execute_search_turn(
        state,
        ["study source"],
        0,
        retrieval_engine=_ScriptedRetrieval(_result(statuses)),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(1, 8),
    )

    assert result.outcomes[0].outcome == EXTRACTION_FAILURE
    assert result.charge.counted is False
    assert state.reissue.queries == ("study source",)
    assert "SOURCE UNAVAILABLE" not in state.feedback


async def test_empty_extraction_results_keep_existing_infrastructure_behavior() -> None:
    state = _AgentState(budget=SourceBudget(10))
    _captured, emit = _events()
    result = await execute_search_turn(
        state,
        ["study source"],
        0,
        retrieval_engine=_ScriptedRetrieval(
            RetrievalResult(
                passages=[],
                all_hits=[SearchHit(url=URL, title="Original study")],
                extracted=[],
                issued_queries=["study source"],
            )
        ),
        bound=_bound(),
        recency_window=None,
        corpus_ids=frozenset(),
        emit=emit,
        position=(1, 8),
    )

    assert result.outcomes[0].outcome == EXTRACTION_FAILURE
    assert result.charge.counted is False
    assert state.reissue.queries == ("study source",)

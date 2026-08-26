"""Bounded retrieval execution for one research turn."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from ..engine import ProviderOperationError, RetrievalEngine
from ..models import RetrievalRequest, RetrievalResult
from ._agent_state import report_usable_passage
from .depth import DepthBound


def query_label(query: str) -> str:
    return query if len(query) <= 80 else query[:77] + "..."


def zero_yield_reason(state: Any, retrieval: RetrievalResult) -> str:
    if not retrieval.all_hits:
        return "no_hits"
    if not retrieval.passages:
        if not retrieval.extracted or all(not doc.fetched_ok for doc in retrieval.extracted):
            return "extraction_failure"
        return "duplicates_or_filtered"
    if sum(report_usable_passage(passage) for passage in retrieval.passages) == 0:
        return "duplicates_or_filtered"
    return "budget" if state.budget.remaining <= 0 else "duplicates_or_filtered"


async def retrieve_one(
    retrieval_engine: RetrievalEngine,
    query: str,
    bound: DepthBound,
    slots: int,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
) -> RetrievalResult:
    request = RetrievalRequest(
        query=query,
        depth=bound.retrieval_depth,
        top_k=min(bound.rerank_top_k, max(1, slots)),
        discover_limit=bound.discover_limit,
        extract_cap=min(bound.extract_cap, max(1, slots)),
        recency_window=recency_window,
        corpus_ids=corpus_ids,
    )
    return await retrieval_engine.retrieve(request)


async def execute_search_turn(
    state: Any,
    queries: list[str],
    turn: int,
    *,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    emit: Any,
) -> None:
    state.last_admitted = set()
    round_no = turn + 1
    labels = [query_label(query) for query in queries]
    slots = state.budget.remaining
    for query, label in zip(queries, labels, strict=True):
        await emit("search", {"subquestion": label, "query": query, "round": round_no})
    results: list[RetrievalResult | BaseException] = await asyncio.gather(
        *(
            retrieve_one(retrieval_engine, query, bound, slots, recency_window, corpus_ids)
            for query in queries
        ),
        return_exceptions=True,
    )
    admitted = 0
    yield_reasons: list[str] = []
    failed_queries = 0
    provider_failures: list[ProviderOperationError] = []
    for query, label, result in zip(queries, labels, results, strict=True):
        added, reason, failed = await _process_result(
            state, query, label, result, turn=turn, round_no=round_no, emit=emit
        )
        admitted += added
        failed_queries += failed
        if reason:
            yield_reasons.append(reason)
        if isinstance(result, ProviderOperationError):
            provider_failures.append(result)
    if queries:
        state.trail.append(
            {
                "kind": "search_turn_summary",
                "turn": turn,
                "round": round_no,
                "queries": len(queries),
                "new_admitted": admitted,
                "failed_queries": failed_queries,
                "yield_reasons": sorted(set(yield_reasons)),
            }
        )
    if queries and len(provider_failures) == len(queries):
        raise provider_failures[0]
    if queries and admitted == 0:
        reasons = ", ".join(sorted(set(yield_reasons))) or "retrieval failures"
        state.feedback = (
            f"{state.feedback} The last research turn admitted no new evidence ({reasons}). "
            "Do not paraphrase those queries. Pivot by changing the angle, source/domain, "
            "or specificity, and trace named works upstream."
        ).strip()


async def _process_result(
    state: Any,
    query: str,
    label: str,
    result: RetrievalResult | BaseException,
    *,
    turn: int,
    round_no: int,
    emit: Any,
) -> tuple[int, str | None, int]:
    state.searches += 1
    if isinstance(result, BaseException):
        name = type(result).__name__
        state.trail.append(
            {
                "kind": "search",
                "turn": turn,
                "query": query,
                "admitted": 0,
                "final_admitted_count": 0,
                "error": name,
                "provider_error": name,
                "result": "failed",
            }
        )
        await emit(
            "observation",
            {
                "subquestion": label,
                "query": query,
                "round": round_no,
                "ok": False,
                "provider_error": name,
                "detail": f"retrieval failed: {name}: {result}",
            },
        )
        return 0, None, 1
    added = state.admit_retrieved(result)
    reason = None if added else zero_yield_reason(state, result)
    state.trail.append(
        {
            "kind": "search",
            "turn": turn,
            "query": query,
            "admitted": added,
            "final_admitted_count": added,
            "result": "evidence" if added else "empty",
            "retrieval_trace": result.notes.get("retrieval_trace", {}),
            **({"yield_reason": reason} if reason else {}),
        }
    )
    observation = {
        "subquestion": label,
        "round": round_no,
        "ok": True,
        "added": added,
        "total_for_subq": len(state.pool),
        "remaining_budget": state.budget.remaining,
        "retrieval_trace": result.notes.get("retrieval_trace", {}),
    }
    if reason:
        observation.update(
            yield_reason=reason,
            detail="No usable evidence: "
            + reason.replace("_", " ")
            + ". Pivot the query or trace the named source upstream.",
        )
    await emit("observation", observation)
    return added, reason, 0

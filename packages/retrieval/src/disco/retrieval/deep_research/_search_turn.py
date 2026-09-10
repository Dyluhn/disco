"""Issuing one turn's queries — for the model, and for the loop itself.

Both callers run the same code path on purpose. A search the SYSTEM re-issues
out of the untested-query queue has to admit evidence, dedupe, charge the source
budget, write the audit trail and reach the model as an ordinary observation in
exactly the way a model-planned search does; the only differences are the
``origin`` stamped on every row it writes and the fact that it never charges the
model's turn budget. One code path is what keeps those two facts from drifting
into two behaviors.

Split out of ``agent.py`` with the state it operates on (``_agent_state``) so
the loop file stays a loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .._direct_source import _source_url
from .._transport_retry import (
    PROVIDER_DEGRADED,
    SearchDegradation,
    engine_cooldown_seconds,
    marker_engine,
    search_degradation_from_notes,
)
from ..engine import RetrievalEngine
from ..models import RetrievalRequest, RetrievalResult, SearchHit
from ..url_policy import normalize_domain, source_url_key
from ._agent_state import _AgentState, _report_usable_passage, _retrieved_source_passages
from ._progress_events import EmitFn, emit_query_refused, emit_turn
from ._reissue_queue import QueuedQuery, ReissueQueue
from ._search_outcomes import (
    DISCOVERED,
    EXTRACTION_FAILURE,
    QUERY_REJECTED_KIND,
    SOURCE_UNAVAILABLE,
    NarrowedQuery,
    already_admitted_count,
    normalize_query,
    zero_admission_feedback,
    zero_yield_detail,
)
from ._turn_accounting import (
    ADMITTED,
    INFRASTRUCTURE_OUTCOMES,
    TRANSPORT_FAILED,
    TurnCharge,
    charge_for_search_turn,
)
from .depth import DepthBound

#: Who issued a search. ``model`` is the lead's own plan; ``system`` is the loop
#: draining its re-issue queue. Stamped on every event, trail row and search_io
#: record so no observer — the harness included — can mistake the host's retry
#: for the model repeating itself.
Origin = Literal["model", "system"]

#: The per-query outcome for a query the host refused to put on the wire a
#: second time. Deliberately NOT an infrastructure class: nothing broke, the
#: host answered out of the pool, so the query charges neither a search nor a
#: source slot and never counts as a turn the model spent on the world.
ALREADY_SEARCHED = "already_searched"


def _query_label(query: str) -> str:
    return query if len(query) <= 80 else query[:77] + "..."


def _narrowing_fields(
    narrowed: Mapping[str, NarrowedQuery] | None, query: str
) -> dict[str, object]:
    """The freshness wall's verdict on a query it matched and issued anyway.

    A narrowing is the one wall verdict a reader cannot infer from the events:
    the query IS issued, so it reaches the wire as an ordinary ``search`` and
    looks like any other. The ``query_narrowed`` trail row records it for an
    auditor, but the trail only reaches the UI on a Stop checkpoint, so on a
    finished report the trace shows the same query twice with nothing to say the
    second was admitted on purpose.

    Both keys are ABSENT on a query the wall never matched, so there is one
    shape to read and no flag to interpret.
    """
    entry = (narrowed or {}).get(query)
    if entry is None:
        return {}
    return {"narrowed_from": entry.narrowed_from, "narrowing": list(entry.added)}


def _zero_yield_reason(state: _AgentState, retrieval: RetrievalResult) -> str:
    """Explain an empty admission so the next model turn can pivot intelligently."""
    if not retrieval.all_hits:
        # A zero the host could not test (the provider stayed degraded through
        # every below-the-model retry) is NOT a semantic miss and must never be
        # reported as one.
        if search_degradation_from_notes(retrieval.notes) is not None:
            return PROVIDER_DEGRADED
        return "no_hits"
    if retrieval.notes.get("discovery_only") is True:
        return DISCOVERED
    if not retrieval.passages:
        if _all_sources_unavailable(retrieval):
            return SOURCE_UNAVAILABLE
        if not retrieval.extracted or all(not doc.fetched_ok for doc in retrieval.extracted):
            return EXTRACTION_FAILURE
        return "duplicates_or_filtered"
    usable = sum(
        _report_usable_passage(passage) for passage in _retrieved_source_passages(retrieval)
    )
    if usable == 0:
        return "duplicates_or_filtered"
    # If useful passages were returned but none survived admission, they were
    # already represented or the source budget was consumed.
    if state.budget.remaining <= 0:
        return "budget"
    return "duplicates_or_filtered"


def _all_sources_unavailable(retrieval: RetrievalResult) -> bool:
    """Recognize only a complete set of settled not-found/paywall failures."""
    settled = {"not_found", "paywalled"}
    return bool(retrieval.extracted) and all(
        not doc.fetched_ok and doc.status in settled for doc in retrieval.extracted
    )


def _zero_yield(
    state: _AgentState, retrieval: RetrievalResult, added: int
) -> tuple[str | None, SearchDegradation | None]:
    """Classify one search's empty admission, plus the outage behind it if any."""
    if added:
        return None, None
    reason = _zero_yield_reason(state, retrieval)
    if reason != PROVIDER_DEGRADED:
        return reason, None
    return reason, search_degradation_from_notes(retrieval.notes)


@dataclass(frozen=True)
class QueryOutcome:
    """How one issued query ended — the unit both accounting and the queue read."""

    query: str
    outcome: str
    added: int = 0
    duplicates: int = 0
    new_discovered: int = 0
    degradation: SearchDegradation | None = None
    engines: tuple[str, ...] = ()

    @property
    def untested(self) -> bool:
        return self.outcome in INFRASTRUCTURE_OUTCOMES


@dataclass(frozen=True)
class SearchTurnResult:
    """One search turn's verdict: what it cost, and how each query ended."""

    charge: TurnCharge
    outcomes: tuple[QueryOutcome, ...]
    admitted: int


async def _retrieve_one(
    retrieval_engine: RetrievalEngine,
    query: str,
    bound: DepthBound,
    slots: int,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    selected_hit: SearchHit | None = None,
) -> RetrievalResult:
    request = RetrievalRequest(
        query=query,
        discovery_only=_source_url(query) is None,
        selected_hit=selected_hit,
        depth=bound.retrieval_depth,
        top_k=min(bound.rerank_top_k, max(1, slots)),
        distinct_sources=True,
        discover_limit=bound.discover_limit,
        extract_cap=min(bound.extract_cap, max(1, slots)),
        recency_window=recency_window,
        corpus_ids=corpus_ids,
    )
    return await retrieval_engine.retrieve(request)


async def _record_failure(
    state: _AgentState,
    query: str,
    label: str,
    error: BaseException,
    *,
    turn: int,
    round_no: int,
    emit: EmitFn,
    origin: Origin,
) -> QueryOutcome:
    """A retrieval exception: an ok=False observation, and the loop continues."""
    state.trail.append(
        {
            "kind": "search",
            "turn": turn,
            "query": query,
            "origin": origin,
            "admitted": 0,
            "final_admitted_count": 0,
            "error": type(error).__name__,
            # Preserve the existing failure behavior while making a provider
            # exception distinguishable from a genuine zero-hit RetrievalResult
            # in the persisted trail.
            "provider_error": type(error).__name__,
            "result": "failed",
        }
    )
    await emit(
        "observation",
        {
            "subquestion": label,
            "query": query,
            "round": round_no,
            "origin": origin,
            "ok": False,
            "provider_error": type(error).__name__,
            "detail": f"retrieval failed: {type(error).__name__}: {error}",
        },
    )
    return QueryOutcome(query=query, outcome=TRANSPORT_FAILED)


async def _record_result(
    state: _AgentState,
    query: str,
    label: str,
    result: RetrievalResult,
    *,
    turn: int,
    round_no: int,
    emit: EmitFn,
    origin: Origin,
) -> QueryOutcome:
    """Admit one retrieval result, write its audit row, emit its observation."""
    # The pool is append-only inside `admit_retrieved`, so the slice past the
    # prior length is exactly what THIS query banked. The subquestion ledger
    # needs domains per query and the trail is the only place that survives a
    # resume, so the row records them rather than leaving the ledger to guess
    # which passage came from which search.
    new_discovered = (
        len(
            {
                source_url_key(hit.url)
                for hit in result.all_hits
                if source_url_key(hit.url) not in state.seen_hit_urls
            }
        )
        if _source_url(query) is None
        else 0
    )
    banked_from = len(state.pool)
    added = state.admit_retrieved(result)
    admitted_domains = sorted(
        {
            domain
            for passage in state.pool[banked_from:]
            if (domain := normalize_domain(passage.source_url))
        }
    )
    yield_reason, degradation = _zero_yield(state, result, added)
    duplicates = (
        already_admitted_count(result.passages, state.seen_ids, state.seen_urls)
        if yield_reason
        else 0
    )
    state.trail.append(
        {
            "kind": "search",
            "turn": turn,
            "query": query,
            "origin": origin,
            "admitted": added,
            "final_admitted_count": added,
            "new_discovered": new_discovered,
            "result": "evidence"
            if added
            else "discovery"
            if yield_reason == DISCOVERED
            else "empty",
            "retrieval_trace": result.notes.get("retrieval_trace", {}),
            **({"admitted_domains": admitted_domains} if admitted_domains else {}),
            **({"yield_reason": yield_reason} if yield_reason else {}),
        }
    )
    await emit(
        "observation",
        {
            "subquestion": label,
            "round": round_no,
            "origin": origin,
            "ok": True,
            "added": added,
            "new_discovered": new_discovered,
            "total_for_subq": len(state.pool),
            "remaining_budget": state.budget.remaining,
            "retrieval_trace": result.notes.get("retrieval_trace", {}),
            **(
                {
                    "yield_reason": yield_reason,
                    "detail": zero_yield_detail(yield_reason, degradation),
                }
                if yield_reason
                else {}
            ),
        },
    )
    return QueryOutcome(
        query=query,
        outcome=yield_reason or ADMITTED,
        added=added,
        duplicates=duplicates,
        new_discovered=new_discovered,
        degradation=degradation,
        engines=tuple(dict.fromkeys(marker_engine(marker) for marker in degradation.engines))
        if degradation is not None
        else (),
    )


def _take_over_untested(
    queue: ReissueQueue,
    outcomes: Sequence[QueryOutcome],
    *,
    turn: int,
    reissued: Mapping[str, QueuedQuery] | None,
) -> list[str]:
    """Hand every untested query to the host's own retry, and say which stuck.

    A query the host was ALREADY retrying goes back through ``requeue``, which
    is where its bounded re-issue budget is spent; a query the model issued goes
    in fresh. Either way the return value is the host's real state, not its
    intention, because that is what the model's feedback is built from.
    """
    taken: list[str] = []
    for outcome in outcomes:
        if not outcome.untested:
            continue
        prior = (reissued or {}).get(normalize_query(outcome.query))
        accepted = (
            queue.requeue(prior, reason=outcome.outcome, engines=outcome.engines)
            if prior is not None
            else queue.enqueue(
                outcome.query, turn=turn, reason=outcome.outcome, engines=outcome.engines
            )
        )
        if accepted:
            taken.append(outcome.query)
    return taken


def _prior_search_outcome(earlier: Mapping[str, Any]) -> str:
    return str(
        earlier.get("yield_reason")
        or (
            f"admitted {earlier['admitted']}"
            if earlier.get("admitted")
            else earlier.get("result") or "empty"
        )
    )


async def _wall_repeated_queries(
    state: _AgentState,
    queries: Sequence[str],
    *,
    turn: int,
    origin: Origin,
    narrowed: Mapping[str, NarrowedQuery] | None,
    emit: EmitFn,
) -> tuple[list[str], list[QueryOutcome]]:
    """Refuse a query this run already put to the search layer, and say why.

    The freshness wall in ``_search_outcomes`` stands in front of a model PLAN;
    this one stands in front of the WIRE, and it catches what that wall never
    got to judge. The measured case is a resumed run: ``resume_trail`` carries
    the prior run's searches forward while the re-issue queue starts empty, so a
    query the prior run already issued has nothing left refusing it and goes out
    again against engines that suspend at volume — for results the pool already
    holds.

    Two issues are sanctioned and never refused. ``origin == "system"`` is the
    loop draining its own re-issue queue, which exists precisely to run an
    untested query again on a bounded budget. A NARROWING is a query the
    freshness wall matched and issued anyway because it carries a scope the
    earlier query lacked, so it reaches sources the earlier one could not.

    Returns the queries to issue and one :class:`QueryOutcome` per refusal. The
    refusals are written in the ``query_rejected`` shape the other walls use —
    never as a ``search`` row, because nothing was searched.
    """
    if origin == "system":
        return list(queries), []
    # Reversed, so an earlier row overwrites a later one and the survivor is the
    # turn the query was FIRST issued on — the one the refusal names.
    issued = {
        normalize_query(str(row["query"])): row
        for row in reversed(state.trail)
        if row.get("kind") == "search" and row.get("query")
    }
    issuable: list[str] = []
    walled: list[QueryOutcome] = []
    named: list[str] = []
    for query in queries:
        earlier = None if query in (narrowed or {}) else issued.get(normalize_query(query))
        if earlier is None:
            issuable.append(query)
            continue
        outcome = _prior_search_outcome(earlier)
        row: dict[str, Any] = {
            "kind": QUERY_REJECTED_KIND,
            "turn": turn,
            "query": query,
            "origin": origin,
            "rejected": ALREADY_SEARCHED,
            "duplicates": earlier.get("query"),
            "duplicates_outcome": outcome,
        }
        first_issued = earlier.get("turn")
        if isinstance(first_issued, int):
            row["duplicates_turn"] = first_issued
        state.trail.append(row)
        await emit_query_refused(emit, row)
        walled.append(QueryOutcome(query=query, outcome=ALREADY_SEARCHED))
        where = f"turn {first_issued}" if isinstance(first_issued, int) else "earlier this run"
        named.append(f'"{query}" was searched on {where} (outcome: {outcome})')
    if walled:
        # The model reads this on its next turn. Rule 3: why, the state the run
        # is in now, the exact next action, and what stays allowed.
        gaps = "; ".join(str(gap) for gap in state.coverage.get("open") or [])
        pivot = (
            f"your own open coverage gaps are: {gaps}."
            if gaps
            else "pick a different source class, domain, or specificity."
        )
        message = (
            "QUERIES NOT SEARCHED — ALREADY ISSUED THIS RUN. The host does not spend "
            "a second search on a query it has already put to the search layer: "
            f"{'; '.join(named)}. STATE: nothing was searched for them and no source "
            "slot was spent; whatever they admitted is already in the evidence pool "
            "you were given, and every other query you planned this turn was issued. "
            f"NEXT: spend this turn on a different angle — {pivot} STILL AVAILABLE: a "
            "NARROWING of one of them IS issued — add a site:/filetype: scope, quote "
            "an exact title, name a DOI, or add a year, page, docket or PMID number "
            "the earlier query lacked."
        )
        state.feedback = f"{state.feedback} {message}".strip()
    return issuable, walled


def _turn_summary(
    outcomes: Sequence[QueryOutcome], charge: TurnCharge, *, turn: int, origin: Origin
) -> dict[str, Any]:
    return {
        "kind": "search_turn_summary",
        "turn": turn,
        "round": turn + 1,
        "origin": origin,
        "queries": len(outcomes),
        "new_admitted": sum(outcome.added for outcome in outcomes),
        "new_discovered": sum(outcome.new_discovered for outcome in outcomes),
        "failed_queries": sum(outcome.outcome == TRANSPORT_FAILED for outcome in outcomes),
        "yield_reasons": sorted(
            {outcome.outcome for outcome in outcomes if outcome.outcome != ADMITTED}
        ),
        **charge.trail_fields(),
    }


def _empty_turn_feedback(outcomes: Sequence[QueryOutcome], queued: Sequence[str]) -> str:
    return zero_admission_feedback(
        [(outcome.query, outcome.outcome) for outcome in outcomes if outcome.outcome != ADMITTED],
        [
            (outcome.query, outcome.degradation)
            for outcome in outcomes
            if outcome.degradation is not None
        ],
        sum(outcome.duplicates for outcome in outcomes),
        queued=queued,
        cooling=engine_cooldown_seconds(),
    )


def eligible_source_queries(state: _AgentState, queries: Sequence[str], turn: int) -> list[str]:
    """Close acquisition at its source cap while allowing read-only refinement."""
    if state.budget.remaining > 0 or not queries:
        return list(queries)
    state.trail.append({"kind": "source_budget_refusal", "turn": turn, "queries": queries})
    state.feedback = (
        "No source slots remain, so these queries were not issued. Inspect admitted "
        "sources or mark ready_to_write with material evidence gaps left explicit."
    )
    return []


def _selected_hit(query: str, hits: Sequence[SearchHit]) -> SearchHit | None:
    if _source_url(query) is None:
        return None
    key = source_url_key(query)
    return next((hit for hit in hits if source_url_key(hit.url) == key), None)


async def execute_search_turn(
    state: _AgentState,
    queries: Sequence[str],
    turn: int,
    *,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    emit: EmitFn,
    position: tuple[int, int],
    origin: Origin = "model",
    reissued: Mapping[str, QueuedQuery] | None = None,
    narrowed: Mapping[str, NarrowedQuery] | None = None,
) -> SearchTurnResult:
    """Run one turn's queries concurrently; admit through the quality filter
    + SourceBudget; emit the same search/observation event shapes the UI
    already parses. A retrieval exception becomes an ok=False observation and
    the loop continues (the source budget keeps ticking).

    The returned charge says whether this turn spent the MODEL's turn budget:
    a turn whose every query died in infrastructure did not.

    A model query this run already issued never reaches the provider
    (``_wall_repeated_queries``); it comes back as an ``already_searched``
    outcome and costs nothing.
    """
    state.last_admitted = set()
    queries = eligible_source_queries(state, queries, turn)
    round_no = turn + 1
    # Rebound on purpose: from here down, `queries` is what this turn ISSUES.
    # A walled repeat costs no search, no source slot and no phase event, so a
    # turn whose every query was walled behaves exactly like one whose every
    # query the freshness wall refused — it issues nothing.
    queries, walled = await _wall_repeated_queries(
        state, queries, turn=turn, origin=origin, narrowed=narrowed, emit=emit
    )
    labels = [_query_label(query) for query in queries]
    slots = state.budget.remaining
    if queries:
        await emit_turn(emit, position=position, phase="searching", subquestion=labels[0])
    for query, label in zip(queries, labels, strict=True):
        await emit(
            "search",
            {
                "subquestion": label,
                "query": query,
                "round": round_no,
                "origin": origin,
                **_narrowing_fields(narrowed, query),
            },
        )
    results: list[RetrievalResult | BaseException] = await asyncio.gather(
        *(
            _retrieve_one(
                retrieval_engine,
                query,
                bound,
                slots,
                recency_window,
                corpus_ids,
                _selected_hit(query, state.all_hits),
            )
            for query in queries
        ),
        return_exceptions=True,
    )
    outcomes: list[QueryOutcome] = []
    for query, label, result in zip(queries, labels, results, strict=True):
        state.searches += 1
        recorded = (
            await _record_failure(
                state, query, label, result,
                turn=turn, round_no=round_no, emit=emit, origin=origin,
            )
            if isinstance(result, BaseException)
            else await _record_result(
                state, query, label, result,
                turn=turn, round_no=round_no, emit=emit, origin=origin,
            )
        )  # fmt: skip
        outcomes.append(recorded)
    charge = charge_for_search_turn([outcome.outcome for outcome in outcomes])
    admitted = sum(outcome.added for outcome in outcomes)
    if queries:
        state.trail.append(_turn_summary(outcomes, charge, turn=turn, origin=origin))
    queued = _take_over_untested(state.reissue, outcomes, turn=turn, reissued=reissued)
    if queries and admitted == 0:
        empty = _empty_turn_feedback(outcomes, queued)
        state.feedback = f"{state.feedback} {empty}".strip()
    return SearchTurnResult(charge=charge, outcomes=(*walled, *outcomes), admitted=admitted)


async def drain_reissue_queue(
    state: _AgentState,
    turn: int,
    *,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    emit: EmitFn,
    position: tuple[int, int],
) -> SearchTurnResult | None:
    """Run the untested queries whose engines are live again — the host's retry.

    Called at the top of every turn, before the model is asked for a plan, so
    the results are already in the evidence digest the model reads. Returns None
    when nothing is ready, which is the normal healthy case.
    """
    ready = state.reissue.take_ready(engine_cooldown_seconds())
    if not ready:
        return None
    return await execute_search_turn(
        state,
        [item.query for item in ready],
        turn,
        retrieval_engine=retrieval_engine,
        bound=bound,
        recency_window=recency_window,
        corpus_ids=corpus_ids,
        emit=emit,
        position=position,
        origin="system",
        reissued={item.normalized: item for item in ready},
    )


__all__ = [
    "ALREADY_SEARCHED",
    "Origin",
    "QueryOutcome",
    "SearchTurnResult",
    "drain_reissue_queue",
    "execute_search_turn",
]

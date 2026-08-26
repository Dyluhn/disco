"""The agentic research loop — the lead model directs the whole investigation.

One model call per turn: the model sees the question, its own brief, the
accumulated evidence digest, query outcomes, coverage state, and a live budget
countdown. It returns one strict JSON decision object with new search pivots and
a readiness signal. There is no model-controlled done action. The admitted
evidence pool is the material the whole-report writer (`writer.py`) works from.

Termination is always honest: `bounded_by` says which hard cap stopped research
or whether the user stopped it. A non-stopped run with zero usable evidence is
a `ResearchAgentError`, never a report-shaped diagnostic.
"""

from __future__ import annotations

import datetime
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from disco.core import LLMMessage
from disco.core.inspect import record_model_io
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    LLMRouter,
    ModelRole,
    Requirement,
)
from disco.core.think import strip_think_spans

from ..engine import RetrievalEngine
from ..models import Passage, SearchHit
from ._agent_messages import fresh_queries as _fresh_queries
from ._agent_messages import turn_user_message as _turn_user_message
from ._agent_parsing import Turn as _Turn
from ._agent_parsing import decode_json_object as _decode_json_object
from ._agent_parsing import parse_turn as _parse_turn
from ._agent_parsing import turn_payload as _turn_payload
from ._agent_readiness import apply_readiness_feedback
from ._agent_search import execute_search_turn as _execute_search_turn_impl
from ._agent_state import AgentState as _AgentState
from ._agent_turn import one_model_turn as _one_model_turn_impl
from ._budget import SourceBudget
from .depth import DepthBound

__all__ = [
    "_AgentState",
    "_decode_json_object",
    "_parse_turn",
    "run_research_agent",
    "ResearchOutcome",
]

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]
AckSteersFn = Callable[[list[str]], None] | None

# Module attribute (not a bare `time.monotonic` call site) so hermetic tests
# can drive the research clock without patching the global `time` module.
_monotonic: Callable[[], float] = time.monotonic

# ≤3 queries per turn — bounded per-turn cost on local models; the model can
# always issue another turn.
_MAX_QUERIES_PER_TURN = 3
# 3 consecutive turns that stay malformed after their one precise re-ask is a
# provider failure — the run's only failure class.
_MAX_MALFORMED_TURNS = 3
# Digest excerpt sizes: fuller text for the passages admitted last turn (the
# model is deciding whether that lead paid off), a short gist for the rest.
_GIST_CHARS = 200
_FRESH_GIST_CHARS = 600
# When less than this fraction of the research budget remains, the turn
# message appends the wrap-up warning.
_WRAP_UP_FRACTION = 0.25


class ResearchAgentError(RuntimeError):
    """The provider could not sustain the turn protocol (malformed turns)."""


@dataclass
class ResearchOutcome:
    """What the research loop produced, handed to the whole-report writer."""

    brief: str
    passages: list[Passage]  # the admitted evidence pool
    all_hits: list[SearchHit]
    trail: list[dict[str, Any]]  # audit trail: queries, admissions, pivots
    bounded_by: str | None  # "wall_clock" | "sources" | "stopped" | None
    coverage: dict[str, Any] = field(default_factory=dict)


_RESEARCH_SYSTEM_PROMPT = (
    "You are the lead researcher directing a long-horizon research "
    "investigation. Each turn you see the question, your own brief, the "
    "evidence admitted so far, and a live budget line; you decide what to "
    "search next, when to pivot, and when the evidence is sufficient for the "
    "report to be written.\n\n"
    "METHOD:\n"
    "- FIRST TURN: map the question top-down before narrowing. In the brief, "
    "name the major angles a domain expert would expect; put the angles that "
    "still need evidence in coverage.open. This is a living map, not a fixed "
    "questionnaire: revise, merge, or add angles as the evidence changes.\n"
    "- COVER EVERY MAJOR ANGLE BEFORE GOING DEEP. Map the question's core "
    "angles (actors, mechanisms, numbers, timelines, economics, criticisms) "
    "across the initial searches; broad coverage does not mean generic "
    "queries. Target the strongest likely original or authoritative source "
    "for each angle, then drill into specifics once the map is laid out.\n"
    "- PREFER PRIMARY AND AUTHORITATIVE SOURCES: original announcements, "
    "filings, specifications, measurements, peer-reviewed work, official "
    "documentation — over aggregator summaries and secondhand commentary. "
    "Start this on the first turn: do not spend the opening source budget on "
    "generic latest/best/breakthrough roundup queries. If a generic search is "
    "needed to discover names, use its very next query to find the original.\n"
    "- CHASE CITATIONS UPSTREAM. If a page reports a named study, benchmark, "
    "filing, announcement, or dataset, keep that original work as an open "
    "lead until you find it or have genuinely exhausted targeted searches. "
    "Use the original for the finding; retain the summary only when it adds "
    "distinct interpretation. If the publisher page is paywalled or cannot "
    "be extracted, search the exact title or DOI with PDF, preprint, author, "
    "or institutional repository; do not replace it with an SEO summary.\n"
    "- STOP REPEATING A DEAD END. After two distinct zero-yield attempts to "
    "chase a named publisher or report upstream, mark that lead unresolved "
    "or single-source and pivot to another authoritative source class.\n"
    "- TREAT WEAK SECONDARY PAGES AS LEADS, NOT PROOF. Do not mark a "
    "load-bearing angle covered when its evidence is only listicles, "
    "consultancy or vendor marketing, prediction pages, or anonymous "
    "summaries. Keep the angle open and pivot with a targeted site: query "
    "to the named institution, paper, regulator, filing, model card, "
    "specification, or strong independent reporting. Multiple pages from "
    "one domain are one source, not independent corroboration.\n"
    "- CROSS-VALIDATE LOAD-BEARING CLAIMS. Important numbers and claims need "
    "independent confirmation; search specifically to confirm or refute them "
    "rather than trusting a single source.\n"
    "- ACTIVELY SEEK COUNTEREVIDENCE. Search for independent criticism, "
    "failures, negative results, and skeptical analysis; an evidence pool "
    "built only from proponents' material produces a wrong report.\n"
    "- NOTE DATES. Prefer current sources, watch for stale figures, and when "
    "the question is time-sensitive, search for the latest developments "
    "explicitly.\n"
    "- USER STEER lines are priority guidance from the user. Act on them in "
    "your very next searches, before returning to your own plan.\n"
    "- THE EVIDENCE POOL IS WHAT THE REPORT WILL BE WRITTEN FROM. Only "
    "admitted sources reach the writer: if a finding matters, make sure a "
    "source that states it is in the pool before you finish.\n"
    "- BUDGET. The live budget line shows remaining research seconds and "
    "source slots. Plan the investigation to land inside it; when the "
    "wrap-up warning appears, mark readiness only if the evidence floor is "
    "satisfied.\n\n"
    "RESPONSE FORMAT — reply with ONE strict JSON object and nothing else "
    "(no markdown fences, no commentary):\n"
    "- First turn: {first_schema}\n"
    "- Later turns: {later_schema}"
)

_FIRST_TURN_SCHEMA = json.dumps(
    {
        "brief": "how you read the question",
        "decision_summary": "what you will do next and why",
        "coverage": {"covered": [], "open": ["remaining gap"], "contradictions_checked": []},
        "queries": ["new query"],
        "ready_to_write": False,
    },
    ensure_ascii=False,
)
_LATER_TURN_SCHEMA = json.dumps(
    {
        "decision_summary": "what you will do next and why",
        "coverage": {"covered": [], "open": ["remaining gap"], "contradictions_checked": []},
        "queries": ["new query"],
        "ready_to_write": False,
    },
    ensure_ascii=False,
)


def _system_prompt(recency_window: Literal["month", "week"] | None) -> str:
    prompt = _RESEARCH_SYSTEM_PROMPT.replace("{first_schema}", _FIRST_TURN_SCHEMA).replace(
        "{later_schema}", _LATER_TURN_SCHEMA
    )
    if recency_window is None:
        return prompt
    today = datetime.date.today().isoformat()
    label = "month" if recency_window == "month" else "week"
    return (
        f"Today's date is {today}. The user wants research focused on the "
        f"PAST {label.upper()}: bias queries toward recent developments and "
        "current figures over historical background.\n\n" + prompt
    )


def _writing_reserve_s(bound: DepthBound) -> float:
    """The tier-proportional slice of the wall clock held back for report
    writing, so research cannot starve the writer. Moved verbatim from the
    v1 drain loop's `seconds_left` — the budget the model is shown is the
    same one the system enforces."""
    minimum_words = bound.report_min_words
    if minimum_words >= 8_000:
        estimated_sections = 12
    elif minimum_words >= 4_000:
        estimated_sections = 9
    elif minimum_words >= 1_500:
        estimated_sections = 5
    else:
        estimated_sections = 1
    reserve = max(60.0, 35.0 * estimated_sections + 45.0)
    return min(reserve, bound.max_wall_clock_s * 0.4)


def research_budget_s(bound: DepthBound) -> float:
    """Total research seconds for the tier (wall clock minus writing reserve)."""
    return max(0.0, bound.max_wall_clock_s - _writing_reserve_s(bound))


def research_seconds_left(bound: DepthBound, started: float) -> float:
    return max(0.0, research_budget_s(bound) - (_monotonic() - started))


# ---------------------------------------------------------------------------
# Turn parsing — strict JSON with precise re-ask feedback.
# ---------------------------------------------------------------------------


async def _complete_turn(
    router: LLMRouter, messages: list[LLMMessage], *, namespace: str
) -> tuple[str, CompletionResponse, int]:
    request = CompletionRequest(
        profile=CapabilityProfile(
            role=ModelRole.RAG_ANSWERER,
            requirements=frozenset({Requirement.JSON_MODE}),
        ),
        messages=messages,
        temperature=0.0,
        response_format="json",
        enable_thinking=False,
        metadata={
            "conversation_id": str(namespace)[:256],
            "inspect_stage": "research_turn",
        },
    )
    started = time.perf_counter()
    response = await router.complete(request)
    latency_ms = max(0, int((time.perf_counter() - started) * 1_000))
    return strip_think_spans(response.text), response, latency_ms


def _record_turn_io(
    namespace: str,
    messages: list[LLMMessage],
    text: str,
    response: CompletionResponse,
    *,
    parsed: _Turn | None,
    error: str | None,
    attempt: int,
    latency_ms: int,
) -> None:
    routing = response.routing
    record_model_io(
        namespace,
        stage="research_turn",
        attempt=attempt,
        role=ModelRole.RAG_ANSWERER.value,
        model=response.model_used,
        provider=routing.provider if routing is not None else None,
        request_id=response.request_id,
        request={
            "messages": [message.model_dump(mode="json") for message in messages],
            "temperature": 0.0,
            "response_format": "json",
            "enable_thinking": False,
        },
        response={"text": text},
        declared_decision=_turn_payload(parsed),
        latency_ms=latency_ms,
        usage=response.usage.model_dump(mode="json"),
        finish_reason=response.finish_reason,
        parse_error=error,
    )


async def _one_model_turn(
    router: LLMRouter,
    system_prompt: str,
    user_message: str,
    *,
    expect_brief: bool,
    namespace: str,
) -> tuple[_Turn | None, str | None]:
    return await _one_model_turn_impl(
        router,
        system_prompt,
        user_message,
        expect_brief=expect_brief,
        namespace=namespace,
        complete_turn=_complete_turn,
        record_turn_io=_record_turn_io,
        first_schema=_FIRST_TURN_SCHEMA,
        later_schema=_LATER_TURN_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Evidence admission + the per-turn message.
# ---------------------------------------------------------------------------


def _normalize_query(query: str) -> str:
    return " ".join(query.casefold().split())


# ---------------------------------------------------------------------------
# Search execution — ≤3 concurrent retrievals per turn.
# ---------------------------------------------------------------------------


async def _execute_search_turn(
    state: _AgentState,
    queries: list[str],
    turn: int,
    *,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    emit: EmitFn,
) -> None:
    """Run one turn's concurrent retrievals through the admission pipeline."""
    await _execute_search_turn_impl(
        state,
        queries,
        turn,
        retrieval_engine=retrieval_engine,
        bound=bound,
        recency_window=recency_window,
        corpus_ids=corpus_ids,
        emit=emit,
    )


# ---------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------


def _drain_hooks(
    state: _AgentState,
    pop_steers: Callable[[], list[Any]] | None,
    pop_injected_sources: Callable[[], list[Passage]] | None,
) -> tuple[list[str], list[str]]:
    """Drain mid-run steer strings + user-injected passages at the turn
    boundary; both are recorded in the trail. Injected passages are admitted
    directly (user-provided → exempt from the quality filter)."""
    raw_steers = pop_steers() if pop_steers is not None else []
    steers: list[str] = []
    steer_ids: list[str] = []
    for raw in raw_steers:
        text, steer_id = _coerce_steer(raw)
        if not text.strip():
            continue
        steers.append(text)
        if steer_id is not None:
            steer_ids.append(steer_id)
        # ID-bearing steers are intentionally *peeked* until a valid model
        # turn consumes them. Keep retrying with the same guidance, but record
        # the audit fact once rather than making malformed responses look like
        # repeated user steering.
        already_recorded = steer_id is not None and any(
            entry.get("kind") == "steer" and entry.get("steer_id") == steer_id
            for entry in state.trail
        )
        if not already_recorded:
            state.trail.append(
                {
                    "kind": "steer",
                    "text": text,
                    **({"steer_id": steer_id} if steer_id else {}),
                }
            )
    injected = pop_injected_sources() if pop_injected_sources is not None else []
    if injected:
        fresh = state.admit_exempt(injected)
        state.trail.append({"kind": "injected", "count": len(fresh)})
    return steers, steer_ids


def _coerce_steer(raw: object) -> tuple[str, str | None]:
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, Mapping):
        return str(raw.get("text", "") or ""), _valid_steer_id(raw.get("steer_id"))
    return str(getattr(raw, "text", "") or ""), _valid_steer_id(getattr(raw, "steer_id", None))


def _valid_steer_id(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _initial_turn(state: _AgentState) -> int:
    turns = (entry.get("turn", -1) for entry in state.trail)
    return max((turn for turn in turns if isinstance(turn, int)), default=-1) + 1


def _loop_terminal(
    state: _AgentState,
    bound: DepthBound,
    started: float,
    should_cancel: Callable[[], bool] | None,
) -> str | None:
    if should_cancel is not None and should_cancel():
        return "stopped"
    remaining = research_seconds_left(bound, started)
    if remaining <= 0:
        if state.pool:
            return "wall_clock"
        raise ResearchAgentError("research exhausted its wall-clock budget without usable evidence")
    if state.budget.remaining <= 0:
        if state.pool:
            return "sources"
        raise ResearchAgentError("research exhausted its source budget without usable evidence")
    return None


async def _run_turn_step(
    state: _AgentState,
    turn: int,
    *,
    query: str,
    router: LLMRouter,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    emit: EmitFn,
    pop_steers: Callable[[], list[Any]] | None,
    pop_injected_sources: Callable[[], list[Passage]] | None,
    ack_steers: AckSteersFn,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    remaining: float,
    total_s: float,
    system_prompt: str,
    namespace: str,
    malformed_streak: int,
) -> tuple[bool, int]:
    steers, steer_ids = _drain_hooks(state, pop_steers, pop_injected_sources)
    user_message = _turn_user_message(
        query, state, steers, remaining_s=remaining, total_s=total_s, bound=bound
    )
    parsed, error = await _one_model_turn(
        router,
        system_prompt,
        user_message,
        expect_brief=not state.brief,
        namespace=namespace,
    )
    if parsed is None:
        next_streak = malformed_streak + 1
        state.trail.append({"kind": "malformed", "turn": turn, "error": error or ""})
        if next_streak >= _MAX_MALFORMED_TURNS:
            raise ResearchAgentError(
                f"{_MAX_MALFORMED_TURNS} consecutive malformed research turns; "
                f"last parse error: {error}"
            )
        return False, next_streak
    if steer_ids:
        unique_steer_ids = list(dict.fromkeys(steer_ids))
        for steer_id in unique_steer_ids:
            await emit("steer_applied", {"steer_id": steer_id})
        if ack_steers is not None:
            ack_steers(unique_steer_ids)
    done = await _handle_parsed_turn(
        state,
        parsed,
        turn,
        retrieval_engine=retrieval_engine,
        bound=bound,
        recency_window=recency_window,
        corpus_ids=corpus_ids,
        emit=emit,
    )
    return done, 0


async def _handle_parsed_turn(
    state: _AgentState,
    parsed: _Turn,
    turn: int,
    *,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    emit: EmitFn,
) -> bool:
    """Apply one parsed turn. Returns True only when host gates admit readiness."""
    state.feedback = ""
    if parsed.brief and not state.brief:
        state.brief = parsed.brief
        state.trail.append({"kind": "brief", "text": state.brief})
        await emit("brief", {"text": state.brief})
    state.decision_summary = parsed.decision_summary
    state.coverage = parsed.coverage
    state.trail.append({"kind": "decision", "turn": turn, "text": parsed.decision_summary})
    state.trail.append({"kind": "coverage", "turn": turn, "coverage": parsed.coverage})
    fresh, repeated = _fresh_queries(state, parsed.queries)
    if fresh:
        # This is an effort floor: only a parsed turn with at least one fresh
        # query earns credit, regardless of whether retrieval finds evidence.
        state.turns_completed += 1
    if repeated:
        state.feedback = (
            "These queries were already attempted and must be replaced with a pivot: "
            + ", ".join(repeated)
        )
        for query in repeated:
            state.trail.append(
                {"kind": "query_rejected", "turn": turn, "query": query, "rejected": "repeat"}
            )
    await _execute_search_turn(
        state,
        fresh,
        turn,
        retrieval_engine=retrieval_engine,
        bound=bound,
        recency_window=recency_window,
        corpus_ids=corpus_ids,
        emit=emit,
    )
    return apply_readiness_feedback(state, parsed, bound, fresh, turn)


async def _turn_loop(
    state: _AgentState,
    *,
    query: str,
    router: LLMRouter,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    emit: EmitFn,
    should_cancel: Callable[[], bool] | None,
    pop_steers: Callable[[], list[Any]] | None,
    pop_injected_sources: Callable[[], list[Passage]] | None,
    ack_steers: AckSteersFn,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    started: float,
    total_s: float,
    system_prompt: str,
    namespace: str,
) -> str | None:
    """Run turns until a terminal condition; returns `bounded_by`."""
    malformed_streak = 0
    turn = _initial_turn(state)
    while True:
        terminal = _loop_terminal(state, bound, started, should_cancel)
        if terminal is not None:
            return terminal
        remaining = research_seconds_left(bound, started)
        done, malformed_streak = await _run_turn_step(
            state,
            turn,
            query=query,
            router=router,
            retrieval_engine=retrieval_engine,
            bound=bound,
            recency_window=recency_window,
            corpus_ids=corpus_ids,
            emit=emit,
            pop_steers=pop_steers,
            pop_injected_sources=pop_injected_sources,
            ack_steers=ack_steers,
            remaining=remaining,
            total_s=total_s,
            system_prompt=system_prompt,
            namespace=namespace,
            malformed_streak=malformed_streak,
        )
        if done:
            return None
        turn += 1


async def run_research_agent(
    query: str,
    *,
    router: LLMRouter,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    namespace: str,
    emit: EmitFn,
    should_cancel: Callable[[], bool] | None = None,
    pop_steers: Callable[[], list[Any]] | None = None,
    pop_injected_sources: Callable[[], list[Passage]] | None = None,
    ack_steers: AckSteersFn = None,
    recency_window: Literal["month", "week"] | None = None,
    corpus_ids: frozenset[str] = frozenset(),
    upload_passages: list[Passage] | None = None,
    resume_passages: list[Passage] | None = None,
    resume_trail: list[dict[str, Any]] | None = None,
) -> ResearchOutcome:
    """Run the agentic research loop to a terminal state.

    `upload_passages` and `resume_passages` seed the pool without consuming
    the web-source budget (uploads are user-provided; a resumed run's pool
    was gathered under its own prior budget). `resume_trail` carries the
    prior run's audit entries forward so checkpoints stay cumulative.
    `should_cancel` makes Stop real: polled every turn; on True the loop
    returns immediately with the pool for checkpointing."""
    started = _monotonic()
    state = _AgentState(budget=SourceBudget(bound.max_sources))
    state.trail.extend(resume_trail or [])
    state.restore_trail_state()
    state.admit_exempt([*(upload_passages or []), *(resume_passages or [])])
    total_s = research_budget_s(bound)
    await emit("phase", {"phase": "gather", "mode": "agent"})
    bounded_by = await _turn_loop(
        state,
        query=query,
        router=router,
        retrieval_engine=retrieval_engine,
        bound=bound,
        emit=emit,
        should_cancel=should_cancel,
        pop_steers=pop_steers,
        pop_injected_sources=pop_injected_sources,
        ack_steers=ack_steers,
        recency_window=recency_window,
        corpus_ids=corpus_ids,
        started=started,
        total_s=total_s,
        system_prompt=_system_prompt(recency_window),
        namespace=namespace,
    )
    return ResearchOutcome(
        brief=state.brief,
        passages=state.pool,
        all_hits=state.all_hits,
        trail=state.trail,
        bounded_by=bounded_by,
        coverage=state.coverage,
    )

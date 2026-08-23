"""The agentic research loop — the lead model directs the whole investigation.

One model call per turn: the model sees the question, its own brief, the
accumulated evidence digest, and a live budget countdown, and answers with a
strict-JSON action — search (≤3 queries, run concurrently through the
retrieval engine) or done. The system enforces ONLY hard budgets (research
wall clock, source slots); there are no theme counters, no template probes,
and no host-side sufficiency judgment. The admitted evidence pool is the
material the whole-report writer (`writer.py`) works from.

Termination is always honest: `bounded_by` says what stopped the research
("wall_clock", "sources", "stopped", or None when the model called done), and
`dead_end` is set ONLY when research terminated (not user-stopped) with zero
usable passages — the writer's system-gated dead-end account is keyed off
that flag alone and is never mentioned to the model here.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from disco.core import LLMMessage
from disco.core.llm import (
    CapabilityProfile,
    CompletionRequest,
    LLMRouter,
    ModelRole,
)
from disco.core.think import strip_think_spans

from ..engine import RetrievalEngine
from ..models import Passage, RetrievalRequest, RetrievalResult, SearchHit
from ..url_policy import source_url_key
from ._budget import SourceBudget
from .depth import DepthBound

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

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
    dead_end: bool  # terminated (not stopped) with zero usable passages


_RESEARCH_SYSTEM_PROMPT = (
    "You are the lead researcher directing a long-horizon research "
    "investigation. Each turn you see the question, your own brief, the "
    "evidence admitted so far, and a live budget line; you decide what to "
    "search next, when to pivot, and when the evidence is sufficient for the "
    "report to be written.\n\n"
    "METHOD:\n"
    "- COVER EVERY MAJOR ANGLE BEFORE GOING DEEP. Map the question's core "
    "angles (actors, mechanisms, numbers, timelines, economics, criticisms) "
    "with broad searches first; drill into specifics once the map is laid "
    "out.\n"
    "- PREFER PRIMARY AND AUTHORITATIVE SOURCES: original announcements, "
    "filings, specifications, measurements, peer-reviewed work, official "
    "documentation — over aggregator summaries and secondhand commentary.\n"
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
    "wrap-up warning appears, finish your current leads and call done.\n\n"
    "RESPONSE FORMAT — reply with ONE strict JSON object and nothing else "
    "(no markdown fences, no commentary):\n"
    "- First turn: {first_schema}\n"
    "- Later turns: {later_schema}"
)

_FIRST_TURN_SCHEMA = (
    '{"brief": "<2-4 sentences: how you read the question and the angles you '
    'plan to research>", "action": "search", "queries": ["<query>", ...]} '
    "with 1-3 queries"
)
_LATER_TURN_SCHEMA = (
    '{"action": "search", "queries": ["<query>", ...]} with 1-3 queries, or '
    '{"action": "done", "reason": "<why the gathered evidence is sufficient '
    'to answer the question>"}'
)


def _system_prompt(recency_window: Literal["month", "week"] | None) -> str:
    prompt = _RESEARCH_SYSTEM_PROMPT.format(
        first_schema=_FIRST_TURN_SCHEMA, later_schema=_LATER_TURN_SCHEMA
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
    estimated_sections = min(bound.max_subquestions, estimated_sections)
    reserve = max(60.0, 35.0 * estimated_sections + 45.0)
    return min(reserve, bound.max_wall_clock_s * 0.4)


def research_budget_s(bound: DepthBound) -> float:
    """Total research seconds for the tier (wall clock minus writing reserve)."""
    return max(0.0, bound.max_wall_clock_s - _writing_reserve_s(bound))


def research_seconds_left(bound: DepthBound, started: float) -> float:
    return max(0.0, research_budget_s(bound) - (_monotonic() - started))


def _estimate_max_turns(bound: DepthBound, total_budget_s: float) -> int:
    """A display-only turn estimate for the UI's round counter."""
    del bound
    return int(max(3, min(16, total_budget_s // 60 + 1)))


# ---------------------------------------------------------------------------
# Turn parsing — strict JSON with precise re-ask feedback.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Turn:
    action: Literal["search", "done"]
    queries: tuple[str, ...] = ()
    brief: str = ""
    reason: str = ""


def _decode_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence is not None:
        candidate = fence.group(1).strip()
    try:
        value: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        start, end = candidate.find("{"), candidate.rfind("}")
        if not (0 <= start < end):
            return None, f"not valid JSON: {exc}"
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None, f"not valid JSON: {exc}"
    if not isinstance(value, dict):
        return None, (
            f"the top-level JSON value must be an object, got {type(value).__name__}"
        )
    return value, None


def _parse_queries(value: dict[str, Any]) -> tuple[tuple[str, ...] | None, str | None]:
    raw = value.get("queries")
    if not isinstance(raw, list):
        return None, '"queries" must be a JSON array of 1-3 non-empty strings'
    queries = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    if not queries:
        return None, '"queries" must contain at least one non-empty string'
    return tuple(queries[:_MAX_QUERIES_PER_TURN]), None


def _parse_turn(text: str, *, expect_brief: bool) -> tuple[_Turn | None, str | None]:
    """Parse one turn response. Returns `(turn, None)` or `(None, precise
    parse error)` — the error text goes verbatim into the one re-ask."""
    value, error = _decode_json_object(text)
    if value is None:
        return None, error
    action = value.get("action")
    if action not in ("search", "done"):
        return None, f'"action" must be exactly "search" or "done", got {action!r}'
    raw_brief = value.get("brief")
    brief = raw_brief.strip() if isinstance(raw_brief, str) else ""
    if expect_brief:
        if not brief:
            return None, 'the first turn must include a non-empty "brief" string'
        if action != "search":
            return None, 'the first turn must have "action": "search" with 1-3 queries'
    if action == "done":
        raw_reason = value.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        return _Turn(action="done", brief=brief, reason=reason), None
    queries, error = _parse_queries(value)
    if queries is None:
        return None, error
    return _Turn(action="search", queries=queries, brief=brief), None


async def _complete_turn(router: LLMRouter, messages: list[LLMMessage]) -> str:
    response = await router.complete(
        CompletionRequest(
            profile=CapabilityProfile(role=ModelRole.RAG_ANSWERER),
            messages=messages,
            temperature=0.0,
        )
    )
    return strip_think_spans(response.text)


async def _one_model_turn(
    router: LLMRouter, system_prompt: str, user_message: str, *, expect_brief: bool
) -> tuple[_Turn | None, str | None]:
    """One turn = one model call, plus at most one re-ask carrying the exact
    parse error and the required schema (precise feedback, never a bare no)."""
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_message),
    ]
    text = await _complete_turn(router, messages)
    parsed, error = _parse_turn(text, expect_brief=expect_brief)
    if parsed is not None:
        return parsed, None
    schema = _FIRST_TURN_SCHEMA if expect_brief else _LATER_TURN_SCHEMA
    retry_messages = [
        *messages,
        LLMMessage(role="assistant", content=text),
        LLMMessage(
            role="user",
            content=(
                f"Your response could not be used: {error}. Reply again with "
                f"ONE strict JSON object matching exactly this schema: {schema}"
            ),
        ),
    ]
    text = await _complete_turn(router, retry_messages)
    return _parse_turn(text, expect_brief=expect_brief)


# ---------------------------------------------------------------------------
# Evidence admission + the per-turn message.
# ---------------------------------------------------------------------------


def _report_usable_passage(passage: Passage) -> bool:
    """Reject obvious extraction furniture before it consumes source capacity.
    Retrieval remains auditable through ``all_hits``; the filter is structural
    and conservative so substantive claims are never dropped on an inferred
    topic judgment. (Moved from the v1 gather loop.)"""
    text = " ".join(passage.text.split())
    if len(re.findall(r"[A-Za-z][\w'-]*", text)) < 5:
        return False
    lowered = text.casefold()
    return not (
        lowered.startswith(("last verified:", "last updated:", "published:"))
        or "privacy policy" in lowered
        or "terms of service" in lowered
        or text.count("|") >= 2
    )


@dataclass
class _AgentState:
    budget: SourceBudget
    pool: list[Passage] = field(default_factory=list)
    all_hits: list[SearchHit] = field(default_factory=list)
    trail: list[dict[str, Any]] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    seen_urls: set[str] = field(default_factory=set)
    seen_hit_urls: set[str] = field(default_factory=set)
    last_admitted: set[str] = field(default_factory=set)
    brief: str = ""
    searches: int = 0

    def admit_exempt(self, passages: list[Passage]) -> list[Passage]:
        """Admit user-provided passages directly: exempt from the quality
        filter AND the web-source budget (uploads/injections never consume
        the retrieval allowance)."""
        fresh: list[Passage] = []
        for passage in passages:
            if passage.id in self.seen_ids:
                continue
            self.seen_ids.add(passage.id)
            if passage.source_url:
                self.seen_urls.add(source_url_key(passage.source_url))
            self.pool.append(passage)
            fresh.append(passage)
        return fresh

    def admit_retrieved(self, retrieval: RetrievalResult) -> int:
        """Quality-filter, dedup (id + url), and budget-charge one retrieval
        result's passages; accumulate its deduped discovery hits."""
        candidates = [
            passage
            for passage in retrieval.passages
            if passage.id not in self.seen_ids
            and source_url_key(passage.source_url) not in self.seen_urls
            and _report_usable_passage(passage)
        ]
        admitted, _charged = self.budget.admit(candidates)
        for passage in admitted:
            self.seen_ids.add(passage.id)
            self.seen_urls.add(source_url_key(passage.source_url))
            self.pool.append(passage)
            self.last_admitted.add(passage.id)
        for hit in retrieval.all_hits:
            key = source_url_key(hit.url)
            if key not in self.seen_hit_urls:
                self.seen_hit_urls.add(key)
                self.all_hits.append(hit)
        return len(admitted)


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc if url else ""
    return netloc or "(user-provided)"


def _digest_lines(passages: list[Passage], fresh_ids: set[str]) -> list[str]:
    lines: list[str] = []
    for passage in passages:
        date = passage.published_at.isoformat() if passage.published_at else "date unknown"
        title = (passage.source_title or passage.source_url or passage.id)[:120]
        lines.append(f"  [{passage.id}] {title} — {passage.source_url} ({date})")
        limit = _FRESH_GIST_CHARS if passage.id in fresh_ids else _GIST_CHARS
        gist = " ".join(passage.text.split())[:limit]
        lines.append(f"    {gist}")
    return lines


def _evidence_digest(pool: list[Passage], fresh_ids: set[str]) -> str:
    if not pool:
        return "EVIDENCE POOL: empty — nothing admitted yet."
    by_domain: dict[str, list[Passage]] = {}
    for passage in pool:
        by_domain.setdefault(_domain(passage.source_url), []).append(passage)
    lines = [f"EVIDENCE POOL ({len(pool)} sources, grouped by domain):"]
    for domain in sorted(by_domain):
        lines.append(f"{domain}:")
        lines.extend(_digest_lines(by_domain[domain], fresh_ids))
    return "\n".join(lines)


def _budget_line(
    remaining_s: float, total_s: float, bound: DepthBound, slots: int, searches: int
) -> str:
    line = (
        f"BUDGET: {int(remaining_s)}s research time remaining of {int(total_s)}s; "
        f"{slots} of {bound.max_sources} source slots remaining; "
        f"{searches} searches issued so far."
    )
    if total_s > 0 and remaining_s < _WRAP_UP_FRACTION * total_s:
        line += (
            "\nWRAP-UP WARNING: less than 25% of the research budget remains — "
            "finish your current leads and call done."
        )
    return line


def _turn_user_message(
    query: str,
    state: _AgentState,
    steers: list[str],
    *,
    remaining_s: float,
    total_s: float,
    bound: DepthBound,
) -> str:
    parts = [f"QUESTION: {query}"]
    if state.brief:
        parts.append(f"YOUR BRIEF: {state.brief}")
    if steers:
        parts.append(
            "USER STEER (priority user guidance — act on these in your next "
            "searches):\n" + "\n".join(f"- {steer}" for steer in steers)
        )
    parts.append(_evidence_digest(state.pool, state.last_admitted))
    parts.append(
        _budget_line(remaining_s, total_s, bound, state.budget.remaining, state.searches)
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Search execution — ≤3 concurrent retrievals per turn.
# ---------------------------------------------------------------------------


def _query_label(query: str) -> str:
    return query if len(query) <= 80 else query[:77] + "..."


async def _retrieve_one(
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


async def _execute_search_turn(
    state: _AgentState,
    queries: list[str],
    turn: int,
    *,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    rounds_max: int,
    emit: EmitFn,
) -> None:
    """Run one turn's queries concurrently; admit through the quality filter
    + SourceBudget; emit the same search/observation event shapes the UI
    already parses. A retrieval exception becomes an ok=False observation and
    the loop continues (the budget keeps ticking)."""
    state.last_admitted = set()
    round_no = turn + 1
    labels = [_query_label(query) for query in queries]
    slots = state.budget.remaining
    for query, label in zip(queries, labels, strict=True):
        await emit(
            "search",
            {"subquestion": label, "query": query, "round": round_no, "rounds_max": rounds_max},
        )
    results: list[RetrievalResult | BaseException] = await asyncio.gather(
        *(
            _retrieve_one(retrieval_engine, query, bound, slots, recency_window, corpus_ids)
            for query in queries
        ),
        return_exceptions=True,
    )
    for query, label, result in zip(queries, labels, results, strict=True):
        state.searches += 1
        if isinstance(result, BaseException):
            state.trail.append(
                {"kind": "search", "turn": turn, "query": query, "admitted": 0,
                 "error": type(result).__name__}
            )
            await emit(
                "observation",
                {
                    "subquestion": label,
                    "round": round_no,
                    "ok": False,
                    "detail": f"retrieval failed: {type(result).__name__}: {result}",
                },
            )
            continue
        added = state.admit_retrieved(result)
        state.trail.append(
            {"kind": "search", "turn": turn, "query": query, "admitted": added}
        )
        await emit(
            "observation",
            {
                "subquestion": label,
                "round": round_no,
                "ok": True,
                "added": added,
                "total_for_subq": len(state.pool),
                "remaining_budget": state.budget.remaining,
            },
        )


# ---------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------


def _drain_hooks(
    state: _AgentState,
    pop_steers: Callable[[], list[str]] | None,
    pop_injected_sources: Callable[[], list[Passage]] | None,
) -> list[str]:
    """Drain mid-run steer strings + user-injected passages at the turn
    boundary; both are recorded in the trail. Injected passages are admitted
    directly (user-provided → exempt from the quality filter)."""
    steers = pop_steers() if pop_steers is not None else []
    for steer in steers:
        state.trail.append({"kind": "steer", "text": steer})
    injected = pop_injected_sources() if pop_injected_sources is not None else []
    if injected:
        fresh = state.admit_exempt(injected)
        state.trail.append({"kind": "injected", "count": len(fresh)})
    return steers


async def _handle_parsed_turn(
    state: _AgentState,
    parsed: _Turn,
    turn: int,
    *,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    rounds_max: int,
    emit: EmitFn,
) -> bool:
    """Apply one parsed turn. Returns True when the model called done."""
    if parsed.brief and not state.brief:
        state.brief = parsed.brief
        state.trail.append({"kind": "brief", "text": state.brief})
        await emit("brief", {"text": state.brief})
    if parsed.action == "done":
        state.trail.append({"kind": "done", "reason": parsed.reason})
        return True
    await _execute_search_turn(
        state,
        list(parsed.queries),
        turn,
        retrieval_engine=retrieval_engine,
        bound=bound,
        recency_window=recency_window,
        corpus_ids=corpus_ids,
        rounds_max=rounds_max,
        emit=emit,
    )
    return False


async def _turn_loop(
    state: _AgentState,
    *,
    query: str,
    router: LLMRouter,
    retrieval_engine: RetrievalEngine,
    bound: DepthBound,
    emit: EmitFn,
    should_cancel: Callable[[], bool] | None,
    pop_steers: Callable[[], list[str]] | None,
    pop_injected_sources: Callable[[], list[Passage]] | None,
    recency_window: Literal["month", "week"] | None,
    corpus_ids: frozenset[str],
    started: float,
    total_s: float,
    rounds_max: int,
    system_prompt: str,
) -> str | None:
    """Run turns until a terminal condition; returns `bounded_by`."""
    malformed_streak = 0
    turn = 0
    while True:
        if should_cancel is not None and should_cancel():
            return "stopped"
        remaining = research_seconds_left(bound, started)
        if remaining <= 0:
            return "wall_clock"
        if state.budget.remaining <= 0:
            return "sources"
        steers = _drain_hooks(state, pop_steers, pop_injected_sources)
        user_message = _turn_user_message(
            query, state, steers, remaining_s=remaining, total_s=total_s, bound=bound
        )
        parsed, error = await _one_model_turn(
            router, system_prompt, user_message, expect_brief=not state.brief
        )
        if parsed is None:
            malformed_streak += 1
            state.trail.append({"kind": "malformed", "turn": turn, "error": error or ""})
            if malformed_streak >= _MAX_MALFORMED_TURNS:
                raise ResearchAgentError(
                    f"{_MAX_MALFORMED_TURNS} consecutive malformed research turns; "
                    f"last parse error: {error}"
                )
            turn += 1
            continue
        malformed_streak = 0
        done = await _handle_parsed_turn(
            state,
            parsed,
            turn,
            retrieval_engine=retrieval_engine,
            bound=bound,
            recency_window=recency_window,
            corpus_ids=corpus_ids,
            rounds_max=rounds_max,
            emit=emit,
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
    pop_steers: Callable[[], list[str]] | None = None,
    pop_injected_sources: Callable[[], list[Passage]] | None = None,
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
    del namespace  # per-conversation scoping is carried by the emit callback
    started = _monotonic()
    state = _AgentState(budget=SourceBudget(bound.max_sources))
    state.trail.extend(resume_trail or [])
    state.admit_exempt([*(upload_passages or []), *(resume_passages or [])])
    total_s = research_budget_s(bound)
    rounds_max = _estimate_max_turns(bound, total_s)
    await emit("phase", {"phase": "gather", "mode": "agent", "max_turns": rounds_max})
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
        recency_window=recency_window,
        corpus_ids=corpus_ids,
        started=started,
        total_s=total_s,
        rounds_max=rounds_max,
        system_prompt=_system_prompt(recency_window),
    )
    dead_end = bounded_by != "stopped" and not state.pool
    return ResearchOutcome(
        brief=state.brief,
        passages=state.pool,
        all_hits=state.all_hits,
        trail=state.trail,
        bounded_by=bounded_by,
        dead_end=dead_end,
    )

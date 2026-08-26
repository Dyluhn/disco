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

import asyncio
import datetime
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

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

from ..engine import ProviderOperationError, RetrievalEngine
from ..models import Passage, RetrievalRequest, RetrievalResult, SearchHit
from ..ranking import merge_search_hits
from ..url_policy import source_url_key
from ._budget import SourceBudget
from .depth import DepthBound
from .source_identity import distinct_work_count

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


@dataclass(frozen=True)
class _Turn:
    brief: str
    decision_summary: str
    coverage: dict[str, Any]
    queries: tuple[str, ...] = ()
    ready_to_write: bool = False


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
        return None, (f"the top-level JSON value must be an object, got {type(value).__name__}")
    return value, None


def _parse_queries(value: dict[str, Any]) -> tuple[tuple[str, ...] | None, str | None]:
    raw = value.get("queries")
    if not isinstance(raw, list):
        return None, '"queries" must be a JSON array of 1-3 non-empty strings'
    queries = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    if not queries:
        return None, '"queries" must contain at least one non-empty string'
    return tuple(queries[:_MAX_QUERIES_PER_TURN]), None


def _parse_coverage(value: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    raw = value.get("coverage")
    if not isinstance(raw, dict):
        return None, '"coverage" must be an object with covered, open, and contradictions_checked'
    covered_raw = raw.get("covered")
    open_raw = raw.get("open")
    checked_raw = raw.get("contradictions_checked")
    if (
        not isinstance(covered_raw, list)
        or not isinstance(open_raw, list)
        or not isinstance(checked_raw, list)
    ):
        return None, '"coverage" fields covered, open, and contradictions_checked must be arrays'
    covered: list[dict[str, Any]] = []
    for item in covered_raw:
        if isinstance(item, str):
            angle = item.strip()
            if not angle:
                return None, 'each covered item must have a non-empty string "angle"'
            covered.append({"angle": angle, "evidence_ids": []})
            continue
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("angle"), str)
            or not item["angle"].strip()
        ):
            return None, 'each covered item must have a string "angle"'
        # Some otherwise usable control responses describe an angle before
        # they have mapped it to admitted passages. Treat an omitted/null
        # mapping as unresolved coverage; the host readiness gates below still
        # require non-empty, admitted evidence ids before writing.
        evidence_ids = item.get("evidence_ids")
        if evidence_ids is None:
            evidence_ids = []
        if not isinstance(evidence_ids, list) or not all(
            isinstance(item_id, str) for item_id in evidence_ids
        ):
            return None, 'each covered item must have an "evidence_ids" string array'
        covered.append(
            {
                "angle": item["angle"].strip(),
                "evidence_ids": [item_id.strip() for item_id in evidence_ids if item_id.strip()],
            }
        )

    def strings(items: list[Any]) -> list[str]:
        return [item.strip() for item in items if isinstance(item, str) and item.strip()]

    return {
        "covered": covered,
        "open": strings(open_raw),
        "contradictions_checked": strings(checked_raw),
    }, None


def _parse_turn(text: str, *, expect_brief: bool) -> tuple[_Turn | None, str | None]:
    """Parse one turn response. Returns `(turn, None)` or `(None, precise
    parse error)` — the error text goes verbatim into the one re-ask."""
    value, error = _decode_json_object(text)
    if value is None:
        return None, error
    raw_brief = value.get("brief", "")
    if not isinstance(raw_brief, str) or (expect_brief and not raw_brief.strip()):
        return None, '"brief" must be a non-empty string on the first turn'
    raw_decision = value.get("decision_summary")
    if not isinstance(raw_decision, str) or not raw_decision.strip():
        return None, '"decision_summary" must be a non-empty string'
    coverage, error = _parse_coverage(value)
    if coverage is None:
        return None, error
    ready = value.get("ready_to_write")
    if not isinstance(ready, bool):
        return None, '"ready_to_write" must be a boolean'
    queries, error = _parse_queries(value)
    if queries is None and not ready:
        return None, error
    return _Turn(
        brief=raw_brief.strip(),
        decision_summary=raw_decision.strip(),
        coverage=coverage,
        queries=queries or (),
        ready_to_write=ready,
    ), None


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


def _turn_payload(turn: _Turn | None) -> dict[str, Any] | None:
    if turn is None:
        return None
    return {
        "brief": turn.brief,
        "decision_summary": turn.decision_summary,
        "coverage": turn.coverage,
        "queries": list(turn.queries),
        "ready_to_write": turn.ready_to_write,
    }


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
    """One turn = one model call, plus at most one re-ask carrying the exact
    parse error and the required schema (precise feedback, never a bare no)."""
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_message),
    ]
    text, response, latency_ms = await _complete_turn(router, messages, namespace=namespace)
    parsed, error = _parse_turn(text, expect_brief=expect_brief)
    _record_turn_io(
        namespace,
        messages,
        text,
        response,
        parsed=parsed,
        error=error,
        attempt=1,
        latency_ms=latency_ms,
    )
    if parsed is not None:
        return parsed, None
    schema = _FIRST_TURN_SCHEMA if expect_brief else _LATER_TURN_SCHEMA
    retry_messages = [
        *messages,
        LLMMessage(
            role="user",
            content=(
                f"Your response could not be used: {error}. Reply again with "
                f"ONE strict JSON object matching exactly this schema: {schema}"
            ),
        ),
    ]
    text, response, latency_ms = await _complete_turn(
        router, retry_messages, namespace=namespace
    )
    parsed, error = _parse_turn(text, expect_brief=expect_brief)
    _record_turn_io(
        namespace,
        retry_messages,
        text,
        response,
        parsed=parsed,
        error=error,
        attempt=2,
        latency_ms=latency_ms,
    )
    return parsed, error


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
    decision_summary: str = ""
    coverage: dict[str, Any] = field(
        default_factory=lambda: {
            "covered": [],
            "open": [],
            "contradictions_checked": [],
        }
    )
    turns_completed: int = 0
    feedback: str = ""
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
        candidates: list[Passage] = []
        candidate_ids: set[str] = set()
        candidate_urls: set[str] = set()
        for passage in retrieval.passages:
            key = source_url_key(passage.source_url)
            if (
                passage.id in self.seen_ids
                or passage.id in candidate_ids
                or key in self.seen_urls
                or key in candidate_urls
                or not _report_usable_passage(passage)
            ):
                continue
            candidate_ids.add(passage.id)
            candidate_urls.add(key)
            candidates.append(passage)
        admitted, _charged = self.budget.admit(candidates)
        for passage in admitted:
            self.seen_ids.add(passage.id)
            self.seen_urls.add(source_url_key(passage.source_url))
            self.pool.append(passage)
            self.last_admitted.add(passage.id)
        for hit in retrieval.all_hits:
            key = source_url_key(hit.url)
            if key in self.seen_hit_urls:
                for index, existing in enumerate(self.all_hits):
                    if source_url_key(existing.url) == key:
                        self.all_hits[index] = merge_search_hits(existing, hit)
                        break
                continue
            self.seen_hit_urls.add(key)
            self.all_hits.append(hit)
        return len(admitted)

    def restore_trail_state(self) -> None:
        """Restore model-owned state from a stopped run's audit trail."""
        search_turns = {
            entry["turn"]
            for entry in self.trail
            if entry.get("kind") == "search" and isinstance(entry.get("turn"), int)
        }
        for entry in self.trail:
            if entry.get("kind") == "brief" and isinstance(entry.get("text"), str):
                self.brief = entry["text"]
            elif entry.get("kind") == "decision" and isinstance(entry.get("text"), str):
                self.decision_summary = entry["text"]
            elif entry.get("kind") == "coverage" and isinstance(entry.get("coverage"), dict):
                self.coverage = entry["coverage"]
        # A research turn earns effort credit only when it issued a fresh
        # query. Multiple queries in one turn count once; queryless decisions,
        # malformed turns, and rejected repeats do not.
        self.turns_completed = len(search_turns)


def _normalize_query(query: str) -> str:
    return " ".join(query.casefold().split())


def _query_history(state: _AgentState) -> list[dict[str, Any]]:
    return [
        {
            key: entry[key]
            for key in (
                "turn",
                "query",
                "admitted",
                "result",
                "yield_reason",
                "error",
                "rejected",
            )
            if key in entry
        }
        for entry in state.trail
        if entry.get("kind") in {"search", "query_rejected"}
    ]


def _upstream_zero_yield_warning(state: _AgentState) -> str:
    """Give the lead a bounded pivot after two fresh, empty research turns."""
    streak = 0
    summaries = [entry for entry in state.trail if entry.get("kind") == "search_turn_summary"]
    for summary in reversed(summaries):
        if summary.get("queries", 0) <= 0 or summary.get("new_admitted", 0) != 0:
            break
        # Queries reach this point only after _fresh_queries has rejected
        # repeats, so consecutive empty turns represent distinct attempts.
        streak += 1
    if streak < 2:
        return ""
    return (
        "Two distinct research turns chasing an upstream lead yielded no new "
        "admitted evidence. Stop paraphrasing or retrying that named "
        "publisher/report; mark the claim or angle unresolved or single-source "
        "if appropriate, and pivot to a different authoritative class (primary "
        "paper, filing, regulator, official documentation, or independent "
        "reporting). If the host evidence floors are already met, proceed toward "
        "writing."
    )


def _fresh_queries(state: _AgentState, queries: tuple[str, ...]) -> tuple[list[str], list[str]]:
    seen = {
        _normalize_query(str(entry["query"]))
        for entry in state.trail
        if entry.get("kind") in {"search", "query_rejected"} and entry.get("query")
    }
    fresh: list[str] = []
    repeated: list[str] = []
    for query in queries:
        normalized = _normalize_query(query)
        if not normalized or normalized in seen:
            repeated.append(query)
            continue
        seen.add(normalized)
        fresh.append(query)
    return fresh, repeated


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
            "finish your current leads and mark ready_to_write only when the "
            "host readiness floor is satisfied."
        )
    return line


def _depth_directive(bound: DepthBound, turn: int) -> str:
    """Scale breadth and audit prompts without introducing fixed subquestions."""
    if bound.retrieval_depth == "shallow":
        return (
            "DEPTH GUIDANCE (quick): keep the map broad, then perform one targeted "
            "counterevidence check and a brief primary-source/upstream audit before wrap-up."
        )
    if bound.retrieval_depth == "deep":
        if turn and turn % 2 == 0:
            return (
                "DEPTH GUIDANCE (exhaustive): refresh the coverage map now; pursue a missing "
                "angle or minority view, trace load-bearing claims upstream, and seek independent "
                "counterevidence before deciding readiness."
            )
        return (
            "DEPTH GUIDANCE (exhaustive): broaden the map, include minority viewpoints, and "
            "treat every named study, benchmark, filing, or dataset as an upstream lead."
        )
    if turn and turn % 2 == 0:
        return (
            "DEPTH GUIDANCE (standard): refresh coverage and audit load-bearing secondary claims "
            "to their original sources; use the next search for an independent counterexample "
            "where a claim is surprising or disputed."
        )
    return (
        "DEPTH GUIDANCE (standard): map major angles first, then follow named sources upstream "
        "and reserve a targeted search for criticism or negative results."
    )


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
    if state.decision_summary:
        parts.append(f"YOUR LAST DECISION: {state.decision_summary}")
    if steers:
        parts.append(
            "USER STEER (priority user guidance — act on these in your next "
            "searches):\n" + "\n".join(f"- {steer}" for steer in steers)
        )
    parts.append(_evidence_digest(state.pool, state.last_admitted))
    parts.append(
        "MODEL-OWNED COVERAGE STATE:\n"
        + json.dumps(state.coverage, ensure_ascii=False, sort_keys=True)
    )
    history = _query_history(state)
    parts.append(
        "FULL QUERY OUTCOME HISTORY:\n"
        + (json.dumps(history, ensure_ascii=False) if history else "(none yet)")
    )
    if state.feedback:
        parts.append(f"HOST FEEDBACK — correct this on the next turn: {state.feedback}")
    parts.append(_depth_directive(bound, state.turns_completed))
    parts.append(_budget_line(remaining_s, total_s, bound, state.budget.remaining, state.searches))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Search execution — ≤3 concurrent retrievals per turn.
# ---------------------------------------------------------------------------


def _query_label(query: str) -> str:
    return query if len(query) <= 80 else query[:77] + "..."


def _zero_yield_reason(state: _AgentState, retrieval: RetrievalResult) -> str:
    """Explain an empty admission so the next model turn can pivot intelligently."""
    if not retrieval.all_hits:
        return "no_hits"
    if not retrieval.passages:
        if not retrieval.extracted or all(not doc.fetched_ok for doc in retrieval.extracted):
            return "extraction_failure"
        return "duplicates_or_filtered"
    usable = sum(_report_usable_passage(passage) for passage in retrieval.passages)
    if usable == 0:
        return "duplicates_or_filtered"
    # If useful passages were returned but none survived admission, they were
    # already represented or the source budget was consumed.
    if state.budget.remaining <= 0:
        return "budget"
    return "duplicates_or_filtered"


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
            {"subquestion": label, "query": query, "round": round_no},
        )
    results: list[RetrievalResult | BaseException] = await asyncio.gather(
        *(
            _retrieve_one(retrieval_engine, query, bound, slots, recency_window, corpus_ids)
            for query in queries
        ),
        return_exceptions=True,
    )
    admitted_this_turn = 0
    yield_reasons: list[str] = []
    failed_queries = 0
    provider_failures: list[ProviderOperationError] = []
    for query, label, result in zip(queries, labels, results, strict=True):
        state.searches += 1
        if isinstance(result, BaseException):
            failed_queries += 1
            if isinstance(result, ProviderOperationError):
                provider_failures.append(result)
            state.trail.append(
                {
                    "kind": "search",
                    "turn": turn,
                    "query": query,
                    "admitted": 0,
                    "final_admitted_count": 0,
                    "error": type(result).__name__,
                    # Preserve the existing failure behavior while making a
                    # provider exception distinguishable from a genuine
                    # zero-hit RetrievalResult in the persisted trail.
                    "provider_error": type(result).__name__,
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
                    "provider_error": type(result).__name__,
                    "detail": f"retrieval failed: {type(result).__name__}: {result}",
                },
            )
            continue
        added = state.admit_retrieved(result)
        admitted_this_turn += added
        yield_reason = None if added else _zero_yield_reason(state, result)
        if yield_reason:
            yield_reasons.append(yield_reason)
        state.trail.append(
            {
                "kind": "search",
                "turn": turn,
                "query": query,
                "admitted": added,
                "final_admitted_count": added,
                "result": "evidence" if added else "empty",
                "retrieval_trace": result.notes.get("retrieval_trace", {}),
                **({"yield_reason": yield_reason} if yield_reason else {}),
            }
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
                "retrieval_trace": result.notes.get("retrieval_trace", {}),
                **(
                    {
                        "yield_reason": yield_reason,
                        "detail": (
                            "No usable evidence: "
                            + yield_reason.replace("_", " ")
                            + ". Pivot the query or trace the named source upstream."
                        ),
                    }
                    if yield_reason
                    else {}
                ),
            },
        )
    if queries:
        state.trail.append(
            {
                "kind": "search_turn_summary",
                "turn": turn,
                "round": round_no,
                "queries": len(queries),
                "new_admitted": admitted_this_turn,
                "failed_queries": failed_queries,
                "yield_reasons": sorted(set(yield_reasons)),
            }
        )
    if queries and len(provider_failures) == len(queries):
        raise provider_failures[0]
    if queries and admitted_this_turn == 0:
        reasons = ", ".join(sorted(set(yield_reasons))) or "retrieval failures"
        pivot = (
            "The last research turn admitted no new evidence "
            f"({reasons}). Do not paraphrase those queries. Pivot by changing "
            "the angle, source/domain, or specificity, and trace named works upstream."
        )
        state.feedback = f"{state.feedback} {pivot}".strip()


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
        if isinstance(raw, str):
            text, steer_id = raw, None
        elif isinstance(raw, Mapping):
            text = str(raw.get("text", "") or "")
            steer_id = raw.get("steer_id")
        else:
            text = str(getattr(raw, "text", "") or "")
            steer_id = getattr(raw, "steer_id", None)
            if not isinstance(steer_id, str) or not steer_id.strip():
                steer_id = None
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
    if not parsed.ready_to_write:
        warning = _upstream_zero_yield_warning(state)
        if warning:
            state.feedback = f"{state.feedback} {warning}".strip()
        return False
    unknown_ids = sorted(
        {
            evidence_id
            for item in state.coverage.get("covered", [])
            for evidence_id in item.get("evidence_ids", [])
            if evidence_id not in state.seen_ids
        }
    )
    supported_themes = sum(
        bool(item.get("angle"))
        and bool(item.get("evidence_ids"))
        and all(evidence_id in state.seen_ids for evidence_id in item["evidence_ids"])
        for item in state.coverage.get("covered", [])
    )
    if state.turns_completed < bound.minimum_research_turns:
        if not fresh:
            state.feedback = (
                "Readiness rejected: issue at least one new research query as a fresh "
                "pivot before trying again."
            )
        else:
            state.feedback = (
                f"Continue researching: at least {bound.minimum_research_turns} research turns "
                f"are required; {state.turns_completed} completed."
            )
    elif len(state.pool) < bound.minimum_useful_sources:
        state.feedback = (
            f"Continue researching: at least {bound.minimum_useful_sources} useful sources "
            f"are required; {len(state.pool)} admitted."
        )
    elif distinct_work_count(state.pool) < bound.min_evidence_sources:
        state.feedback = (
            f"Continue researching: at least {bound.min_evidence_sources} distinct works "
            f"are required; {distinct_work_count(state.pool)} admitted. Multiple passages "
            "or mirrors of one work do not count as independent evidence."
        )
    elif unknown_ids:
        state.feedback = (
            "Coverage references evidence ids that are not admitted: "
            + ", ".join(unknown_ids)
            + ". Cite only admitted ids."
        )
    elif supported_themes < bound.min_evidence_themes:
        state.feedback = (
            f"Continue the top-down coverage pass: at least {bound.min_evidence_themes} "
            f"evidence-backed major angles are required at this depth; {supported_themes} "
            "are recorded. Add only natural angles for this question and cite their "
            "admitted evidence ids."
        )
    elif state.coverage.get("open"):
        state.feedback = (
            "Resolve these critical open coverage gaps before marking readiness: "
            + "; ".join(str(gap) for gap in state.coverage["open"])
        )
    elif not state.coverage.get("contradictions_checked"):
        state.feedback = (
            "Before marking readiness, run a targeted search for the strongest "
            "published objection, negative result, or conflicting evidence and record "
            "what was checked in contradictions_checked."
        )
    else:
        state.trail.append({"kind": "ready", "turn": turn})
        return True
    warning = _upstream_zero_yield_warning(state)
    if warning:
        state.feedback = f"{state.feedback} {warning}".strip()
    if parsed.ready_to_write and not fresh:
        state.feedback = (
            state.feedback + " Readiness also requires at least one new research query as a fresh "
            "pivot before trying again."
        )
    state.trail.append({"kind": "ready_rejected", "turn": turn, "reason": state.feedback})
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
    turn = (
        max(
            (entry.get("turn", -1) for entry in state.trail if isinstance(entry.get("turn"), int)),
            default=-1,
        )
        + 1
    )
    while True:
        if should_cancel is not None and should_cancel():
            return "stopped"
        remaining = research_seconds_left(bound, started)
        if remaining <= 0:
            if state.pool:
                return "wall_clock"
            raise ResearchAgentError(
                "research exhausted its wall-clock budget without usable evidence"
            )
        if state.budget.remaining <= 0:
            if state.pool:
                return "sources"
            raise ResearchAgentError("research exhausted its source budget without usable evidence")
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
        if steer_ids:
            # A valid parse is the only point at which the host can say the
            # turn consumed the steer. Persist the fact before removing it
            # from the live queue, so malformed/retried turns keep receiving it.
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

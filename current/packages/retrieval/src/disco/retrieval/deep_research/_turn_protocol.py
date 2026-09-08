"""Private parser for the research turn response contract."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

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

from ._json_closure import close_json_containers
from ._output_ceiling import (
    EMPTY_SHAPES,
    EMPTY_TURN_REASK,
    OUTPUT_CEILING_REASK,
    TurnCeiling,
    TurnFailureShape,
    ceiling_hit,
    empty_turn_error,
    turn_failure_shape,
)
from ._source_inspection import Inspection, parse_inspections
from ._source_lookup import SOURCE_LOOKUP_INSTRUCTION

_MAX_QUERIES_PER_TURN = 3
_TURN_KEYS = ("queries", "ready_to_write")
_COVERAGE_KEYS = ("covered", "open", "contradictions_checked")


@dataclass(frozen=True)
class _Turn:
    brief: str
    decision_summary: str
    coverage: dict[str, Any]
    queries: tuple[str, ...] = ()
    ready_to_write: bool = False
    repairs: tuple[str, ...] = ()
    inspections: tuple[Inspection, ...] = ()


def _decode_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    candidate = text.strip()
    try:
        value: Any = json.loads(candidate)
    except json.JSONDecodeError as exc:
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
        if fence is not None:
            candidate = fence.group(1).strip()
        else:
            start, end = candidate.find("{"), candidate.rfind("}")
            if not (0 <= start < end):
                return None, f"not valid JSON: {exc}"
            candidate = candidate[start : end + 1]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None, f"not valid JSON: {exc}"
    if not isinstance(value, dict):
        return None, (f"the top-level JSON value must be an object, got {type(value).__name__}")
    return value, None


def _lift_misplaced_keys(value: dict[str, Any]) -> list[str]:
    coverage = value.get("coverage")
    if not isinstance(coverage, dict):
        return []
    moved: list[str] = []
    for key in _TURN_KEYS:
        if key in value or key not in coverage:
            continue
        value[key] = coverage.pop(key)
        moved.append(f'lifted "{key}" out of "coverage" (misplaced closing brace)')
    for key in _COVERAGE_KEYS:
        if key in coverage or key not in value:
            continue
        coverage[key] = value.pop(key)
        moved.append(f'moved "{key}" into "coverage" (misplaced closing brace)')
    return moved


def _parse_queries(value: dict[str, Any]) -> tuple[tuple[str, ...] | None, str | None]:
    raw = value.get("queries")
    if not isinstance(raw, list):
        return None, '"queries" must be a JSON array of 1-3 non-empty strings'
    queries = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    if not queries:
        return None, '"queries" must contain at least one non-empty string'
    return tuple(queries[:_MAX_QUERIES_PER_TURN]), None


def _coverage_list(raw: dict[str, Any], key: str) -> tuple[list[Any] | None, str | None]:
    if key not in raw or raw[key] is None:
        return [], None
    if not isinstance(raw[key], list):
        return None, (
            f'"coverage.{key}" must be a JSON array (got {type(raw[key]).__name__}); omit it '
            "entirely if you have nothing to put in it"
        )
    return raw[key], None


def _parse_finding(item: dict[str, Any]) -> dict[str, str] | str:
    """Preserve substantive analysis while allowing legacy angle/source-only records."""
    finding = item.get("finding")
    if finding is None:
        return {}
    if not isinstance(finding, str):
        return 'each covered item must have a string "finding" when supplied'
    return {"finding": finding.strip()}


def _parse_covered(items: list[Any]) -> list[dict[str, Any]] | str:
    covered: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            angle = item.strip()
            if not angle:
                return 'each covered item must have a non-empty string "angle"'
            covered.append({"angle": angle, "evidence_ids": []})
            continue
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("angle"), str)
            or not item["angle"].strip()
        ):
            return 'each covered item must have a string "angle"'
        evidence_ids = item.get("evidence_ids")
        if evidence_ids is None:
            evidence_ids = []
        if not isinstance(evidence_ids, list) or not all(
            isinstance(item_id, str) for item_id in evidence_ids
        ):
            return 'each covered item must have an "evidence_ids" string array'
        finding = _parse_finding(item)
        if isinstance(finding, str):
            return finding
        covered.append(
            {
                **finding,
                "angle": item["angle"].strip(),
                "evidence_ids": [item_id.strip() for item_id in evidence_ids if item_id.strip()],
            }
        )
    return covered


def _strings(items: list[Any]) -> list[str]:
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _parse_coverage(value: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    raw = value.get("coverage")
    if not isinstance(raw, dict):
        return None, '"coverage" must be an object with covered, open, and contradictions_checked'
    arrays: dict[str, list[Any]] = {}
    for key in _COVERAGE_KEYS:
        items, error = _coverage_list(raw, key)
        if items is None:
            return None, error
        arrays[key] = items
    covered = _parse_covered(arrays["covered"])
    if isinstance(covered, str):
        return None, covered
    return {
        "covered": covered,
        "open": _strings(arrays["open"]),
        "contradictions_checked": _strings(arrays["contradictions_checked"]),
    }, None


def _parse_readiness(value: dict[str, Any]) -> tuple[bool | None, str | None, str | None]:
    if "ready_to_write" not in value or value["ready_to_write"] is None:
        return False, 'read a missing "ready_to_write" as false', None
    ready = value["ready_to_write"]
    if not isinstance(ready, bool):
        return (
            None,
            None,
            (
                f'"ready_to_write" must be a boolean (got {type(ready).__name__}); '
                "omit it entirely when you are not ready"
            ),
        )
    return ready, None, None


def _parse_text_fields(value: dict[str, Any], *, expect_brief: bool) -> tuple[str, str, str | None]:
    brief = value.get("brief", "")
    if not isinstance(brief, str) or (expect_brief and not brief.strip()):
        return "", "", '"brief" must be a non-empty string on the first turn'
    decision = value.get("decision_summary")
    if not isinstance(decision, str) or not decision.strip():
        return "", "", '"decision_summary" must be a non-empty string'
    return brief.strip(), decision.strip(), None


def _turn_object(
    text: str, allow_closing_containers: bool
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    value, error = _decode_json_object(text)
    repairs: list[str] = []
    if value is None and allow_closing_containers:
        closed = close_json_containers(text)
        if closed is not None:
            value, _ = _decode_json_object(closed)
            if value is not None:
                repairs.append(
                    "added missing closing JSON container delimiters to a stopped response"
                )
    return value, error if value is None else None, repairs


def _parse_turn(
    text: str, *, expect_brief: bool, allow_closing_containers: bool = False
) -> tuple[_Turn | None, str | None]:
    value, error, repairs = _turn_object(text, allow_closing_containers)
    if value is None:
        return None, error
    repairs.extend(_lift_misplaced_keys(value))
    raw_brief, raw_decision, error = _parse_text_fields(value, expect_brief=expect_brief)
    if error is not None:
        return None, error
    coverage, error = _parse_coverage(value)
    if coverage is None:
        return None, error
    ready, repair, error = _parse_readiness(value)
    if ready is None:
        return None, error
    if repair is not None:
        repairs.append(repair)
    queries, error = _parse_queries(value)
    inspections, inspection_error = parse_inspections(value.get("inspect"))
    if inspection_error:
        return None, inspection_error
    if inspections and ready:
        return (
            None,
            'Use "inspect" with "ready_to_write": false; read the result next turn',
        )
    if queries is None and not ready and not inspections:
        return None, error
    return _Turn(
        brief=raw_brief.strip(),
        decision_summary=raw_decision.strip(),
        coverage=coverage,
        queries=queries or (),
        ready_to_write=ready,
        repairs=tuple(repairs),
        inspections=inspections,
    ), None


def _turn_response_format() -> str:
    """One action contract for both initial instructions and parser feedback."""
    common = {
        "brief": "current task interpretation and findings (required first, replace when revised)",
        "decision_summary": "the next action and why it is needed",
        "coverage": {"covered": [], "open": [], "contradictions_checked": []},
    }
    actions = (
        {"queries": ["new query"], "ready_to_write": False},
        {
            "inspect": [
                {"source_id": "an admitted source ID", "focus": "the qualification to check"}
            ],
            "queries": [],
            "ready_to_write": False,
        },
        {"queries": [], "ready_to_write": True},
    )
    return (
        "RESPONSE FORMAT: return ONE strict JSON object, with no fences or commentary. "
        "Include brief on the first turn; later, replace it when evidence changes your "
        "interpretation or findings, retaining source IDs and material qualifications. "
        "An omitted brief keeps the current version. Always include decision_summary and coverage; "
        "Each covered item records angle, finding (the answer supported so far, including "
        "material conditions and limits), and evidence_ids (admitted source IDs). "
        "A topic name or source list is not a finding. Revise findings when evidence "
        "changes them; keep unanswered parts in open even when another part is covered. "
        "Preserve your actual findings, open gaps and contradiction checks. "
        "Acquire sources with 1-3 queries: each is search terms or a complete HTTP(S) "
        "URL to read directly, without a search. Inspect 1-2 admitted sources, "
        "or combine those read-only actions. "
        "A narrative saying you will inspect is not an inspection: include the inspect array. "
        "These are complete examples of search, inspection and finish; replace their sample values "
        "with your actual state and source IDs:\n"
        + "\n".join(json.dumps({**common, **action}, ensure_ascii=False) for action in actions)
        + "\n"
        + SOURCE_LOOKUP_INSTRUCTION
        + "Inspection uses an admitted source ID. To search in the same turn, "
        "put 1-3 new queries in queries. Read both results next turn; "
        "inspection cannot be combined with finish."
    )


async def _complete_turn(
    router: LLMRouter, messages: list[LLMMessage], *, namespace: str, max_tokens: int
) -> tuple[str, CompletionResponse, int]:
    request = CompletionRequest(
        profile=CapabilityProfile(
            role=ModelRole.RAG_ANSWERER, requirements=frozenset({Requirement.JSON_MODE})
        ),
        messages=messages,
        temperature=0.0,
        response_format="json",
        max_tokens=max_tokens,
        # Leave reasoning selection to the configured provider/model.
        enable_thinking=None,
        metadata={"conversation_id": str(namespace)[:256], "inspect_stage": "research_turn"},
    )
    started = time.perf_counter()
    response = await router.complete(request)
    return (
        strip_think_spans(response.text),
        response,
        max(0, int((time.perf_counter() - started) * 1_000)),
    )


def _turn_payload(turn: _Turn | None) -> dict[str, Any] | None:
    if turn is None:
        return None
    return {
        "brief": turn.brief,
        "decision_summary": turn.decision_summary,
        "coverage": turn.coverage,
        "queries": list(turn.queries),
        "inspect": [
            {
                "source_id": item.source_id,
                "focus": item.focus,
                "start": item.start,
                "find": item.find,
            }
            for item in turn.inspections
        ],
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
        },
        response={"text": text},
        declared_decision=_turn_payload(parsed),
        latency_ms=latency_ms,
        usage=response.usage.model_dump(mode="json"),
        finish_reason=response.finish_reason,
        parse_error=error,
    )


@dataclass(frozen=True)
class _Attempt:
    turn: _Turn | None
    error: str | None
    ceiling_tokens: int | None
    shape: TurnFailureShape | None
    text: str = ""


@dataclass(frozen=True)
class _TurnResult:
    turn: _Turn | None
    error: str | None
    ceiling_tokens: int | None
    shape: TurnFailureShape | None
    ceiling: int


async def _attempt_turn(
    router: LLMRouter,
    messages: list[LLMMessage],
    *,
    expect_brief: bool,
    namespace: str,
    attempt: int,
    ceiling: TurnCeiling,
) -> _Attempt:
    cap = ceiling.tokens
    text, response, latency_ms = await _complete_turn(
        router, messages, namespace=namespace, max_tokens=cap
    )
    parsed, error = _parse_turn(
        text,
        expect_brief=expect_brief,
        allow_closing_containers=response.finish_reason == "stop",
    )
    hit = ceiling_hit(response, cap=cap)
    shape = None
    if hit is not None or parsed is None:
        shape = turn_failure_shape(
            finish_reason=response.finish_reason, content_chars=len(text.strip())
        )
        parsed = None
        if shape in EMPTY_SHAPES:
            error = empty_turn_error(finish_reason=response.finish_reason, ceiling=cap)
        elif hit is not None:
            error = OUTPUT_CEILING_REASK
    _record_turn_io(
        namespace,
        messages,
        text,
        response,
        parsed=parsed,
        error=error,
        attempt=attempt,
        latency_ms=latency_ms,
    )
    ceiling.grow_after(
        finish_reason=response.finish_reason,
        content_chars=len(text.strip()),
        output_tokens=response.usage.output_tokens,
    )
    return _Attempt(parsed, None if parsed is not None else error, hit, shape, text)


def _reask(attempt: _Attempt, *, expect_brief: bool) -> str:
    problem = (
        EMPTY_TURN_REASK
        if attempt.shape in EMPTY_SHAPES
        else f"Your response could not be used: {attempt.error}"
    )
    return (
        f"{problem}. No action from it was applied; "
        "the retained evidence and remaining work are shown above. "
        + ("This is still the first turn; include brief. " if expect_brief else "")
        + _turn_response_format()
    )


async def _one_model_turn(
    router: LLMRouter,
    system_prompt: str,
    user_message: str,
    *,
    expect_brief: bool,
    namespace: str,
    ceiling: TurnCeiling,
) -> _TurnResult:
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_message),
    ]
    first = await _attempt_turn(
        router, messages, expect_brief=expect_brief, namespace=namespace, attempt=1, ceiling=ceiling
    )
    if first.turn is not None:
        return _TurnResult(first.turn, None, first.ceiling_tokens, None, ceiling.tokens)
    second = await _attempt_turn(
        router,
        [
            *messages,
            LLMMessage(role="assistant", content=first.text),
            LLMMessage(role="user", content=_reask(first, expect_brief=expect_brief)),
        ],
        expect_brief=expect_brief,
        namespace=namespace,
        attempt=2,
        ceiling=ceiling,
    )
    return _TurnResult(
        second.turn,
        second.error,
        first.ceiling_tokens or second.ceiling_tokens,
        second.shape,
        ceiling.tokens,
    )

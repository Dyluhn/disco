"""Strict JSON turn parsing for the research agent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_QUERIES_PER_TURN = 3


@dataclass(frozen=True)
class Turn:
    brief: str
    decision_summary: str
    coverage: dict[str, Any]
    queries: tuple[str, ...] = ()
    ready_to_write: bool = False


def decode_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
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
        return None, f"the top-level JSON value must be an object, got {type(value).__name__}"
    return value, None


def parse_queries(value: dict[str, Any]) -> tuple[tuple[str, ...] | None, str | None]:
    raw = value.get("queries")
    if not isinstance(raw, list):
        return None, '"queries" must be a JSON array of 1-3 non-empty strings'
    queries = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    if not queries:
        return None, '"queries" must contain at least one non-empty string'
    return tuple(queries[:MAX_QUERIES_PER_TURN]), None


def parse_coverage(value: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    raw = value.get("coverage")
    if not isinstance(raw, dict):
        return None, '"coverage" must be an object with covered, open, and contradictions_checked'
    covered_raw, open_raw, checked_raw = (
        raw.get(name) for name in ("covered", "open", "contradictions_checked")
    )
    if not all(isinstance(field, list) for field in (covered_raw, open_raw, checked_raw)):
        return None, '"coverage" fields covered, open, and contradictions_checked must be arrays'
    assert isinstance(covered_raw, list)
    assert isinstance(open_raw, list)
    assert isinstance(checked_raw, list)
    covered: list[dict[str, Any]] = []
    for item in covered_raw:
        if isinstance(item, str) and not item.strip():
            return None, 'each covered item must have a non-empty string "angle"'
        parsed = _covered_item(item)
        if parsed is None:
            return None, 'each covered item must have a string "angle"'
        covered.append(parsed)
    return {
        "covered": covered,
        "open": _strings(open_raw),
        "contradictions_checked": _strings(checked_raw),
    }, None


def _covered_item(item: object) -> dict[str, Any] | None:
    if isinstance(item, str):
        angle = item.strip()
        return {"angle": angle, "evidence_ids": []} if angle else None
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("angle"), str)
        or not item["angle"].strip()
    ):
        return None
    evidence_ids = item.get("evidence_ids") or []
    if not isinstance(evidence_ids, list) or not all(
        isinstance(item_id, str) for item_id in evidence_ids
    ):
        return None
    return {
        "angle": item["angle"].strip(),
        "evidence_ids": [item_id.strip() for item_id in evidence_ids if item_id.strip()],
    }


def _strings(items: list[object]) -> list[str]:
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def parse_turn(text: str, *, expect_brief: bool) -> tuple[Turn | None, str | None]:
    value, error = decode_json_object(text)
    if value is None:
        return None, error
    raw_brief = value.get("brief", "")
    if not isinstance(raw_brief, str) or (expect_brief and not raw_brief.strip()):
        return None, '"brief" must be a non-empty string on the first turn'
    raw_decision = value.get("decision_summary")
    if not isinstance(raw_decision, str) or not raw_decision.strip():
        return None, '"decision_summary" must be a non-empty string'
    coverage, error = parse_coverage(value)
    if coverage is None:
        return None, error
    ready = value.get("ready_to_write")
    if not isinstance(ready, bool):
        return None, '"ready_to_write" must be a boolean'
    queries, error = parse_queries(value)
    if queries is None and not ready:
        return None, error
    return Turn(raw_brief.strip(), raw_decision.strip(), coverage, queries or (), ready), None


def turn_payload(turn: Turn | None) -> dict[str, Any] | None:
    if turn is None:
        return None
    return {
        "brief": turn.brief,
        "decision_summary": turn.decision_summary,
        "coverage": turn.coverage,
        "queries": list(turn.queries),
        "ready_to_write": turn.ready_to_write,
    }

"""Evidence readers and helpers for efficiency observability."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..events import KIND_ACTION, KIND_CONDENSATION, KIND_STATUS, kind_of
from ..oracles.thrash import repair_spans
from ..oracles.tool_scope import turn_mode_counts
from ..provider_ledger import record_applies_to_conversation

# The product stamps this prefix on the status detail when a verification pass
# made no progress. It is a work-efficiency signal, never a verdict here.
_VERIFY_NO_PROGRESS_PREFIX = "verify_no_progress:"


def oracle_facts(oracle_results: Any, oracle: str) -> tuple[dict[str, Any] | None, str | None]:
    """The facts dict and status of the LAST result published by ``oracle``.

    The last one wins because an oracle may publish a preliminary SKIP followed
    by the substantive result (ToolScopeOracle does exactly this).
    """
    facts: dict[str, Any] | None = None
    status: str | None = None
    if not isinstance(oracle_results, list):
        return None, None
    for result in oracle_results:
        if not isinstance(result, dict) or result.get("oracle") != oracle:
            continue
        candidate = result.get("facts")
        if isinstance(candidate, dict):
            facts = candidate
            status = str(result.get("status") or "") or None
    return facts, status


def int_fact(facts: dict[str, Any] | None, key: str) -> int | None:
    """An int fact, or None when absent/not-an-int. Never coerces a bool."""
    if not facts:
        return None
    value = facts.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def event_epoch(event: dict[str, Any]) -> float | None:
    stamp = event.get("timestamp") or event.get("created_at")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def event_span_seconds(events: Sequence[dict[str, Any]]) -> float | None:
    """Wall-clock span of the conversation, from its own durable timestamps."""
    epochs = [e for e in (event_epoch(ev) for ev in events) if e is not None]
    if len(epochs) < 2:
        return None
    return round(max(epochs) - min(epochs), 3)


def token_totals(trace: dict[str, Any] | None) -> dict[str, int] | None:
    """Summed token counts across ``agent.step`` end spans, or None."""
    if not isinstance(trace, dict):
        return None
    spans = trace.get("spans")
    if not isinstance(spans, list):
        return None
    totals = {"in": 0, "out": 0, "cached": 0}
    seen = False
    for span in spans:
        if not isinstance(span, dict) or span.get("span") != "agent.step":
            continue
        if span.get("event") != "end":
            continue
        for key, field in (("in", "in_tokens"), ("out", "out_tokens"), ("cached", "cached_tokens")):
            value = span.get(field)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            totals[key] += value
            seen = True
    return totals if seen else None


def provider_call_split(ledger: Any, conversation_id: str | None) -> dict[str, int | None]:
    """Split a run's ledger slice into what it can and cannot claim as its own."""
    if not isinstance(ledger, list):
        return {
            "conversation_bound": None,
            "unattributed_in_window": None,
            "foreign_excluded": None,
            "total_attributed": None,
        }
    bound = 0
    unattributed = 0
    foreign = 0
    for record in ledger:
        if not isinstance(record, dict):
            continue
        cid = record.get("conversation_id")
        if cid is None or str(cid).strip() == "":
            if record_applies_to_conversation(record, conversation_id):
                unattributed += 1
            continue
        if str(cid).strip() == str(conversation_id or "").strip():
            bound += 1
        else:
            foreign += 1
    return {
        "conversation_bound": bound,
        "unattributed_in_window": unattributed,
        "foreign_excluded": foreign,
        "total_attributed": bound + unattributed,
    }


def no_progress_events(events: Sequence[dict[str, Any]]) -> int:
    return sum(
        1
        for event in events
        if kind_of(event) == KIND_STATUS
        and str(event.get("detail") or "").startswith(_VERIFY_NO_PROGRESS_PREFIX)
    )


def polling_samples(trace: dict[str, Any] | None) -> int | None:
    """How many times the harness POLLED. Reported so it is visibly not a turn."""
    if not isinstance(trace, dict):
        return None
    aggregation = trace.get("aggregation")
    if not isinstance(aggregation, dict):
        return None
    return int_fact(aggregation, "sample_count")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path) -> list[dict[str, Any]] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def count_actions(events: Sequence[dict[str, Any]]) -> int:
    return sum(1 for e in events if kind_of(e) == KIND_ACTION)


def count_compactions(events: Sequence[dict[str, Any]]) -> int:
    return sum(1 for e in events if kind_of(e) == KIND_CONDENSATION)


def count_repair_spans(trace: dict[str, Any] | None) -> int | None:
    if not isinstance(trace, dict):
        return None
    spans = trace.get("spans")
    if not isinstance(spans, list):
        return None
    return len(repair_spans(spans))


def turn_counts(trace: dict[str, Any] | None) -> dict[str, int]:
    return turn_mode_counts((trace or {}).get("tool_scopes"))

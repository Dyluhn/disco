"""Reconstruct bounded search I/O records from the observed event stream.

The public conversation stream historically exposed only the lead agent's
``search`` action.  Newer diagnostic producers may emit one structured
``search_io``/``retrieval`` record containing the provider response.  This
adapter accepts both shapes so old cassettes remain replayable and a run never
claims that an absent provider response was an empty result.

Only retrieval metadata is copied.  Agent thoughts, prompts, and free-form
messages are deliberately ignored; this is an operator diagnostic, not a
hidden-reasoning trace.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._observe import ObservationEvent, redact

_MAX_TEXT = 240
_MAX_HITS = 32
_MAX_EXTRACT = 32
_ERROR_CLASS_RE = re.compile(
    r"^(?:anti_bot|paywalled|not_found|timeout|empty_content|other|"
    r"redirect/http_[3-5][0-9]{2}|upstream_http_[45][0-9]{2})$"
)
_SECRET_QUERY_KEYS = {"token", "access_token", "api_key", "apikey", "key", "sig", "signature"}
_STRUCTURED_KINDS = frozenset(
    {
        "search_io",
        "search_trace",
        "retrieval",
        "retrieval_io",
        "search_result",
        "retrieval_result",
    }
)


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    return value[:limit] + ("…" if len(value) > limit else "")


def _safe_url(value: Any) -> str | None:
    """Keep source identity while removing credentials and secret query args."""
    raw = _text(value, 2_000)
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return raw[:_MAX_TEXT]
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        query = [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in _SECRET_QUERY_KEYS
        ]
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))[:_MAX_TEXT]
    except (TypeError, ValueError):
        return raw[:_MAX_TEXT]


def _safe_error_class(value: Any) -> str | None:
    """Retain only the bounded extraction classes emitted by retrieval."""

    candidate = str(value or "").strip().casefold()
    return candidate if _ERROR_CLASS_RE.fullmatch(candidate) else None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _nested_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the event body while retaining the outer frame as a fallback."""
    # Public tool observations are framed as event -> tool_result ->
    # structured.  Flatten those wrappers first; each is a transport envelope,
    # not a separate search operation.
    merged_deep = dict(value)
    current: Mapping[str, Any] = value
    for key in ("event", "tool_result", "structured"):
        nested = current.get(key)
        if not isinstance(nested, Mapping):
            continue
        merged_deep.update(nested)
        current = nested
    value = merged_deep
    for key in (
        "event",
        "search_io",
        "retrieval",
        "retrieval_trace",
        "trace",
        "result",
        "response",
        "data",
    ):
        nested = value.get(key)
        if isinstance(nested, Mapping) and (
            any(k in nested for k in ("query", "planned_query", "raw_hits", "hits", "passages"))
            or key in {"search_io", "retrieval", "retrieval_trace"}
        ):
            merged = dict(value)
            merged.update(nested)
            return merged
    return value


def _event_body(
    event: ObservationEvent | Mapping[str, Any],
) -> tuple[Mapping[str, Any], int | None, int | None, str | None]:
    if isinstance(event, ObservationEvent):
        return event.payload, event.elapsed_ms, event.seq, event.kind
    payload = event.get("payload")
    body: Mapping[str, Any] = payload if isinstance(payload, Mapping) else event
    elapsed = event.get("elapsed_ms")
    seq = event.get("seq")
    kind = event.get("kind")
    return (
        body,
        elapsed if isinstance(elapsed, int) else None,
        seq if isinstance(seq, int) else None,
        str(kind) if kind is not None else None,
    )


def _kind(body: Mapping[str, Any]) -> str:
    nested = body.get("event")
    if isinstance(nested, Mapping):
        return str(nested.get("kind") or body.get("kind") or body.get("type") or "").casefold()
    return str(body.get("kind") or body.get("type") or body.get("name") or "").casefold()


def _action_args(body: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Extract structured search tool arguments from a public action frame."""
    candidates: list[Any] = [body]
    nested = body.get("event")
    if isinstance(nested, Mapping):
        candidates.append(nested)
    for candidate in candidates:
        tool_call = candidate.get("tool_call") if isinstance(candidate, Mapping) else None
        if not isinstance(tool_call, Mapping):
            continue
        name = str(tool_call.get("tool_name") or tool_call.get("name") or "").casefold()
        args = tool_call.get("arguments")
        if name == "search" and isinstance(args, Mapping):
            return args
    return None


def _hits(value: Any) -> tuple[int | None, list[dict[str, Any]]]:
    if not isinstance(value, list):
        return None, []
    rows: list[dict[str, Any]] = []
    for hit in value[:_MAX_HITS]:
        if not isinstance(hit, Mapping):
            continue
        row: dict[str, Any] = {}
        title = _first(hit, "title", "source_title", "name")
        url = _first(hit, "url", "source_url", "link")
        engine = _first(hit, "source_engine", "engine", "provider")
        rank = _first(hit, "rank", "position")
        if title is not None:
            row["title"] = _text(title)
        if url is not None:
            row["url"] = _safe_url(url)
        if engine is not None:
            row["engine"] = _text(engine, 80)
        if rank is not None:
            row["rank"] = rank
        status = _first(hit, "status", "outcome")
        if status is not None:
            row["status"] = _text(status, 80)
        snippet = hit.get("snippet")
        if isinstance(snippet, Mapping):
            snippet_text = _first(snippet, "text", "content", "value")
        else:
            snippet_text = snippet
        if snippet_text is not None:
            # Search snippets are provider text, never citable evidence.
            row["snippet"] = {"text": _text(snippet_text), "untrusted": True}
        rows.append(row)
    return len(value), rows


def _extract(value: Any) -> tuple[int | None, int | None, dict[str, int], list[dict[str, Any]]]:
    if not isinstance(value, list):
        return None, None, {}, []
    statuses: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    ok = 0
    for item in value[:_MAX_EXTRACT]:
        if not isinstance(item, Mapping):
            continue
        status = str(
            _first(item, "status", "outcome") or ("ok" if item.get("fetched_ok") else "error")
        )
        status = status.casefold()
        statuses[status] = statuses.get(status, 0) + 1
        ok += status == "ok"
        row = {"url": _safe_url(_first(item, "url", "source_url")), "status": status}
        error_class = _safe_error_class(_first(item, "error_class"))
        if error_class is not None:
            row["error_class"] = error_class
        if _first(item, "error", "detail"):
            row["error"] = _text(_first(item, "error", "detail"))
        rows.append(row)
    return len(value), ok, statuses, rows


def _passage_count(body: Mapping[str, Any]) -> int | None:
    value = _first(body, "passage_count", "passages_count")
    if isinstance(value, int):
        return value
    passages = _first(body, "passages", "candidates")
    return len(passages) if isinstance(passages, list) else None


def _is_structured(body: Mapping[str, Any]) -> bool:
    kind = _kind(body)
    if kind in _STRUCTURED_KINDS:
        return True
    return any(
        key in body
        for key in (
            "raw_hits",
            "raw_discovered_hit_count",
            "provider_outcome",
            "provider_diagnostic",
            "provider_error",
            "extraction_statuses",
            "admission_count",
            "retrieval_trace",
        )
    )


def _diagnostic(value: Any) -> dict[str, Any] | None:
    """Keep named provider degradation facts without copying arbitrary text."""
    if not isinstance(value, Mapping):
        return None
    allowed = {
        "provider_error",
        "error",
        "detail",
        "message",
        "status",
        "outcome",
        "status_code",
        "result_count",
        "latency_ms",
        "unresponsive_engines",
        "failed_engines",
    }
    output: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text == "providers" and isinstance(item, Mapping):
            output[key_text] = {
                _text(provider, 100) or "unknown": _diagnostic(details) or {}
                for provider, details in list(item.items())[:16]
            }
            continue
        if key_text not in allowed:
            continue
        if isinstance(item, list):
            output[key_text] = [_text(entry, 100) for entry in item[:16]]
        elif isinstance(item, Mapping):
            output[key_text] = _diagnostic(item) or {}
        elif isinstance(item, (int, float, bool)):
            output[key_text] = item
        else:
            output[key_text] = _text(item, 240)
    return redact(output) if output else None


def _diagnostic_value(value: Any, *keys: str) -> Any:
    """Find one named fact in the bounded provider diagnostic tree."""
    if not isinstance(value, Mapping):
        return None
    found = _first(value, *keys)
    if found is not None:
        return found
    for nested in value.values():
        found = _diagnostic_value(nested, *keys)
        if found is not None:
            return found
    return None


def _query_fields(body: Mapping[str, Any], args: Mapping[str, Any] | None) -> tuple[Any, Any]:
    planned = (
        args.get("query")
        if args is not None and args.get("query")
        else _first(body, "planned_query", "query", "plan_query", "subquestion")
    )
    issued = _first(body, "issued_query", "actual_query", "post_transform_query", "executed_query")
    issued_queries = _first(body, "issued_queries")
    if issued is None and isinstance(issued_queries, list) and issued_queries:
        issued = issued_queries[0]
    return planned, issued


def _extraction_fields(
    body: Mapping[str, Any],
) -> tuple[Any, Any, dict[str, int], list[dict[str, Any]]]:
    value = _first(body, "extractions", "extracted", "extraction_results", "documents")
    count, ok, statuses, rows = _extract(value)
    if count is not None:
        return count, ok, statuses, rows
    summary = body.get("extraction")
    if isinstance(summary, Mapping):
        count = _first(summary, "attempted", "count")
        ok = _first(summary, "success", "ok")
        statuses = _status_summary(summary.get("statuses"), statuses, rows)
        if isinstance(summary.get("statuses"), list):
            _, _, statuses, rows = _extract(summary["statuses"])
    raw_statuses = _first(body, "extraction_statuses")
    if isinstance(raw_statuses, Mapping):
        statuses = dict(raw_statuses)
    return (
        _first(body, "extraction_count", "extraction_attempted") if count is None else count,
        _first(body, "extraction_ok_count", "successful_extractions") if ok is None else ok,
        statuses,
        rows,
    )


def _status_summary(
    value: Any, statuses: dict[str, int], rows: list[dict[str, Any]]
) -> dict[str, int]:
    if isinstance(value, Mapping):
        return dict(value)
    return statuses


def _provider_fields(body: Mapping[str, Any]) -> tuple[Any, Any, dict[str, Any] | None]:
    diagnostic = _diagnostic(_first(body, "provider_diagnostic", "provider_diagnostics"))
    error = _first(body, "provider_error", "error", "exception", "failure")
    if error is None and diagnostic:
        error = _diagnostic_value(diagnostic, "provider_error", "error", "detail", "message")
    outcome = _first(body, "provider_outcome", "outcome", "result_status", "status")
    if isinstance(outcome, Mapping):
        error = error or _first(outcome, "error", "detail", "message")
        outcome = _first(outcome, "status", "outcome", "ok")
    if error:
        outcome = "error"
    elif diagnostic and _diagnostic_value(diagnostic, "unresponsive_engines", "failed_engines"):
        outcome = "degraded"
    elif outcome is None:
        outcome = (
            "ok"
            if isinstance(body.get("ok"), bool) and body["ok"]
            else "error"
            if isinstance(body.get("ok"), bool)
            else None
        )
    return outcome, error, diagnostic


def _record_from(
    body: Mapping[str, Any], *, elapsed_ms: int | None, seq: int | None, source: str | None
) -> dict[str, Any] | None:
    args = _action_args(body)
    if args is not None:
        body = {**body, **args}
    planned, issued = _query_fields(body, args)
    if planned is None and issued is None:
        return None
    raw_value = _first(body, "raw_hits", "hits", "results", "all_hits")
    raw_count, raw_hits = _hits(raw_value)
    if raw_count is None:
        raw_count = _first(body, "raw_discovered_hit_count", "hit_count", "rows")
    extraction_count, extraction_ok, extraction_statuses, extraction_rows = _extraction_fields(body)
    extraction_error_classes: dict[str, int] = {}
    for extraction_row in extraction_rows:
        error_class = extraction_row.get("error_class")
        if isinstance(error_class, str):
            extraction_error_classes[error_class] = extraction_error_classes.get(error_class, 0) + 1
    provider_outcome, provider_error, provider_diagnostic = _provider_fields(body)
    if provider_outcome is None:
        provider_outcome = "ok" if raw_count is not None else "not_observed"
    row: dict[str, Any] = {
        "seq": seq,
        "elapsed_ms": elapsed_ms,
        "round": _first(body, "round", "round_no"),
        "turn": _first(body, "turn", "turn_no"),
        "planned_query": _text(planned),
        "issued_query": _text(issued),
        "provider": _text(_first(body, "provider", "provider_name", "source_engine"), 100),
        "provider_outcome": _text(provider_outcome, 80),
        "provider_error": _text(provider_error),
        "provider_diagnostic": provider_diagnostic,
        "raw_hit_count": raw_count,
        "raw_hits": raw_hits,
        "extraction_count": extraction_count,
        "extraction_ok_count": extraction_ok,
        "extraction_statuses": extraction_statuses,
        "extraction_error_classes": extraction_error_classes,
        "extractions": extraction_rows,
        "passage_count": _first(body, "reranked_passage_count")
        if _first(body, "reranked_passage_count") is not None
        else _passage_count(body),
        "admission_count": _first(
            body,
            "admission_count",
            "admitted",
            "added",
            "new_admitted",
            "final_admitted_count",
        ),
        "yield_reason": _text(_first(body, "yield_reason", "yield", "no_yield_reason"), 120),
        "latency_ms": _first(body, "latency_ms", "duration_ms", "elapsed_ms_provider")
        or _diagnostic_value(provider_diagnostic, "latency_ms"),
        "source": source,
    }
    return redact(row)


def _variants(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Expand one retrieval observation into its per-issued-query records."""
    base = _nested_payload(body)
    trace = base.get("retrieval_trace")
    if not isinstance(trace, Mapping):
        return [base]
    queries = trace.get("queries")
    if not isinstance(queries, list) or not queries:
        return [base]
    variants: list[Mapping[str, Any]] = []
    root_extraction = trace.get("extraction")
    for query in queries:
        if not isinstance(query, Mapping):
            continue
        merged = dict(base)
        merged.update(query)
        _merge_variant_extraction(merged, query, root_extraction)
        # Per-query trace carries extraction/hits; observation carries these
        # control facts and remains the fallback when a producer omits them.
        variants.append(merged)
    return variants or [base]


def _merge_variant_extraction(
    merged: dict[str, Any], query: Mapping[str, Any], root_extraction: object
) -> None:
    query_extraction = query.get("extraction")
    if not isinstance(query_extraction, Mapping) or not isinstance(root_extraction, Mapping):
        return
    statuses = root_extraction.get("statuses")
    if isinstance(statuses, list):
        hit_urls = {
            _safe_url(hit.get("url"))
            for hit in query.get("hits", [])
            if isinstance(hit, Mapping) and hit.get("url")
        }
        statuses = [
            status
            for status in statuses
            if isinstance(status, Mapping) and _safe_url(status.get("url")) in hit_urls
        ]
    merged["extraction"] = {**query_extraction, "statuses": statuses or []}


def collect_search_io(
    events: Sequence[ObservationEvent | Mapping[str, Any]],
    inspect_trace: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return one bounded record per observed search request/response.

    Structured response records supersede action-only records with the same
    ``(round, turn, query)`` key.  Consequently adding backend diagnostics does
    not double-count existing cassettes.
    """
    rows: list[dict[str, Any]] = []
    candidates = _search_candidates(events, inspect_trace)
    for body, elapsed, seq, source in candidates:
        nested = body.get("event") if isinstance(body.get("event"), Mapping) else body
        probe = _nested_payload(nested if isinstance(nested, Mapping) else body)
        structured = _is_structured(probe)
        if not _is_search_candidate(body, probe, structured):
            continue
        for variant in _variants(body):
            _append_search_row(rows, variant, elapsed, seq, source, probe, structured)
    return rows


def _search_candidates(
    events: Sequence[ObservationEvent | Mapping[str, Any]], inspect_trace: Mapping[str, Any] | None
) -> list[tuple[Mapping[str, Any], int | None, int | None, str | None]]:
    candidates = [(*_event_body(event),) for event in events]
    if isinstance(inspect_trace, Mapping):
        trace = inspect_trace.get("inspect_trace")
        trace = trace if isinstance(trace, Mapping) else inspect_trace
        for item in trace.get("events", []) if isinstance(trace, Mapping) else []:
            if isinstance(item, Mapping):
                candidates.append((item, item.get("elapsed_ms"), item.get("seq"), "inspect"))
    return candidates


def _is_search_candidate(
    body: Mapping[str, Any], probe: Mapping[str, Any], structured: bool
) -> bool:
    simple = _kind(probe) in {"search", "search_result"} and _first(
        probe, "query", "planned_query", "subquestion"
    )
    return structured or _action_args(body) is not None or bool(simple)


def _append_search_row(
    rows: list[dict[str, Any]],
    variant: Mapping[str, Any],
    elapsed: int | None,
    seq: int | None,
    source: str | None,
    probe: Mapping[str, Any],
    structured: bool,
) -> None:
    row = _record_from(variant, elapsed_ms=elapsed, seq=seq, source=source)
    if row is None:
        _attach_legacy_extraction(rows, probe)
        return
    action_key = (
        row.get("round"),
        row.get("turn"),
        row.get("planned_query") or row.get("issued_query"),
    )
    if structured:
        _replace_structured_row(rows, row, action_key)
    elif not any(_action_key(prior) == action_key for prior in rows):
        rows.append(row)


def _action_key(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return row.get("round"), row.get("turn"), row.get("planned_query") or row.get("issued_query")


def _attach_legacy_extraction(rows: list[dict[str, Any]], probe: Mapping[str, Any]) -> None:
    if _kind(probe) not in {"extract", "fetch"} or not rows:
        return
    prior = rows[-1]
    count = _first(probe, "passage_count", "passages_count")
    prior.update(
        extraction_count=1,
        extraction_ok_count=1 if count else 0,
        extraction_statuses={"ok": 1} if count else {"error": 1},
        passage_count=count if count is not None else prior.get("passage_count"),
    )


def _replace_structured_row(
    rows: list[dict[str, Any]], row: dict[str, Any], action_key: tuple[Any, Any, Any]
) -> None:
    response_key = (*action_key, row.get("issued_query"))
    for index, prior in enumerate(rows):
        prior_key = (*_action_key(prior), prior.get("issued_query"))
        if prior_key == response_key and prior.get("provider_outcome") != "not_observed":
            rows[index] = row
            return
    rows[:] = [
        prior
        for prior in rows
        if not (
            _action_key(prior) == action_key and prior.get("provider_outcome") == "not_observed"
        )
    ]
    rows.append(row)


def render_search_timeline(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render a compact operator-facing timeline; details stay in JSONL."""
    lines = [
        "# Deep Research search timeline",
        "",
        "| # | Round/turn | Planned → issued | Provider | Outcome | Hits | Extract | "
        "Passages | Admitted | Yield | Latency |",
        "| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for index, row in enumerate(rows, 1):
        planned = str(row.get("planned_query") or "?").replace("|", "\\|")
        issued = str(row.get("issued_query") or "?").replace("|", "\\|")
        provider = str(row.get("provider") or "?").replace("|", "\\|")
        outcome = str(row.get("provider_outcome") or "?").replace("|", "\\|")
        lines.append(
            f"| {index} | {row.get('round') or '?'} / {row.get('turn') or '?'} | "
            f"{planned} → {issued} | {provider} | {outcome} | "
            f"{row.get('raw_hit_count') if row.get('raw_hit_count') is not None else '?'} | "
            f"{row.get('extraction_count') if row.get('extraction_count') is not None else '?'} | "
            f"{row.get('passage_count') if row.get('passage_count') is not None else '?'} | "
            f"{row.get('admission_count') if row.get('admission_count') is not None else '?'} | "
            f"{str(row.get('yield_reason') or '—').replace('|', '\\|')} | "
            f"{row.get('latency_ms') if row.get('latency_ms') is not None else '?'} |"
        )
    if not rows:
        lines.extend(
            [
                "",
                "No structured search I/O was observed. Existing action-only cassettes expose "
                "planned queries in `events.jsonl`; provider responses were not part "
                "of that capture.",
            ]
        )
    return "\n".join(lines) + "\n"


__all__ = ["collect_search_io", "render_search_timeline"]

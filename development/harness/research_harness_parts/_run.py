"""Run/batch orchestration, report rendering, and artifact writing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from ..cassette import Cassette
from ._checks import (
    _depth_word_bounds,
    _final_candidate,
    _is_stopped_checkpoint,
    _report_word_count,
    check_invariants,
    normalize_report,
)
from ._observe import Observation, ObservationEvent, ResearchRequest, _phase_counts, redact
from ._quality_metrics import report_quality_metrics
from ._search_io import collect_search_io, render_search_timeline
from ._thrash import analyze_thrash
from ._transports import ResearchTransport, _transport_for

SCHEMA_VERSION = 2


class HarnessFailure(RuntimeError):
    """A run failed an invariant or could not obtain a provider response."""


@dataclass
class ResearchRunResult:
    ok: bool
    request: dict[str, Any]
    report: dict[str, Any]
    report_markdown: str
    events: list[ObservationEvent]
    probes: list[dict[str, Any]]
    errors: list[str]
    bounds: list[str]
    invariants: dict[str, bool]
    telemetry: dict[str, Any]
    run_id: str | None = None
    batch_id: str | None = None
    model_io: list[dict[str, Any]] = field(default_factory=list)
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)
    search_io: list[dict[str, Any]] = field(default_factory=list)
    inspect_trace: dict[str, Any] | None = None
    thrash: dict[str, Any] = field(default_factory=dict)
    deck: dict[str, Any] | None = None
    artifacts: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return redact(
            {
                "schema_version": SCHEMA_VERSION,
                "ok": self.ok,
                "request": self.request,
                "report": self.report,
                "report_markdown": self.report_markdown,
                "events": [asdict(event) for event in self.events],
                "probes": self.probes,
                "errors": self.errors,
                "bounds": self.bounds,
                "invariants": self.invariants,
                "telemetry": self.telemetry,
                "run_id": self.run_id,
                "batch_id": self.batch_id,
                "model_io": self.model_io,
                "provider_attempts": self.provider_attempts,
                "search_io": self.search_io,
                "inspect_trace": self.inspect_trace,
                "thrash": self.thrash,
                "deck": self.deck,
                "artifacts": self.artifacts,
            }
        )


async def run_harness(
    request: ResearchRequest,
    *,
    transport: ResearchTransport | None = None,
    output_dir: str | Path | None = None,
    run_id: str | None = None,
    batch_id: str | None = None,
) -> ResearchRunResult:
    """Run one request, observe it, normalize its report, and write artifacts."""
    observer = Observation()
    transport = transport or _transport_for(request)
    frames: list[dict[str, Any]] = []
    try:
        frames = await transport.collect(request, observer)
    except Exception as exc:  # provider/network/replay failures are run failures
        observer.errors.append(f"{type(exc).__name__}: {exc}")
    report = normalize_report(frames, request)
    observer.finalize(report)
    deck = await _collect_deck(request, transport, frames, observer.errors, report)
    search_io = collect_search_io(observer.events, observer.inspect_trace)
    # A provider/control failure may terminate the transport before it emits a
    # report frame.  There is then no report to serialize or validate: doing so
    # would construct a blank ``ReportEvent`` and append a misleading contract
    # error after the real terminal error.  Preserve the single observed error
    # and let the normal invariants describe the absent report.  If a report
    # frame did arrive, keep validating it so malformed successful output still
    # fails visibly.
    markdown = _render_run_report(report, observer.errors, frames)
    invariants, thrash = _run_diagnostics(request, report, markdown, observer)
    report_words = _report_word_count(report)
    quality = report_quality_metrics(report)
    telemetry = _run_telemetry(
        request, observer, search_io, markdown, report_words, quality, run_id, batch_id, deck
    )
    target = _depth_word_bounds(request.depth)
    invariants["depth_length_target"] = bool(
        target and target[0] <= report_words <= target[1]
    ) or _is_stopped_checkpoint(report)
    contract_invariants = {
        name: passed for name, passed in invariants.items() if name != "thrash_clean"
    }
    ok = not observer.errors and all(contract_invariants.values())
    result = ResearchRunResult(
        ok=ok,
        request=redact(asdict(request)),
        report=report,
        report_markdown=markdown,
        events=observer.events,
        probes=observer.probes,
        errors=observer.errors,
        bounds=sorted(set(observer.bounds)),
        invariants=invariants,
        telemetry=telemetry,
        run_id=run_id,
        batch_id=batch_id,
        model_io=observer.model_io,
        provider_attempts=observer.provider_attempts,
        search_io=search_io,
        inspect_trace=observer.inspect_trace,
        thrash=thrash,
        deck=deck,
    )
    if output_dir is not None:
        write_artifacts(result, output_dir)
    return result


async def _collect_deck(request, transport, frames, errors, report):
    if not request.deck:
        return None
    if errors or _final_candidate(frames) is None:
        return {"requested": True, "ok": False, "skipped": "report_not_successful"}
    correlate = getattr(transport, "correlate_deck", None)
    if not callable(correlate):
        return {"requested": True, "ok": False, "skipped": "deck_not_observable_on_transport"}
    try:
        value = await cast(Callable, correlate)(request, report)
        return (
            dict(value)
            if isinstance(value, Mapping)
            else {"requested": True, "ok": False, "skipped": "deck_not_observable_on_transport"}
        )
    except Exception as exc:
        return {
            "requested": True,
            "ok": False,
            "error": f"{type(exc).__name__}: {redact(str(exc))}"[:500],
        }


def _render_run_report(report, errors, frames):
    if errors and _final_candidate(frames) is None:
        return ""
    try:
        return render_report_markdown(report)
    except Exception as exc:
        errors.append(f"ReportContractError: {exc}")
        return ""


def _run_diagnostics(request, report, markdown, observer):
    invariants = check_invariants(report, markdown, observer, depth=request.depth)
    thrash = analyze_thrash(observer.events, model_io=observer.model_io)
    invariants["thrash_clean"] = bool(thrash.get("passed"))
    invariants["inspect_trace_complete"] = (
        bool(observer.inspect_trace) and bool(observer.model_io)
        if request.capture_inspect
        else True
    )
    return invariants, thrash


def _run_telemetry(
    request, observer, search_io, markdown, report_words, quality, run_id, batch_id, deck
):
    return {
        "duration_ms": max(0, round((time.monotonic() - observer.started) * 1000)),
        "event_count": len(observer.events),
        "probe_count": len(observer.probes),
        "source_count": observer.source_count,
        "citation_count": observer.citation_count,
        "report_chars": len(markdown),
        "report_words": report_words,
        "quality": quality,
        "requested_depth": request.depth,
        "requested_recency": request.recency,
        "requested_model": request.model,
        "requested_provider": request.provider,
        "phase_counts": _phase_counts(observer.events),
        "actual_models": observer.actual_models,
        "actual_providers": observer.actual_providers,
        "model_io_count": len(observer.model_io),
        # The backend emits a bounded ``started`` row and a terminal row for
        # each provider call. Count starts when present; old/replayed traces
        # containing only terminal rows still count conservatively.
        "provider_attempt_count": (
            sum(1 for item in observer.provider_attempts if item.get("outcome") == "started")
            or len(observer.provider_attempts)
        ),
        "provider_attempt_event_count": len(observer.provider_attempts),
        "provider_error_count": sum(
            1 for item in observer.provider_attempts if item.get("outcome") == "error"
        ),
        "provider_retry_count": sum(
            1 for item in observer.provider_attempts if item.get("retry_scheduled") is True
        ),
        "search_io_count": len(search_io),
        "trace_event_count": len(observer.inspect_trace.get("events", []))
        if observer.inspect_trace
        else 0,
        "run_id": run_id,
        "batch_id": batch_id,
        "deck_requested": request.deck,
        "deck_ok": bool(deck and deck.get("ok")),
        "deck_target_conversation_id": deck.get("target_conversation_id") if deck else None,
    }


async def run_batch(
    requests: Sequence[ResearchRequest],
    *,
    transport_factory: Callable[[ResearchRequest], ResearchTransport] | None = None,
    output_dir: str | Path,
    max_concurrency: int = 2,
    batch_id: str | None = None,
) -> list[ResearchRunResult]:
    """Run an acceptance corpus concurrently with bounded backpressure.

    Each worker owns its transport, observer, and output directory.  Results
    are gathered in input order even when completion order differs, and one
    failed worker does not cancel its siblings.
    """
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    request_list = list(requests)
    batch_id = (
        batch_id
        or hashlib.sha256(
            json.dumps([request.query for request in request_list], ensure_ascii=False).encode()
        ).hexdigest()[:16]
    )
    semaphore = asyncio.Semaphore(max_concurrency)

    async def one(index: int, request: ResearchRequest) -> ResearchRunResult:
        async with semaphore:
            return await _run_batch_one(index, request, root, transport_factory, batch_id)

    results = list(
        await asyncio.gather(
            *(one(index, request) for index, request in enumerate(request_list, start=1))
        )
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "ok": all(result.ok for result in results),
        "batch_id": batch_id,
        "concurrency": max_concurrency,
        "runs_total": len(results),
        "runs_passed": sum(result.ok for result in results),
        "runs_failed": sum(not result.ok for result in results),
        "aggregate": _aggregate_metrics(results),
        "runs": [
            {
                "index": index,
                "query": result.request.get("query"),
                "ok": result.ok,
                "output_dir": str(Path(output_dir) / f"run-{index:02d}"),
                "artifacts": result.artifacts,
                "invariants": result.invariants,
                "telemetry": result.telemetry,
                "errors": result.errors,
                "thrash": result.thrash,
                "deck": result.deck,
            }
            for index, result in enumerate(results, start=1)
        ],
    }
    _write_batch_outputs(root, manifest)
    return results


async def _run_batch_one(index, request, root, transport_factory, batch_id):
    run_id = f"run-{index:02d}-{hashlib.sha256(request.query.encode()).hexdigest()[:10]}"
    child = root / f"run-{index:02d}"
    try:
        transport = transport_factory(request) if transport_factory else None
        return await run_harness(
            request, transport=transport, output_dir=child, run_id=run_id, batch_id=batch_id
        )
    except Exception as exc:
        failed = ResearchRunResult(
            ok=False,
            request=redact({**asdict(request), "run_id": run_id, "batch_id": batch_id}),
            report={},
            report_markdown="",
            events=[],
            probes=[],
            errors=[f"{type(exc).__name__}: {exc}"],
            bounds=[],
            invariants={},
            telemetry={"run_id": run_id, "batch_id": batch_id},
            run_id=run_id,
            batch_id=batch_id,
            thrash={
                "passed": False,
                "signals": {},
                "findings": [{"code": "WORKER_FAILURE", "severity": "hard"}],
            },
            deck={
                "requested": request.deck,
                "ok": False,
                "error": "worker_failed_before_deck_correlation",
            }
            if request.deck
            else None,
        )
        try:
            write_artifacts(failed, child)
        except Exception as artifact_exc:
            failed.errors.append(
                f"{type(artifact_exc).__name__}: failed to write failure artifacts: {artifact_exc}"
            )
        return failed


def _write_batch_outputs(root: Path, manifest: Mapping[str, Any]) -> None:
    (root / "batch_summary.json").write_text(
        json.dumps(redact(manifest), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        f"# Deep Research acceptance batch — {'PASS' if manifest['ok'] else 'FAIL'}",
        "",
        "| Run | Status | Words | Sources | Output |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for run in manifest["runs"]:
        status = "PASS" if run["ok"] else "FAIL"
        lines.append(
            f"| {run['index']:02d} | {status} | "
            f"{run['telemetry'].get('report_words', 0)} | "
            f"{run['telemetry'].get('source_count', 0)} | {run['output_dir']} |"
        )
    reliability = manifest["aggregate"].get("query_reliability", {})
    if reliability:
        lines.extend(
            [
                "",
                "## Query reliability",
                "",
                "| Query | Runs | Pass rate | Word range | Variance |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for query, metrics in reliability.items():
            words = metrics["report_words"]
            safe_query = str(query).replace("|", "\\|")
            lines.append(
                f"| {safe_query} | {metrics['runs']} | "
                f"{metrics['pass_rate']:.1%} | {words['min']}–{words['max']} | "
                f"{words['variance']} |"
            )
    (root / "batch_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _aggregate_metrics(results: Sequence[ResearchRunResult]) -> dict[str, Any]:
    durations = sorted(int(result.telemetry.get("duration_ms", 0)) for result in results)
    words = sorted(int(result.telemetry.get("report_words", 0)) for result in results)

    def percentile(values: list[int], fraction: float) -> int:
        if not values:
            return 0
        return values[min(len(values) - 1, int((len(values) - 1) * fraction))]

    quality_rows = [
        result.telemetry.get("quality", {})
        for result in results
        if isinstance(result.telemetry.get("quality"), Mapping)
    ]
    return {
        "duration_ms": {"p50": percentile(durations, 0.50), "p95": percentile(durations, 0.95)},
        "report_words": {"p50": percentile(words, 0.50), "p95": percentile(words, 0.95)},
        "model_io_calls": sum(len(result.model_io) for result in results),
        "provider_attempts": sum(
            int(result.telemetry.get("provider_attempt_count", 0)) for result in results
        ),
        "deck_requested": sum(bool(result.request.get("deck")) for result in results),
        "deck_passed": sum(bool(result.deck and result.deck.get("ok")) for result in results),
        "thrash_findings": sum(len(result.thrash.get("findings", [])) for result in results),
        "thrash_clean_rate": round(
            sum(bool(result.thrash.get("passed")) for result in results) / len(results), 3
        )
        if results
        else 0.0,
        "quality": _aggregate_quality(quality_rows, percentile),
        "trace_complete": sum(bool(result.inspect_trace) for result in results),
        "query_reliability": _query_reliability(results, percentile),
    }


def _aggregate_quality(quality_rows, percentile):
    return {
        "distinct_works_p50": percentile(
            sorted(int(row.get("distinct_work_count", 0)) for row in quality_rows), 0.50
        ),
        "single_work_specific_claims": sum(
            int(row.get("single_work_specific_claims", 0)) for row in quality_rows
        ),
        "near_duplicate_body_paragraphs": sum(
            int(row.get("near_duplicate_body_paragraphs", 0)) for row in quality_rows
        ),
        "max_dominant_work_claim_share": max(
            (float(row.get("dominant_work_claim_share", 0.0)) for row in quality_rows), default=0.0
        ),
    }


def _query_reliability(
    results: Sequence[ResearchRunResult],
    percentile: Callable[[list[int], float], int],
) -> dict[str, Any]:
    """Group repeated requests so reliability and output variance are visible."""
    grouped: dict[str, list[ResearchRunResult]] = {}
    for result in results:
        query = str(result.request.get("query") or "")
        grouped.setdefault(query, []).append(result)
    output: dict[str, Any] = {}
    for query, rows in grouped.items():
        values = sorted(int(row.telemetry.get("report_words", 0)) for row in rows)
        mean = sum(values) / len(values) if values else 0.0
        variance = sum((value - mean) ** 2 for value in values) / len(values) if values else 0.0
        output[query] = {
            "runs": len(rows),
            "passed": sum(row.ok for row in rows),
            "failed": sum(not row.ok for row in rows),
            "pass_rate": round(sum(row.ok for row in rows) / len(rows), 3),
            "thrash_clean_rate": round(
                sum(bool(row.thrash.get("passed")) for row in rows) / len(rows), 3
            ),
            "report_words": {
                "min": min(values, default=0),
                "max": max(values, default=0),
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
                "variance": round(variance, 3),
            },
        }
    return output


def render_report_markdown(report: Mapping[str, Any]) -> str:
    """Render through the product serializer when the report is valid.

    The harness still has a small compatibility fallback for historical fake
    frames that predate the ReportEvent fields.  Live/product-shaped reports
    therefore use the exact server export implementation without requiring a
    server lifecycle or network call.
    """
    if str(report.get("kind") or "") == "research_checkpoint":
        return ""
    if report.get("legacy_replay"):
        return _render_legacy_report_markdown(report)
    from disco.agent_server.report_export import serialize_markdown
    from disco.core import ReportEvent

    payload = {key: value for key, value in report.items() if key in ReportEvent.model_fields}
    event = ReportEvent.model_validate(payload)
    return serialize_markdown(event)


def _render_legacy_report_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# {report.get('query') or 'Research request'}",
        "",
        "## Executive summary",
        "",
        str(report.get("summary") or "").strip(),
        "",
    ]
    for section in report.get("sections", []):
        if not isinstance(section, Mapping):
            continue
        raw_level = section.get("level", 2)
        try:
            level = min(6, max(2, int(raw_level)))
        except (TypeError, ValueError):
            level = 2
        lines.extend(
            [
                f"{'#' * level} {section.get('title') or 'Untitled section'}",
                "",
                str(section.get("markdown") or "").strip(),
                "",
            ]
        )
    lines.extend(["## Sources", ""])
    for index, passage in enumerate(report.get("passages", []), start=1):
        if isinstance(passage, Mapping):
            title = passage.get("source_title") or "Source"
            url = passage.get("source_url") or ""
            lines.append(f"[{index}] {title} — {url}")
    return "\n".join(lines).rstrip() + "\n"


def write_artifacts(result: ResearchRunResult, output_dir: str | Path) -> dict[str, str]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "report.md"
    report_json = root / "report.json"
    event_jsonl = root / "events.jsonl"
    model_io_jsonl = root / "model_io.jsonl"
    provider_attempts_jsonl = root / "provider_attempts.jsonl"
    search_io_jsonl = root / "search_io.jsonl"
    inspect_json = root / "inspect.json"
    thrash_json = root / "thrash.json"
    deck_json = root / "deck.json"
    timeline_markdown = root / "timeline.md"
    search_timeline_markdown = root / "search_timeline.md"
    cassette_jsonl = root / "cassette.jsonl"
    summary_json = root / "summary.json"
    markdown_summary = root / "summary.md"
    report_path.write_text(result.report_markdown, encoding="utf-8")
    report_json.write_text(
        json.dumps(redact(result.report), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_artifact_logs(
        result, event_jsonl, model_io_jsonl, provider_attempts_jsonl, search_io_jsonl
    )
    inspect_json.write_text(
        json.dumps(redact(result.inspect_trace or {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    thrash_json.write_text(
        json.dumps(redact(result.thrash), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if result.deck is not None:
        deck_json.write_text(
            json.dumps(redact(result.deck), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    timeline_markdown.write_text(render_timeline_markdown(result), encoding="utf-8")
    search_timeline_markdown.write_text(render_search_timeline(result.search_io), encoding="utf-8")
    cassette = Cassette()
    cassette.record(
        "research.frames",
        {"query": result.request.get("query")},
        [redact(event.payload) for event in result.events],
    )
    if result.model_io:
        cassette.record(
            "research.model_io",
            {"query": result.request.get("query")},
            [redact(item) for item in result.model_io],
        )
    if result.provider_attempts:
        cassette.record(
            "research.provider_attempts",
            {"query": result.request.get("query")},
            [redact(item) for item in result.provider_attempts],
        )
    if result.inspect_trace:
        cassette.record(
            "research.inspect",
            {"query": result.request.get("query")},
            redact(result.inspect_trace),
        )
    if result.deck is not None:
        cassette.record(
            "research.deck",
            {"query": result.request.get("query")},
            redact(result.deck),
        )
    cassette.save(cassette_jsonl)
    result.artifacts.update(
        {
            "report": str(report_path),
            "report_json": str(report_json),
            "events_jsonl": str(event_jsonl),
            "model_io_jsonl": str(model_io_jsonl),
            "provider_attempts_jsonl": str(provider_attempts_jsonl),
            "search_io_jsonl": str(search_io_jsonl),
            "inspect_json": str(inspect_json),
            "thrash_json": str(thrash_json),
            "timeline_markdown": str(timeline_markdown),
            "search_timeline_markdown": str(search_timeline_markdown),
            "cassette_jsonl": str(cassette_jsonl),
            "summary_json": str(summary_json),
            "summary_markdown": str(markdown_summary),
        }
    )
    if result.deck is not None:
        result.artifacts["deck_json"] = str(deck_json)
    summary_json.write_text(
        json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_summary.write_text(render_summary_markdown(result), encoding="utf-8")
    return result.artifacts


def _write_artifact_logs(result, *paths: Path) -> None:
    rows = (
        (result.events, lambda item: asdict(item)),
        (result.model_io, lambda item: item),
        (result.provider_attempts, lambda item: item),
        (result.search_io, lambda item: item),
    )
    for (values, encoder), path in zip(rows, paths, strict=True):
        with path.open("w", encoding="utf-8") as handle:
            for item in values:
                handle.write(json.dumps(redact(encoder(item)), sort_keys=True) + "\n")


def render_timeline_markdown(result: ResearchRunResult) -> str:
    """Concise operator timeline; full bounded payloads remain in JSONL."""
    lines = ["# Deep Research model timeline", ""]
    bundle = result.inspect_trace or {}
    nested = bundle.get("inspect_trace") if isinstance(bundle, Mapping) else None
    trace = nested if isinstance(nested, Mapping) else bundle
    events = trace.get("events", []) if isinstance(trace, Mapping) else []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        lines.extend(_timeline_event_lines(event))
    if len(lines) == 2:
        lines.append("- No inspect timeline was captured for this run.")
    lines.extend(
        [
            "",
            "Full bounded, redacted inputs and outputs: `model_io.jsonl`.",
            "Provider lifecycle metadata: `provider_attempts.jsonl`.",
            "",
        ]
    )
    return "\n".join(lines)


def _timeline_event_lines(event: Mapping[str, Any]) -> list[str]:
    kind = str(event.get("kind") or "event")
    seq = event.get("seq", "?")
    if kind == "model_io":
        return _timeline_model_io(event, seq)
    if kind == "model_attempt":
        return [_timeline_attempt(event, seq)]
    if kind == "routing":
        return [
            f"- {seq}: routed `{event.get('role')}` to "
            f"`{event.get('provider')}/{event.get('chosen_model')}`"
        ]
    return []


def _timeline_model_io(event, seq):
    stage, model, attempt = (
        event.get("stage") or event.get("role") or "model",
        event.get("model") or "unknown model",
        event.get("attempt") or 1,
    )
    line = f"- {seq}: **{stage}** attempt {attempt} on `{model}`"
    latency = event.get("latency_ms")
    lines = [line + (f" ({latency} ms)" if latency is not None else "")]
    decision = event.get("declared_decision")
    if isinstance(decision, Mapping):
        summary = str(decision.get("decision_summary") or "").strip()
        if summary:
            lines.append(f"  - Decision: {summary}")
        queries = decision.get("queries")
        if isinstance(queries, list) and queries:
            lines.append("  - Queries: " + "; ".join(str(item) for item in queries))
    return lines


def _timeline_attempt(event, seq):
    detail = f" ({event.get('latency_ms')} ms)" if event.get("latency_ms") is not None else ""
    if event.get("error_class"):
        detail += f" [{event['error_class']}]"
    return (
        f"- {seq}: provider attempt **{event.get('stage') or 'router'}** "
        f"{event.get('attempt') or 1} "
        f"`{event.get('provider') or 'unknown provider'}/"
        f"{event.get('model') or 'unknown model'}` → "
        f"{event.get('outcome') or 'unknown'}{detail}"
    )


def render_summary_markdown(result: ResearchRunResult) -> str:
    status = "PASS" if result.ok else "FAIL"
    lines = [
        f"# Deep Research harness — {status}",
        "",
        f"- Query: {result.request.get('query')}",
        f"- Transport: {result.request.get('surface')} / depth={result.request.get('depth')}",
        (
            f"- Events: {result.telemetry.get('event_count')} | "
            f"probes: {result.telemetry.get('probe_count')}"
        ),
        (
            f"- Sources: {result.telemetry.get('source_count')} | "
            f"citations: {result.telemetry.get('citation_count')}"
        ),
        f"- Duration: {result.telemetry.get('duration_ms')} ms",
        "",
        "## Invariants",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'}: {name}" for name, passed in result.invariants.items()
    )
    lines.extend(
        [
            "",
            "## Provider attempts",
            "",
            (
                f"- {result.telemetry.get('provider_attempt_count', 0)} attempts | "
                f"{result.telemetry.get('provider_retry_count', 0)} retries scheduled | "
                f"{result.telemetry.get('provider_error_count', 0)} errors"
            ),
            "",
            "## Thrash signals",
            "",
            f"- {'PASS' if result.thrash.get('passed', True) else 'FAIL'}: "
            f"{result.thrash.get('signals', {})}",
        ]
    )
    if result.deck is not None:
        validation = result.deck.get("artifact_validation")
        typed = validation.get("typed_slides", False) if isinstance(validation, Mapping) else False
        lines.extend(
            [
                "",
                "## Report → deck correlation",
                "",
                f"- {'PASS' if result.deck.get('ok') else 'FAIL'}: "
                f"source={result.deck.get('source_conversation_id', 'n/a')} "
                f"target={result.deck.get('target_conversation_id', 'n/a')}",
                f"- Typed slides: {typed}",
            ]
        )
    if result.errors:
        lines.extend(["", "## Errors", "", *[f"- {error}" for error in result.errors]])
    lines.extend(["", "## Report", "", result.report_markdown])
    return "\n".join(lines).rstrip() + "\n"

"""Run/batch orchestration, report rendering, and artifact writing."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..cassette import Cassette
from ._checks import (
    _DEPTH_WORD_TARGETS,
    _is_stopped_checkpoint,
    _report_word_count,
    check_invariants,
    normalize_report,
)
from ._observe import Observation, ObservationEvent, ResearchRequest, _phase_counts, redact
from ._transports import ResearchTransport, _transport_for

SCHEMA_VERSION = 1


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
                "artifacts": self.artifacts,
            }
        )


async def run_harness(
    request: ResearchRequest,
    *,
    transport: ResearchTransport | None = None,
    output_dir: str | Path | None = None,
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
    markdown = render_report_markdown(report)
    invariants = check_invariants(report, markdown, observer, depth=request.depth)
    report_words = _report_word_count(report)
    telemetry = {
        "duration_ms": max(0, round((time.monotonic() - observer.started) * 1000)),
        "event_count": len(observer.events),
        "probe_count": len(observer.probes),
        "source_count": observer.source_count,
        "citation_count": observer.citation_count,
        "report_chars": len(markdown),
        "report_words": report_words,
        "requested_depth": request.depth,
        "requested_recency": request.recency,
        "requested_model": request.model,
        "requested_provider": request.provider,
        "phase_counts": _phase_counts(observer.events),
    }
    target = _DEPTH_WORD_TARGETS.get(request.depth)
    # A stopped checkpoint has no report body, so the depth word target does not
    # apply to it; every other report is measured against its requested tier.
    invariants["depth_length_target"] = (
        bool(target and target[0] <= report_words <= target[1])
        or _is_stopped_checkpoint(report)
    )
    ok = not observer.errors and all(invariants.values())
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
    )
    if output_dir is not None:
        write_artifacts(result, output_dir)
    return result


async def run_batch(
    requests: Sequence[ResearchRequest],
    *,
    transport_factory: Callable[[ResearchRequest], ResearchTransport] | None = None,
    output_dir: str | Path,
) -> list[ResearchRunResult]:
    """Run an acceptance corpus while retaining one complete trace per query.

    Runs are intentionally sequential.  This keeps provider rate limits and
    event ordering predictable, while each child directory contains the same
    ``report.*``, ``events.jsonl``, ``cassette.jsonl`` and ``summary.*`` files
    as a single run.  A redacted batch manifest makes the corpus easy to
    consume from CI without collapsing the underlying evidence.
    """
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    results: list[ResearchRunResult] = []
    for index, request in enumerate(requests, start=1):
        child = root / f"run-{index:02d}"
        transport = transport_factory(request) if transport_factory else None
        results.append(await run_harness(request, transport=transport, output_dir=child))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "ok": all(result.ok for result in results),
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
            }
            for index, result in enumerate(results, start=1)
        ],
    }
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
            f"| {run['index']:02d} | {status} | {run['telemetry']['report_words']} | "
            f"{run['telemetry']['source_count']} | {run['output_dir']} |"
        )
    (root / "batch_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results


def render_report_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Deep Research: {report.get('query') or 'Research request'}",
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
    cassette_jsonl = root / "cassette.jsonl"
    summary_json = root / "summary.json"
    markdown_summary = root / "summary.md"
    report_path.write_text(result.report_markdown, encoding="utf-8")
    report_json.write_text(
        json.dumps(redact(result.report), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with event_jsonl.open("w", encoding="utf-8") as handle:
        for event in result.events:
            handle.write(json.dumps(redact(asdict(event)), sort_keys=True) + "\n")
    cassette = Cassette()
    cassette.record(
        "research.frames",
        {"query": result.request.get("query")},
        [redact(event.payload) for event in result.events],
    )
    cassette.save(cassette_jsonl)
    result.artifacts.update(
        {
            "report": str(report_path),
            "report_json": str(report_json),
            "events_jsonl": str(event_jsonl),
            "cassette_jsonl": str(cassette_jsonl),
            "summary_json": str(summary_json),
            "summary_markdown": str(markdown_summary),
        }
    )
    summary_json.write_text(
        json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    markdown_summary.write_text(render_summary_markdown(result), encoding="utf-8")
    return result.artifacts


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
    if result.errors:
        lines.extend(["", "## Errors", "", *[f"- {error}" for error in result.errors]])
    lines.extend(["", "## Report", "", result.report_markdown])
    return "\n".join(lines).rstrip() + "\n"

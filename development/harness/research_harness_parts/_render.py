"""Pure timeline and summary renderers for the research harness."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ._checks import declared_unverified_sentences
from ._failures import failures_markdown

if TYPE_CHECKING:
    from ._run import ResearchRunResult


def _model_timeline(event: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    seq = event.get("seq", "?")
    stage = event.get("stage") or event.get("role") or "model"
    model = event.get("model") or "unknown model"
    attempt = event.get("attempt") or 1
    latency = event.get("latency_ms")
    decision = event.get("declared_decision")
    lines.append(
        f"- {seq}: **{stage}** attempt {attempt} on `{model}`"
        + (f" ({latency} ms)" if latency is not None else "")
    )
    if isinstance(decision, Mapping):
        summary = str(decision.get("decision_summary") or "").strip()
        queries = decision.get("queries")
        if summary:
            lines.append(f"  - Decision: {summary}")
        if isinstance(queries, list) and queries:
            lines.append("  - Queries: " + "; ".join(str(item) for item in queries))
    return lines


def _attempt_timeline(event: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    seq = event.get("seq", "?")
    stage = event.get("stage") or "router"
    provider = event.get("provider") or "unknown provider"
    model = event.get("model") or "unknown model"
    attempt = event.get("attempt") or 1
    outcome = event.get("outcome") or "unknown"
    latency = event.get("latency_ms")
    error_class = event.get("error_class")
    detail = f" ({latency} ms)" if latency is not None else ""
    if error_class:
        detail += f" [{error_class}]"
    lines.append(
        f"- {seq}: provider attempt **{stage}** {attempt} `{provider}/{model}` → {outcome}{detail}"
    )
    return lines


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
        kind = str(event.get("kind") or "event")
        seq = event.get("seq", "?")
        if kind == "model_io":
            lines.extend(_model_timeline(event))
        elif kind == "model_attempt":
            lines.extend(_attempt_timeline(event))
        elif kind == "routing":
            lines.append(
                f"- {seq}: routed `{event.get('role')}` to "
                f"`{event.get('provider')}/{event.get('chosen_model')}`"
            )
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
    lines.extend(_declared_misses_section(result))
    if result.errors:
        lines.extend(["", "## Errors", "", *[f"- {error}" for error in result.errors]])
    lines.extend(failures_markdown(result.failures))
    lines.extend(["", "## Report", "", result.report_markdown])
    return "\n".join(lines).rstrip() + "\n"


def _declared_misses_section(result: ResearchRunResult) -> list[str]:
    declared = declared_unverified_sentences(result.report)
    if not declared:
        return []
    return [
        "",
        "## Unverified sentences (shipped as written)",
        "",
        *[f"- {line}" for line in declared],
    ]

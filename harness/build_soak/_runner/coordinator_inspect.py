"""Required inspect-trace admission for one Build Soak run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import failure_codes as fc
from ..adapters.disco_api import CollectedRun, inspect_aggregate_violations
from ..evidence_sink import _timeline_md
from .bindings import CoordinatorBindings
from .preflight import (
    _is_terminal_driver_preflight_trace,
    _is_terminal_sandbox_preflight_trace,
)
from .records import _invalid_run_record


def _inspect_agent_spans(trace: dict[str, Any]) -> list[dict[str, Any]]:
    spans = trace.get("spans")
    if not isinstance(spans, list):
        return []
    return [span for span in spans if isinstance(span, dict) and span.get("span") == "agent.step"]


def _missing_inspect_parts(
    trace: dict[str, Any],
    aggregation: Any,
    routing: Any,
    agent_spans: list[dict[str, Any]],
    *,
    terminal_preloop: bool,
    aggregate_violations: list[Any],
) -> list[str]:
    checks = (
        ("internally consistent inspect aggregate", not aggregate_violations),
        (
            "lossless inspect aggregation",
            isinstance(aggregation, dict)
            and aggregation.get("lossless") is True
            and aggregation.get("finalized") is True
            and trace.get("dropped_event_count") == 0,
        ),
        ("routing_decisions", isinstance(routing, list) and bool(routing)),
        ("agent.step spans", bool(agent_spans) or terminal_preloop),
    )
    return [name for name, present in checks if not present]


def required_inspect_record(
    run: CollectedRun,
    scenario: dict[str, Any],
    runtime: CoordinatorBindings,
    *,
    required: bool,
    out_root: str | Path,
    run_id: str,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str,
    repo_dirty: bool,
    kernel: str,
    started_at: str,
    seed: int | None,
) -> dict[str, Any] | None:
    """Retain and reject incomplete required inspect evidence, otherwise annotate preflight."""
    if not required:
        return None
    trace = run.inspect_trace or {}
    aggregation = trace.get("aggregation")
    routing = trace.get("routing_decisions")
    agent_spans = _inspect_agent_spans(trace)
    driver_preflight = _is_terminal_driver_preflight_trace(
        run,
        trace,
        routing,
        agent_spans,
    )
    sandbox_preflight = _is_terminal_sandbox_preflight_trace(
        run,
        trace,
        routing,
        agent_spans,
        scenario,
    )
    aggregate_violations = inspect_aggregate_violations(
        trace,
        conversation_id=str(run.conversation_id or ""),
    )
    missing = _missing_inspect_parts(
        trace,
        aggregation,
        routing,
        agent_spans,
        terminal_preloop=driver_preflight or sandbox_preflight,
        aggregate_violations=aggregate_violations,
    )
    if missing:
        run.timeline.append("required inspect evidence incomplete: missing " + ", ".join(missing))
        provider_ledger = runtime.provider_ledger_for_run(run)
        runtime.assemble_dossier(
            out_root,
            run_id,
            scenario,
            run,
            model=model,
            autonomous=autonomous,
            commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            kernel=kernel,
            started_at=started_at,
            seed=seed,
            provider_ledger=provider_ledger,
        )
        return _invalid_run_record(
            out_root,
            run_id,
            scenario,
            "required per-conversation inspect trace was absent or incomplete",
            code=fc.MISSING_REQUIRED_EVIDENCE,
            first_broken_link="model_request -> inspect_trace",
            facts={
                "missing_trace_parts": missing,
                "inspect_aggregate_violations": aggregate_violations,
                "inspect_aggregation": (aggregation if isinstance(aggregation, dict) else None),
            },
            conversation_id=run.conversation_id,
            timeline_markdown=_timeline_md(scenario, run),
        )
    if driver_preflight:
        run.timeline.append(
            "inspect trace proved a terminal driver preflight failure before "
            "the agent loop; no agent.step span was expected"
        )
    elif sandbox_preflight:
        run.timeline.append(
            "inspect trace and durable status proved a terminal sandbox preflight "
            "failure before the agent loop; no agent.step span was expected"
        )
    return None

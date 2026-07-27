"""Build-soak efficiency observability — how much WORK a run cost to pass.

A PASS says the run was correct. It says nothing about whether it took 12 actions
or 100. This module makes that difference visible, so an efficiency regression
cannot hide behind a green batch.

What this is NOT
----------------
It is **observability, not a gate**. Nothing here fails a run, changes an oracle
verdict, alters a failure code, or introduces a universal action/turn threshold —
different scenario shapes legitimately need different amounts of work, and an
AppKit lifecycle is not comparable to a static-page build. Regressions and
outliers surface as diagnostic warnings for a human to read.

Where the numbers come from
---------------------------
Every metric is derived from durable, conversation-bound evidence already sealed
into the dossier, and reuses the CANONICAL calculation rather than restating it:

  ``actions``                 ``events.KIND_ACTION`` (the same event predicate
                              every oracle counts through)
  ``planning``/``execution``  :func:`oracles.tool_scope.turn_mode_counts` — the
                              same mode constants ToolScopeOracle adjudicates
  ``compactions``             ``kind == "condensation"``, identical to
                              ScenarioLifecycleOracle's ``compaction_count``
  repairs / thrash maxima     ThrashOracle's own published facts
  provider calls              the conversation-bound provider-call ledger
  tokens                      ``agent.step`` end spans in the inspect trace

Honesty rules enforced here
---------------------------
* A metric that cannot be PROVEN from the evidence is ``None`` and its name is
  listed in ``unavailable``. It is never silently zero — "0 actions" and "we
  could not read the actions" are opposite findings.
* Polling samples and inspection events are never counted as model turns or tool
  actions. ``polling_samples`` is reported separately, precisely so it is
  visible as *not* a turn.
* Provider calls are split into ``conversation_bound`` (records carrying THIS
  conversation's id — authoritative) and ``unattributed_in_window`` (records the
  ledger left unscoped, which under concurrency may belong to another run).
  They are never merged into one confident number.
* ``actions_per_verified_requirement`` is emitted only when the denominator is
  authoritative — i.e. the governed verification oracle actually PASSED with a
  positive required-check count. Otherwise it is omitted entirely.

Reading ``elapsed_s`` honestly
-----------------------------
``elapsed_s`` is wall-clock and is therefore **confounded by worker count**.
Observed 2026-07-27: two canaries at 1 worker read 72–75% "faster" than the same
scenarios in an 8-worker batch on a 12-core host, while their action counts moved
only −14% and −25%. The machine was contended, not the agent improved.

So: ``actions``, ``model_turns``, ``provider_calls`` and ``compactions`` are the
concurrency-independent measures of work, and they are what a regression claim
should rest on. ``elapsed_s`` is reported because operators need it, and compared
only against a baseline collected at the same concurrency.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .events import (
    KIND_ACTION,
    KIND_CONDENSATION,
    KIND_STATUS,
    kind_of,
    normalize_events,
)
from .oracles.thrash import repair_spans
from .oracles.tool_scope import turn_mode_counts
from .provider_ledger import record_applies_to_conversation

SCHEMA_VERSION = 1

# The product stamps this prefix on the status detail when a verification pass
# made no progress (disco.core.loop.finish.common._VERIFY_MARKER_PREFIX). It is a
# work-efficiency signal, never a verdict here.
_VERIFY_NO_PROGRESS_PREFIX = "verify_no_progress:"

# Metrics the batch aggregate reports quantiles for. Order is the report order.
AGGREGATED_METRICS: tuple[str, ...] = (
    "actions",
    "model_turns",
    "planning_turns",
    "execution_turns",
    "provider_calls_conversation_bound",
    "compactions",
    "model_repairs",
    "elapsed_s",
)


# --------------------------------------------------------------------------
# Scenario families — derived, deterministic, and documented as derived
# --------------------------------------------------------------------------
#
# The governed scenario files declare no family field, so the family is derived
# from the scenario id by an explicit ordered rule. Comparing an AppKit lifecycle
# against a static-page build is meaningless, so the aggregate always partitions
# by this value.
_FAMILY_RULES: tuple[tuple[str, str], ...] = (
    ("p4_appkit_", "appkit"),
    ("p4_ff_context_", "context_pressure"),
    ("p4_ff_import_", "freeform_import"),
    ("p4_ff_static_", "freeform_static"),
    ("p4_ff_react_", "freeform_react"),
    ("p4_ff_node_", "freeform_node"),
    ("p4_ff_python_", "freeform_python"),
)


def scenario_family(scenario_id: str | None) -> str:
    """The comparison family for ``scenario_id``.

    Unknown ids return ``"unclassified"`` rather than being folded into a
    plausible-looking neighbour — a mis-grouped run would silently corrupt the
    family quantiles it lands in.
    """
    text = str(scenario_id or "")
    for prefix, family in _FAMILY_RULES:
        if text.startswith(prefix):
            return family
    return "unclassified"


# --------------------------------------------------------------------------
# Evidence readers — tolerant, never raising into the runner
# --------------------------------------------------------------------------


def _oracle_facts(oracle_results: Any, oracle: str) -> tuple[dict[str, Any] | None, str | None]:
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


def _int_fact(facts: dict[str, Any] | None, key: str) -> int | None:
    """An int fact, or None when absent/not-an-int. Never coerces a bool."""
    if not facts:
        return None
    value = facts.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _event_epoch(event: dict[str, Any]) -> float | None:
    stamp = event.get("timestamp") or event.get("created_at")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _event_span_seconds(events: Sequence[dict[str, Any]]) -> float | None:
    """Wall-clock span of the conversation, from its own durable timestamps."""
    epochs = [e for e in (_event_epoch(ev) for ev in events) if e is not None]
    if len(epochs) < 2:
        return None
    return round(max(epochs) - min(epochs), 3)


def _token_totals(trace: dict[str, Any] | None) -> dict[str, int] | None:
    """Summed token counts across ``agent.step`` end spans, or None.

    Returns None unless at least one span actually carried token counts — a
    provider that does not report usage must read as ``unavailable``, never as
    zero tokens spent.
    """
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


def _provider_call_split(ledger: Any, conversation_id: str | None) -> dict[str, int | None]:
    """Split a run's ledger slice into what it can and cannot claim as its own.

    The ledger's own :func:`record_applies_to_conversation` treats an UNSCOPED
    record as applying to every conversation — correct fail-closed behaviour for
    a serial relay log, but under concurrency such a record may belong to another
    worker. So the populations stay separate here and the reader decides:

      ``conversation_bound``      records carrying THIS conversation's id. The
                                  only authoritative count.
      ``unattributed_in_window``  records the ledger left unscoped. Genuinely
                                  this run's on a serial lane; ambiguous under
                                  concurrency. Never folded into the above.
      ``foreign_excluded``        records explicitly bound to ANOTHER
                                  conversation. Disclosed rather than dropped
                                  silently, because a non-zero value means the
                                  slice was not scoped as expected.
    """
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


def _no_progress_events(events: Sequence[dict[str, Any]]) -> int:
    return sum(
        1
        for event in events
        if kind_of(event) == KIND_STATUS
        and str(event.get("detail") or "").startswith(_VERIFY_NO_PROGRESS_PREFIX)
    )


def _polling_samples(trace: dict[str, Any] | None) -> int | None:
    """How many times the harness POLLED. Reported so it is visibly not a turn."""
    if not isinstance(trace, dict):
        return None
    aggregation = trace.get("aggregation")
    if not isinstance(aggregation, dict):
        return None
    return _int_fact(aggregation, "sample_count")


# --------------------------------------------------------------------------
# The per-run record
# --------------------------------------------------------------------------


def efficiency_record(
    *,
    scenario_id: str | None,
    seed: Any = None,
    status: str | None = None,
    failure_code: str | None = None,
    conversation_id: str | None = None,
    events: Iterable[Any] | None = None,
    trace: dict[str, Any] | None = None,
    ledger: list[dict[str, Any]] | None = None,
    oracle_results: Any = None,
    elapsed_s: float | None = None,
) -> dict[str, Any]:
    """One run's normalized ``efficiency`` record.

    Pure: same evidence in, same record out. Nothing here reads the clock, the
    filesystem, or the network, so a sealed dossier replays to identical numbers
    at zero provider cost.
    """
    normalized: list[dict[str, Any]] = []
    events_readable = False
    if events is not None:
        try:
            normalized = normalize_events(events)
            events_readable = True
        except (ValueError, TypeError):
            normalized = []
            events_readable = False

    unavailable: list[str] = []

    def mark(name: str, value: Any) -> Any:
        if value is None:
            unavailable.append(name)
        return value

    actions = len([e for e in normalized if kind_of(e) == KIND_ACTION]) if events_readable else None
    compactions = (
        len([e for e in normalized if kind_of(e) == KIND_CONDENSATION]) if events_readable else None
    )

    turns = turn_mode_counts((trace or {}).get("tool_scopes"))
    scopes = (trace or {}).get("tool_scopes")
    turns_readable = isinstance(scopes, list) and bool(scopes)
    planning_turns = turns["planning"] if turns_readable else None
    execution_turns = turns["execution"] if turns_readable else None
    model_turns = (
        planning_turns + execution_turns
        if planning_turns is not None and execution_turns is not None
        else None
    )

    thrash_facts, _ = _oracle_facts(oracle_results, "ThrashOracle")
    verify_facts, verify_status = _oracle_facts(oracle_results, "GovernedVerificationOracle")

    # ThrashOracle publishes the authoritative repair count post-run. Before it
    # has run (the LIVE readout) the same spans it counts are already in the
    # trace, so the shared `repair_spans` helper gives the identical number
    # without a second definition. Oracle facts still win when present.
    repairs = _int_fact(thrash_facts, "model_repair_count")
    if repairs is None and isinstance(trace, dict):
        spans = trace.get("spans")
        if isinstance(spans, list):
            repairs = len(repair_spans(spans))

    calls = _provider_call_split(ledger, conversation_id)
    tokens = _token_totals(trace)

    if elapsed_s is None and events_readable:
        elapsed_s = _event_span_seconds(normalized)
        elapsed_source = "conversation_event_span" if elapsed_s is not None else None
    else:
        elapsed_source = "runner_wall_clock" if elapsed_s is not None else None

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "scenario_family": scenario_family(scenario_id),
        "seed": seed,
        "status": status,
        "failure_code": failure_code,
        "elapsed_s": mark("elapsed_s", elapsed_s),
        "elapsed_source": elapsed_source,
        "actions": mark("actions", actions),
        "planning_turns": mark("planning_turns", planning_turns),
        "execution_turns": mark("execution_turns", execution_turns),
        "model_turns": mark("model_turns", model_turns),
        "unrecognized_turn_modes": turns["unrecognized"] if turns_readable else None,
        "provider_calls": {
            **calls,
            "conversation_bound": mark(
                "provider_calls_conversation_bound", calls["conversation_bound"]
            ),
        },
        "compactions": mark("compactions", compactions),
        "model_repairs": mark("model_repairs", repairs),
        "model_repair_categories": (thrash_facts or {}).get("model_repair_counts")
        if thrash_facts
        else None,
        "repeated_action_max": _int_fact(thrash_facts, "longest_identical_action_streak"),
        "repeated_error_max": _int_fact(thrash_facts, "largest_same_tool_error_group"),
        "repeated_shell_verification_max": _int_fact(
            thrash_facts, "largest_semantic_shell_repeat_group"
        ),
        "repeated_script_restart_max": _int_fact(
            thrash_facts, "largest_background_script_restart_group"
        ),
        "actionless_pauses": mark(
            "actionless_pauses", _int_fact(thrash_facts, "actionless_pauses")
        ),
        "no_progress_events": _no_progress_events(normalized) if events_readable else None,
        # Deliberately adjacent to the turn counts and deliberately NOT one of
        # them: a polling sample is the harness looking, not the model working.
        "polling_samples": _polling_samples(trace),
        "tokens": tokens if tokens is not None else "unavailable",
    }

    # Only emit the ratio when the denominator is authoritative: the governed
    # verification oracle PASSED (so the requirements were genuinely verified)
    # and counted at least one required check.
    required_checks = _int_fact(verify_facts, "required_checks")
    if verify_status == "PASS" and required_checks and actions is not None:
        record["actions_per_verified_requirement"] = round(actions / required_checks, 3)

    record["unavailable"] = sorted(set(unavailable))
    return record


def efficiency_record_from_dossier(run_dir: Path, classification: dict[str, Any]) -> dict[str, Any]:
    """Build a record by reading one run's sealed dossier from disk.

    Tolerant by construction: a missing or malformed evidence slice yields
    ``None``/``unavailable`` for the metrics it fed, never an exception into the
    runner and never a changed verdict.
    """
    conversation_id = str(classification.get("conversation_id") or "") or None
    conv_dir = run_dir / "conversations" / (conversation_id or "")

    trace: dict[str, Any] | None = None
    loaded = _read_json(conv_dir / "inspect-trace.json")
    if isinstance(loaded, dict):
        trace = loaded

    events = _read_jsonl(conv_dir / "events.jsonl")
    ledger = _read_jsonl(conv_dir / "provider-call-ledger.jsonl")

    return efficiency_record(
        scenario_id=classification.get("scenario_id"),
        seed=classification.get("seed"),
        status=classification.get("status"),
        failure_code=classification.get("code"),
        conversation_id=conversation_id,
        events=events,
        trace=trace,
        ledger=ledger,
        oracle_results=classification.get("oracle_results"),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]] | None:
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


# --------------------------------------------------------------------------
# Aggregation — deterministic, small-batch correct
# --------------------------------------------------------------------------


def _metric_value(record: dict[str, Any], metric: str) -> float | None:
    if metric == "provider_calls_conversation_bound":
        calls = record.get("provider_calls")
        value = calls.get("conversation_bound") if isinstance(calls, dict) else None
    else:
        value = record.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile — deterministic and exact for tiny batches.

    Nearest-rank is chosen over interpolation on purpose: every reported value is
    a value that a REAL run actually produced, so "p95 actions = 19" always names
    an observed run rather than a number no run ever hit. Rank is
    ``ceil(fraction * n)`` clamped to ``[1, n]``.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def median(values: Sequence[float]) -> float | None:
    """True median: the mean of the two middle values on an even count."""
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return None
    mid = count // 2
    if count % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _metric_stats(records: Sequence[dict[str, Any]], metric: str) -> dict[str, Any]:
    pairs = [(r, _metric_value(r, metric)) for r in records]
    usable = [(r, v) for r, v in pairs if v is not None]
    values = [v for _, v in usable]
    stats: dict[str, Any] = {
        "count": len(values),
        "unavailable_count": len(pairs) - len(usable),
        "median": median(values),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "max": None,
        "worst_run_id": None,
        "worst_scenario_id": None,
        "worst_seed": None,
    }
    if usable:
        worst_record, worst_value = max(usable, key=lambda item: item[1])
        stats["max"] = worst_value
        # Only NAME a worst run when the metric actually has spread. With every
        # run tied (the common `model_repairs: 0` case) "worst run" would finger
        # whichever run sorted first, reading as an outlier that does not exist.
        if min(values) != max(values):
            stats["worst_run_id"] = worst_record.get("run_id")
            stats["worst_scenario_id"] = worst_record.get("scenario_id")
            stats["worst_seed"] = worst_record.get("seed")
    return stats


def aggregate(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Batch efficiency summary, overall and partitioned by scenario family."""
    families: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        families.setdefault(str(record.get("scenario_family") or "unclassified"), []).append(record)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_count": len(records),
        "overall": {metric: _metric_stats(records, metric) for metric in AGGREGATED_METRICS},
        "by_family": {
            family: {
                "run_count": len(group),
                "metrics": {metric: _metric_stats(group, metric) for metric in AGGREGATED_METRICS},
            }
            for family, group in sorted(families.items())
        },
    }


# --------------------------------------------------------------------------
# Baseline comparison — explicit, scenario-matched, never auto-selected
# --------------------------------------------------------------------------


def compare_to_baseline(
    records: Sequence[dict[str, Any]],
    baseline_records: Sequence[dict[str, Any]],
    *,
    outlier_ratio: float = 1.5,
) -> dict[str, Any]:
    """Compare against an EXPLICITLY supplied prior accepted baseline.

    There is deliberately no "pick the most recent historical batch" fallback:
    silently choosing a baseline would let the comparison mean whatever the
    directory happened to contain. The caller names the baseline or there is no
    comparison.

    Matching is per SCENARIO id (the finest honest grain); a scenario absent from
    either side is reported as unmatched rather than compared against the family
    average. ``passing_but_abnormal`` lists runs that PASSED yet exceeded the
    baseline median for their scenario by more than ``outlier_ratio``.
    """
    baseline_by_scenario: dict[str, list[dict[str, Any]]] = {}
    for record in baseline_records:
        baseline_by_scenario.setdefault(str(record.get("scenario_id")), []).append(record)

    comparisons: list[dict[str, Any]] = []
    abnormal: list[dict[str, Any]] = []
    unmatched: list[str] = []

    current_by_scenario: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        current_by_scenario.setdefault(str(record.get("scenario_id")), []).append(record)

    for scenario_id in sorted(current_by_scenario):
        group = current_by_scenario[scenario_id]
        base_group = baseline_by_scenario.get(scenario_id)
        if not base_group:
            unmatched.append(scenario_id)
            continue
        for metric in AGGREGATED_METRICS:
            base_values = [
                v for v in (_metric_value(r, metric) for r in base_group) if v is not None
            ]
            cur_values = [v for v in (_metric_value(r, metric) for r in group) if v is not None]
            base_median = median(base_values)
            cur_median = median(cur_values)
            if base_median is None or cur_median is None:
                continue
            delta = cur_median - base_median
            pct = (delta / base_median * 100.0) if base_median else None
            comparisons.append(
                {
                    "scenario_id": scenario_id,
                    "metric": metric,
                    "baseline_median": base_median,
                    "current_median": cur_median,
                    "absolute_change": round(delta, 3),
                    "percent_change": round(pct, 1) if pct is not None else None,
                }
            )
            if base_median > 0:
                for record in group:
                    value = _metric_value(record, metric)
                    if (
                        value is not None
                        and str(record.get("status")) == "PASS"
                        and value > base_median * outlier_ratio
                    ):
                        abnormal.append(
                            {
                                "scenario_id": scenario_id,
                                "seed": record.get("seed"),
                                "run_id": record.get("run_id"),
                                "metric": metric,
                                "value": value,
                                "baseline_median": base_median,
                                "ratio": round(value / base_median, 2),
                            }
                        )

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_run_count": len(baseline_records),
        "current_run_count": len(records),
        "outlier_ratio": outlier_ratio,
        "comparisons": comparisons,
        "passing_but_abnormal": abnormal,
        "unmatched_scenarios": unmatched,
    }


# --------------------------------------------------------------------------
# Human-readable report
# --------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1f}" if value % 1 else str(int(value))
    return str(value)


def render_table(records: Sequence[dict[str, Any]]) -> str:
    """The per-run efficiency table printed at the end of a batch."""
    header = (
        f"{'scenario':32} {'seed':>7} {'status':>7} {'act':>4} {'plan':>5} "
        f"{'exec':>5} {'calls':>6} {'cmpct':>6} {'rep':>4} {'elapsed':>9}"
    )
    lines = [header, "-" * len(header)]
    for record in records:
        calls = record.get("provider_calls")
        bound = calls.get("conversation_bound") if isinstance(calls, dict) else None
        elapsed = record.get("elapsed_s")
        lines.append(
            f"{str(record.get('scenario_id'))[:32]:32} "
            f"{_fmt(record.get('seed')):>7} "
            f"{str(record.get('status') or '?'):>7} "
            f"{_fmt(record.get('actions')):>4} "
            f"{_fmt(record.get('planning_turns')):>5} "
            f"{_fmt(record.get('execution_turns')):>5} "
            f"{_fmt(bound):>6} "
            f"{_fmt(record.get('compactions')):>6} "
            f"{_fmt(record.get('model_repairs')):>4} "
            f"{(_fmt(elapsed) + 's') if elapsed is not None else 'n/a':>9}"
        )
    return "\n".join(lines)


def render_report(
    records: Sequence[dict[str, Any]],
    summary: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    title: str = "Build-soak efficiency report",
) -> str:
    """Concise human-readable report written beside the batch summary."""
    out: list[str] = [f"# {title}", ""]
    out.append(
        "Diagnostic only — no threshold here fails a run. Families are reported "
        "separately because their shapes are not comparable."
    )
    out.append("")
    out.append(f"Runs: {summary.get('run_count', 0)}")
    out.append("")

    out.append("## Per-run")
    out.append("")
    out.append("```")
    out.append(render_table(records))
    out.append("```")
    out.append("")

    for family, block in (summary.get("by_family") or {}).items():
        out.append(f"## Family: {family}  ({block.get('run_count', 0)} runs)")
        out.append("")
        out.append("| metric | count | median | p90 | p95 | max | worst run |")
        out.append("|---|---:|---:|---:|---:|---:|---|")
        for metric, stats in (block.get("metrics") or {}).items():
            if not stats.get("count"):
                continue
            worst = stats.get("worst_scenario_id") or "—"
            seed = stats.get("worst_seed")
            worst_label = f"{worst}" + (f" / seed {seed}" if seed is not None else "")
            out.append(
                f"| {metric} | {stats.get('count')} | {_fmt(stats.get('median'))} | "
                f"{_fmt(stats.get('p90'))} | {_fmt(stats.get('p95'))} | "
                f"{_fmt(stats.get('max'))} | {worst_label} |"
            )
        out.append("")

    unavailable = sorted({name for record in records for name in (record.get("unavailable") or [])})
    if unavailable:
        out.append("## Unavailable metrics")
        out.append("")
        out.append(
            "These could not be proven from the sealed evidence and are reported as "
            "`null`, not zero:"
        )
        out.append("")
        for name in unavailable:
            affected = sum(1 for r in records if name in (r.get("unavailable") or []))
            out.append(f"- `{name}` — {affected} run(s)")
        out.append("")

    if baseline:
        out.append("## Baseline comparison")
        out.append("")
        out.append(f"Explicitly supplied baseline of {baseline.get('baseline_run_count')} run(s).")
        out.append("")
        out.append(
            "> `elapsed_s` is wall-clock and is confounded by worker count — a batch run "
            "at lower concurrency looks faster without doing less work. Rest a regression "
            "claim on `actions`, `model_turns`, `provider_calls` or `compactions`, and "
            "compare elapsed only against a baseline collected at the same concurrency."
        )
        out.append("")
        abnormal = baseline.get("passing_but_abnormal") or []
        if abnormal:
            out.append(
                f"**{len(abnormal)} passing-but-abnormal observation(s)** "
                f"(> {baseline.get('outlier_ratio')}x the baseline median):"
            )
            out.append("")
            for item in abnormal:
                out.append(
                    f"- `{item['scenario_id']}` seed {item['seed']}: {item['metric']} "
                    f"= {_fmt(item['value'])} vs baseline median "
                    f"{_fmt(item['baseline_median'])} ({item['ratio']}x)"
                )
            out.append("")
        else:
            out.append("No passing run exceeded the outlier ratio.")
            out.append("")
        changed = [
            c
            for c in (baseline.get("comparisons") or [])
            if c.get("percent_change") not in (None, 0.0)
        ]
        if changed:
            out.append("| scenario | metric | baseline | current | Δ | Δ% |")
            out.append("|---|---|---:|---:|---:|---:|")
            for item in sorted(
                changed, key=lambda c: abs(c.get("percent_change") or 0), reverse=True
            )[:40]:
                out.append(
                    f"| {item['scenario_id']} | {item['metric']} | "
                    f"{_fmt(item['baseline_median'])} | {_fmt(item['current_median'])} | "
                    f"{item['absolute_change']:+g} | {item['percent_change']:+.1f}% |"
                )
            out.append("")
        unmatched = baseline.get("unmatched_scenarios") or []
        if unmatched:
            out.append(
                "Unmatched scenarios (present now, absent from the baseline): "
                + ", ".join(f"`{s}`" for s in unmatched)
            )
            out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------
# Live progress — bounded, zero provider calls
# --------------------------------------------------------------------------


class LiveEfficiencyProgress:
    """Bounded live counters for one in-flight run.

    Emits ONLY when a meaningful counter changes or ``interval_s`` has elapsed
    since the last emission, so a fast poll loop cannot flood the log. Makes no
    provider calls and reads nothing the harness had not already collected —
    polling samples update the clock, never the counters.
    """

    def __init__(
        self,
        *,
        scenario_id: str,
        seed: Any,
        emit: Any = print,
        interval_s: float = 30.0,
    ) -> None:
        self._scenario_id = scenario_id
        self._seed = seed
        self._emit = emit
        self._interval_s = interval_s
        self._last_signature: tuple[Any, ...] | None = None
        self._last_emit_at: float | None = None

    def update(
        self,
        *,
        now: float,
        elapsed_s: float,
        actions: int | None = None,
        planning_turns: int | None = None,
        execution_turns: int | None = None,
        provider_calls: int | None = None,
        compactions: int | None = None,
        repairs: int | None = None,
        state: str = "",
    ) -> bool:
        """Emit a progress line when warranted. Returns True when it emitted.

        ``now`` is passed in rather than read from the clock so the caller owns
        the time source and this stays testable without sleeping.
        """
        signature = (
            actions,
            planning_turns,
            execution_turns,
            provider_calls,
            compactions,
            repairs,
            state,
        )
        counters_changed = signature != self._last_signature
        interval_elapsed = (
            self._last_emit_at is None or (now - self._last_emit_at) >= self._interval_s
        )
        if not counters_changed and not interval_elapsed:
            return False
        self._last_signature = signature
        self._last_emit_at = now
        self._emit(
            f"[eff] {self._scenario_id} · seed {self._seed} · {elapsed_s:.0f}s · "
            f"act {_fmt(actions)} · plan {_fmt(planning_turns)} · "
            f"exec {_fmt(execution_turns)} · calls {_fmt(provider_calls)} · "
            f"cmpct {_fmt(compactions)} · rep {_fmt(repairs)} · {state or '?'}"
        )
        return True

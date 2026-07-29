"""The per-run efficiency record builder."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..events import normalize_events
from . import _readers as readers

SCHEMA_VERSION = 1

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
    """The comparison family for ``scenario_id``."""
    text = str(scenario_id or "")
    for prefix, family in _FAMILY_RULES:
        if text.startswith(prefix):
            return family
    return "unclassified"


def _build_turn_metrics(
    trace: dict[str, Any] | None,
) -> tuple[int | None, int | None, int | None, int | None, dict[str, int]]:
    """Return (planning, execution, model_turns, unrecognized, raw_counts)."""
    turns = readers.turn_counts(trace)
    scopes = (trace or {}).get("tool_scopes")
    turns_readable = isinstance(scopes, list) and bool(scopes)
    planning = turns["planning"] if turns_readable else None
    execution = turns["execution"] if turns_readable else None
    model_turns = planning + execution if planning is not None and execution is not None else None
    unrecognized = turns["unrecognized"] if turns_readable else None
    return planning, execution, model_turns, unrecognized, turns


def _build_repair_metrics(
    trace: dict[str, Any] | None,
    oracle_results: Any,
) -> tuple[int | None, dict[str, Any] | None]:
    """Return (repair_count, thrash_facts)."""
    thrash_facts, _ = readers.oracle_facts(oracle_results, "ThrashOracle")
    repairs = readers.int_fact(thrash_facts, "model_repair_count")
    if repairs is None and isinstance(trace, dict):
        repairs = readers.count_repair_spans(trace)
    return repairs, thrash_facts


def _resolve_elapsed(
    elapsed_s: float | None,
    events_readable: bool,
    normalized: list[dict[str, Any]],
) -> tuple[float | None, str | None]:
    """Resolve elapsed_s and its source label."""
    if elapsed_s is None and events_readable:
        elapsed_s = readers.event_span_seconds(normalized)
        return elapsed_s, "conversation_event_span" if elapsed_s is not None else None
    return elapsed_s, "runner_wall_clock" if elapsed_s is not None else None


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

    actions = readers.count_actions(normalized) if events_readable else None
    compactions = readers.count_compactions(normalized) if events_readable else None

    planning_turns, execution_turns, model_turns, unrecognized, _turns = _build_turn_metrics(trace)
    repairs, thrash_facts = _build_repair_metrics(trace, oracle_results)

    verify_facts, verify_status = readers.oracle_facts(oracle_results, "GovernedVerificationOracle")
    calls = readers.provider_call_split(ledger, conversation_id)
    tokens = readers.token_totals(trace)

    elapsed_s, elapsed_source = _resolve_elapsed(elapsed_s, events_readable, normalized)

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
        "unrecognized_turn_modes": unrecognized,
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
        "repeated_action_max": readers.int_fact(thrash_facts, "longest_identical_action_streak"),
        "repeated_error_max": readers.int_fact(thrash_facts, "largest_same_tool_error_group"),
        "repeated_shell_verification_max": readers.int_fact(
            thrash_facts, "largest_semantic_shell_repeat_group"
        ),
        "repeated_script_restart_max": readers.int_fact(
            thrash_facts, "largest_background_script_restart_group"
        ),
        "actionless_pauses": mark(
            "actionless_pauses", readers.int_fact(thrash_facts, "actionless_pauses")
        ),
        "no_progress_events": readers.no_progress_events(normalized) if events_readable else None,
        "polling_samples": readers.polling_samples(trace),
        "tokens": tokens if tokens is not None else "unavailable",
    }

    required_checks = readers.int_fact(verify_facts, "required_checks")
    if verify_status == "PASS" and required_checks and actions is not None:
        record["actions_per_verified_requirement"] = round(actions / required_checks, 3)

    record["unavailable"] = sorted(set(unavailable))
    return record


def efficiency_record_from_dossier(run_dir: Path, classification: dict[str, Any]) -> dict[str, Any]:
    """Build a record by reading one run's sealed dossier from disk."""
    conversation_id = str(classification.get("conversation_id") or "") or None
    conv_dir = run_dir / "conversations" / (conversation_id or "")

    trace: dict[str, Any] | None = None
    loaded = readers.read_json(conv_dir / "inspect-trace.json")
    if isinstance(loaded, dict):
        trace = loaded

    events = readers.read_jsonl(conv_dir / "events.jsonl")
    ledger = readers.read_jsonl(conv_dir / "provider-call-ledger.jsonl")

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

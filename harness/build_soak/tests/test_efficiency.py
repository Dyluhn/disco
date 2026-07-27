"""Efficiency observability — exact counting, honest gaps, deterministic stats.

Every count here is asserted against CONSTRUCTED durable evidence, so a change to
a counting rule fails loudly instead of quietly re-baselining. The final test
replays the ten sealed Epic-4 context dossiers and reproduces their known
work-cost range without making a single model call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.build_soak.efficiency import (
    AGGREGATED_METRICS,
    LiveEfficiencyProgress,
    aggregate,
    compare_to_baseline,
    efficiency_record,
    efficiency_record_from_dossier,
    median,
    percentile,
    render_report,
    render_table,
    scenario_family,
)

_CID = "conv_efficiency_test"


# --------------------------------------------------------------------------
# Evidence builders — the shapes the product actually persists
# --------------------------------------------------------------------------


def _event(seq: int, kind: str, **extra: object) -> dict[str, object]:
    return {
        "seq": seq,
        "id": f"evt_{seq}",
        "kind": kind,
        "source": extra.pop("source", "agent"),
        "timestamp": f"2026-07-27T00:00:{seq:02d}.000000Z",
        **extra,
    }


def _events(*, actions: int, condensations: int, no_progress: int = 0) -> list[dict[str, object]]:
    out: list[dict[str, object]] = [_event(0, "message", source="user", message={"role": "user"})]
    seq = 1
    for _ in range(actions):
        out.append(_event(seq, "action", tool_call={"tool_name": "file_write"}))
        out.append(
            _event(seq + 1, "observation", action_id=f"evt_{seq}", tool_result={"success": True})
        )
        seq += 2
    for _ in range(condensations):
        out.append(_event(seq, "condensation"))
        seq += 1
    for _ in range(no_progress):
        out.append(_event(seq, "status", status="RUNNING", detail="verify_no_progress:host:abc"))
        seq += 1
    out.append(_event(seq, "status", status="FINISHED"))
    return out


def _trace(
    *,
    planning: int,
    execution: int,
    tokens: tuple[int, int, int] | None = (100, 20, 50),
    sample_count: int | None = 900,
    unrecognized: int = 0,
) -> dict[str, object]:
    scopes: list[dict[str, object]] = []
    for _ in range(planning):
        scopes.append({"mode": "planning", "allowed_tools": ["file_read"]})
    for _ in range(execution):
        scopes.append({"mode": "interactive", "allowed_tools": ["file_write"]})
    for _ in range(unrecognized):
        scopes.append({"mode": "teleportation", "allowed_tools": []})

    spans: list[dict[str, object]] = []
    for index in range(planning + execution):
        spans.append({"span": "agent.step", "event": "start", "request_id": f"req_{index}"})
        end: dict[str, object] = {
            "span": "agent.step",
            "event": "end",
            "request_id": f"req_{index}",
        }
        if tokens is not None:
            end["in_tokens"], end["out_tokens"], end["cached_tokens"] = tokens
        spans.append(end)
    # Polling/inspection noise that must never be counted as a turn or action.
    spans.append({"span": "context_pack", "event": "point", "included": True})
    spans.append({"span": "request_budget.preview", "event": "point"})

    trace: dict[str, object] = {"tool_scopes": scopes, "spans": spans}
    if sample_count is not None:
        trace["aggregation"] = {"sample_count": sample_count, "lossless": True}
    return trace


def _ledger(*, bound: int, unattributed: int) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for _ in range(bound):
        out.append({"host": "opencode.ai", "conversation_id": _CID, "has_tools": True})
    for _ in range(unattributed):
        out.append({"host": "opencode.ai", "conversation_id": None, "has_tools": False})
    return out


def _oracles(
    *,
    repairs: int = 0,
    repair_categories: dict[str, int] | None = None,
    actionless: int = 0,
    required_checks: int | None = None,
    verify_status: str = "PASS",
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = [
        {
            "oracle": "ThrashOracle",
            "status": "PASS",
            "facts": {
                "action_count": 0,
                "actionless_pauses": actionless,
                "model_repair_count": repairs,
                "model_repair_counts": repair_categories or {},
                "longest_identical_action_streak": 1,
                "largest_same_tool_error_group": 0,
                "largest_semantic_shell_repeat_group": 0,
                "largest_background_script_restart_group": 0,
            },
        }
    ]
    if required_checks is not None:
        results.append(
            {
                "oracle": "GovernedVerificationOracle",
                "status": verify_status,
                "facts": {"required_checks": required_checks},
            }
        )
    return results


def _record(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "scenario_id": "p4_ff_static_basic",
        "seed": 1,
        "status": "PASS",
        "conversation_id": _CID,
        "events": _events(actions=5, condensations=2),
        "trace": _trace(planning=3, execution=4),
        "ledger": _ledger(bound=7, unattributed=2),
        "oracle_results": _oracles(),
    }
    kwargs.update(overrides)
    return efficiency_record(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Exact counting
# --------------------------------------------------------------------------


def test_counts_actions_turns_calls_and_compactions_exactly():
    record = _record()
    assert record["actions"] == 5
    assert record["planning_turns"] == 3
    assert record["execution_turns"] == 4
    assert record["model_turns"] == 7
    assert record["compactions"] == 2
    assert record["provider_calls"]["conversation_bound"] == 7
    assert record["unavailable"] == []


def test_polling_samples_are_reported_but_never_counted_as_turns_or_actions():
    # 900 polling samples and 2 inspection spans against 7 real turns: if the
    # reporter ever folded sampling into work, this is where it would show.
    record = _record(trace=_trace(planning=3, execution=4, sample_count=900))
    assert record["polling_samples"] == 900
    assert record["model_turns"] == 7
    assert record["actions"] == 5


def test_unattributed_provider_calls_stay_separate_from_conversation_bound():
    # An unscoped ledger record may belong to another concurrent worker, so it
    # must never inflate this run's authoritative call count.
    record = _record(ledger=_ledger(bound=4, unattributed=9))
    calls = record["provider_calls"]
    assert calls["conversation_bound"] == 4
    assert calls["unattributed_in_window"] == 9
    assert calls["foreign_excluded"] == 0
    assert calls["total_attributed"] == 13


def test_a_foreign_conversations_calls_are_disclosed_not_silently_dropped():
    # A record bound to ANOTHER conversation means the slice was not scoped as
    # expected; it must be visible rather than vanish into the difference.
    ledger = _ledger(bound=2, unattributed=1)
    ledger.append(
        {"host": "opencode.ai", "conversation_id": "conv_someone_else", "has_tools": True}
    )
    record = _record(ledger=ledger)
    calls = record["provider_calls"]
    assert calls["conversation_bound"] == 2
    assert calls["foreign_excluded"] == 1
    assert calls["total_attributed"] == 3


def test_live_repairs_fall_back_to_the_shared_repair_spans_before_the_oracle_runs():
    # Mid-run there is no ThrashOracle result yet, but the same agent.repair
    # spans it counts are already in the trace.
    trace = _trace(planning=1, execution=1)
    spans = trace["spans"]
    assert isinstance(spans, list)
    spans.append({"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"})
    spans.append({"span": "agent.repair", "event": "point", "repair_kind": "bad_json"})
    spans.append({"span": "agent.repair", "event": "point", "repair_kind": ""})  # not a repair
    record = _record(trace=trace, oracle_results=[])
    assert record["model_repairs"] == 2


def test_the_thrash_oracle_fact_wins_over_the_live_span_fallback():
    trace = _trace(planning=1, execution=1)
    spans = trace["spans"]
    assert isinstance(spans, list)
    spans.append({"span": "agent.repair", "event": "point", "repair_kind": "unknown_tool"})
    record = _record(trace=trace, oracle_results=_oracles(repairs=7))
    assert record["model_repairs"] == 7


def test_repairs_and_thrash_maxima_come_from_the_thrash_oracle_facts():
    record = _record(
        oracle_results=_oracles(repairs=3, repair_categories={"bad_json": 2, "unknown_tool": 1})
    )
    assert record["model_repairs"] == 3
    assert record["model_repair_categories"] == {"bad_json": 2, "unknown_tool": 1}
    assert record["repeated_action_max"] == 1
    assert record["repeated_error_max"] == 0


def test_no_progress_events_are_counted_from_the_product_status_detail():
    record = _record(events=_events(actions=2, condensations=1, no_progress=3))
    assert record["no_progress_events"] == 3


def test_elapsed_is_derived_from_the_conversations_own_timestamps():
    record = _record()
    assert record["elapsed_s"] is not None
    assert record["elapsed_source"] == "conversation_event_span"


def test_explicit_runner_elapsed_wins_and_is_labelled_as_such():
    record = _record(elapsed_s=42.5)
    assert record["elapsed_s"] == 42.5
    assert record["elapsed_source"] == "runner_wall_clock"


def test_unrecognized_turn_mode_is_neither_planning_nor_execution():
    record = _record(trace=_trace(planning=2, execution=2, unrecognized=3))
    assert record["planning_turns"] == 2
    assert record["execution_turns"] == 2
    assert record["unrecognized_turn_modes"] == 3


# --------------------------------------------------------------------------
# Honest gaps — unavailable, never a fabricated zero
# --------------------------------------------------------------------------


def test_missing_trace_makes_turns_unavailable_not_zero():
    record = _record(trace=None)
    assert record["planning_turns"] is None
    assert record["execution_turns"] is None
    assert record["model_turns"] is None
    assert "planning_turns" in record["unavailable"]
    assert "model_turns" in record["unavailable"]


def test_missing_ledger_makes_provider_calls_unavailable_not_zero():
    record = _record(ledger=None)
    assert record["provider_calls"]["conversation_bound"] is None
    assert "provider_calls_conversation_bound" in record["unavailable"]


def test_provider_that_reports_no_token_usage_reads_unavailable():
    record = _record(trace=_trace(planning=1, execution=1, tokens=None))
    assert record["tokens"] == "unavailable"


def test_tokens_are_summed_across_agent_step_end_spans_only():
    record = _record(trace=_trace(planning=2, execution=3, tokens=(10, 2, 5)))
    # 5 turns -> 5 end spans; start spans and inspection spans carry no tokens.
    assert record["tokens"] == {"in": 50, "out": 10, "cached": 25}


def test_malformed_events_do_not_raise_and_do_not_fabricate_counts():
    record = _record(events=[{"no_kind_here": True}])
    assert record["actions"] is None
    assert record["compactions"] is None
    assert "actions" in record["unavailable"]


def test_no_oracle_and_no_trace_makes_repairs_unavailable_not_zero():
    record = _record(oracle_results=[], trace=None)
    assert record["model_repairs"] is None
    assert "model_repairs" in record["unavailable"]


def test_a_readable_trace_with_no_repair_spans_proves_zero_repairs():
    # Zero here is EARNED, not invented: the trace was readable and contained no
    # agent.repair span. Contrast with the test above, where nothing was legible.
    record = _record(oracle_results=[])
    assert record["model_repairs"] == 0
    assert "model_repairs" not in record["unavailable"]


def test_actionless_pauses_are_unavailable_without_the_thrash_oracle():
    # No non-oracle source exists for this one, so it stays honestly unknown.
    record = _record(oracle_results=[])
    assert record["actionless_pauses"] is None
    assert "actionless_pauses" in record["unavailable"]


def test_ratio_is_omitted_unless_the_denominator_is_authoritative():
    # No governed verification result at all -> no denominator.
    assert "actions_per_verified_requirement" not in _record()

    # Present but the oracle did not PASS -> the requirement count is not proof.
    not_passed = _record(oracle_results=_oracles(required_checks=5, verify_status="FAIL"))
    assert "actions_per_verified_requirement" not in not_passed

    # Passed with a positive count -> authoritative.
    authoritative = _record(oracle_results=_oracles(required_checks=5))
    assert authoritative["actions_per_verified_requirement"] == 1.0


def test_zero_required_checks_never_divides():
    record = _record(oracle_results=_oracles(required_checks=0))
    assert "actions_per_verified_requirement" not in record


# --------------------------------------------------------------------------
# Families
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario_id", "expected"),
    [
        ("p4_appkit_create", "appkit"),
        ("p4_ff_context_catalog", "context_pressure"),
        ("p4_ff_static_basic", "freeform_static"),
        ("p4_ff_react_steer", "freeform_react"),
        ("p4_ff_node_pause", "freeform_node"),
        ("p4_ff_python_cancel_recovery", "freeform_python"),
        ("p4_ff_import_rollback", "freeform_import"),
        ("something_else", "unclassified"),
        (None, "unclassified"),
    ],
)
def test_scenario_family_is_deterministic(scenario_id, expected):
    assert scenario_family(scenario_id) == expected


# --------------------------------------------------------------------------
# Aggregation — deterministic on tiny, odd, even and mixed batches
# --------------------------------------------------------------------------


def test_median_handles_odd_and_even_counts():
    assert median([5]) == 5
    assert median([1, 3, 5]) == 3
    assert median([1, 3, 5, 7]) == 4  # mean of the two middles
    assert median([]) is None


def test_percentile_is_nearest_rank_so_it_always_names_a_real_run():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 0.90) == 9
    assert percentile(values, 0.95) == 10
    assert percentile([7], 0.95) == 7
    assert percentile([], 0.5) is None


def test_aggregate_reports_counts_quantiles_and_the_worst_run():
    records = [
        _record(
            scenario_id="p4_ff_static_basic", seed=n, events=_events(actions=n, condensations=1)
        )
        for n in (4, 9, 2)
    ]
    for record, run_id in zip(records, ("r0", "r1", "r2"), strict=True):
        record["run_id"] = run_id
    summary = aggregate(records)
    actions = summary["overall"]["actions"]
    assert actions["count"] == 3
    assert actions["median"] == 4
    assert actions["max"] == 9
    assert actions["worst_seed"] == 9
    assert actions["worst_run_id"] == "r1"


def test_no_worst_run_is_named_when_every_run_ties():
    # All three at 5 actions: there is no outlier, so naming one would invent a
    # finding. The max is still reported.
    records = [
        _record(
            scenario_id="p4_ff_static_basic", seed=n, events=_events(actions=5, condensations=0)
        )
        for n in (1, 2, 3)
    ]
    stats = aggregate(records)["overall"]["actions"]
    assert stats["max"] == 5
    assert stats["worst_seed"] is None
    assert stats["worst_scenario_id"] is None


def test_aggregate_partitions_by_family_and_never_mixes_shapes():
    records = [
        _record(
            scenario_id="p4_appkit_create", seed=1, events=_events(actions=30, condensations=0)
        ),
        _record(
            scenario_id="p4_ff_static_basic", seed=2, events=_events(actions=3, condensations=0)
        ),
        _record(
            scenario_id="p4_ff_static_basic", seed=3, events=_events(actions=5, condensations=0)
        ),
    ]
    summary = aggregate(records)
    families = summary["by_family"]
    assert set(families) == {"appkit", "freeform_static"}
    assert families["appkit"]["metrics"]["actions"]["median"] == 30
    assert families["freeform_static"]["metrics"]["actions"]["median"] == 4
    assert families["freeform_static"]["run_count"] == 2


def test_aggregate_counts_unavailable_metrics_separately_from_zero():
    records = [
        _record(seed=1, trace=None),
        _record(seed=2, trace=_trace(planning=2, execution=2)),
    ]
    stats = aggregate(records)["overall"]["model_turns"]
    assert stats["count"] == 1
    assert stats["unavailable_count"] == 1
    assert stats["median"] == 4


def test_aggregate_of_an_empty_batch_is_all_none_not_zero():
    summary = aggregate([])
    assert summary["run_count"] == 0
    for metric in AGGREGATED_METRICS:
        stats = summary["overall"][metric]
        assert stats["count"] == 0
        assert stats["median"] is None
        assert stats["max"] is None


# --------------------------------------------------------------------------
# Baseline comparison — explicit and scenario-matched
# --------------------------------------------------------------------------


def test_baseline_comparison_is_scenario_matched_and_reports_absolute_and_percent():
    baseline = [
        _record(
            scenario_id="p4_ff_static_basic", seed=1, events=_events(actions=10, condensations=0)
        )
    ]
    current = [
        _record(
            scenario_id="p4_ff_static_basic", seed=2, events=_events(actions=15, condensations=0)
        )
    ]
    result = compare_to_baseline(current, baseline)
    actions = next(
        c
        for c in result["comparisons"]
        if c["metric"] == "actions" and c["scenario_id"] == "p4_ff_static_basic"
    )
    assert actions["baseline_median"] == 10
    assert actions["current_median"] == 15
    assert actions["absolute_change"] == 5
    assert actions["percent_change"] == 50.0


def test_baseline_never_compares_across_different_scenarios():
    baseline = [_record(scenario_id="p4_appkit_create", seed=1)]
    current = [_record(scenario_id="p4_ff_static_basic", seed=2)]
    result = compare_to_baseline(current, baseline)
    assert result["comparisons"] == []
    assert result["unmatched_scenarios"] == ["p4_ff_static_basic"]


def test_passing_but_abnormal_runs_are_identified_without_failing_them():
    baseline = [
        _record(
            scenario_id="p4_ff_static_basic", seed=1, events=_events(actions=10, condensations=0)
        )
    ]
    current = [
        _record(
            scenario_id="p4_ff_static_basic",
            seed=2,
            status="PASS",
            events=_events(actions=40, condensations=0),
        )
    ]
    result = compare_to_baseline(current, baseline)
    abnormal = [a for a in result["passing_but_abnormal"] if a["metric"] == "actions"]
    assert len(abnormal) == 1
    assert abnormal[0]["ratio"] == 4.0
    # The run's own verdict is untouched — this is a diagnostic, not a gate.
    assert current[0]["status"] == "PASS"


def test_a_failing_outlier_is_not_reported_as_passing_but_abnormal():
    baseline = [
        _record(
            scenario_id="p4_ff_static_basic", seed=1, events=_events(actions=10, condensations=0)
        )
    ]
    current = [
        _record(
            scenario_id="p4_ff_static_basic",
            seed=2,
            status="FAIL",
            events=_events(actions=40, condensations=0),
        )
    ]
    result = compare_to_baseline(current, baseline)
    assert result["passing_but_abnormal"] == []


# --------------------------------------------------------------------------
# Live progress — bounded, and it calls no provider
# --------------------------------------------------------------------------


def test_live_progress_emits_on_change_and_is_silent_when_nothing_moved():
    lines: list[str] = []
    progress = LiveEfficiencyProgress(
        scenario_id="p4_ff_static_basic", seed=7, emit=lines.append, interval_s=30.0
    )
    assert progress.update(now=0.0, elapsed_s=0.0, actions=1, state="RUNNING") is True
    # Same counters, 1s later, interval not elapsed -> no emission.
    assert progress.update(now=1.0, elapsed_s=1.0, actions=1, state="RUNNING") is False
    assert progress.update(now=2.0, elapsed_s=2.0, actions=2, state="RUNNING") is True
    assert len(lines) == 2


def test_live_progress_still_heartbeats_once_the_interval_elapses():
    lines: list[str] = []
    progress = LiveEfficiencyProgress(scenario_id="s", seed=1, emit=lines.append, interval_s=30.0)
    progress.update(now=0.0, elapsed_s=0.0, actions=1, state="RUNNING")
    assert progress.update(now=29.0, elapsed_s=29.0, actions=1, state="RUNNING") is False
    assert progress.update(now=31.0, elapsed_s=31.0, actions=1, state="RUNNING") is True


def test_live_progress_line_carries_every_required_field():
    lines: list[str] = []
    progress = LiveEfficiencyProgress(scenario_id="p4_appkit_create", seed=42, emit=lines.append)
    progress.update(
        now=0.0,
        elapsed_s=12.0,
        actions=3,
        planning_turns=2,
        execution_turns=1,
        provider_calls=4,
        compactions=1,
        repairs=0,
        state="RUNNING",
    )
    line = lines[0]
    for fragment in (
        "p4_appkit_create",
        "seed 42",
        "12s",
        "act 3",
        "plan 2",
        "exec 1",
        "calls 4",
        "cmpct 1",
        "rep 0",
        "RUNNING",
    ):
        assert fragment in line


def test_a_polling_burst_cannot_flood_the_log():
    lines: list[str] = []
    progress = LiveEfficiencyProgress(scenario_id="s", seed=1, emit=lines.append, interval_s=30.0)
    for tick in range(300):  # 300 polls, nothing changing
        progress.update(now=tick * 0.1, elapsed_s=tick * 0.1, actions=5, state="RUNNING")
    assert len(lines) == 1


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_table_and_report_render_unavailable_metrics_as_na_not_zero():
    records = [_record(seed=1, trace=None, ledger=None)]
    records[0]["run_id"] = "r0"
    table = render_table(records)
    assert "n/a" in table
    report = render_report(records, aggregate(records))
    assert "Unavailable metrics" in report
    assert "planning_turns" in report


def test_report_separates_families():
    records = [
        _record(scenario_id="p4_appkit_create", seed=1),
        _record(scenario_id="p4_ff_static_basic", seed=2),
    ]
    report = render_report(records, aggregate(records))
    assert "Family: appkit" in report
    assert "Family: freeform_static" in report


# --------------------------------------------------------------------------
# Retrospective validation against the SEALED Epic-4 dossiers
# --------------------------------------------------------------------------

_EPIC4 = Path("/var/home/dylan/build-platform-campaign-evidence/2026-07-26/epic4")


def _sealed_dossiers() -> list[tuple[Path, dict]]:
    found: list[tuple[Path, dict]] = []
    for seed_dir in sorted(_EPIC4.glob("seed-4600*-scoped")):
        for classification_path in seed_dir.glob("*/*/classification.json"):
            found.append(
                (
                    classification_path.parent,
                    json.loads(classification_path.read_text(encoding="utf-8")),
                )
            )
    return found


@pytest.mark.skipif(not _EPIC4.is_dir(), reason="sealed Epic-4 evidence not present on this host")
def test_reproduces_the_sealed_epic4_context_lane_without_model_calls():
    """Replays ten sealed PASS dossiers and reproduces their known work-cost.

    The dossiers are read-only here; nothing mutates or re-locks them. The known
    final-candidate range for this lane is actions 10-19, execution turns 7-15,
    planning turns 6 each, compactions 6-12, all ten PASS.
    """
    dossiers = _sealed_dossiers()
    assert len(dossiers) == 10, f"expected 10 sealed dossiers, found {len(dossiers)}"

    records = [efficiency_record_from_dossier(run_dir, cls) for run_dir, cls in dossiers]

    assert [r["status"] for r in records] == ["PASS"] * 10
    assert all(r["planning_turns"] == 6 for r in records)
    assert min(r["actions"] for r in records) == 10
    assert max(r["actions"] for r in records) == 19
    assert min(r["execution_turns"] for r in records) == 7
    assert max(r["execution_turns"] for r in records) == 15
    assert min(r["compactions"] for r in records) == 6
    assert max(r["compactions"] for r in records) == 12
    # Nothing in this lane should be unprovable.
    assert all(r["unavailable"] == [] for r in records)
    # Turn accounting is internally consistent with the model-call spans.
    assert all(r["planning_turns"] + r["execution_turns"] == r["model_turns"] for r in records)
    # Polling dwarfs real work — proof the two are not conflated.
    assert all(r["polling_samples"] > r["model_turns"] * 10 for r in records)


@pytest.mark.skipif(not _EPIC4.is_dir(), reason="sealed Epic-4 evidence not present on this host")
def test_sealed_epic4_replay_is_byte_identical_across_runs():
    """The reporter is pure: replaying the same sealed evidence twice agrees."""
    dossiers = _sealed_dossiers()
    first = [efficiency_record_from_dossier(d, c) for d, c in dossiers]
    second = [efficiency_record_from_dossier(d, c) for d, c in dossiers]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

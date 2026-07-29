"""Batch aggregation and baseline comparison — deterministic, small-batch correct."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from ._record import AGGREGATED_METRICS, SCHEMA_VERSION


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
    """Nearest-rank percentile — deterministic and exact for tiny batches."""
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


def _find_abnormal_runs(
    group: list[dict[str, Any]],
    *,
    scenario_id: str,
    metric: str,
    base_median: float,
    outlier_ratio: float,
) -> list[dict[str, Any]]:
    """Find passing runs that exceed the outlier ratio for one metric."""
    abnormal: list[dict[str, Any]] = []
    if base_median <= 0:
        return abnormal
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
    return abnormal


def _compare_one_scenario(
    scenario_id: str,
    group: list[dict[str, Any]],
    base_group: list[dict[str, Any]],
    *,
    outlier_ratio: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare one scenario's current vs baseline. Returns (comparisons, abnormal)."""
    comparisons: list[dict[str, Any]] = []
    abnormal: list[dict[str, Any]] = []
    for metric in AGGREGATED_METRICS:
        base_values = [v for v in (_metric_value(r, metric) for r in base_group) if v is not None]
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
        abnormal.extend(
            _find_abnormal_runs(
                group,
                scenario_id=scenario_id,
                metric=metric,
                base_median=base_median,
                outlier_ratio=outlier_ratio,
            )
        )
    return comparisons, abnormal


def compare_to_baseline(
    records: Sequence[dict[str, Any]],
    baseline_records: Sequence[dict[str, Any]],
    *,
    outlier_ratio: float = 1.5,
) -> dict[str, Any]:
    """Compare against an EXPLICITLY supplied prior accepted baseline."""
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
        cmp, abn = _compare_one_scenario(
            scenario_id, group, base_group, outlier_ratio=outlier_ratio
        )
        comparisons.extend(cmp)
        abnormal.extend(abn)

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_run_count": len(baseline_records),
        "current_run_count": len(records),
        "outlier_ratio": outlier_ratio,
        "comparisons": comparisons,
        "passing_but_abnormal": abnormal,
        "unmatched_scenarios": unmatched,
    }

"""Build-soak efficiency observability — compatibility facade.

The actual logic now lives in :mod:`._efficiency`:
* :mod:`._efficiency._readers` — evidence readers and helpers.
* :mod:`._efficiency._record` — the per-run efficiency record builder.
* :mod:`._efficiency._aggregate` — batch aggregation and baseline comparison.
* :mod:`._efficiency._report` — human-readable report rendering.
* :mod:`._efficiency._live` — bounded live progress counters.

This module re-exports every public symbol so existing imports are unchanged.
"""

from __future__ import annotations

from ._efficiency._aggregate import (
    _metric_value as _metric_value,
)
from ._efficiency._aggregate import (
    aggregate,
    compare_to_baseline,
    median,
    percentile,
)
from ._efficiency._live import LiveEfficiencyProgress
from ._efficiency._record import (
    AGGREGATED_METRICS,
    SCHEMA_VERSION,
    efficiency_record,
    efficiency_record_from_dossier,
    scenario_family,
)
from ._efficiency._report import render_report, render_table

__all__ = [
    "AGGREGATED_METRICS",
    "LiveEfficiencyProgress",
    "SCHEMA_VERSION",
    "aggregate",
    "compare_to_baseline",
    "efficiency_record",
    "efficiency_record_from_dossier",
    "median",
    "percentile",
    "render_report",
    "render_table",
    "scenario_family",
]

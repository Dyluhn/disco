"""Human-readable report rendering for efficiency observability."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


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


def _render_family_block(family: str, block: dict[str, Any]) -> list[str]:
    """Render one family's metric table."""
    out: list[str] = [f"## Family: {family}  ({block.get('run_count', 0)} runs)", ""]
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
    return out


def _render_unavailable(records: Sequence[dict[str, Any]]) -> list[str]:
    """Render the unavailable-metrics section."""
    unavailable = sorted({name for record in records for name in (record.get("unavailable") or [])})
    if not unavailable:
        return []
    out: list[str] = ["## Unavailable metrics", ""]
    out.append(
        "These could not be proven from the sealed evidence and are reported as `null`, not zero:"
    )
    out.append("")
    for name in unavailable:
        affected = sum(1 for r in records if name in (r.get("unavailable") or []))
        out.append(f"- `{name}` — {affected} run(s)")
    out.append("")
    return out


def _render_baseline_abnormal(abnormal: list[dict[str, Any]], outlier_ratio: Any) -> list[str]:
    """Render the passing-but-abnormal section of the baseline comparison."""
    if not abnormal:
        return ["No passing run exceeded the outlier ratio.", ""]
    out: list[str] = [
        f"**{len(abnormal)} passing-but-abnormal observation(s)** "
        f"(> {outlier_ratio}x the baseline median):",
        "",
    ]
    for item in abnormal:
        out.append(
            f"- `{item['scenario_id']}` seed {item['seed']}: {item['metric']} "
            f"= {_fmt(item['value'])} vs baseline median "
            f"{_fmt(item['baseline_median'])} ({item['ratio']}x)"
        )
    out.append("")
    return out


def _render_baseline_changes(baseline: dict[str, Any]) -> list[str]:
    """Render the changed-metrics table from the baseline comparison."""
    changed = [
        c for c in (baseline.get("comparisons") or []) if c.get("percent_change") not in (None, 0.0)
    ]
    if not changed:
        return []
    out: list[str] = ["| scenario | metric | baseline | current | Δ | Δ% |"]
    out.append("|---|---|---:|---:|---:|---:|")
    for item in sorted(changed, key=lambda c: abs(c.get("percent_change") or 0), reverse=True)[:40]:
        out.append(
            f"| {item['scenario_id']} | {item['metric']} | "
            f"{_fmt(item['baseline_median'])} | {_fmt(item['current_median'])} | "
            f"{item['absolute_change']:+g} | {item['percent_change']:+.1f}% |"
        )
    out.append("")
    return out


def _render_baseline(baseline: dict[str, Any]) -> list[str]:
    """Render the full baseline comparison section."""
    out: list[str] = ["## Baseline comparison", ""]
    out.append(f"Explicitly supplied baseline of {baseline.get('baseline_run_count')} run(s).")
    out.append("")
    out.append(
        "> `elapsed_s` is wall-clock and is confounded by worker count — a batch run "
        "at lower concurrency looks faster without doing less work. Rest a regression "
        "claim on `actions`, `model_turns`, `provider_calls` or `compactions`, and "
        "compare elapsed only against a baseline collected at the same concurrency."
    )
    out.append("")
    out.extend(
        _render_baseline_abnormal(
            baseline.get("passing_but_abnormal") or [], baseline.get("outlier_ratio")
        )
    )
    out.extend(_render_baseline_changes(baseline))
    unmatched = baseline.get("unmatched_scenarios") or []
    if unmatched:
        out.append(
            "Unmatched scenarios (present now, absent from the baseline): "
            + ", ".join(f"`{s}`" for s in unmatched)
        )
        out.append("")
    return out


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
        out.extend(_render_family_block(family, block))

    out.extend(_render_unavailable(records))

    if baseline:
        out.extend(_render_baseline(baseline))

    return "\n".join(out)

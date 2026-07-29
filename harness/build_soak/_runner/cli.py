"""Bounded Build Soak cli owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.reliability.state import source_revision as _source_revision

from ..adapters.disco_api import (
    DiscoApiClient,
    InfraProbeError,
    Transport,
)
from ..efficiency import (
    aggregate as aggregate_efficiency,
)
from ..efficiency import (
    compare_to_baseline as compare_efficiency_to_baseline,
)
from ..efficiency import (
    render_report,
    render_table,
)
from ..resources import (
    AdmissionPolicy,
    HostResources,
    ResourceGate,
    ResourcePool,
    read_host_resources,
    resolve_worker_count,
    safe_worker_count,
)
from .batch import (
    _exit_code,
    _policy_dict,
    _policy_from_args,
    _require_exact_provider,
    _run_batch_item,
    _run_stop_on_non_pass_cohorts,
)
from .common import (
    _BATCH_SUMMARY_NAME,
    _DEFAULT_BASE_URL,
    _DEFAULT_HARD_CAP_S,
    _DEFAULT_INACTIVITY_S,
    _DEFAULT_OUT,
    _SCENARIOS,
)
from .coordinator import (
    run_once,
)
from .ledger import (
    _relay_log_path,
)
from .records import (
    _infra_failure_record,
    _invalid_run_record,
)
from .scenario_io import (
    _driver_catalog_contains,
    _materialize_task_seed,
    batch_summary_name,
    load_scenarios,
)

ScenarioLoader = Callable[[str | Path], dict[str, dict[str, Any]]]


class _CliFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _select_scenarios(
    args: argparse.Namespace,
    *,
    scenario_loader: ScenarioLoader | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    scenarios = (scenario_loader or load_scenarios)(args.scenarios)
    requested = (
        list(scenarios)
        if args.scenario.strip().lower() == "all"
        else [item.strip() for item in args.scenario.split(",") if item.strip()]
    )
    unknown = sorted(set(requested) - set(scenarios))
    if not requested or unknown:
        raise _CliFailure(
            2,
            f"unknown scenario(s) {unknown or [args.scenario]!r}; known: {sorted(scenarios)}",
        )
    if args.iterations <= 0:
        raise _CliFailure(2, "--iterations must be positive")
    return requested, {scenario_id: scenarios[scenario_id] for scenario_id in requested}


def _preflight_scenario_controls(
    selected: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, dict[str, Any]]:
    relay_required = any(
        scenario.get("requires_relay_ledger", True) for scenario in selected.values()
    )
    if relay_required and not _relay_log_path():
        raise _CliFailure(
            3,
            "[build-soak] INFRA_FAILURE: scenario requires the relay ledger to "
            "adjudicate terminal cleanup + provider-after-terminal — set "
            "DISCO_PROVIDER_LEDGER or MINIMAX_RELAY_LOG. Refusing to run a soak "
            "that could SKIP-as-PASS.",
        )
    if relay_required:
        try:
            selected = _require_exact_provider(
                selected,
                expected_host=args.expected_provider_host,
                expected_model=args.expected_provider_model,
            )
        except ValueError as exc:
            raise _CliFailure(3, f"[build-soak] INFRA_FAILURE: {exc}") from exc
    restart_scenarios = sorted(
        scenario_id
        for scenario_id, scenario in selected.items()
        if isinstance(scenario, dict)
        and (scenario.get("lifecycle") or {}).get("restart_after_terminal")
    )
    has_restart_control = bool(
        os.environ.get("DISCO_RELIABILITY_STACK_CONTROL_URL")
        and os.environ.get("DISCO_RELIABILITY_STACK_CONTROL_TOKEN")
    )
    if restart_scenarios and not has_restart_control:
        raise _CliFailure(
            3,
            "[build-soak] INFRA_FAILURE: "
            f"{', '.join(restart_scenarios)} declare a restart lifecycle, but "
            "DISCO_RELIABILITY_STACK_CONTROL_URL / _TOKEN are unset. That control "
            "is provisioned for a disposable App+Agent pair; refusing to restart "
            "a shared stack or record missing harness control as a product failure.",
        )
    return selected


def _source_identity(repo_root: Path) -> tuple[str, str, bool]:
    try:
        return _source_revision(repo_root)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        raise _CliFailure(
            3,
            f"[build-soak] INFRA_FAILURE: could not determine exact source identity: {exc}",
        ) from exc


async def _preflight_inspect(
    args: argparse.Namespace,
    selected: dict[str, dict[str, Any]],
    model: str | None,
    transport_type: type[Any],
) -> bool:
    required = any(
        bool(scenario.get("requires_inspect_trace", True)) for scenario in selected.values()
    )
    if not required:
        return False
    try:
        transport = transport_type(args.base_url)
        status, data = await transport.get_json("/api/debug/inspect")
    except Exception as exc:  # noqa: BLE001
        raise _CliFailure(
            3,
            f"[build-soak] INFRA_FAILURE: inspect preflight failed: {exc}",
        ) from exc
    if status != 200 or data.get("enabled") is not True:
        raise _CliFailure(
            3,
            "[build-soak] INFRA_FAILURE: DISCO_INSPECT is not enabled; refusing "
            "to run without per-conversation model/routing logs.",
        )
    if model:
        try:
            models_status, models_data = await transport.get_json("/models")
        except Exception as exc:  # noqa: BLE001
            raise _CliFailure(
                3,
                f"[build-soak] INFRA_FAILURE: driver-model preflight failed: {exc}",
            ) from exc
        if models_status != 200 or not _driver_catalog_contains(models_data, model):
            raise _CliFailure(
                3,
                "[build-soak] INFRA_FAILURE: requested driver model is not live "
                "and driver-eligible; verify its signed origin approval, secret "
                "binding, and tool-calling capability.",
            )
    return True


@dataclass(frozen=True)
class _ResourceRuntime:
    policy: AdmissionPolicy
    initial: HostResources
    safe_workers: int
    workers: int
    requested_label: str
    pool: ResourcePool
    semaphore: asyncio.Semaphore


def _resource_runtime(
    args: argparse.Namespace,
    batch_dir: Path,
) -> _ResourceRuntime:
    try:
        policy = _policy_from_args(args)
    except ValueError as exc:
        raise _CliFailure(2, f"invalid resource policy: {exc}") from exc
    initial = read_host_resources(disk_path=batch_dir)
    safe_workers = safe_worker_count(initial, policy)
    try:
        workers = resolve_worker_count(args.parallel, initial, policy)
    except ValueError as exc:
        raise _CliFailure(2, str(exc)) from exc
    workers = min(workers, args.iterations)
    if workers == 0:
        raise _CliFailure(
            3,
            "[build-soak] INFRA_FAILURE: no worker fits while preserving the "
            f"configured desktop reserve ({args.memory_reserve_gib:g} GiB RAM, "
            f"{args.disk_reserve_gib:g} GiB disk).",
        )
    requested_label = str(args.parallel)
    gate = ResourceGate(
        policy,
        disk_path=batch_dir,
        poll_s=args.resource_poll,
        wait_timeout_s=args.resource_wait_timeout,
    )
    return _ResourceRuntime(
        policy=policy,
        initial=initial,
        safe_workers=safe_workers,
        workers=workers,
        requested_label=requested_label,
        pool=ResourcePool(gate, policy, max_workers=workers),
        semaphore=asyncio.Semaphore(workers),
    )


@dataclass(frozen=True)
class _BatchExecution:
    args: argparse.Namespace
    requested: list[str]
    selected: dict[str, dict[str, Any]]
    batch_stamp: str
    batch_dir: Path
    db_path: str
    projects_root: str
    model: str | None
    commit: str
    repo_revision: str
    repo_dirty: bool
    resources: _ResourceRuntime
    transport_type: type[Any]


async def _run_iteration(execution: _BatchExecution, index: int) -> dict[str, Any]:
    args = execution.args
    scenario_id = execution.requested[index % len(execution.requested)]
    task_seed = args.seed_base + index
    scenario = _materialize_task_seed(execution.selected[scenario_id], task_seed)
    run_id = f"build_soak_{scenario_id}_{execution.batch_stamp}_{index:03d}"
    try:
        async with execution.resources.semaphore, execution.resources.pool.slot():
            transport: Transport = execution.transport_type(args.base_url)
            client = DiscoApiClient(
                transport,
                db_path=execution.db_path,
                projects_root=execution.projects_root,
                snapshot_wait_s=args.snapshot_wait,
                require_workspace_commit=True,
            )
            classification = await run_once(
                client,
                scenario,
                run_id=run_id,
                out_root=execution.batch_dir,
                model=execution.model,
                autonomous=bool(args.autonomous or scenario.get("autonomous")),
                commit=execution.commit,
                repo_revision=execution.repo_revision,
                repo_dirty=execution.repo_dirty,
                kernel=args.kernel,
                timeout_s=args.timeout,
                hard_cap_s=args.hard_cap,
                require_inspect_trace=bool(scenario.get("requires_inspect_trace", True)),
                parallel_workers=execution.resources.workers,
                seed=task_seed,
            )
    except TimeoutError as exc:
        classification = _infra_failure_record(
            execution.batch_dir,
            run_id,
            scenario,
            InfraProbeError(
                "HOST_RESOURCE_ADMISSION_TIMEOUT",
                {
                    "reason": str(exc),
                    "policy": _policy_dict(execution.resources.policy),
                },
            ),
        )
    except Exception as exc:  # noqa: BLE001 — keep the rest of the soak running
        classification = _invalid_run_record(
            execution.batch_dir,
            run_id,
            scenario,
            f"parallel worker error: {type(exc).__name__}: {exc}",
        )
    status = str(classification.get("status"))
    code = classification.get("code")
    print(
        f"[{index + 1}/{args.iterations}] {run_id}: {status}"
        + (f" / {code}" if code else "")
        + f"  -> {execution.batch_dir / run_id}"
    )
    return _run_batch_item(
        index=index,
        run_id=run_id,
        base=execution.batch_dir,
        classification=classification,
    )


def _announce_resource_runtime(
    args: argparse.Namespace,
    resources: _ResourceRuntime,
) -> None:
    if resources.requested_label != "auto" and int(resources.requested_label) > resources.workers:
        print(
            f"[build-soak] clamped --parallel {resources.requested_label} "
            f"to {resources.workers} for the current resource headroom."
        )
    print(
        f"[build-soak] {args.iterations} iterations, {resources.workers} "
        f"parallel workers (safe now: {resources.safe_workers}); reserving "
        f"{args.memory_reserve_gib:g} GiB RAM and {args.disk_reserve_gib:g} "
        "GiB disk for the desktop."
    )


def _batch_summary(
    execution: _BatchExecution,
    runs: list[dict[str, Any]],
    *,
    batch_id: str,
    started_at: str,
    stopped_after_cohort_index: int | None,
    inspect_required: bool,
) -> dict[str, Any]:
    status_counts = Counter(str(item.get("status")) for item in runs)
    failure_counts = Counter(str(item.get("code")) for item in runs if item.get("code"))
    final_resources = read_host_resources(disk_path=execution.batch_dir)
    args = execution.args
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "scenario_ids": execution.requested,
        "scenario_counts": dict(
            sorted(Counter(str(item.get("scenario_id")) for item in runs).items())
        ),
        "surfaces": sorted(
            {str(scenario.get("surface", "build")) for scenario in execution.selected.values()}
        ),
        "commit": execution.commit,
        "repo_revision": execution.repo_revision,
        "repo_dirty": execution.repo_dirty,
        "model": execution.model,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "iterations": args.iterations,
        "admission": {
            "mode": "bounded_cohorts_stop_on_non_pass",
            "requested_iterations": args.iterations,
            "admitted_iterations": len(runs),
            "not_admitted_iterations": args.iterations - len(runs),
            "stopped_after_cohort_index": stopped_after_cohort_index,
        },
        "seed_base": args.seed_base,
        "parallel": {
            "requested": execution.resources.requested_label,
            "resolved": execution.resources.workers,
            "safe_at_start": execution.resources.safe_workers,
        },
        "resource_policy": _policy_dict(execution.resources.policy),
        "resources": {
            "initial": execution.resources.initial.to_dict(),
            "final": final_resources.to_dict(),
        },
        "inspect_required": inspect_required,
        "status_counts": dict(sorted(status_counts.items())),
        "failure_code_counts": dict(sorted(failure_counts.items())),
        "runs": runs,
    }


def _write_batch_reports(
    args: argparse.Namespace,
    batch_dir: Path,
    summary: dict[str, Any],
    runs: list[dict[str, Any]],
) -> None:
    records = [{**(item.get("efficiency") or {}), "run_id": item.get("run_id")} for item in runs]
    baseline = _efficiency_baseline_comparison(
        records,
        getattr(args, "efficiency_baseline", ""),
    )
    summary["efficiency_summary"] = aggregate_efficiency(records)
    if baseline is not None:
        summary["efficiency_baseline_comparison"] = baseline
    summary_path = batch_dir / batch_summary_name(getattr(args, "summary_name", None))
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"[build-soak] batch report -> {summary_path}")
    report_path = batch_dir / "efficiency-report.md"
    report_path.write_text(
        render_report(records, summary["efficiency_summary"], baseline=baseline),
        encoding="utf-8",
    )
    print(f"[build-soak] efficiency report -> {report_path}")
    print(f"\n{render_table(records)}\n")


async def amain(
    args: argparse.Namespace,
    *,
    scenario_loader: ScenarioLoader | None = None,
) -> int:
    from ..adapters.disco_api import HttpTransport  # live deps only on the CLI path

    repo_root = Path(__file__).resolve().parents[2]
    try:
        requested_scenarios, selected = _select_scenarios(
            args,
            scenario_loader=scenario_loader,
        )
        selected = _preflight_scenario_controls(selected, args)
        repo_revision, commit, repo_dirty = _source_identity(repo_root)
        db_path = args.db or os.environ.get("DISCO_DB") or str(repo_root / "disco.db")
        model = args.model or os.environ.get("DISCO_SOAK_MODEL") or None
        projects_root = args.projects_root or os.environ.get("DISCO_PROJECTS_ROOT") or ""
        require_inspect_preflight = await _preflight_inspect(
            args,
            selected,
            model,
            HttpTransport,
        )
    except _CliFailure as exc:
        print(str(exc), file=sys.stderr)
        return exc.code

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    batch_stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    batch_label = requested_scenarios[0] if len(requested_scenarios) == 1 else "scenario_matrix"
    batch_id = f"batch_{batch_label}_{batch_stamp}"
    batch_dir = out_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC).isoformat()

    try:
        resources = _resource_runtime(args, batch_dir)
    except _CliFailure as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    _announce_resource_runtime(args, resources)
    execution = _BatchExecution(
        args=args,
        requested=requested_scenarios,
        selected=selected,
        batch_stamp=batch_stamp,
        batch_dir=batch_dir,
        db_path=db_path,
        projects_root=projects_root,
        model=model,
        commit=commit,
        repo_revision=repo_revision,
        repo_dirty=repo_dirty,
        resources=resources,
        transport_type=HttpTransport,
    )
    runs, stopped_after_cohort_index = await _run_stop_on_non_pass_cohorts(
        args.iterations,
        resources.workers,
        lambda index: _run_iteration(execution, index),
    )
    runs = sorted(runs, key=lambda item: int(item["index"]))
    if stopped_after_cohort_index is not None:
        print(
            "[build-soak] non-PASS in completed cohort; "
            f"{args.iterations - len(runs)} later iteration(s) were not admitted"
        )
    summary = _batch_summary(
        execution,
        runs,
        batch_id=batch_id,
        started_at=started_at,
        stopped_after_cohort_index=stopped_after_cohort_index,
        inspect_required=require_inspect_preflight,
    )
    _write_batch_reports(args, batch_dir, summary, runs)
    return max((_exit_code(str(item.get("status"))) for item in runs), default=0)


def _efficiency_baseline_comparison(
    records: list[dict[str, Any]], baseline_path: str
) -> dict[str, Any] | None:
    """Compare against an operator-named baseline summary, or return None.

    Never auto-selects a historical batch: an unspecified baseline means no
    comparison, and an unreadable one is reported as an error rather than
    silently degrading into "no regression found".
    """
    if not baseline_path:
        return None
    try:
        loaded = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[build-soak] efficiency baseline unreadable ({baseline_path}): {exc}")
        return {"error": f"baseline unreadable: {exc}", "baseline_path": baseline_path}
    baseline_records = [
        {**(item.get("efficiency") or {}), "run_id": item.get("run_id")}
        for item in (loaded.get("runs") or [])
        if isinstance(item, dict)
    ]
    baseline_records = [r for r in baseline_records if r.get("scenario_id")]
    if not baseline_records:
        print(f"[build-soak] efficiency baseline carries no efficiency records: {baseline_path}")
        return {"error": "baseline carries no efficiency records", "baseline_path": baseline_path}
    comparison = compare_efficiency_to_baseline(records, baseline_records)
    comparison["baseline_path"] = baseline_path
    return comparison


def _add_resource_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--memory-reserve-gib",
        type=float,
        default=32.0,
        help="RAM kept unavailable to new soak workers for the desktop (default: 32 GiB)",
    )
    parser.add_argument("--memory-per-worker-gib", type=float, default=3.0)
    parser.add_argument("--disk-reserve-gib", type=float, default=10.0)
    parser.add_argument("--disk-per-worker-gib", type=float, default=0.5)
    parser.add_argument("--cpus-per-worker", type=int, default=1)
    parser.add_argument("--resource-poll", type=float, default=5.0)
    parser.add_argument("--resource-wait-timeout", type=float, default=1800.0)


def _argument_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Headless live-API Build Soak runner (§25).")
    p.add_argument(
        "--scenario",
        required=True,
        help="one id, a comma-separated scenario matrix, or 'all' from scenarios.yaml",
    )
    p.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="total runs; a scenario matrix is assigned round-robin across them",
    )
    p.add_argument(
        "--parallel",
        default="auto",
        help="concurrent workers: 'auto' (default) or a positive integer, clamped to "
        "live RAM/CPU/disk headroom",
    )
    p.add_argument("--model", default=None, help="driver model (default $DISCO_SOAK_MODEL)")
    p.add_argument(
        "--expected-provider-host",
        default=os.environ.get("DISCO_RELIABILITY_EXPECTED_PROVIDER_HOST", ""),
        help="required substring in every observed provider-ledger host",
    )
    p.add_argument(
        "--expected-provider-model",
        default=os.environ.get("DISCO_RELIABILITY_EXPECTED_PROVIDER_MODEL", ""),
        help="exact model id required in every observed provider-ledger record",
    )
    p.add_argument("--out", default=_DEFAULT_OUT, help="output root for run folders")
    p.add_argument(
        "--summary-name",
        default=_BATCH_SUMMARY_NAME,
        help=(
            "filename of the batch report (default batch-summary.json). "
            "Non-promoting lanes MUST pass a different name: the promotion reader "
            "discovers evidence with rglob('batch-summary.json') and reads the "
            "newest match, so a qualification batch written under that name would "
            "be counted as governed promotion evidence."
        ),
    )
    p.add_argument(
        "--efficiency-baseline",
        default="",
        help=(
            "path to a PRIOR ACCEPTED batch summary to compare work-cost against. "
            "Must be named explicitly — the runner never auto-selects a historical "
            "batch, because a silently chosen baseline makes the comparison mean "
            "whatever the directory happened to contain. Comparison is diagnostic: "
            "it never changes a verdict or the exit code."
        ),
    )
    p.add_argument("--autonomous", action="store_true", help="headless auto-approve")
    p.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    p.add_argument(
        "--kernel",
        choices=("disco",),
        default="disco",
        help="Build kernel evidence label; only disco is supported.",
    )
    p.add_argument("--db", default=None, help="disco.db path (default $DISCO_DB or repo/disco.db)")
    p.add_argument(
        "--projects-root",
        default=None,
        help="ProjectStore root for the authoritative workspace snapshot read "
        "(default $DISCO_PROJECTS_ROOT or the agent-server's own resolved default)",
    )
    p.add_argument(
        "--snapshot-wait",
        type=float,
        default=45.0,
        help="seconds to wait for a just-finished build's workspace snapshot to flush "
        "(cold-start flush lag after a fresh sandbox can exceed the old 15s → INVALID_RUN)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_INACTIVITY_S,
        help="PROGRESS-AWARE inactivity window (s): the terminal wait keeps waiting while "
        "the build emits NEW events; it stops only after THIS much no-progress silence "
        "(a genuine wedge) — NOT a blind wall-clock. A still-progressing build is never cut off.",
    )
    p.add_argument(
        "--hard-cap",
        type=float,
        default=_DEFAULT_HARD_CAP_S,
        help="generous safety ceiling (s) bounding a truly-hung run, set well above a normal "
        "build; a cutoff here WHILE STILL PROGRESSING is recorded INVALID_RUN (inconclusive), "
        "not a product BUILD_DID_NOT_FINISH (Bug 15).",
    )
    p.add_argument("--scenarios", default=str(_SCENARIOS))
    p.add_argument(
        "--seed-base",
        type=int,
        default=0,
        help="first deterministic task seed; each iteration increments it by one",
    )
    _add_resource_arguments(p)
    return p


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(_argument_parser().parse_args(argv)))

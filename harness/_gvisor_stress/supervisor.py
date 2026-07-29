"""Process supervision and the gVisor stress entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cleanup import cleanup
from .cli_args import build_parser
from .docker_cli import DockerCLI
from .log_capture import SandboxLogCapture
from .report import human_summary, minimal_failure_report, write_report
from .types import HarnessSetupError
from .util import clipped, debug_log_path, normalize_docker_socket, utc_now
from .workload import run_harness


def worker_process_entry(
    args: argparse.Namespace,
    report_path: Path,
    log_path: Path,
    conversation_id: str,
) -> None:
    """Run all real SDK activity in a process the parent can bound/terminate."""
    started = time.monotonic()
    started_at = utc_now()
    try:
        with SandboxLogCapture(log_path):
            report = asyncio.run(run_harness(args, log_path, conversation_id=conversation_id))
    except BaseException as exc:  # noqa: BLE001 - child must leave a failure artifact
        report = minimal_failure_report(
            args,
            log_path=log_path,
            conversation_id=conversation_id,
            reason=clipped(f"worker {type(exc).__name__}: {exc}"),
            started_at=started_at,
            elapsed_s=time.monotonic() - started,
        )
    write_report(report_path, report)


def supervisor_cleanup(
    *,
    args: argparse.Namespace,
    conversation_id: str,
    deadline: float,
    worker_was_terminated: bool,
) -> tuple[dict[str, Any], str]:
    """Authoritative cleanup after the SDK worker and all its threads are gone."""
    try:
        docker_socket = normalize_docker_socket(args.docker_socket)
    except HarnessSetupError:
        docker_socket = "unix:///var/run/docker.sock"
    docker_cli = DockerCLI(docker_socket)
    remaining = deadline - time.monotonic()
    # If the worker was killed mid-request, keep watching for almost the entire
    # reserved tail: Docker may finish an already-received create after its
    # client disappeared. A normal, quiescent worker needs only a double-check.
    stability = max(0.15, remaining - 4.0) if worker_was_terminated else 0.15
    try:
        cleanup_report = asyncio.run(
            cleanup(
                service=None,
                instances=[],
                conversation_id=conversation_id,
                docker_cli=docker_cli,
                global_deadline=deadline,
                empty_stability_s=stability,
            )
        )
    except BaseException as exc:  # noqa: BLE001 - fail closed in the parent
        cleanup_report = {
            "leak_check_error": clipped(f"supervisor cleanup {type(exc).__name__}: {exc}"),
            "leaks_remaining": [],
        }
    return cleanup_report, docker_cli.transport


@dataclass(frozen=True)
class _SupervisorRun:
    args: argparse.Namespace
    started: float
    started_at: str
    deadline: float
    report_path: Path
    log_path: Path
    conversation_id: str
    cleanup_reserve_s: float
    worker_budget_s: float
    worker_args: argparse.Namespace


@dataclass(frozen=True)
class _WorkerOutcome:
    terminated: bool
    stopped: bool
    exitcode: int | None


def _prepare_supervisor(argv: list[str] | None) -> _SupervisorRun:
    args = build_parser().parse_args(argv)
    started = time.monotonic()
    report_path = args.out.expanduser().resolve()
    log_path = debug_log_path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    supervisor_tail = min(30.0, max(5.0, args.duration_cap * 0.10), args.duration_cap * 0.40)
    worker_budget = max(0.10, args.duration_cap - supervisor_tail)
    worker_args = argparse.Namespace(**vars(args))
    worker_args.duration_cap = max(0.10, worker_budget - min(2.0, worker_budget * 0.10))
    return _SupervisorRun(
        args=args,
        started=started,
        started_at=utc_now(),
        deadline=started + args.duration_cap,
        report_path=report_path,
        log_path=log_path,
        conversation_id=f"gvisor-stress-{uuid.uuid4().hex}",
        cleanup_reserve_s=supervisor_tail,
        worker_budget_s=worker_budget,
        worker_args=worker_args,
    )


def _run_worker(run: _SupervisorRun) -> _WorkerOutcome:
    # The parent stays free of docker-py/default-executor work. If a package call
    # ignores cancellation, terminating this child eliminates its threads first;
    # only then does the parent perform the authoritative final label sweep.
    process = multiprocessing.get_context("spawn").Process(
        target=worker_process_entry,
        args=(
            run.worker_args,
            run.report_path,
            run.log_path,
            run.conversation_id,
        ),
        name="gvisor-stress-worker",
    )
    process.start()
    worker_stop_deadline = run.deadline - run.cleanup_reserve_s
    process.join(timeout=max(0.0, worker_stop_deadline - time.monotonic()))
    terminated = process.is_alive()
    if process.is_alive():
        process.terminate()
        process.join(timeout=max(0.0, min(2.0, run.deadline - time.monotonic())))
    if process.is_alive():
        process.kill()
        process.join(timeout=max(0.0, min(1.0, run.deadline - time.monotonic())))
    return _WorkerOutcome(
        terminated=terminated,
        stopped=not process.is_alive(),
        exitcode=process.exitcode,
    )


def _authoritative_cleanup(
    run: _SupervisorRun,
    worker: _WorkerOutcome,
) -> tuple[dict[str, Any], str]:
    if worker.stopped:
        return supervisor_cleanup(
            args=run.args,
            conversation_id=run.conversation_id,
            deadline=run.deadline,
            worker_was_terminated=worker.terminated,
        )
    return (
        {
            "leak_check_error": (
                "SDK worker was still alive; a final label snapshot would not be authoritative"
            ),
            "leaks_remaining": [],
        },
        "not_run_worker_alive",
    )


def _load_worker_report(run: _SupervisorRun) -> dict[str, Any]:
    try:
        return json.loads(run.report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return minimal_failure_report(
            run.args,
            log_path=run.log_path,
            conversation_id=run.conversation_id,
            reason=clipped(f"worker report unavailable: {type(exc).__name__}: {exc}"),
            started_at=run.started_at,
            elapsed_s=time.monotonic() - run.started,
        )


def _supervisor_failures(
    worker: _WorkerOutcome,
    final_cleanup: dict[str, Any],
    cap_hit: bool,
) -> list[str]:
    failures: list[str] = []
    if worker.terminated:
        failures.append("supervisor terminated the SDK worker at its wall-clock boundary")
    if not worker.stopped:
        failures.append("SDK worker could not be stopped before final cleanup")
    if worker.exitcode not in (0, None) and not worker.terminated:
        failures.append(f"SDK worker exited with status {worker.exitcode}")
    if final_cleanup.get("leak_check_error"):
        failures.append("supervisor container leak verification failed")
    if final_cleanup.get("leaks_remaining"):
        failures.append("supervisor found containers remaining after cleanup")
    if final_cleanup.get("volume_leak_check_error"):
        failures.append("supervisor volume leak verification failed")
    if final_cleanup.get("volumes_remaining"):
        failures.append("supervisor found volumes remaining after cleanup")
    if cap_hit:
        failures.append("global wall-clock duration cap was reached")
    return failures


def _apply_supervisor_evidence(
    report: dict[str, Any],
    run: _SupervisorRun,
    worker: _WorkerOutcome,
    final_cleanup: dict[str, Any],
    cleanup_transport: str,
    total_elapsed: float,
    cap_hit: bool,
) -> None:
    report.setdefault("cleanup", {})["supervisor_final"] = final_cleanup
    report["elapsed_s"] = round(total_elapsed, 6)
    report["finished_at"] = utc_now()
    configuration = report.setdefault("configuration", {})
    configuration["conversation_id"] = run.conversation_id
    configuration["duration_cap_s"] = run.args.duration_cap
    configuration["worker_duration_cap_s"] = round(run.worker_args.duration_cap, 3)
    configuration["supervisor_cleanup_reserve_s"] = round(run.cleanup_reserve_s, 3)
    report.setdefault("timing", {})["global_duration_cap_hit"] = cap_hit
    report["supervisor"] = {
        "worker_budget_s": round(run.worker_budget_s, 3),
        "worker_was_terminated": worker.terminated,
        "worker_stopped_before_final_cleanup": worker.stopped,
        "worker_exitcode": worker.exitcode,
        "final_cleanup_transport": cleanup_transport,
        "final_cleanup_authoritative": worker.stopped,
    }


def _recompute_parent_validity(
    report: dict[str, Any],
    args: argparse.Namespace,
    cap_hit: bool,
) -> None:
    counters = report.get("counters", {})
    timing = report.get("timing", {})
    completed_all = (
        counters.get("containers_created") == args.containers
        and counters.get("execs_scheduled") == args.containers * args.execs_per_container
        and counters.get("execs_completed") == args.containers * args.execs_per_container
    )
    probes_covered = counters.get("status_probe_containers_covered", 0) >= args.containers
    other_validity = (
        not report.get("setup_errors")
        and not report.get("orchestration_errors")
        and counters.get("instrumentation_errors", 0) == 0
        and counters.get("outstanding_create_tasks", 0) == 0
        and completed_all
        and probes_covered
        and not timing.get("workload_timed_out", False)
        and not cap_hit
    )
    policy = report.setdefault("pass_policy", {})
    policy["validity_pass"] = other_validity
    report["passed"] = bool(policy.get("core_pass", False) and other_validity)
    report["verdict"] = "pass" if report["passed"] else "fail"
    report["failure_reasons"] = [
        reason
        for reason in report.get("failure_reasons", [])
        if reason
        not in {
            "sandbox resource leak verification failed or resources remain",
            "global duration cap was reached",
        }
    ]


def _apply_final_verdict(
    report: dict[str, Any],
    args: argparse.Namespace,
    failures: list[str],
    cap_hit: bool,
) -> None:
    if failures:
        report["passed"] = False
        report["verdict"] = "fail"
        reasons = report.setdefault("failure_reasons", [])
        reasons.extend(reason for reason in failures if reason not in reasons)
        report.setdefault("pass_policy", {})["validity_pass"] = False
        return
    _recompute_parent_validity(report, args, cap_hit)


def main(argv: list[str] | None = None) -> int:
    run = _prepare_supervisor(argv)
    worker = _run_worker(run)
    final_cleanup, cleanup_transport = _authoritative_cleanup(run, worker)
    report = _load_worker_report(run)
    total_elapsed = time.monotonic() - run.started
    cap_hit = total_elapsed >= run.args.duration_cap
    _apply_supervisor_evidence(
        report,
        run,
        worker,
        final_cleanup,
        cleanup_transport,
        total_elapsed,
        cap_hit,
    )
    failures = _supervisor_failures(worker, final_cleanup, cap_hit)
    _apply_final_verdict(report, run.args, failures, cap_hit)
    write_report(run.report_path, report)
    print(human_summary(report, run.report_path), flush=True)
    print(
        "REAL_APIS: SandboxConfig -> service_from_config -> LocalSandboxService."
        "healthcheck/create -> LocalSandboxInstance.exec_shell -> "
        "destroy/destroy_by_conversation",
        flush=True,
    )
    return 0 if report["passed"] else 1

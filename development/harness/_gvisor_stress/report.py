"""Report serialization and human summary for gVisor stress."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from .constants import IMAGE
from .util import utc_now


def workload_pass_policy(
    *,
    allowed_op_failures: int,
    core_pass: bool,
    validity_pass: bool,
) -> dict[str, Any]:
    return {
        "needless_recreates_required": 0,
        "allowed_op_failures": allowed_op_failures,
        "op_failure_rate_ceiling": 0.01,
        "op_failure_threshold_justification": (
            "Allow at most floor(1% of planned execs): transient per-op API errors "
            "are retryable upstream, but more than 1% is too disruptive for a build. "
            "Small runs below 100 execs allow zero failures."
        ),
        "core_pass": core_pass,
        "validity_pass": validity_pass,
        "validity_requirements": [
            "all requested containers created",
            "all planned execs completed",
            "status/inspect probes covered every created container",
            "classifier instrumentation remained operational",
            "no retained container-create worker remained unsettled",
            "the workload completed before the reserved cleanup window",
            "global duration cap was not reached",
            "final docker ps and volume label checks succeeded with no leaks",
        ],
    }


def workload_metric_semantics() -> dict[str, str]:
    return {
        "death_verdicts": (
            "Final SandboxUnavailableError verdicts with dead/died phrasing observed "
            "at the real classifier boundary."
        ),
        "confirmed_deaths": (
            "Death verdicts whose immediate independent inspect successfully read "
            "State.Running=false. Inspect errors/404s are not confirmation."
        ),
        "needless_recreates": "death_verdicts - confirmed_deaths",
        "transient_downgrades": (
            "Provisional death classifications overturned by the shared re-verify "
            "window into 'transient sandbox API error' per-op verdicts."
        ),
        "op_failures": (
            "Logical execs that raised to the caller or returned nonzero/timed-out results."
        ),
    }


def human_summary(report: dict[str, Any], report_path: Path) -> str:
    counters = report["counters"]
    allowed = report["pass_policy"]["allowed_op_failures"]
    cleanup_data = report.get("cleanup", {})
    authoritative_cleanup = cleanup_data.get("supervisor_final") or cleanup_data
    leaks: int | str = (
        "UNKNOWN"
        if authoritative_cleanup.get("leak_check_error")
        else len(authoritative_cleanup.get("leaks_remaining", []))
    )
    volumes: int | str = (
        "UNKNOWN"
        if authoritative_cleanup.get("volume_leak_check_error")
        else len(authoritative_cleanup.get("volumes_remaining", []))
    )
    return (
        f"{report['verdict'].upper()} gvisor-stress: "
        f"execs={counters['execs_completed']}/{counters['execs_planned']} "
        f"death_verdicts={counters['death_verdicts']} "
        f"confirmed_deaths={counters['confirmed_deaths']} "
        f"needless_recreates={counters['needless_recreates']} "
        f"transient_downgrades={counters['transient_downgrades']} "
        f"op_failures={counters['op_failures']} (allowed<={allowed}) "
        f"container_leaks={leaks} volume_leaks={volumes} report={report_path}"
    )


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def minimal_failure_report(
    args: argparse.Namespace,
    *,
    log_path: Path,
    conversation_id: str,
    reason: str,
    started_at: str,
    elapsed_s: float,
) -> dict[str, Any]:
    """Fail-closed report used only if the supervised worker cannot report."""
    planned = args.containers * args.execs_per_container
    allowed = math.floor(planned * 0.01)
    counters = {
        "death_verdicts": 0,
        "confirmed_deaths": 0,
        "needless_recreates": 0,
        "transient_downgrades": 0,
        "per_op_classifications": 0,
        "op_failures": 0,
        "containers_created": 0,
        "execs_planned": planned,
        "execs_scheduled": 0,
        "execs_attempted": 0,
        "execs_completed": 0,
        "execs_succeeded": 0,
        "execs_cancelled": 0,
    }
    return {
        "schema_version": 1,
        "harness": "gvisor/docker-py sandbox concurrency stress",
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_s": round(elapsed_s, 6),
        "timing": {
            "workload_timed_out": True,
            "create_phase_timed_out": False,
            "work_deadline_hit": False,
            "global_duration_cap_hit": True,
        },
        "configuration": {
            "backend": "local",
            "runtime": args.runtime,
            "docker_socket_argument": args.docker_socket,
            "image": IMAGE,
            "containers": args.containers,
            "execs_per_container": args.execs_per_container,
            "concurrency_per_container": args.concurrency,
            "duration_cap_s": args.duration_cap,
            "conversation_id": conversation_id,
        },
        "real_service": {"class": None, "observed_instance_base_class": None},
        "real_apis_used": [
            "SandboxConfig",
            "service_from_config -> LocalSandboxService",
            "healthcheck/create/exec_shell/destroy/destroy_by_conversation",
        ],
        "counters": counters,
        "pass_policy": {
            "needless_recreates_required": 0,
            "allowed_op_failures": allowed,
            "op_failure_rate_ceiling": 0.01,
            "core_pass": False,
            "validity_pass": False,
        },
        "verdict": "fail",
        "passed": False,
        "failure_reasons": [reason],
        "setup_errors": [],
        "orchestration_errors": [reason],
        "events": {},
        "cleanup": {},
        "debug_log": str(log_path),
    }

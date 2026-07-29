"""Typed state and results for the gVisor stress harness."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .constants import MAX_EVENT_SAMPLES


class HarnessSetupError(RuntimeError):
    """The host cannot provide the real Docker/runsc test prerequisites."""


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    spawn_error: str | None = None
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.spawn_error is None


@dataclass
class Metrics:
    planned_execs: int
    start_monotonic: float
    containers_created: int = 0
    container_create_failures: int = 0
    container_create_cancellations: int = 0
    execs_scheduled: int = 0
    execs_attempted: int = 0
    execs_completed: int = 0
    execs_succeeded: int = 0
    execs_cancelled: int = 0
    death_verdicts: int = 0
    confirmed_deaths: int = 0
    confirmation_alive: int = 0
    confirmation_inconclusive: int = 0
    transient_downgrades: int = 0
    per_op_classifications: int = 0
    op_failures: int = 0
    op_exceptions: int = 0
    op_nonzero_results: int = 0
    op_timed_out_results: int = 0
    op_outer_timeouts: int = 0
    status_inspect_probes: int = 0
    status_probe_running: int = 0
    status_probe_not_running: int = 0
    status_probe_failures: int = 0
    status_probe_completed_instance_ids: set[str] = field(default_factory=set)
    instrumentation_errors: int = 0
    create_phase_timed_out: bool = False
    outstanding_create_tasks: int = 0
    work_deadline_hit: bool = False
    duration_cap_hit: bool = False
    workload_timed_out: bool = False
    setup_errors: list[str] = field(default_factory=list)
    orchestration_errors: list[str] = field(default_factory=list)
    create_error_samples: list[dict[str, Any]] = field(default_factory=list)
    op_failure_samples: list[dict[str, Any]] = field(default_factory=list)
    death_confirmations: list[dict[str, Any]] = field(default_factory=list)
    status_probe_error_samples: list[dict[str, Any]] = field(default_factory=list)

    def since_start(self) -> float:
        return time.monotonic() - self.start_monotonic

    def add_sample(self, target: list[dict[str, Any]], sample: dict[str, Any]) -> None:
        if len(target) < MAX_EVENT_SAMPLES:
            target.append(sample)

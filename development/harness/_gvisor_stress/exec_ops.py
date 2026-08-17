"""Bounded execution and status-probe operations."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .constants import (
    COMMANDS,
    PER_EXEC_OUTER_TIMEOUT_S,
    PER_EXEC_TIMEOUT_S,
    PROBE_INTERVAL_S,
    PROBE_TIMEOUT_S,
)
from .types import Metrics
from .util import clipped


async def run_one_exec(
    *,
    instance: Any,
    container_index: int,
    exec_index: int,
    semaphore: asyncio.Semaphore,
    start_gate: asyncio.Event,
    work_deadline: float,
    metrics: Metrics,
) -> None:
    await start_gate.wait()
    try:
        async with semaphore:
            remaining = work_deadline - time.monotonic()
            if remaining <= 0:
                metrics.execs_cancelled += 1
                return
            metrics.execs_attempted += 1
            command_name, command = COMMANDS[(container_index + exec_index) % len(COMMANDS)]
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    instance.exec_shell(command, timeout_s=PER_EXEC_TIMEOUT_S),
                    timeout=min(PER_EXEC_OUTER_TIMEOUT_S, remaining),
                )
            except asyncio.CancelledError:
                metrics.execs_cancelled += 1
                raise
            except TimeoutError as exc:
                metrics.op_failures += 1
                metrics.op_outer_timeouts += 1
                metrics.execs_completed += 1
                metrics.add_sample(
                    metrics.op_failure_samples,
                    {
                        "container_index": container_index,
                        "instance_id": instance.id,
                        "exec_index": exec_index,
                        "command": command_name,
                        "kind": "harness_outer_timeout",
                        "error": clipped(exc),
                        "elapsed_s": round(time.monotonic() - started, 6),
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001 - caller-visible operation failure
                metrics.op_failures += 1
                metrics.op_exceptions += 1
                metrics.execs_completed += 1
                metrics.add_sample(
                    metrics.op_failure_samples,
                    {
                        "container_index": container_index,
                        "instance_id": instance.id,
                        "exec_index": exec_index,
                        "command": command_name,
                        "kind": "exception",
                        "error": clipped(f"{type(exc).__name__}: {exc}"),
                        "elapsed_s": round(time.monotonic() - started, 6),
                    },
                )
                return

            metrics.execs_completed += 1
            if result.timed_out or result.exit_code != 0:
                metrics.op_failures += 1
                if result.timed_out:
                    metrics.op_timed_out_results += 1
                if result.exit_code != 0:
                    metrics.op_nonzero_results += 1
                metrics.add_sample(
                    metrics.op_failure_samples,
                    {
                        "container_index": container_index,
                        "instance_id": instance.id,
                        "exec_index": exec_index,
                        "command": command_name,
                        "kind": "failed_exec_result",
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                        "stdout": clipped(result.stdout),
                        "stderr": clipped(result.stderr),
                        "elapsed_s": round(time.monotonic() - started, 6),
                    },
                )
            else:
                metrics.execs_succeeded += 1
    except asyncio.CancelledError:
        raise


async def status_probe_loop(
    *,
    instance: Any,
    container_index: int,
    start_gate: asyncio.Event,
    stop_event: asyncio.Event,
    work_deadline: float,
    metrics: Metrics,
) -> None:
    """Continuously reload state through the real shared docker-py client."""
    await start_gate.wait()
    while not stop_event.is_set() and time.monotonic() < work_deadline:
        try:
            alive = await asyncio.wait_for(
                asyncio.to_thread(instance._safe_reload),  # harness-level status instrumentation
                timeout=min(PROBE_TIMEOUT_S, max(0.001, work_deadline - time.monotonic())),
            )
            if alive:
                metrics.status_probe_running += 1
            else:
                metrics.status_probe_not_running += 1
            metrics.status_inspect_probes += 1
            metrics.status_probe_completed_instance_ids.add(instance.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - probe evidence, not an exec verdict
            metrics.status_probe_failures += 1
            metrics.status_inspect_probes += 1
            metrics.status_probe_completed_instance_ids.add(instance.id)
            metrics.add_sample(
                metrics.status_probe_error_samples,
                {
                    "container_index": container_index,
                    "instance_id": instance.id,
                    "error": clipped(f"{type(exc).__name__}: {exc}"),
                    "elapsed_s": round(metrics.since_start(), 6),
                },
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=PROBE_INTERVAL_S)
        except TimeoutError:
            pass

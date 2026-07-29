"""Supervised SDK workload and evidence assembly."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from disco.tools.sandbox import (
    LocalSandboxService,
    SandboxConfig,
    SandboxSpec,
    service_from_config,
)

from .cleanup import await_with_deadline, cleanup, stop_tasks
from .constants import (
    CLI_TIMEOUT_S,
    CREATE_TIMEOUT_S,
    HEALTHCHECK_TIMEOUT_S,
    IMAGE,
    MAX_EVENT_SAMPLES,
    OWNER_ID,
    PER_EXEC_OUTER_TIMEOUT_S,
    PER_EXEC_TIMEOUT_S,
    PROBE_INTERVAL_S,
)
from .docker_cli import DockerCLI
from .exec_ops import run_one_exec, status_probe_loop
from .observer import ClassifierObserver, install_classifier_observer
from .report import workload_metric_semantics, workload_pass_policy
from .types import HarnessSetupError, Metrics
from .util import _LOG, cleanup_reserve, clipped, normalize_docker_socket, utc_now


@dataclass(frozen=True)
class _WorkloadConfig:
    args: argparse.Namespace
    log_path: Path
    started_at: str
    started: float
    global_deadline: float
    reserve_s: float
    work_deadline: float
    planned_execs: int
    docker_socket: str
    socket_error: str | None
    conversation_id: str
    client_timeout_s: int
    stop_timeout_s: int


@dataclass
class _WorkloadState:
    metrics: Metrics
    docker_cli: DockerCLI
    service: LocalSandboxService | None = None
    instances: list[Any] = field(default_factory=list)
    create_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    create_task_indices: dict[asyncio.Task[Any], int] = field(default_factory=dict)
    harvested_create_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    create_group: asyncio.Future[Any] | None = None
    exec_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    probe_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    stop_probes: asyncio.Event = field(default_factory=asyncio.Event)
    start_gate: asyncio.Event = field(default_factory=asyncio.Event)
    docker_version: str | None = None
    observed_instance_base: str | None = None
    cleanup_report: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _WorkloadVerdict:
    needless_recreates: int
    allowed_op_failures: int
    core_pass: bool
    leak_check_ok: bool
    completed_all: bool
    probe_coverage_ok: bool
    validity_pass: bool
    passed: bool
    failure_reasons: list[str]


def _workload_config(
    args: argparse.Namespace,
    log_path: Path,
    conversation_id: str | None,
) -> _WorkloadConfig:
    started_at = utc_now()
    started = time.monotonic()
    global_deadline = started + args.duration_cap
    reserve = cleanup_reserve(args.duration_cap)
    socket_error: str | None = None
    try:
        docker_socket = normalize_docker_socket(args.docker_socket)
    except HarnessSetupError as exc:
        # No resource can have been created yet. Use the normal local socket so
        # the report and authoritative empty cleanup check can still be produced.
        docker_socket = "unix:///var/run/docker.sock"
        socket_error = clipped(f"{type(exc).__name__}: {exc}")
    return _WorkloadConfig(
        args=args,
        log_path=log_path,
        started_at=started_at,
        started=started,
        global_deadline=global_deadline,
        reserve_s=reserve,
        work_deadline=global_deadline - reserve,
        planned_execs=args.containers * args.execs_per_container,
        docker_socket=docker_socket,
        socket_error=socket_error,
        conversation_id=conversation_id or f"gvisor-stress-{uuid.uuid4().hex}",
        client_timeout_s=max(1, min(30, int(max(1.0, reserve / 4)))),
        stop_timeout_s=max(1, min(5, int(max(1.0, reserve / 12)))),
    )


def _workload_state(config: _WorkloadConfig) -> _WorkloadState:
    return _WorkloadState(
        metrics=Metrics(
            planned_execs=config.planned_execs,
            start_monotonic=config.started,
        ),
        docker_cli=DockerCLI(config.docker_socket),
    )


def _harvest_finished_creates(
    state: _WorkloadState,
) -> None:
    """Collect every retained service.create result exactly once."""
    for task in state.create_tasks:
        if task in state.harvested_create_tasks or not task.done():
            continue
        state.harvested_create_tasks.add(task)
        index = state.create_task_indices[task]
        if task.cancelled():
            state.metrics.container_create_cancellations += 1
            continue
        try:
            instance = task.result()
        except Exception as exc:  # noqa: BLE001 - retain exact create failure
            state.metrics.container_create_failures += 1
            state.metrics.add_sample(
                state.metrics.create_error_samples,
                {
                    "container_index": index,
                    "error": clipped(f"{type(exc).__name__}: {exc}"),
                },
            )
            continue
        state.instances.append(instance)
        state.metrics.containers_created += 1
        _LOG.debug(
            "created container index=%d instance=%s docker_id=%s",
            index,
            instance.id,
            getattr(getattr(instance, "_container", None), "id", "unknown"),
        )


async def _preflight_service(
    config: _WorkloadConfig,
    state: _WorkloadState,
) -> None:
    if config.socket_error:
        raise HarnessSetupError(config.socket_error)
    state.docker_version = await await_with_deadline(
        state.docker_cli.preflight(),
        deadline=config.work_deadline,
        max_timeout_s=CLI_TIMEOUT_S + 1,
    )
    resolved = service_from_config(
        SandboxConfig(
            backend="local",
            docker_socket=config.docker_socket,
            runtime=config.args.runtime,
            image=IMAGE,
            client_timeout_s=config.client_timeout_s,
            stop_timeout_s=config.stop_timeout_s,
        )
    )
    if not isinstance(resolved, LocalSandboxService):
        raise HarnessSetupError(
            "service_from_config did not select LocalSandboxService; "
            f"got {type(resolved).__module__}.{type(resolved).__name__}. "
            "Use a real Docker socket path (a socket containing 'podman' is intentionally "
            "routed to PodmanSandboxService)."
        )
    state.service = resolved
    state.observed_instance_base = install_classifier_observer(
        resolved,
        ClassifierObserver(state.metrics, state.docker_cli),
    )
    await await_with_deadline(
        resolved.healthcheck(),
        deadline=config.work_deadline,
        max_timeout_s=HEALTHCHECK_TIMEOUT_S,
    )
    _LOG.info(
        "preflight complete docker=%s service=%s runtime=%s image=%s socket=%s",
        state.docker_version,
        type(resolved).__name__,
        config.args.runtime,
        IMAGE,
        config.docker_socket,
    )


async def _create_instances(
    config: _WorkloadConfig,
    state: _WorkloadState,
) -> None:
    service = state.service
    if service is None:
        raise HarnessSetupError("LocalSandboxService was unavailable after preflight")
    for index in range(config.args.containers):
        task = asyncio.create_task(
            service.create(
                SandboxSpec(),
                owner_id=OWNER_ID,
                conversation_id=config.conversation_id,
            ),
            name=f"create-{index}",
        )
        state.create_tasks.append(task)
        state.create_task_indices[task] = index
    state.create_group = asyncio.gather(
        *state.create_tasks,
        return_exceptions=True,
    )
    try:
        await await_with_deadline(
            asyncio.shield(state.create_group),
            deadline=config.work_deadline,
            max_timeout_s=CREATE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        state.metrics.create_phase_timed_out = True
        state.metrics.workload_timed_out = True
        state.metrics.work_deadline_hit = time.monotonic() >= config.work_deadline
        raise HarnessSetupError(
            "concurrent container creation exceeded its bounded phase; "
            "retained workers will be drained before label cleanup"
        ) from exc
    _harvest_finished_creates(state)
    if state.metrics.containers_created != config.args.containers:
        raise HarnessSetupError(
            f"only {state.metrics.containers_created}/{config.args.containers} "
            "containers created; a partial load is not a valid stress run"
        )


async def _exec_worker(
    *,
    config: _WorkloadConfig,
    state: _WorkloadState,
    instance: Any,
    container_index: int,
    semaphore: asyncio.Semaphore,
    next_exec_index: list[int],
) -> None:
    while time.monotonic() < config.work_deadline:
        exec_index = next_exec_index[container_index]
        if exec_index >= config.args.execs_per_container:
            return
        # No await separates this read and increment, so same-loop workers cannot
        # claim the same logical operation.
        next_exec_index[container_index] += 1
        await run_one_exec(
            instance=instance,
            container_index=container_index,
            exec_index=exec_index,
            semaphore=semaphore,
            start_gate=state.start_gate,
            work_deadline=config.work_deadline,
            metrics=state.metrics,
        )


async def _run_exec_load(
    config: _WorkloadConfig,
    state: _WorkloadState,
) -> None:
    semaphores = [asyncio.Semaphore(config.args.concurrency) for _instance in state.instances]
    for container_index, instance in enumerate(state.instances):
        state.probe_tasks.append(
            asyncio.create_task(
                status_probe_loop(
                    instance=instance,
                    container_index=container_index,
                    start_gate=state.start_gate,
                    stop_event=state.stop_probes,
                    work_deadline=config.work_deadline,
                    metrics=state.metrics,
                ),
                name=f"status-probe-{container_index}",
            )
        )
    next_exec_index = [0 for _instance in state.instances]
    workers_per_container = min(
        config.args.concurrency,
        config.args.execs_per_container,
    )
    for worker_slot in range(workers_per_container):
        for container_index, instance in enumerate(state.instances):
            state.exec_tasks.append(
                asyncio.create_task(
                    _exec_worker(
                        config=config,
                        state=state,
                        instance=instance,
                        container_index=container_index,
                        semaphore=semaphores[container_index],
                        next_exec_index=next_exec_index,
                    ),
                    name=f"exec-worker-{worker_slot}-{container_index}",
                )
            )
    state.metrics.execs_scheduled = len(state.instances) * config.args.execs_per_container
    state.start_gate.set()
    if not state.exec_tasks:
        return
    try:
        await await_with_deadline(
            asyncio.gather(*state.exec_tasks),
            deadline=config.work_deadline,
            max_timeout_s=max(
                0.001,
                config.work_deadline - time.monotonic(),
            ),
        )
    except TimeoutError:
        state.metrics.workload_timed_out = True
        state.metrics.work_deadline_hit = time.monotonic() >= config.work_deadline
        await stop_tasks(
            state.exec_tasks,
            deadline=config.global_deadline,
        )


async def _execute_workload(
    config: _WorkloadConfig,
    state: _WorkloadState,
) -> None:
    await _preflight_service(config, state)
    await _create_instances(config, state)
    await _run_exec_load(config, state)


def _record_workload_error(
    config: _WorkloadConfig,
    state: _WorkloadState,
    exc: BaseException,
) -> None:
    if isinstance(exc, HarnessSetupError):
        state.metrics.setup_errors.append(clipped(f"{type(exc).__name__}: {exc}"))
        _LOG.error("setup failed: %s", exc)
        return
    if isinstance(exc, TimeoutError):
        state.metrics.workload_timed_out = True
        state.metrics.work_deadline_hit = time.monotonic() >= config.work_deadline
        state.metrics.orchestration_errors.append(clipped(f"TimeoutError: {exc}"))
        _LOG.error("workload deadline reached: %s", exc)
        return
    state.metrics.orchestration_errors.append(clipped(f"{type(exc).__name__}: {exc}"))
    _LOG.exception("unexpected harness orchestration error")


async def _drain_create_tasks(
    config: _WorkloadConfig,
    state: _WorkloadState,
    cleanup_tail: float,
) -> None:
    pending = [task for task in state.create_tasks if not task.done()]
    drain_deadline = config.global_deadline - cleanup_tail
    if pending and drain_deadline > time.monotonic():
        await asyncio.wait(
            pending,
            timeout=max(0.0, drain_deadline - time.monotonic()),
        )
    _harvest_finished_creates(state)
    pending = [task for task in state.create_tasks if not task.done()]
    state.metrics.outstanding_create_tasks = len(pending)
    if pending:
        state.metrics.orchestration_errors.append(
            f"{len(pending)} retained container-create task(s) did not "
            "settle before the cleanup tail"
        )
        await stop_tasks(
            pending,
            deadline=config.global_deadline,
            max_timeout_s=min(3.0, cleanup_tail),
        )
        _harvest_finished_creates(state)
    if state.create_group is not None and state.create_group.done():
        with contextlib.suppress(Exception):
            state.create_group.result()


async def _finalize_workload(
    config: _WorkloadConfig,
    state: _WorkloadState,
) -> None:
    state.start_gate.set()
    await stop_tasks(state.exec_tasks, deadline=config.global_deadline)
    if state.metrics.execs_completed < state.metrics.execs_scheduled:
        state.metrics.execs_cancelled = max(
            state.metrics.execs_cancelled,
            state.metrics.execs_scheduled - state.metrics.execs_completed,
        )
    state.stop_probes.set()
    await stop_tasks(state.probe_tasks, deadline=config.global_deadline)
    cleanup_tail = min(30.0, max(1.0, config.reserve_s * 0.25))
    await _drain_create_tasks(config, state, cleanup_tail)
    try:
        state.cleanup_report = await cleanup(
            service=state.service,
            instances=state.instances,
            conversation_id=config.conversation_id,
            docker_cli=state.docker_cli,
            global_deadline=config.global_deadline,
            empty_stability_s=(
                min(10.0, max(0.15, cleanup_tail * 0.8))
                if state.metrics.outstanding_create_tasks
                else 0.15
            ),
        )
    except Exception as exc:  # noqa: BLE001 - preserve a JSON failure report
        state.metrics.orchestration_errors.append(clipped(f"cleanup {type(exc).__name__}: {exc}"))
        state.cleanup_report = {
            "leak_check_error": clipped(f"cleanup crashed: {type(exc).__name__}: {exc}"),
            "leaks_remaining": [],
        }


def _failure_reasons(
    config: _WorkloadConfig,
    state: _WorkloadState,
    verdict: _WorkloadVerdict,
) -> list[str]:
    metrics = state.metrics
    deadline_reason = (
        "workload deadline reached; reserved cleanup window began"
        if metrics.work_deadline_hit
        else "a bounded workload phase timed out"
    )
    conditions = [
        (
            verdict.needless_recreates != 0,
            f"needless_recreates={verdict.needless_recreates} (must be 0)",
        ),
        (
            metrics.op_failures > verdict.allowed_op_failures,
            f"op_failures={metrics.op_failures} exceeds allowed {verdict.allowed_op_failures}",
        ),
        (bool(metrics.setup_errors), "setup/preflight failed"),
        (bool(metrics.orchestration_errors), "harness orchestration failed"),
        (metrics.instrumentation_errors != 0, "classifier instrumentation failed"),
        (
            not verdict.completed_all,
            "incomplete load: "
            f"containers={metrics.containers_created}/{config.args.containers}, "
            f"execs={metrics.execs_completed}/{config.planned_execs}",
        ),
        (
            not verdict.probe_coverage_ok,
            "periodic status/inspect probes did not cover every container",
        ),
        (metrics.create_phase_timed_out, "container creation phase timed out"),
        (
            metrics.work_deadline_hit or metrics.workload_timed_out,
            deadline_reason,
        ),
        (
            metrics.outstanding_create_tasks != 0,
            "container create workers remained unsettled at cleanup",
        ),
        (metrics.duration_cap_hit, "global duration cap was reached"),
        (
            not verdict.leak_check_ok,
            "sandbox resource leak verification failed or resources remain",
        ),
    ]
    return [reason for condition, reason in conditions if condition]


def _adjudicate_workload(
    config: _WorkloadConfig,
    state: _WorkloadState,
) -> _WorkloadVerdict:
    metrics = state.metrics
    needless = metrics.death_verdicts - metrics.confirmed_deaths
    allowed = math.floor(config.planned_execs * 0.01)
    leak_ok = _cleanup_is_verified(state.cleanup_report)
    completed = _load_completed(config, metrics)
    created_ids = {instance.id for instance in state.instances}
    probes_ok = created_ids.issubset(metrics.status_probe_completed_instance_ids)
    core_pass = needless == 0 and metrics.op_failures <= allowed
    validity_pass = _validity_pass(
        metrics,
        completed=completed,
        probes_ok=probes_ok,
        leak_ok=leak_ok,
    )
    provisional = _WorkloadVerdict(
        needless_recreates=needless,
        allowed_op_failures=allowed,
        core_pass=core_pass,
        leak_check_ok=leak_ok,
        completed_all=completed,
        probe_coverage_ok=probes_ok,
        validity_pass=validity_pass,
        passed=core_pass and validity_pass,
        failure_reasons=[],
    )
    return replace(
        provisional,
        failure_reasons=_failure_reasons(config, state, provisional),
    )


def _cleanup_is_verified(cleanup_report: dict[str, Any]) -> bool:
    return (
        cleanup_report.get("leak_check_error") is None
        and not cleanup_report.get("leaks_remaining")
        and cleanup_report.get("volume_leak_check_error") is None
        and not cleanup_report.get("volumes_remaining")
    )


def _load_completed(config: _WorkloadConfig, metrics: Metrics) -> bool:
    return (
        metrics.containers_created == config.args.containers
        and metrics.execs_scheduled == config.planned_execs
        and metrics.execs_completed == config.planned_execs
    )


def _validity_pass(
    metrics: Metrics,
    *,
    completed: bool,
    probes_ok: bool,
    leak_ok: bool,
) -> bool:
    return (
        not metrics.setup_errors
        and not metrics.orchestration_errors
        and metrics.instrumentation_errors == 0
        and completed
        and probes_ok
        and not metrics.workload_timed_out
        and metrics.outstanding_create_tasks == 0
        and not metrics.duration_cap_hit
        and leak_ok
    )


def _counter_evidence(
    config: _WorkloadConfig,
    metrics: Metrics,
    verdict: _WorkloadVerdict,
) -> dict[str, Any]:
    return {
        "death_verdicts": metrics.death_verdicts,
        "confirmed_deaths": metrics.confirmed_deaths,
        "needless_recreates": verdict.needless_recreates,
        "transient_downgrades": metrics.transient_downgrades,
        "per_op_classifications": metrics.per_op_classifications,
        "op_failures": metrics.op_failures,
        "confirmation_alive": metrics.confirmation_alive,
        "confirmation_inconclusive": metrics.confirmation_inconclusive,
        "containers_created": metrics.containers_created,
        "container_create_failures": metrics.container_create_failures,
        "container_create_cancellations": metrics.container_create_cancellations,
        "execs_planned": config.planned_execs,
        "execs_scheduled": metrics.execs_scheduled,
        "execs_attempted": metrics.execs_attempted,
        "execs_completed": metrics.execs_completed,
        "execs_succeeded": metrics.execs_succeeded,
        "execs_cancelled": metrics.execs_cancelled,
        "op_exceptions": metrics.op_exceptions,
        "op_nonzero_results": metrics.op_nonzero_results,
        "op_timed_out_results": metrics.op_timed_out_results,
        "op_outer_timeouts": metrics.op_outer_timeouts,
        "status_inspect_probes": metrics.status_inspect_probes,
        "status_probe_running": metrics.status_probe_running,
        "status_probe_not_running": metrics.status_probe_not_running,
        "status_probe_failures": metrics.status_probe_failures,
        "status_probe_containers_covered": len(metrics.status_probe_completed_instance_ids),
        "instrumentation_errors": metrics.instrumentation_errors,
        "outstanding_create_tasks": metrics.outstanding_create_tasks,
    }


def _configuration_evidence(
    config: _WorkloadConfig,
    state: _WorkloadState,
) -> dict[str, Any]:
    args = config.args
    return {
        "backend": "local",
        "service_expected": "LocalSandboxService",
        "runtime": args.runtime,
        "docker_socket_argument": args.docker_socket,
        "docker_socket_normalized": config.docker_socket,
        "independent_inspect_transport": state.docker_cli.transport,
        "docker_server_version": state.docker_version,
        "image": IMAGE,
        "containers": args.containers,
        "execs_per_container": args.execs_per_container,
        "concurrency_per_container": args.concurrency,
        "maximum_simultaneous_execs": args.containers * args.concurrency,
        "per_exec_timeout_s": PER_EXEC_TIMEOUT_S,
        "per_exec_outer_timeout_s": PER_EXEC_OUTER_TIMEOUT_S,
        "docker_client_timeout_s": config.client_timeout_s,
        "container_stop_timeout_s": config.stop_timeout_s,
        "status_probe_interval_s": PROBE_INTERVAL_S,
        "duration_cap_s": args.duration_cap,
        "cleanup_reserve_s": round(config.reserve_s, 3),
        "conversation_id": config.conversation_id,
    }


def _workload_report(
    config: _WorkloadConfig,
    state: _WorkloadState,
    verdict: _WorkloadVerdict,
) -> dict[str, Any]:
    metrics = state.metrics
    service = state.service
    return {
        "schema_version": 1,
        "harness": "gvisor/docker-py sandbox concurrency stress",
        "started_at": config.started_at,
        "finished_at": utc_now(),
        "elapsed_s": round(time.monotonic() - config.started, 6),
        "timing": {
            "workload_timed_out": metrics.workload_timed_out,
            "create_phase_timed_out": metrics.create_phase_timed_out,
            "work_deadline_hit": metrics.work_deadline_hit,
            "global_duration_cap_hit": metrics.duration_cap_hit,
        },
        "configuration": _configuration_evidence(config, state),
        "real_service": {
            "class": (
                f"{type(service).__module__}.{type(service).__name__}"
                if service is not None
                else None
            ),
            "observed_instance_base_class": state.observed_instance_base,
        },
        "real_apis_used": [
            "SandboxConfig(backend='local', runtime=<runsc|runc>, image='disco-sandbox:base')",
            "service_from_config(config) -> LocalSandboxService",
            "LocalSandboxService.healthcheck()",
            "LocalSandboxService.create(SandboxSpec(), owner_id=..., conversation_id=...)",
            "LocalSandboxInstance.exec_shell(command, timeout_s=...)",
            "LocalSandboxInstance.destroy()",
            "LocalSandboxService.destroy_by_conversation(conversation_id)",
        ],
        "counters": _counter_evidence(config, metrics, verdict),
        "metric_semantics": workload_metric_semantics(),
        "pass_policy": workload_pass_policy(
            allowed_op_failures=verdict.allowed_op_failures,
            core_pass=verdict.core_pass,
            validity_pass=verdict.validity_pass,
        ),
        "verdict": "pass" if verdict.passed else "fail",
        "passed": verdict.passed,
        "failure_reasons": verdict.failure_reasons,
        "setup_errors": metrics.setup_errors,
        "orchestration_errors": metrics.orchestration_errors,
        "events": {
            "create_error_samples": metrics.create_error_samples,
            "op_failure_samples": metrics.op_failure_samples,
            "death_confirmations": metrics.death_confirmations,
            "status_probe_error_samples": metrics.status_probe_error_samples,
            "samples_capped_at": MAX_EVENT_SAMPLES,
        },
        "cleanup": state.cleanup_report,
        "debug_log": str(config.log_path),
    }


async def run_harness(
    args: argparse.Namespace,
    log_path: Path,
    *,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Run one bounded SDK workload and always return a fail-closed report."""
    config = _workload_config(args, log_path, conversation_id)
    state = _workload_state(config)
    try:
        await _execute_workload(config, state)
    except (HarnessSetupError, TimeoutError) as exc:
        _record_workload_error(config, state, exc)
    except Exception as exc:  # noqa: BLE001 - report and still tear down
        _record_workload_error(config, state, exc)
    finally:
        await _finalize_workload(config, state)
    if time.monotonic() >= config.global_deadline:
        state.metrics.duration_cap_hit = True
    verdict = _adjudicate_workload(config, state)
    return _workload_report(config, state, verdict)

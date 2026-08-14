"""Bounded Build Soak batch owner."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .. import failure_codes as fc
from ..efficiency import (
    efficiency_record_from_dossier,
)
from ..resources import (
    GIB,
    AdmissionPolicy,
)


def _exit_code(status: str) -> int:
    return {
        fc.PASS: 0,
        fc.FAIL: 1,
        fc.INVALID_RUN: 2,
        fc.INFRA_FAILURE: 3,
    }.get(status, 1)


def _policy_from_args(args: argparse.Namespace) -> AdmissionPolicy:
    return AdmissionPolicy(
        memory_reserve_bytes=int(args.memory_reserve_gib * GIB),
        memory_per_worker_bytes=int(args.memory_per_worker_gib * GIB),
        disk_reserve_bytes=int(args.disk_reserve_gib * GIB),
        disk_per_worker_bytes=int(args.disk_per_worker_gib * GIB),
        cpus_per_worker=args.cpus_per_worker,
    )


def _policy_dict(policy: AdmissionPolicy) -> dict[str, int]:
    return {
        "memory_reserve_bytes": policy.memory_reserve_bytes,
        "memory_per_worker_bytes": policy.memory_per_worker_bytes,
        "disk_reserve_bytes": policy.disk_reserve_bytes,
        "disk_per_worker_bytes": policy.disk_per_worker_bytes,
        "cpus_per_worker": policy.cpus_per_worker,
    }


def _run_batch_item(
    *, index: int, run_id: str, base: Path, classification: dict[str, Any]
) -> dict[str, Any]:
    cid = str(classification.get("conversation_id") or "")
    trace_path = base / run_id / "conversations" / cid / "inspect-trace.json"
    trace: dict[str, Any] = {}
    if cid and trace_path.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            loaded = json.loads(trace_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                trace = loaded
    thrash = next(
        (
            result
            for result in classification.get("oracle_results") or []
            if isinstance(result, dict) and result.get("oracle") == "ThrashOracle"
        ),
        None,
    )
    # Bounded aggregation scalars only (work-order §6.2): the batch summary must
    # disclose source drops, continuity, finalization, conflicts, and the
    # lossless verdict without ever copying the unbounded event list.
    aggregation_raw = trace.get("aggregation")
    aggregation = aggregation_raw if isinstance(aggregation_raw, dict) else {}
    return {
        "index": index,
        "run_id": run_id,
        "run_dir": str(base / run_id),
        "scenario_id": classification.get("scenario_id"),
        "status": classification.get("status"),
        "code": classification.get("code"),
        "severity": classification.get("severity"),
        "conversation_id": classification.get("conversation_id"),
        "seed": classification.get("seed"),
        "inspect": {
            "captured": bool(trace),
            "event_count": int(trace.get("event_count") or 0),
            "routing_decisions": len(trace.get("routing_decisions") or []),
            "spans": len(trace.get("spans") or []),
            "aggregate_dropped_event_count": trace.get("dropped_event_count"),
            "source_dropped_event_count": trace.get("source_dropped_event_count"),
            "lossless": aggregation.get("lossless"),
            "finalized": aggregation.get("finalized"),
            "continuity": aggregation.get("continuity"),
            "continuity_reason": aggregation.get("continuity_reason"),
            "sample_count": aggregation.get("sample_count"),
            "accepted_sample_count": aggregation.get("accepted_sample_count"),
            "overlap_sample_count": aggregation.get("overlap_sample_count"),
            "unique_event_count": aggregation.get("unique_event_count"),
            "first_retained_seq": aggregation.get("first_retained_seq"),
            "last_retained_seq": aggregation.get("last_retained_seq"),
            "conflict_count": aggregation.get("conflict_count"),
            "failure_reasons": aggregation.get("failure_reasons"),
        },
        "thrash_oracle": thrash,
        # Work-cost observability (diagnostic only — no threshold here changes a
        # verdict). Derived from this run's own sealed dossier, so it replays
        # offline to identical numbers.
        "efficiency": efficiency_record_from_dossier(base / run_id, classification),
    }


async def _run_stop_on_non_pass_cohorts(
    iterations: int,
    workers: int,
    run_iteration: Callable[[int], Awaitable[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int | None]:
    """Admit bounded cohorts and stop before the next one after any non-PASS.

    A semaphore alone limits concurrency but still queues every later task; as
    soon as one slot frees, a later model call can start before the current
    cohort's verdicts are known. Reliability admission is stricter: finish and
    adjudicate the whole bounded cohort, then admit the next cohort only when
    every result is PASS. Calls already in the failing cohort remain preserved
    evidence but no later index is created.
    """

    if iterations <= 0 or workers <= 0:
        raise ValueError("iterations and workers must be positive")
    runs: list[dict[str, Any]] = []
    for start in range(0, iterations, workers):
        stop = min(start + workers, iterations)
        cohort = await asyncio.gather(*(run_iteration(index) for index in range(start, stop)))
        runs.extend(cohort)
        if any(str(item.get("status")) != "PASS" for item in cohort):
            return runs, stop - 1
    return runs, None


def _require_exact_provider(
    selected: dict[str, dict[str, Any]],
    *,
    expected_host: str,
    expected_model: str,
    expected_vision_host: str = "",
    expected_vision_model: str = "",
) -> dict[str, dict[str, Any]]:
    """Inject the campaign's exact wire-provider assertion into every scenario.

    Scenario fixtures remain provider-neutral for hermetic classifier tests. A
    live run, however, must not let that neutrality turn the provider oracle into
    a SKIP. Existing contradictory assertions are rejected instead of overwritten.
    """

    host = expected_host.strip()
    model = expected_model.strip()
    if not host or not model:
        raise ValueError("live provider evidence requires nonempty expected host and model")
    vision_host = expected_vision_host.strip()
    vision_model = expected_vision_model.strip()
    if bool(vision_host) != bool(vision_model):
        raise ValueError(
            "live visual-provider evidence requires both expected vision host and model"
        )
    secured = copy.deepcopy(selected)
    for scenario_id, scenario in secured.items():
        assertions = scenario.setdefault("assertions", {})
        if not isinstance(assertions, dict):
            raise ValueError(f"{scenario_id}.assertions must be a mapping")
        existing = assertions.get("provider") or {}
        if not isinstance(existing, dict):
            raise ValueError(f"{scenario_id}.assertions.provider must be a mapping")
        for field, required in (("require_host_substr", host), ("model", model)):
            configured = existing.get(field)
            if configured is not None and str(configured) != required:
                raise ValueError(
                    f"{scenario_id}.assertions.provider.{field}={configured!r} "
                    f"conflicts with required {required!r}"
                )
        configured_visual = existing.get("visual_observer")
        if configured_visual is not None:
            required_visual = {
                "require_host_substr": vision_host,
                "model": vision_model,
            }
            if not vision_host or configured_visual != required_visual:
                raise ValueError(
                    f"{scenario_id}.assertions.provider.visual_observer="
                    f"{configured_visual!r} conflicts with required {required_visual!r}"
                )
        assertions["provider"] = {
            **existing,
            "require_ledger": True,
            "require_host_substr": host,
            "model": model,
        }
        if vision_host:
            assertions["provider"]["visual_observer"] = {
                "require_host_substr": vision_host,
                "model": vision_model,
            }
    return secured

"""Terminal cleanup admission for one Build Soak run."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from .. import failure_codes as fc
from ..adapters.disco_api import (
    INACTIVE_TIMEOUT,
    LIVE_THRASH_STOP,
    CollectedRun,
)
from ..evidence_sink import _timeline_md
from ..ports import ProductClient
from .bindings import CoordinatorBindings
from .records import _invalid_run_record
from .thrash import (
    _confirmed_live_thrash_stop,
    _current_thrash_failure_matches_retained,
)


def _allow_global_cleanup_fallback(parallel_workers: int) -> bool:
    """Allow a global delta only when this process is the sole soak lane.

    ``parallel_workers`` is the inner worker count and cannot describe the
    separate lane processes used by the live campaign.  The lane launcher sets
    this explicit boundary marker; ordinary one-process runs retain the
    historical serial fallback.
    """
    if os.environ.get("DISCO_BUILD_SOAK_OUTER_LANE") == "1":
        return False
    return parallel_workers <= 1


def _trusted_inactive_nonterminal(evidence: Any) -> bool:
    return bool(
        isinstance(evidence, dict)
        and evidence.get("schema_version") == 1
        and evidence.get("drive_status") == INACTIVE_TIMEOUT
        and evidence.get("terminal_snapshot_available") is False
        and type(evidence.get("frozen_max_seq")) is int
    )


def _trusted_live_thrash_nonterminal(
    evidence: Any,
    run: CollectedRun,
    scenario: dict[str, Any],
) -> bool:
    return bool(
        isinstance(evidence, dict)
        and evidence.get("schema_version") == 1
        and evidence.get("drive_status") == LIVE_THRASH_STOP
        and evidence.get("terminal_snapshot_available") is False
        and type(evidence.get("frozen_max_seq")) is int
        and evidence.get("preserved_for") == "strictly_confirmed_live_thrash_stop"
        and evidence.get("strict_monitor_confirmed") is True
        and _confirmed_live_thrash_stop(run)
        and _current_thrash_failure_matches_retained(run, scenario)
    )


async def _collect_cleanup_evidence(
    client: ProductClient,
    run: CollectedRun,
    runtime: CoordinatorBindings,
    *,
    baseline_containers: int | None,
    baseline_dangling_volumes: set[str] | None,
    parallel_workers: int,
) -> dict[str, Any]:
    try:
        return await runtime.collect_terminal_cleanup_evidence(
            client,
            run.conversation_id,
            run,
            baseline_containers=baseline_containers,
            relay_log=str(runtime.relay_log_path() or "") or None,
            timeline=getattr(run, "timeline", []),
            baseline_dangling_volumes=baseline_dangling_volumes,
            allow_global_cleanup_fallback=_allow_global_cleanup_fallback(parallel_workers),
        )
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            getattr(run, "timeline", []).append(f"REL-5 cleanup measurement error: {exc}")
        return {}


def _missing_cleanup_record(
    run: CollectedRun,
    scenario: dict[str, Any],
    runtime: CoordinatorBindings,
    evidence: dict[str, Any],
    missing: list[str],
    *,
    out_root: str | Path,
    run_id: str,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str,
    repo_dirty: bool,
    kernel: str,
    started_at: str,
    seed: int | None,
) -> dict[str, Any]:
    run.product_evidence = evidence
    run.timeline.append("terminal cleanup evidence incomplete: missing " + ", ".join(missing))
    provider_ledger = runtime.provider_ledger_for_run(run)
    runtime.assemble_dossier(
        out_root,
        run_id,
        scenario,
        run,
        model=model,
        autonomous=autonomous,
        commit=commit,
        repo_revision=repo_revision,
        repo_dirty=repo_dirty,
        kernel=kernel,
        started_at=started_at,
        seed=seed,
        provider_ledger=provider_ledger,
    )
    return _invalid_run_record(
        out_root,
        run_id,
        scenario,
        "terminal cleanup not adjudicable — missing evidence slice(s): "
        f"{', '.join(missing)} "
        "(set MINIMAX_RELAY_LOG and ensure the container probe is available so the "
        "sidecar/cleanup oracles cannot silently SKIP into a green pass)",
        code=fc.RUN_INTERRUPTED,
        first_broken_link="terminal -> cleanup_evidence_unmeasurable",
        facts={"missing_slices": missing},
        conversation_id=run.conversation_id,
        timeline_markdown=_timeline_md(scenario, run),
    )


def _annotate_trusted_cleanup_gap(
    run: CollectedRun,
    evidence: dict[str, Any],
    nonterminal: dict[str, Any],
    missing: list[str],
    *,
    live_thrash: bool,
) -> None:
    if live_thrash:
        nonterminal["terminal_cleanup_slices_unavailable"] = sorted(missing)
        run.timeline.append(
            "terminal cleanup evidence unavailable after strictly confirmed live-thrash "
            "stop; preserving the decisive retained monitor verdict without granting "
            "cleanup proof; missing " + ", ".join(missing)
        )
    else:
        nonterminal["terminal_cleanup_slices_not_applicable"] = sorted(missing)
        run.timeline.append(
            "terminal cleanup evidence not required for decisive nonterminal product "
            "failure; missing " + ", ".join(missing)
        )
    run.product_evidence = evidence


async def cleanup_admission_record(
    client: ProductClient,
    run: CollectedRun,
    scenario: dict[str, Any],
    runtime: CoordinatorBindings,
    *,
    baseline_containers: int | None,
    baseline_dangling_volumes: set[str] | None,
    parallel_workers: int,
    out_root: str | Path,
    run_id: str,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str,
    repo_dirty: bool,
    kernel: str,
    started_at: str,
    seed: int | None,
) -> dict[str, Any] | None:
    """Collect cleanup facts and reject only untrusted gaps in configured live mode."""
    evidence = await _collect_cleanup_evidence(
        client,
        run,
        runtime,
        baseline_containers=baseline_containers,
        baseline_dangling_volumes=baseline_dangling_volumes,
        parallel_workers=parallel_workers,
    )
    missing = [name for name in ("lifecycle", "sidecar", "cleanup") if name not in evidence]
    if not runtime.relay_log_path() or not missing:
        return None
    nonterminal = evidence.get("nonterminal_adjudication")
    inactive = _trusted_inactive_nonterminal(nonterminal)
    live_thrash = _trusted_live_thrash_nonterminal(nonterminal, run, scenario)
    if not inactive and not live_thrash:
        return _missing_cleanup_record(
            run,
            scenario,
            runtime,
            evidence,
            missing,
            out_root=out_root,
            run_id=run_id,
            model=model,
            autonomous=autonomous,
            commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            kernel=kernel,
            started_at=started_at,
            seed=seed,
        )
    assert isinstance(nonterminal, dict)
    _annotate_trusted_cleanup_gap(
        run,
        evidence,
        nonterminal,
        missing,
        live_thrash=live_thrash,
    )
    return None

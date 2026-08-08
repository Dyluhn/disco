"""Reliability campaign selection, execution, and receipt assembly."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from harness.reliability.matrix import PROOFS, ReliabilityMatrix, Suite, load_matrix
from harness.reliability.state import (
    INVALID,
    PASS,
    promotion_report,
    record_campaign,
    source_revision,
    state_transaction,
)

from .common import _split_values, _utc_now
from .resource_pool import GIB, WeightedSuitePool, read_host_resources
from .suite_execution import _run_suite


def _selection_claims(suites: list[Suite]) -> set[str]:
    return {claim_id for suite in suites for claim_id in suite.claims}


def _print_matrix(matrix: ReliabilityMatrix, suites: list[Suite]) -> None:
    for suite in suites:
        claims = ", ".join(suite.claims)
        print(
            f"{suite.id:32} {suite.proof:12} {suite.kind:12} "
            f"units={suite.units:<4} memory={suite.memory_gib:g}GiB  {claims}"
        )


def _project_python(args: argparse.Namespace, repo: Path) -> Path | None:
    configured_python = (
        args.python
        or os.environ.get("DISCO_RELIABILITY_PYTHON")
        or os.environ.get("PMX_VENV_PY")
        or str(repo / ".venv" / "bin" / "python3")
    )
    project_python = Path(configured_python).expanduser()
    if not project_python.is_absolute():
        project_python = (repo / project_python).resolve()
    if not project_python.is_file() or not os.access(project_python, os.X_OK):
        print(
            "reliability campaign requires the repository Python environment; "
            f"not executable: {project_python}",
            file=sys.stderr,
        )
        return None
    return project_python


def _selected_suites(
    args: argparse.Namespace,
    matrix: ReliabilityMatrix,
) -> list[Suite] | None:
    proofs = _split_values(args.proof)
    surfaces = _split_values(args.surface)
    suite_ids = _split_values(args.suite)
    if proofs:
        unknown = proofs - PROOFS
        if unknown:
            print(f"unknown proof type(s): {sorted(unknown)}", file=sys.stderr)
            return None
    if surfaces:
        known_surfaces = {claim.surface for claim in matrix.claims.values()} | {"all"}
        unknown = surfaces - known_surfaces
        if unknown:
            print(f"unknown surface(s): {sorted(unknown)}", file=sys.stderr)
            return None
        # The public CLI documents ``all`` as the wildcard. Do not pass it to
        # matrix intersection as a literal claim surface: that silently selects
        # only suites carrying an ``all`` claim and can produce a false full gate.
        if "all" in surfaces:
            surfaces = None
    if suite_ids:
        unknown = suite_ids - set(matrix.suites)
        if unknown:
            print(f"unknown suite id(s): {sorted(unknown)}", file=sys.stderr)
            return None
    suites = matrix.select_suites(proofs=proofs, surfaces=surfaces, suite_ids=suite_ids)
    if not suites:
        print("selection matched no reliability suites", file=sys.stderr)
        return None
    return suites


def _campaign_parallelism(
    args: argparse.Namespace,
    suites: list[Suite],
    campaign_out: Path,
) -> int | None:
    initial = read_host_resources(disk_path=campaign_out)
    max_parallel = (
        min(len(suites), initial.cpu_count)
        if args.parallel_suites == "auto"
        else int(args.parallel_suites)
    )
    if max_parallel <= 0:
        print("--parallel-suites must be 'auto' or a positive integer", file=sys.stderr)
        return None
    return max_parallel


def _suite_seed_bases(
    args: argparse.Namespace,
    suites: list[Suite],
) -> dict[str, str]:
    build_soaks = [suite for suite in suites if suite.kind == "build_soak"]
    if not build_soaks:
        return {}
    if args.seed_base is None:
        raise ValueError("--seed-base is required when build-soak suites are selected")
    next_seed = args.seed_base
    assigned: dict[str, str] = {}
    for suite in build_soaks:
        assigned[suite.id] = str(next_seed)
        next_seed += suite.units
    return assigned


def _suite_context(
    context: dict[str, str], suite: Suite, seed_bases: dict[str, str]
) -> dict[str, str]:
    if suite.id not in seed_bases:
        return context
    return {**context, "seed_base": seed_bases[suite.id]}


def _record_campaign_promotion(
    *,
    args: argparse.Namespace,
    matrix: ReliabilityMatrix,
    suites: list[Suite],
    campaign_id: str,
    revision: str,
    commit: str,
    dirty: bool,
    started_at: str,
    finished_at: str,
    results: list[dict],
) -> dict:
    state_path = Path(args.state).expanduser().resolve()
    with state_transaction(state_path) as state:
        record_campaign(
            state,
            campaign_id=campaign_id,
            revision=revision,
            commit=commit,
            dirty=dirty,
            started_at=started_at,
            finished_at=finished_at,
            results=results,
        )
        return promotion_report(
            state, matrix, revision=revision, claim_ids=_selection_claims(suites)
        )


def _write_campaign_summary(
    *,
    args: argparse.Namespace,
    campaign_out: Path,
    campaign_id: str,
    revision: str,
    commit: str,
    dirty: bool,
    source_changed: bool,
    started_at: str,
    finished_at: str,
    max_parallel: int,
    results: list[dict],
    promotion: dict,
) -> None:
    report = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "revision": revision,
        "commit": commit,
        "dirty": dirty,
        "source_changed_during_campaign": source_changed,
        "started_at": started_at,
        "finished_at": finished_at,
        "resource_policy": {
            "memory_reserve_gib": args.memory_reserve_gib,
            "disk_reserve_gib": args.disk_reserve_gib,
            "max_parallel_suites": max_parallel,
        },
        "results": results,
        "promotion": promotion,
    }
    report_path = campaign_out / "campaign-summary.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    counts = Counter(result["status"] for result in results)
    print(f"[reliability] report -> {report_path}")
    print(
        "[reliability] "
        + ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        + f"; selected claims promoted={promotion['eligible']}"
    )


async def _run_selected_campaign(
    args: argparse.Namespace,
    *,
    repo: Path,
    project_python: Path,
    matrix: ReliabilityMatrix,
    suites: list[Suite],
) -> int:
    if args.list or args.dry_run:
        _print_matrix(matrix, suites)
        return 0
    seed_bases = _suite_seed_bases(args, suites)

    revision, commit, dirty = source_revision(repo)
    if dirty and any(suite.proof == "fresh_device" for suite in suites):
        print(
            "fresh-device proof cannot certify an uncommitted source tree; commit the "
            "candidate so every clean machine can clone the exact recorded revision",
            file=sys.stderr,
        )
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    campaign_id = f"campaign_{stamp}_{revision[-12:].replace('.', '_')}"
    out_root = Path(args.out).expanduser().resolve()
    campaign_out = out_root / campaign_id
    campaign_out.mkdir(parents=True, exist_ok=False)
    started_at = _utc_now()
    max_parallel = _campaign_parallelism(args, suites, campaign_out)
    if max_parallel is None:
        return 2
    pool = WeightedSuitePool(
        disk_path=campaign_out,
        memory_reserve_bytes=int(args.memory_reserve_gib * GIB),
        disk_reserve_bytes=int(args.disk_reserve_gib * GIB),
        max_parallel=max_parallel,
        poll_s=args.resource_poll,
        wait_timeout_s=args.resource_wait_timeout,
    )
    print(
        f"[reliability] campaign {campaign_id}: {len(suites)} suite(s), up to "
        f"{max_parallel} concurrently; protecting {args.memory_reserve_gib:g} GiB RAM"
    )
    context = {
        "repo": str(repo),
        "frontend": str(repo / "frontend"),
        "python": str(project_python),
        "commit": commit,
        "revision": revision,
    }
    results = await asyncio.gather(
        *(
            _run_suite(
                suite,
                context=_suite_context(context, suite, seed_bases),
                campaign_out=campaign_out,
                pool=pool,
            )
            for suite in suites
        )
    )
    finished_at = _utc_now()

    final_revision, _, _ = source_revision(repo)
    if final_revision != revision:
        for result in results:
            if result["status"] == PASS:
                result["status"] = INVALID
                result["units_passed"] = 0
                result["reason"] = "source tree changed during the campaign"

    promotion = _record_campaign_promotion(
        args=args,
        matrix=matrix,
        suites=suites,
        campaign_id=campaign_id,
        revision=revision,
        commit=commit,
        dirty=dirty,
        started_at=started_at,
        finished_at=finished_at,
        results=results,
    )
    _write_campaign_summary(
        args=args,
        campaign_out=campaign_out,
        campaign_id=campaign_id,
        revision=revision,
        commit=commit,
        dirty=dirty,
        source_changed=final_revision != revision,
        started_at=started_at,
        finished_at=finished_at,
        max_parallel=max_parallel,
        results=results,
        promotion=promotion,
    )
    if any(result["status"] != PASS for result in results):
        return 1
    if args.require_promotion and not promotion["eligible"]:
        return 4
    return 0


async def _amain(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[3]
    project_python = _project_python(args, repo)
    if project_python is None:
        return 2
    matrix = load_matrix(args.matrix)
    suites = _selected_suites(args, matrix)
    if suites is None:
        return 2
    return await _run_selected_campaign(
        args,
        repo=repo,
        project_python=project_python,
        matrix=matrix,
        suites=suites,
    )

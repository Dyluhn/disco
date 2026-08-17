"""Bounded Build Soak coordinator owner."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from ..adapters.disco_api import InfraProbeError
from ..evidence_sink import FilesystemEvidenceSink
from ..ports import ProductClient
from .bindings import CoordinatorBindings
from .classification import (
    classify_dossier,
)
from .cleanup import (
    _collect_terminal_cleanup_evidence,
    _dangling_volume_names,
    _live_disco_container_count,
    _release_conversation,
)
from .common import (
    _DEFAULT_HARD_CAP_S,
)
from .coordinator_cleanup import (
    cleanup_admission_record,
)
from .coordinator_drive import drive_or_record
from .coordinator_inspect import required_inspect_record
from .drive import (
    drive_scenario,
)
from .freeze import (
    _freeze_invalidation_evidence,
)
from .ledger import (
    _provider_ledger_for_run,
    _relay_log_path,
)
from .records import (
    _infra_failure_record,
)


def _coordinator_bindings(bindings: CoordinatorBindings | None) -> CoordinatorBindings:
    if bindings is not None:
        return bindings
    return CoordinatorBindings(
        drive_scenario=drive_scenario,
        collect_terminal_cleanup_evidence=_collect_terminal_cleanup_evidence,
        provider_ledger_for_run=_provider_ledger_for_run,
        relay_log_path=_relay_log_path,
        assemble_dossier=FilesystemEvidenceSink().assemble_dossier,
        live_disco_container_count=_live_disco_container_count,
        dangling_volume_names=_dangling_volume_names,
    )


def _classify_collected_run(
    run: Any,
    scenario: dict[str, Any],
    runtime: CoordinatorBindings,
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
    provider_ledger = runtime.provider_ledger_for_run(run)
    base = runtime.assemble_dossier(
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
    return classify_dossier(
        base,
        scenario,
        run,
        autonomous=autonomous,
        commit=commit,
        seed=seed,
        provider_ledger=provider_ledger,
    )


async def _admission_record(
    client: ProductClient,
    run: Any,
    scenario: dict[str, Any],
    runtime: CoordinatorBindings,
    *,
    require_inspect_trace: bool,
    baseline_containers: int | None,
    baseline_volumes: set[str] | None,
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
    rejected = required_inspect_record(
        run,
        scenario,
        runtime,
        required=require_inspect_trace,
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
    if rejected is not None:
        return rejected
    return await cleanup_admission_record(
        client,
        run,
        scenario,
        runtime,
        baseline_containers=baseline_containers,
        baseline_dangling_volumes=baseline_volumes,
        parallel_workers=parallel_workers,
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


async def run_once(
    client: ProductClient,
    scenario: dict[str, Any],
    *,
    run_id: str,
    out_root: str | Path,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str = "",
    repo_dirty: bool = False,
    kernel: str = "disco",
    timeout_s: float,
    hard_cap_s: float = _DEFAULT_HARD_CAP_S,
    require_inspect_trace: bool = False,
    parallel_workers: int = 1,
    seed: int | None = None,
    bindings: CoordinatorBindings | None = None,
) -> dict[str, Any]:
    """Drive, admit, freeze, and classify one Build Soak run."""
    runtime = _coordinator_bindings(bindings)
    started_at = datetime.now(UTC).isoformat()
    try:
        await client.pre_create_probe(model)
    except InfraProbeError as exc:
        return _infra_failure_record(out_root, run_id, scenario, exc)
    client.last_conversation_id = None
    client.scenario_evidence = {}
    baseline_containers = runtime.live_disco_container_count()
    baseline_volumes = runtime.dangling_volume_names()
    freeze_invalidation = partial(
        _freeze_invalidation_evidence,
        assemble_dossier=runtime.assemble_dossier,
    )
    try:
        outcome = await drive_or_record(
            client,
            scenario,
            out_root=out_root,
            run_id=run_id,
            model=model,
            autonomous=autonomous,
            commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            kernel=kernel,
            timeout_s=timeout_s,
            hard_cap_s=hard_cap_s,
            started_at=started_at,
            seed=seed,
            parallel_workers=parallel_workers,
            baseline_containers=baseline_containers,
            baseline_dangling_volumes=baseline_volumes,
            runtime=runtime,
            freeze_invalidation=freeze_invalidation,
        )
        if isinstance(outcome, dict):
            return outcome
        run = outcome
        rejected = await _admission_record(
            client,
            run,
            scenario,
            runtime,
            require_inspect_trace=require_inspect_trace,
            baseline_containers=baseline_containers,
            baseline_volumes=baseline_volumes,
            parallel_workers=parallel_workers,
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
        if rejected is not None:
            return rejected
        return _classify_collected_run(
            run,
            scenario,
            runtime,
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
    finally:
        await _release_conversation(client, client.last_conversation_id)

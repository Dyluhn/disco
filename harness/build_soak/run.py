"""run.py — the headless live-API Build runner (PR S3, guidelines §25).

    python -m harness.build_soak.run --scenario static_html_minimal --iterations 1 \
        --seed-base <COMMISSIONED_SEED>

Loads a scenario from scenarios.yaml, drives it against the LIVE agent-server
(http://127.0.0.1:8000) via the disco_api adapter ACTING AS THE USER (approve the
plan, send follow-ups at their trigger points), assembles the §5 evidence dossier,
freezes it under the §6 evidence lock (manifest + SHA256 hashes), runs the
deterministic classifier with events + workspace + preview + autonomy (codex #2),
writes classification.json, and exits nonzero on FAIL / INVALID_RUN / INFRA_FAILURE.

The orchestration (`drive_scenario`) and dossier assembly (`assemble_dossier`) are
importable + transport-agnostic so the deterministic tests drive them with a FAKE
transport (no live model spend); `main()` wires the live HttpTransport.
"""

from __future__ import annotations

import argparse
import asyncio as asyncio
import subprocess as subprocess
from pathlib import Path
from typing import Any

from ._runner import classification as _classification_impl
from ._runner import cleanup as _cleanup_impl
from ._runner import cli as _cli_impl
from ._runner.batch import (
    _exit_code as _exit_code,
)
from ._runner.batch import (
    _policy_dict as _policy_dict,
)
from ._runner.batch import (
    _policy_from_args as _policy_from_args,
)
from ._runner.batch import (
    _require_exact_provider as _require_exact_provider,
)
from ._runner.batch import (
    _run_batch_item as _run_batch_item,
)
from ._runner.batch import (
    _run_stop_on_non_pass_cohorts as _run_stop_on_non_pass_cohorts,
)
from ._runner.cleanup import (
    _dangling_volume_names as _dangling_volume_names,
)
from ._runner.cleanup import (
    _disco_volume_names as _disco_volume_names,
)
from ._runner.cleanup import (
    _extract_sandbox_instance_ids as _extract_sandbox_instance_ids,
)
from ._runner.cleanup import (
    _live_disco_container_count as _live_disco_container_count,
)
from ._runner.cleanup import (
    _live_disco_container_names as _live_disco_container_names,
)
from ._runner.cleanup import (
    _new_dangling_volume_count as _new_dangling_volume_count,
)
from ._runner.cleanup import (
    _podman_volume_names as _podman_volume_names,
)
from ._runner.cleanup import (
    _release_conversation as _release_conversation,
)
from ._runner.cleanup import (
    _scoped_disco_container_count as _scoped_disco_container_count,
)
from ._runner.cleanup import (
    _scoped_disco_volume_count as _scoped_disco_volume_count,
)
from ._runner.cli import (
    _efficiency_baseline_comparison as _efficiency_baseline_comparison,
)
from ._runner.common import (
    _BATCH_SUMMARY_NAME as _BATCH_SUMMARY_NAME,
)
from ._runner.common import (
    _BROWSER_EVIDENCE_COLLECTION_ERROR_NAME as _BROWSER_EVIDENCE_COLLECTION_ERROR_NAME,
)
from ._runner.common import (
    _DEFAULT_BASE_URL as _DEFAULT_BASE_URL,
)
from ._runner.common import (
    _DEFAULT_HARD_CAP_S as _DEFAULT_HARD_CAP_S,
)
from ._runner.common import (
    _DEFAULT_INACTIVITY_S as _DEFAULT_INACTIVITY_S,
)
from ._runner.common import (
    _DEFAULT_OUT as _DEFAULT_OUT,
)
from ._runner.common import (
    _GENERIC_CLARIFY_ANSWER as _GENERIC_CLARIFY_ANSWER,
)
from ._runner.common import (
    _MAX_CLARIFY as _MAX_CLARIFY,
)
from ._runner.common import (
    _MAX_DECISION as _MAX_DECISION,
)
from ._runner.common import (
    _MAX_GATES as _MAX_GATES,
)
from ._runner.common import (
    _MAX_RESUMES as _MAX_RESUMES,
)
from ._runner.common import (
    _SCENARIOS as _SCENARIOS,
)
from ._runner.common import (
    _TRIGGER_AFTER_FIRST_FILE_WRITE as _TRIGGER_AFTER_FIRST_FILE_WRITE,
)
from ._runner.common import (
    _TRIGGER_AFTER_TERMINAL as _TRIGGER_AFTER_TERMINAL,
)
from ._runner.freeze import (
    _freeze_invalidation_evidence as _freeze_invalidation_evidence,
)
from ._runner.ledger import (
    _provider_ledger_for_run as _provider_ledger_for_run_owner,
)
from ._runner.ledger import (
    _relay_log_path as _relay_log_path,
)
from ._runner.preflight import (
    _is_terminal_driver_preflight_trace as _is_terminal_driver_preflight_trace,
)
from ._runner.preflight import (
    _is_terminal_sandbox_preflight_trace as _is_terminal_sandbox_preflight_trace,
)
from ._runner.preflight import (
    _latest_durable_status as _latest_durable_status,
)
from ._runner.preflight import (
    _named_sandbox_preflight_detail as _named_sandbox_preflight_detail,
)
from ._runner.records import (
    _finish_unsealable_fail_record as _finish_unsealable_fail_record,
)
from ._runner.records import (
    _infra_failure_record as _infra_failure_record,
)
from ._runner.records import (
    _invalid_run_record as _invalid_run_record,
)
from ._runner.records import (
    _invalidation_conversation_id as _invalidation_conversation_id,
)
from ._runner.scenario_io import (
    _browser_verification_required as _browser_verification_required,
)
from ._runner.scenario_io import (
    _declared_workspace_paths as _declared_workspace_paths,
)
from ._runner.scenario_io import (
    _driver_catalog_contains as _driver_catalog_contains,
)
from ._runner.scenario_io import (
    _is_cancel_after_first_write as _is_cancel_after_first_write,
)
from ._runner.scenario_io import (
    _materialize_task_seed as _materialize_task_seed,
)
from ._runner.scenario_io import (
    _preview_required as _preview_required,
)
from ._runner.scenario_io import (
    batch_summary_name as batch_summary_name,
)
from ._runner.scenario_io import (
    load_scenarios as load_scenarios,
)
from ._runner.temporal import (
    _event_epoch as _event_epoch,
)
from ._runner.temporal import (
    _min_event_epoch as _min_event_epoch,
)
from ._runner.temporal import (
    _status_value_and_detail as _status_value_and_detail,
)
from ._runner.temporal import (
    _terminal_status_epoch as _terminal_status_epoch,
)
from ._runner.thrash import (
    _confirmed_live_thrash_stop as _confirmed_live_thrash_stop,
)
from ._runner.thrash import (
    _current_thrash_failure_matches_retained as _current_thrash_failure_matches_retained,
)
from ._runner.thrash import (
    _first_killed_idle_epoch as _first_killed_idle_epoch,
)
from ._runner.thrash import (
    _strict_live_thrash_stop_boundary as _strict_live_thrash_stop_boundary,
)
from ._runner.triggers import (
    CancelMissedWindowError as CancelMissedWindowError,
)
from ._runner.triggers import (
    _cancel_at_trigger as _cancel_at_trigger,
)
from ._runner.triggers import (
    _drive_to_terminal as _drive_to_terminal,
)
from ._runner.triggers import (
    _export_matches_workspace as _export_matches_workspace,
)
from ._runner.triggers import (
    _governed_artifact_paths as _governed_artifact_paths,
)
from ._runner.triggers import (
    _inject_when_writing as _inject_when_writing,
)
from ._runner.triggers import (
    _pause_resume_at_trigger as _pause_resume_at_trigger,
)
from ._runner.triggers import (
    _PauseOwnership as _PauseOwnership,
)
from ._runner.triggers import (
    _restart_isolated_stack as _restart_isolated_stack,
)
from ._runner.triggers import (
    _wait_for_cancel_idle as _wait_for_cancel_idle,
)
from ._runner.triggers import (
    _wait_for_status as _wait_for_status,
)
from .adapters.disco_api import (
    CollectedRun,
    DiscoApiClient,
)

# The ONE filename the promotion reader discovers. `_build_soak_result` in
# harness/reliability/run.py does `sorted(out.rglob("batch-summary.json"), key=mtime)`
# and reads the NEWEST match, so any batch written under this name -- anywhere
# beneath a searched root -- can be counted as governed promotion evidence.
# Non-promoting lanes therefore write a DIFFERENT name, which makes their
# exclusion structural: the file the reader looks for is never created.


# The runner ACTS AS THE USER (§14 runner directive): when a build asks a clarifying
# question mid-build, the runner answers it so the build proceeds — exactly what a real
# user does. The generic answer MUST instruct "do not ask further questions" so a model
# can't trap the build in a question-loop; a scenario MAY override it with its own
# `clarification_answer`. This does NOT weaken the oracle: the resulting build is still
# adjudicated (plan→approve→execute→deliver) — a build that STILL fails after a
# reasonable clarification is a genuine finding (Bug 17, §17).

# Progress-aware terminal-wait knobs (Bug 15). The terminal wait is NOT a blind
# wall-clock: `inactivity_s` is the NO-PROGRESS window (a build that keeps emitting
# events is never cut off — only genuine silence for this long ends the wait), and
# `hard_cap_s` is the generous safety ceiling that bounds a truly-hung run, set well
# above a normal build (~5min) so a slow-but-progressing build finishes on its real
# terminal rather than being frozen mid-flight + mislabeled BUILD_DID_NOT_FINISH.


# ---- scenario loading -------------------------------------------------------


# ---- orchestration (acts as the user) ---------------------------------------


def _provider_ledger_for_run(run: CollectedRun) -> list[dict[str, Any]] | None:
    """Resolve the legacy relay-path seam at the compatibility boundary."""
    return _provider_ledger_for_run_owner(run, relay_log_path=_relay_log_path)


async def drive_scenario(
    client: DiscoApiClient,
    scenario: dict[str, Any],
    *,
    model: str | None,
    autonomous: bool,
    timeout_s: float = _DEFAULT_INACTIVITY_S,
    hard_cap_s: float = _DEFAULT_HARD_CAP_S,
    seed: int | None = None,
) -> CollectedRun:
    """Thin wrapper delegating to DefaultScenarioDriver (frozen owner)."""
    from ._runner.bindings import ScenarioBindings
    from .scenario_driver import DefaultScenarioDriver

    bindings = ScenarioBindings(
        drive_to_terminal=_drive_to_terminal,
        relay_log_path=_relay_log_path,
    )
    return await DefaultScenarioDriver(bindings).drive(
        client,
        scenario,
        model=model,
        autonomous=autonomous,
        timeout_s=timeout_s,
        hard_cap_s=hard_cap_s,
        seed=seed,
    )


# ---- dossier assembly + evidence lock (§5, §6) ------------------------------


def assemble_dossier(
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    run: CollectedRun,
    *,
    model: str | None,
    autonomous: bool,
    commit: str = "",
    repo_revision: str = "",
    repo_dirty: bool = False,
    seed: int | None = None,
    mode: str = "api",
    kernel: str = "disco",
    started_at: str | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
) -> Path:
    """Thin wrapper delegating to FilesystemEvidenceSink (frozen owner)."""
    from .evidence_sink import FilesystemEvidenceSink

    return FilesystemEvidenceSink().assemble_dossier(
        out_root,
        run_id,
        scenario,
        run,
        model=model,
        autonomous=autonomous,
        commit=commit,
        repo_revision=repo_revision,
        repo_dirty=repo_dirty,
        seed=seed,
        mode=mode,
        kernel=kernel,
        started_at=started_at,
        provider_ledger=provider_ledger,
    )


async def _collect_terminal_cleanup_evidence(
    client: DiscoApiClient,
    cid: str,
    run: Any,
    *,
    baseline_containers: int | None,
    relay_log: str | None,
    timeline: list[str],
    baseline_dangling_volumes: set[str] | None = None,
    grace_s: float = 8.0,
    allow_global_cleanup_fallback: bool = True,
) -> dict[str, Any]:
    """Compatibility delegate with facade-owned host probe bindings."""
    from ._runner.bindings import CleanupBindings

    probes = CleanupBindings(
        live_disco_container_names=_live_disco_container_names,
        disco_volume_names=_disco_volume_names,
        dangling_volume_names=_dangling_volume_names,
    )
    return await _cleanup_impl._collect_terminal_cleanup_evidence(
        client,
        cid,
        run,
        baseline_containers=baseline_containers,
        relay_log=relay_log,
        timeline=timeline,
        baseline_dangling_volumes=baseline_dangling_volumes,
        grace_s=grace_s,
        allow_global_cleanup_fallback=allow_global_cleanup_fallback,
        bindings=probes,
    )


# ---- runner hygiene: kill an abandoned conversation -------------------------


# [Lane A A-M2] The BUILD-terminal set for anchoring provider-after-terminal. EXCLUDES IDLE: IDLE is
# both the pre-kick resting state AND the post-kill state, so anchoring on a (later) IDLE would push
# the boundary PAST real post-FINISHED calls and hide them. Includes VERIFIED (a clean terminal the
# LifecycleOracle accepts but TERMINAL_STATES omits) so a VERIFIED run is adjudicable, not INVALID.


# ---- one full run -----------------------------------------------------------


async def run_once(
    client: DiscoApiClient,
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
) -> dict[str, Any]:
    """Thin wrapper delegating to RunCoordinator (frozen owner)."""
    from ._runner.bindings import CoordinatorBindings
    from .run_coordinator import RunCoordinator

    bindings = CoordinatorBindings(
        drive_scenario=drive_scenario,
        collect_terminal_cleanup_evidence=_collect_terminal_cleanup_evidence,
        provider_ledger_for_run=_provider_ledger_for_run,
        relay_log_path=_relay_log_path,
        assemble_dossier=assemble_dossier,
        live_disco_container_count=_live_disco_container_count,
        dangling_volume_names=_dangling_volume_names,
    )
    return await RunCoordinator(bindings).run_once(
        client,
        scenario,
        run_id=run_id,
        out_root=out_root,
        model=model,
        autonomous=autonomous,
        commit=commit,
        repo_revision=repo_revision,
        repo_dirty=repo_dirty,
        kernel=kernel,
        timeout_s=timeout_s,
        hard_cap_s=hard_cap_s,
        require_inspect_trace=require_inspect_trace,
        parallel_workers=parallel_workers,
        seed=seed,
    )


# ---- CLI --------------------------------------------------------------------


def _events_jsonl(events: list[dict[str, Any]]) -> str:
    """Compatibility delegate to the filesystem evidence owner."""
    from .evidence_sink import _events_jsonl as render

    return render(events)


def _timeline_md(scenario: dict[str, Any], run: CollectedRun) -> str:
    """Compatibility delegate to the filesystem evidence owner."""
    from .evidence_sink import _timeline_md as render

    return render(scenario, run)


def classify_dossier(
    base: Path,
    scenario: dict[str, Any],
    run: CollectedRun,
    *,
    autonomous: bool,
    commit: str = "",
    seed: int | None = None,
    provider_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compatibility delegate to deterministic dossier classification."""
    return _classification_impl.classify_dossier(
        base,
        scenario,
        run,
        autonomous=autonomous,
        commit=commit,
        seed=seed,
        provider_ledger=provider_ledger,
    )


async def _amain(args: argparse.Namespace) -> int:
    """Compatibility delegate to bounded CLI orchestration."""
    return await _cli_impl.amain(args, scenario_loader=load_scenarios)


def main(argv: list[str] | None = None) -> int:
    """Compatibility CLI entrypoint."""
    return _cli_impl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

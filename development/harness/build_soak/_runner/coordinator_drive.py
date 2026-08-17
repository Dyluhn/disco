"""Drive-boundary invalidation handling for one Build Soak run."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from .. import failure_codes as fc
from ..adapters.disco_api import (
    BrowserEvidenceCollectionError,
    CollectedRun,
    FinishUnsealableContentError,
    FollowupPickupError,
    InconclusiveRunError,
    SnapshotNotReadyError,
)
from ..evidence_sink import _timeline_md
from ..ports import ProductClient
from .bindings import CoordinatorBindings
from .coordinator_cleanup import _allow_global_cleanup_fallback
from .records import (
    _finish_unsealable_fail_record,
    _invalid_run_record,
    _invalidation_conversation_id,
)
from .triggers import CancelMissedWindowError


async def _frozen_exception_record(
    client: ProductClient,
    scenario: dict[str, Any],
    exc: Any,
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
    freeze_invalidation: Any,
    code: str,
    first_broken_link: str,
    product_failure: bool = False,
) -> dict[str, Any]:
    cid = _invalidation_conversation_id(exc.facts, client)
    freeze_facts, freeze_timeline = await freeze_invalidation(
        client,
        out_root,
        run_id,
        scenario,
        model=model,
        autonomous=autonomous,
        commit=commit,
        repo_revision=repo_revision,
        repo_dirty=repo_dirty,
        kernel=kernel,
        started_at=started_at,
        seed=seed,
        conversation_id=cid,
        invalidation_code=code,
        invalidation_reason=exc.reason,
    )
    facts = {**exc.facts, **freeze_facts}
    if product_failure:
        return _finish_unsealable_fail_record(
            out_root,
            run_id,
            scenario,
            exc.reason,
            facts=facts,
            conversation_id=cid,
            timeline_markdown=freeze_timeline,
        )
    return _invalid_run_record(
        out_root,
        run_id,
        scenario,
        exc.reason,
        code=code,
        first_broken_link=first_broken_link,
        facts=facts,
        conversation_id=cid,
        timeline_markdown=freeze_timeline,
    )


async def _hardcap_record(
    client: ProductClient,
    scenario: dict[str, Any],
    exc: InconclusiveRunError,
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
    parallel_workers: int,
    baseline_containers: int | None,
    baseline_dangling_volumes: set[str] | None,
    runtime: CoordinatorBindings,
    freeze_invalidation: Any,
) -> dict[str, Any]:
    frozen = exc.collected_run
    hardcap_facts = exc.facts
    hardcap_cid = frozen.conversation_id if frozen is not None else None
    hardcap_timeline = _timeline_md(scenario, frozen) if frozen is not None else None
    if frozen is not None:
        try:
            frozen.product_evidence = await runtime.collect_terminal_cleanup_evidence(
                client,
                frozen.conversation_id,
                frozen,
                baseline_containers=baseline_containers,
                relay_log=str(runtime.relay_log_path() or "") or None,
                timeline=frozen.timeline,
                baseline_dangling_volumes=baseline_dangling_volumes,
                allow_global_cleanup_fallback=_allow_global_cleanup_fallback(parallel_workers),
            )
        except Exception as cleanup_exc:  # noqa: BLE001 — retain partial dossier
            frozen.timeline.append(
                f"progress hard-cap cleanup evidence failed: {type(cleanup_exc).__name__}"
            )
        provider_ledger = runtime.provider_ledger_for_run(frozen)
        runtime.assemble_dossier(
            out_root,
            run_id,
            scenario,
            frozen,
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
    else:
        hardcap_cid = _invalidation_conversation_id(exc.facts, client)
        freeze_facts, hardcap_timeline = await freeze_invalidation(
            client,
            out_root,
            run_id,
            scenario,
            model=model,
            autonomous=autonomous,
            commit=commit,
            repo_revision=repo_revision,
            repo_dirty=repo_dirty,
            kernel=kernel,
            started_at=started_at,
            seed=seed,
            conversation_id=hardcap_cid,
            invalidation_code=fc.RUN_TIMEOUT_WHILE_PROGRESSING,
            invalidation_reason=exc.reason,
        )
        hardcap_facts = {**exc.facts, **freeze_facts}
    return _invalid_run_record(
        out_root,
        run_id,
        scenario,
        exc.reason,
        code=fc.RUN_TIMEOUT_WHILE_PROGRESSING,
        first_broken_link="terminal_wait -> no_terminal_before_hard_cap",
        facts=hardcap_facts,
        conversation_id=hardcap_cid,
        timeline_markdown=hardcap_timeline,
    )


async def _unexpected_drive_record(
    client: ProductClient,
    scenario: dict[str, Any],
    exc: Exception,
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
    freeze_invalidation: Any,
) -> dict[str, Any]:
    cid = _invalidation_conversation_id(None, client)
    reason = f"{type(exc).__name__}: {exc}"
    freeze_facts, freeze_timeline = await freeze_invalidation(
        client,
        out_root,
        run_id,
        scenario,
        model=model,
        autonomous=autonomous,
        commit=commit,
        repo_revision=repo_revision,
        repo_dirty=repo_dirty,
        kernel=kernel,
        started_at=started_at,
        seed=seed,
        conversation_id=cid,
        invalidation_code=fc.RUN_INTERRUPTED,
        invalidation_reason=reason,
    )
    return _invalid_run_record(
        out_root,
        run_id,
        scenario,
        reason,
        facts=freeze_facts,
        conversation_id=cid,
        timeline_markdown=freeze_timeline,
    )


async def _known_failure_record(freeze_record: Any, exc: Any) -> dict[str, Any]:
    """Select the fixed invalidation contract for a known drive-boundary failure."""
    if isinstance(exc, FollowupPickupError):
        return await freeze_record(
            exc,
            code=fc.RUN_INTERRUPTED,
            first_broken_link="followup_send -> no_pickup_before_bound",
        )
    if isinstance(exc, CancelMissedWindowError):
        return await freeze_record(
            exc,
            code=fc.CANCEL_MISSED_WINDOW,
            first_broken_link="cancel_at -> terminal_before_kill",
        )
    if isinstance(exc, FinishUnsealableContentError):
        return await freeze_record(
            exc,
            code=fc.FINISH_UNSEALABLE_CONTENT,
            first_broken_link="finish -> final_workspace_seal",
            product_failure=True,
        )
    if isinstance(exc, SnapshotNotReadyError):
        return await freeze_record(
            exc,
            code=fc.WORKSPACE_SNAPSHOT_NOT_READY,
            first_broken_link="snapshot_flush -> snapshot_behind_agent_final_state",
        )
    return await freeze_record(
        exc,
        code=fc.MISSING_REQUIRED_EVIDENCE,
        first_broken_link="browser_observation -> durable_screenshot_evidence",
    )


async def drive_or_record(
    client: ProductClient,
    scenario: dict[str, Any],
    *,
    out_root: str | Path,
    run_id: str,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str,
    repo_dirty: bool,
    kernel: str,
    timeout_s: float,
    hard_cap_s: float,
    started_at: str,
    seed: int | None,
    parallel_workers: int,
    baseline_containers: int | None,
    baseline_dangling_volumes: set[str] | None,
    runtime: CoordinatorBindings,
    freeze_invalidation: Any,
) -> CollectedRun | dict[str, Any]:
    """Drive once, retaining a complete record for every invalidation boundary."""
    freeze_record = partial(
        _frozen_exception_record,
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
        started_at=started_at,
        seed=seed,
        freeze_invalidation=freeze_invalidation,
    )
    try:
        return await runtime.drive_scenario(
            client,
            scenario,
            model=model,
            autonomous=autonomous,
            timeout_s=timeout_s,
            hard_cap_s=hard_cap_s,
            seed=seed,
        )
    except InconclusiveRunError as exc:
        return await _hardcap_record(
            client,
            scenario,
            exc,
            parallel_workers=parallel_workers,
            baseline_containers=baseline_containers,
            baseline_dangling_volumes=baseline_dangling_volumes,
            runtime=runtime,
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
            freeze_invalidation=freeze_invalidation,
        )
    except (
        FollowupPickupError,
        CancelMissedWindowError,
        FinishUnsealableContentError,
        SnapshotNotReadyError,
        BrowserEvidenceCollectionError,
    ) as exc:
        return await _known_failure_record(freeze_record, exc)
    except Exception as exc:  # noqa: BLE001 — surface the real reason as INVALID_RUN
        return await _unexpected_drive_record(
            client,
            scenario,
            exc,
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
            freeze_invalidation=freeze_invalidation,
        )

"""Bounded Build Soak freeze owner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.disco_api import CollectedRun
from ..evidence_sink import FilesystemEvidenceSink, _timeline_md
from ..ports import ProductClient
from .bindings import EvidenceAssembler
from .ledger import (
    _provider_ledger_for_run,
)


def _note_freeze_slice(
    freeze: dict[str, Any],
    slice_name: str,
    present: bool,
    error: BaseException | None = None,
) -> None:
    (freeze["present"] if present else freeze["missing"]).append(slice_name)
    if error is not None:
        freeze["errors"][slice_name] = type(error).__name__


async def _capture_invalidation_run(
    client: ProductClient,
    conversation_id: str,
    *,
    invalidation_code: str,
    invalidation_reason: str,
    freeze: dict[str, Any],
) -> CollectedRun:
    events: list[dict[str, Any]] = []
    try:
        events = client.collect_events(conversation_id)
        _note_freeze_slice(freeze, "events", True)
    except Exception as exc:  # noqa: BLE001 — disclosed, never fabricated
        _note_freeze_slice(freeze, "events", False, exc)
    capture_marker = {
        "_capture": {
            "status": "not_collected_invalidation",
            "invalidation_code": invalidation_code,
            "reason": invalidation_reason,
        }
    }
    state_final: dict[str, Any] = dict(capture_marker)
    try:
        state_final = await client.get_state(conversation_id)
        _note_freeze_slice(freeze, "state_final", True)
    except Exception as exc:  # noqa: BLE001
        _note_freeze_slice(freeze, "state_final", False, exc)
    inspect_trace: dict[str, Any] | None = None
    try:
        await client.finish_inspect_collection(conversation_id)
        inspect_trace = await client.collect_inspect_trace(conversation_id)
        _note_freeze_slice(freeze, "inspect_trace", inspect_trace is not None)
    except Exception as exc:  # noqa: BLE001
        _note_freeze_slice(freeze, "inspect_trace", False, exc)
    try:
        thrash_monitor = client.live_thrash_monitor
    except Exception:  # noqa: BLE001
        thrash_monitor = {}
    return CollectedRun(
        conversation_id=conversation_id,
        events=events,
        state_initial=dict(capture_marker),
        state_final=state_final,
        workspace_manifest={"files": {}, **capture_marker},
        preview=None,
        inspect_trace=inspect_trace,
        thrash_monitor=thrash_monitor,
        timeline=[
            f"invalidation evidence freeze for {invalidation_code}: {invalidation_reason}",
            f"slices present={freeze['present']} missing={freeze['missing']}",
        ],
    )


def _capture_provider_ledger(
    frozen: CollectedRun, freeze: dict[str, Any]
) -> list[dict[str, Any]] | None:
    try:
        provider_ledger = _provider_ledger_for_run(frozen)
        _note_freeze_slice(freeze, "provider_ledger", provider_ledger is not None)
        return provider_ledger
    except Exception as exc:  # noqa: BLE001
        _note_freeze_slice(freeze, "provider_ledger", False, exc)
        return None


async def _freeze_invalidation_evidence(
    client: ProductClient,
    out_root: str | Path,
    run_id: str,
    scenario: dict[str, Any],
    *,
    model: str | None,
    autonomous: bool,
    commit: str,
    repo_revision: str,
    repo_dirty: bool,
    kernel: str,
    started_at: str | None,
    seed: int | None,
    conversation_id: str | None,
    invalidation_code: str,
    invalidation_reason: str,
    assemble_dossier: EvidenceAssembler | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Best-effort hash-locked dossier freeze for an INVALID_RUN path."""

    freeze: dict[str, Any] = {
        "attempted": True,
        "present": [],
        "missing": [],
        "errors": {},
    }
    if not conversation_id:
        freeze["attempted"] = False
        freeze["missing"] = ["events", "state_final", "inspect_trace", "provider_ledger"]
        freeze["errors"]["conversation_id"] = "unknown before failure"
        return {"evidence_freeze": freeze}, None

    frozen = await _capture_invalidation_run(
        client,
        conversation_id,
        invalidation_code=invalidation_code,
        invalidation_reason=invalidation_reason,
        freeze=freeze,
    )
    provider_ledger = _capture_provider_ledger(frozen, freeze)

    try:
        assembler = assemble_dossier or FilesystemEvidenceSink().assemble_dossier
        assembler(
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
        freeze["dossier_written"] = True
    except Exception as exc:  # noqa: BLE001 — the freeze may not mask the invalidation
        freeze["dossier_written"] = False
        freeze["errors"]["dossier"] = type(exc).__name__
        return {"evidence_freeze": freeze}, None
    return {"evidence_freeze": freeze}, _timeline_md(scenario, frozen)

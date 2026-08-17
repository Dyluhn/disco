"""Hard-cap evidence freeze for a still-progressing Build Soak run."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..adapters.disco_api import CollectedRun, DiscoApiClient, InconclusiveRunError
from ..ports import ProductClient
from .cleanup import _extract_sandbox_instance_ids
from .drive_start import DriveAudit
from .scenario_io import (
    _browser_verification_required,
    _declared_workspace_paths,
    _preview_required,
)
from .temporal import _event_epoch
from .thrash import _status_value_and_detail


@dataclass(frozen=True)
class _EventWatermark:
    events: list[dict[str, Any]]
    trusted: bool
    max_seq: int


@dataclass(frozen=True)
class _DiagnosticStop:
    response: dict[str, Any]
    response_epoch: float | None
    acknowledged: bool
    boundary_seq: int | None
    boundary_epoch: float | None
    release_confirmed: bool


def _pre_stop_watermark(client: ProductClient, conversation_id: str) -> _EventWatermark:
    try:
        events = client.collect_events(conversation_id)
        trusted = True
    except Exception:  # noqa: BLE001 — later collection discloses the retained gap
        events = []
        trusted = False
    max_seq = max((int(event.get("seq", -1)) for event in events), default=-1)
    return _EventWatermark(events, trusted, max_seq)


def _note_workspace_freeze(
    audit: DriveAudit,
    frozen_workspace: dict[str, Any],
) -> None:
    status = frozen_workspace.get("status")
    detail = (
        f" (version_seq={frozen_workspace.get('version_seq')}, "
        f"horizon_seq={frozen_workspace.get('horizon_seq')})"
        if status == "frozen"
        else f" — {frozen_workspace.get('reason')}"
    )
    audit.timeline.append(f"progress hard-cap freeze before kill: {status}{detail}")


async def _kill_progressing_conversation(
    client: ProductClient,
    conversation_id: str,
    audit: DriveAudit,
) -> tuple[dict[str, Any], float | None, bool]:
    response: dict[str, Any] = {}
    response_epoch: float | None = None
    try:
        response = await client.kill(conversation_id)
        response_epoch = time.time()
        http_status = int(response.get("http_status", 0))
        acknowledged = (
            200 <= http_status < 300
            and response.get("killed") is True
            and DiscoApiClient._status_of(response.get("state") or {}) == "IDLE"
        )
        if not acknowledged:
            raise RuntimeError("diagnostic stop was not acknowledged as killed IDLE")
        audit.timeline.append(
            "progress hard-cap diagnostic stop: killed active conversation "
            f"(http {response.get('http_status')})"
        )
        return response, response_epoch, True
    except Exception as exc:  # noqa: BLE001 — retain partial evidence fail-closed
        audit.timeline.append(
            f"progress hard-cap diagnostic stop could not kill conversation: {type(exc).__name__}"
        )
        return response, response_epoch, False


def _durable_stop_boundary(
    events: list[dict[str, Any]],
    watermark: _EventWatermark,
) -> tuple[int, float] | None:
    candidates = [
        (int(event.get("seq", -1)), epoch)
        for event in events
        if watermark.trusted
        and int(event.get("seq", -1)) > watermark.max_seq
        and _status_value_and_detail(event) == ("IDLE", "killed")
        and (epoch := _event_epoch(event)) is not None
    ]
    return min(candidates, default=None)


async def _collect_stopped_events(
    client: ProductClient,
    conversation_id: str,
    watermark: _EventWatermark,
    audit: DriveAudit,
) -> list[dict[str, Any]]:
    try:
        return client.collect_events(conversation_id)
    except Exception as exc:  # noqa: BLE001 — retain every other slice
        audit.timeline.append(f"progress hard-cap event collection failed: {type(exc).__name__}")
        return watermark.events


async def _collect_stopped_state(
    client: ProductClient,
    conversation_id: str,
    audit: DriveAudit,
) -> dict[str, Any]:
    try:
        return await client.get_state(conversation_id)
    except Exception as exc:  # noqa: BLE001
        audit.timeline.append(
            f"progress hard-cap final-state collection failed: {type(exc).__name__}"
        )
        return {"evidence_error": type(exc).__name__}


def _frozen_workspace_manifest(
    frozen_workspace: dict[str, Any],
    audit: DriveAudit,
) -> dict[str, Any]:
    if frozen_workspace.get("status") == "frozen":
        return frozen_workspace.get("manifest") or {}
    audit.timeline.append(
        f"progress hard-cap workspace evidence unavailable: {frozen_workspace.get('status')}"
    )
    return {}


def _frozen_browser_evidence(
    client: ProductClient,
    conversation_id: str,
    scenario: dict[str, Any],
    events: list[dict[str, Any]],
    workspace: dict[str, Any],
    frozen_workspace: dict[str, Any],
    audit: DriveAudit,
) -> dict[str, Any]:
    horizon = frozen_workspace.get("horizon_seq")
    if frozen_workspace.get("status") != "frozen" or not isinstance(horizon, int):
        audit.timeline.append(
            "progress hard-cap browser evidence unavailable: no accepted freeze horizon"
        )
        return {}
    try:
        return client.collect_browser_evidence(
            conversation_id,
            events,
            workspace,
            require_verified_host_screenshot=_browser_verification_required(scenario),
            horizon_seq=horizon,
        )
    except Exception as exc:  # noqa: BLE001
        audit.timeline.append(
            f"progress hard-cap browser-evidence collection failed: {type(exc).__name__}"
        )
        return {}


async def _stopped_preview(
    client: ProductClient,
    conversation_id: str,
    scenario: dict[str, Any],
    audit: DriveAudit,
) -> dict[str, Any] | None:
    if not _preview_required(scenario):
        return None
    try:
        return await client.collect_preview(conversation_id)
    except Exception as exc:  # noqa: BLE001
        audit.timeline.append(f"progress hard-cap preview collection failed: {type(exc).__name__}")
        return None


def _observe_stopped_thrash(
    client: ProductClient,
    events: list[dict[str, Any]],
    inspect_trace: dict[str, Any] | None,
    state_final: dict[str, Any],
    audit: DriveAudit,
) -> None:
    try:
        client.observe_live_thrash_snapshot(
            events,
            inspect_trace,
            terminal_status=DiscoApiClient._status_of(state_final),
        )
    except Exception as exc:  # noqa: BLE001 — retain the remaining dossier
        audit.timeline.append(f"progress hard-cap thrash snapshot failed: {type(exc).__name__}")


def _diagnostic_stop_evidence(
    client: ProductClient,
    frozen_workspace: dict[str, Any],
    stop: _DiagnosticStop,
    watermark: _EventWatermark,
) -> dict[str, Any]:
    return {
        **client.scenario_evidence,
        "diagnostic_stop": {
            "kind": "progressing_hard_cap",
            "release_confirmed": stop.release_confirmed,
            "boundary_epoch": stop.boundary_epoch,
            "boundary_seq": stop.boundary_seq,
            "boundary_source": (
                "durable_killed_status" if stop.boundary_seq is not None else "unconfirmed"
            ),
            "kill_acknowledged": stop.acknowledged,
            "kill_response_epoch": stop.response_epoch,
            "pre_stop_watermark_trusted": watermark.trusted,
            "sandbox_instance_ids": _extract_sandbox_instance_ids(stop.response),
            "workspace_freeze": {
                name: frozen_workspace.get(name)
                for name in (
                    "status",
                    "reason",
                    "version_seq",
                    "paused_seq",
                    "horizon_seq",
                    "tree_digest",
                    "file_count",
                    "total_bytes",
                )
            },
        },
    }


async def freeze_progress_timeout(
    client: ProductClient,
    conversation_id: str,
    scenario: dict[str, Any],
    state_initial: dict[str, Any],
    audit: DriveAudit,
    exc: InconclusiveRunError,
) -> None:
    """Stop spend and attach the strongest honest pre-kill evidence to the timeout."""
    watermark = _pre_stop_watermark(client, conversation_id)
    frozen_workspace = await client.freeze_progressing_workspace(
        conversation_id,
        _declared_workspace_paths(scenario),
    )
    _note_workspace_freeze(audit, frozen_workspace)
    response, response_epoch, acknowledged = await _kill_progressing_conversation(
        client,
        conversation_id,
        audit,
    )
    if not acknowledged:
        client.note_inspect_stop_unconfirmed(conversation_id)
    await client.finish_inspect_collection(conversation_id)
    inspect_trace = await client.collect_inspect_trace(conversation_id)
    events = await _collect_stopped_events(
        client,
        conversation_id,
        watermark,
        audit,
    )
    boundary = _durable_stop_boundary(events, watermark)
    release_confirmed = acknowledged and boundary is not None
    if release_confirmed and client.last_conversation_id == conversation_id:
        client.last_conversation_id = None
    stop = _DiagnosticStop(
        response=response,
        response_epoch=response_epoch,
        acknowledged=acknowledged,
        boundary_seq=boundary[0] if boundary is not None else None,
        boundary_epoch=boundary[1] if boundary is not None else None,
        release_confirmed=release_confirmed,
    )
    state_final = await _collect_stopped_state(client, conversation_id, audit)
    workspace = _frozen_workspace_manifest(frozen_workspace, audit)
    browser = _frozen_browser_evidence(
        client,
        conversation_id,
        scenario,
        events,
        workspace,
        frozen_workspace,
        audit,
    )
    preview = await _stopped_preview(client, conversation_id, scenario, audit)
    _observe_stopped_thrash(client, events, inspect_trace, state_final, audit)
    exc.collected_run = CollectedRun(
        conversation_id=conversation_id,
        events=events,
        state_initial=state_initial,
        state_final=state_final,
        workspace_manifest=workspace,
        preview=preview,
        browser_evidence=browser,
        inspect_trace=inspect_trace,
        thrash_monitor=client.live_thrash_monitor,
        timeline=audit.timeline,
        decision_resolutions=audit.decisions,
        declared_followup_seqs=audit.declared_followup_seqs,
        declared_followup_requires_revision=audit.declared_followup_requires_revision,
        harness_injected_user_seqs=audit.injected_user_seqs,
        product_evidence=_diagnostic_stop_evidence(
            client,
            frozen_workspace,
            stop,
            watermark,
        ),
        diagnostic_stop="progressing_hard_cap",
        diagnostic_stop_epoch=stop.boundary_epoch,
        diagnostic_stop_seq=stop.boundary_seq,
        diagnostic_release_confirmed=stop.release_confirmed,
    )

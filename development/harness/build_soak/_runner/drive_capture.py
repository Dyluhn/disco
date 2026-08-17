"""Final durable boundary and evidence capture for one Build Soak drive."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..adapters.disco_api import (
    INACTIVE_TIMEOUT,
    LIVE_THRASH_STOP,
    PAUSED_STATE,
    TERMINAL_STATES,
    BrowserEvidenceCollectionError,
    CollectedRun,
    DiscoApiClient,
    _referenced_screenshot_paths,
)
from ..events import NormalizationError, normalize_events
from ..ports import ProductClient
from .drive_phases import DrivePhaseResult
from .drive_start import DriveAudit, ScenarioStart
from .scenario_io import (
    _browser_verification_required,
    _declared_workspace_paths,
    _preview_required,
    _verify_driver_context_window_pin,
)
from .thrash import (
    _confirmed_live_thrash_stop,
    _status_value_and_detail,
    _strict_live_thrash_stop_boundary,
)
from .triggers import _export_matches_workspace, _governed_artifact_paths

_TERMINAL_SNAPSHOT_STATES = frozenset({"FINISHED", "VERIFIED", "ERROR", "STUCK"})


@dataclass(frozen=True)
class SnapshotBoundary:
    frozen_events: list[dict[str, Any]]
    latest_status_event: dict[str, Any] | None
    frozen_work_terminal: bool
    inactive_without_terminal: bool
    live_thrash_without_terminal: bool
    nonterminal_without_snapshot: bool
    latest_frozen_status: str
    failed_terminal_without_seal: bool
    paused_work_terminal: bool
    terminal_snapshot_available: bool


@dataclass(frozen=True)
class WorkspaceCapture:
    manifest: dict[str, Any]
    paused: dict[str, Any] | None


@dataclass(frozen=True)
class BrowserCapture:
    files: dict[str, Any]
    diagnostic: dict[str, Any] | None = None
    error: BrowserEvidenceCollectionError | None = None


async def _stop_nonterminal_before_inspect(
    client: ProductClient,
    conversation_id: str,
    state_final: dict[str, Any],
    audit: DriveAudit,
) -> None:
    if DiscoApiClient._status_of(state_final) in TERMINAL_STATES:
        return
    confirmed = False
    try:
        response = await client.kill(conversation_id)
        confirmed = (
            200 <= int(response.get("http_status", 0)) < 300
            and response.get("killed") is True
            and DiscoApiClient._status_of(response.get("state") or {}) == "IDLE"
        )
        audit.timeline.append(
            "stopped nonterminal conversation before inspect freeze "
            f"(confirmed={confirmed}, http {response.get('http_status')})"
        )
    except Exception as exc:  # noqa: BLE001 — taint instead of crash
        audit.timeline.append(
            f"pre-freeze stop of nonterminal conversation failed: {type(exc).__name__}"
        )
    if not confirmed:
        client.note_inspect_stop_unconfirmed(conversation_id)


def _normalized_capture_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return normalize_events(events)
    except (NormalizationError, ValueError, TypeError):
        return []


def _latest_work_status_event(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for event in events:
        if (
            event.get("kind") != "status"
            or event.get("source") != "system"
            or type(event.get("seq")) is not int
        ):
            continue
        status, _detail = _status_value_and_detail(event)
        if status == "IDLE":
            continue
        if latest is None or int(event["seq"]) > int(latest["seq"]):
            latest = event
    return latest


def _event_after_baseline(
    event: dict[str, Any] | None,
    baseline_seq: int | None,
) -> bool:
    return bool(event is not None and (baseline_seq is None or int(event["seq"]) > baseline_seq))


def _snapshot_boundary(
    events: list[dict[str, Any]],
    state_final: dict[str, Any],
    phase: DrivePhaseResult,
    thrash_monitor: dict[str, Any],
) -> SnapshotBoundary:
    frozen = _normalized_capture_events(events)
    latest = _latest_work_status_event(frozen)
    after_baseline = _event_after_baseline(latest, phase.terminal_baseline_seq)
    latest_status = str(latest.get("status")) if latest is not None else ""
    frozen_terminal = after_baseline and latest_status in _TERMINAL_SNAPSHOT_STATES
    inactive = phase.drive_status == INACTIVE_TIMEOUT and not frozen_terminal
    live_thrash = (
        phase.drive_status == LIVE_THRASH_STOP
        and not frozen_terminal
        and _strict_live_thrash_stop_boundary(events, state_final, thrash_monitor)
    )
    nonterminal = inactive or live_thrash
    failed = frozen_terminal and latest_status in {"ERROR", "STUCK"}
    paused = after_baseline and latest_status == PAUSED_STATE
    return SnapshotBoundary(
        frozen_events=frozen,
        latest_status_event=latest,
        frozen_work_terminal=frozen_terminal,
        inactive_without_terminal=inactive,
        live_thrash_without_terminal=live_thrash,
        nonterminal_without_snapshot=nonterminal,
        latest_frozen_status=latest_status,
        failed_terminal_without_seal=failed,
        paused_work_terminal=paused,
        terminal_snapshot_available=not (nonterminal or failed or paused),
    )


async def _capture_workspace(
    client: ProductClient,
    conversation_id: str,
    scenario: dict[str, Any],
    phase: DrivePhaseResult,
    boundary: SnapshotBoundary,
    audit: DriveAudit,
) -> WorkspaceCapture:
    declared = _declared_workspace_paths(scenario)
    if boundary.terminal_snapshot_available:
        return WorkspaceCapture(
            await client.collect_workspace(conversation_id, declared),
            None,
        )
    if boundary.paused_work_terminal:
        paused = client.collect_paused_workspace(conversation_id, declared)
        if paused.get("status") == "frozen":
            audit.timeline.append(
                "collected PAUSED-terminal workspace from the immutable version "
                f"(version_seq={paused.get('version_seq')}, "
                f"horizon_seq={paused.get('horizon_seq')})"
            )
            return WorkspaceCapture(paused.get("manifest") or {}, paused)
        audit.timeline.append(
            "PAUSED-terminal immutable version unavailable "
            f"({paused.get('reason')}); preserving product adjudication"
        )
        return WorkspaceCapture(
            {
                "files": {},
                "_capture": {
                    "status": "not_collected_paused_terminal",
                    "drive_status": phase.drive_status,
                    "reason": str(paused.get("reason")),
                },
            },
            paused,
        )
    failed = boundary.failed_terminal_without_seal
    audit.timeline.append(
        "skipped strict workspace/browser byte capture for "
        + (
            f"failed terminal {boundary.latest_frozen_status}"
            if failed
            else f"nonterminal drive status {phase.drive_status}"
        )
        + "; preserving product adjudication"
    )
    return WorkspaceCapture(
        {
            "files": {},
            "_capture": {
                "status": (
                    "not_collected_failed_terminal" if failed else "not_collected_nonterminal"
                ),
                "drive_status": phase.drive_status,
                "reason": (
                    "strict final workspace seal exists only for successful completion"
                    if failed
                    else "strict atomic workspace evidence requires a durable terminal"
                ),
            },
        },
        None,
    )


def _uncaptured_browser_diagnostic(
    conversation_id: str,
    referenced_paths: list[str],
    boundary: SnapshotBoundary,
) -> dict[str, Any]:
    if boundary.failed_terminal_without_seal:
        kind = "browser_evidence_not_collected_failed_terminal"
        preserved_for = "failed_terminal_product_adjudication"
    elif boundary.paused_work_terminal:
        kind = "browser_evidence_not_collected_paused_terminal"
        preserved_for = "paused_terminal_product_adjudication"
    elif boundary.live_thrash_without_terminal:
        kind = "browser_evidence_not_collected_nonterminal"
        preserved_for = "confirmed_live_thrash_adjudication"
    else:
        kind = "browser_evidence_not_collected_nonterminal"
        preserved_for = "nonterminal_product_adjudication"
    return {
        "schema_version": 1,
        "kind": kind,
        "reason": "strict browser bytes require the unavailable terminal snapshot",
        "facts": {"referenced_paths": referenced_paths},
        "conversation_id": conversation_id,
        "preserved_for": preserved_for,
        "admissible_as_browser_evidence": False,
    }


def _capture_paused_browser(
    client: ProductClient,
    conversation_id: str,
    scenario: dict[str, Any],
    events: list[dict[str, Any]],
    workspace: WorkspaceCapture,
    audit: DriveAudit,
) -> BrowserCapture:
    paused = workspace.paused or {}
    horizon = paused.get("horizon_seq")
    try:
        files = client.collect_browser_evidence(
            conversation_id,
            events,
            workspace.manifest,
            require_verified_host_screenshot=_browser_verification_required(scenario),
            horizon_seq=horizon if type(horizon) is int else None,
        )
        return BrowserCapture(files)
    except Exception as exc:  # noqa: BLE001 — mirror hard-cap containment
        audit.timeline.append(
            f"PAUSED-terminal browser-evidence collection failed: {type(exc).__name__}"
        )
        return BrowserCapture({})


def _capture_browser(
    client: ProductClient,
    conversation_id: str,
    scenario: dict[str, Any],
    events: list[dict[str, Any]],
    workspace: WorkspaceCapture,
    boundary: SnapshotBoundary,
    phase: DrivePhaseResult,
    audit: DriveAudit,
) -> BrowserCapture:
    paused_frozen = workspace.paused is not None and workspace.paused.get("status") == "frozen"
    if paused_frozen:
        return _capture_paused_browser(
            client,
            conversation_id,
            scenario,
            events,
            workspace,
            audit,
        )
    if not boundary.terminal_snapshot_available:
        referenced = _referenced_screenshot_paths(
            events,
            require_verified_host_screenshot=_browser_verification_required(scenario),
        )
        diagnostic = (
            _uncaptured_browser_diagnostic(conversation_id, referenced, boundary)
            if referenced
            else None
        )
        return BrowserCapture({}, diagnostic=diagnostic)
    try:
        return BrowserCapture(
            client.collect_browser_evidence(
                conversation_id,
                events,
                workspace.manifest,
                require_verified_host_screenshot=_browser_verification_required(scenario),
            )
        )
    except BrowserEvidenceCollectionError as exc:
        if phase.drive_status != LIVE_THRASH_STOP:
            raise
        return BrowserCapture({}, error=exc)


def _sample_final_thrash(
    client: ProductClient,
    events: list[dict[str, Any]],
    inspect_trace: dict[str, Any] | None,
    state_final: dict[str, Any],
    workspace: WorkspaceCapture,
    browser: BrowserCapture,
    audit: DriveAudit,
) -> dict[str, Any]:
    client.observe_live_thrash_snapshot(
        events,
        inspect_trace,
        terminal_status=DiscoApiClient._status_of(state_final),
    )
    monitor = client.live_thrash_monitor
    audit.timeline.append(
        "live thrash monitor sampled "
        f"{monitor['sample_count']} times and detected "
        f"{len(monitor['findings'])} threshold crossing(s)"
    )
    audit.timeline.append(
        f"collected {len(events)} events; workspace files={list(workspace.manifest)}; "
        f"browser evidence files={list(browser.files)}"
    )
    return monitor


async def _add_export_evidence(
    client: ProductClient,
    conversation_id: str,
    scenario: dict[str, Any],
    lifecycle: dict[str, Any],
    events: list[dict[str, Any]],
    workspace: dict[str, Any],
    boundary: SnapshotBoundary,
    evidence: dict[str, Any],
    audit: DriveAudit,
) -> None:
    if not boundary.terminal_snapshot_available or not lifecycle.get("export_download"):
        return
    export_status, archive = await client.download_project(conversation_id)
    verified_paths = _governed_artifact_paths(
        events,
        scenario=scenario,
        conversation_id=conversation_id,
    )
    matches, facts = _export_matches_workspace(
        archive,
        workspace,
        _declared_workspace_paths(scenario),
        verified_artifact_paths=verified_paths,
    )
    evidence["export"] = {
        "requested": True,
        "download_present": 200 <= export_status < 300 and bool(archive),
        "download_bytes": len(archive),
        "workspace_match": matches,
        "http_status": export_status,
        **facts,
    }
    audit.timeline.append(
        "downloaded project export after final terminal: "
        f"http={export_status}, bytes={len(archive)}, workspace_match={matches}"
    )


def _nonterminal_product_evidence(
    client: ProductClient,
    phase: DrivePhaseResult,
    boundary: SnapshotBoundary,
    workspace: WorkspaceCapture,
) -> dict[str, Any]:
    evidence = dict(client.scenario_evidence)
    if boundary.nonterminal_without_snapshot:
        adjudication: dict[str, Any] = {
            "schema_version": 1,
            "drive_status": phase.drive_status,
            "terminal_baseline_seq": phase.terminal_baseline_seq,
            "frozen_max_seq": max(
                (
                    int(event["seq"])
                    for event in boundary.frozen_events
                    if type(event.get("seq")) is int
                ),
                default=-1,
            ),
            "terminal_snapshot_available": False,
            "terminal_only_evidence_unavailable": ["workspace", "browser"],
        }
        if boundary.live_thrash_without_terminal:
            adjudication.update(
                {
                    "preserved_for": "strictly_confirmed_live_thrash_stop",
                    "strict_monitor_confirmed": True,
                }
            )
        evidence["nonterminal_adjudication"] = adjudication
    if boundary.paused_work_terminal and workspace.paused is not None:
        paused = workspace.paused
        evidence["paused_terminal_adjudication"] = {
            "schema_version": 1,
            "drive_status": phase.drive_status,
            "capture_status": paused.get("status"),
            "version_seq": paused.get("version_seq"),
            "horizon_seq": paused.get("horizon_seq"),
            "reason": paused.get("reason"),
        }
    return evidence


def _browser_error_disclosure(
    conversation_id: str,
    browser: BrowserCapture,
) -> dict[str, Any] | None:
    if browser.diagnostic is not None:
        return browser.diagnostic
    if browser.error is None:
        return None
    return {
        "schema_version": 1,
        "kind": "browser_evidence_collection_error",
        "exception_type": type(browser.error).__name__,
        "reason": browser.error.reason,
        "facts": dict(browser.error.facts),
        "conversation_id": conversation_id,
        "preserved_for": "confirmed_live_thrash_stop",
        "admissible_as_browser_evidence": False,
    }


def _assemble_collected_run(
    start: ScenarioStart,
    audit: DriveAudit,
    events: list[dict[str, Any]],
    state_final: dict[str, Any],
    inspect_trace: dict[str, Any] | None,
    workspace: WorkspaceCapture,
    browser: BrowserCapture,
    preview: dict[str, Any] | None,
    monitor: dict[str, Any],
    product_evidence: dict[str, Any],
) -> CollectedRun:
    return CollectedRun(
        conversation_id=start.conversation_id,
        events=events,
        state_initial=start.state_initial,
        state_final=state_final,
        workspace_manifest=workspace.manifest,
        preview=preview,
        browser_evidence=browser.files,
        inspect_trace=inspect_trace,
        thrash_monitor=monitor,
        timeline=audit.timeline,
        decision_resolutions=audit.decisions,
        declared_followup_seqs=audit.declared_followup_seqs,
        declared_followup_requires_revision=audit.declared_followup_requires_revision,
        harness_injected_user_seqs=audit.injected_user_seqs,
        product_evidence=product_evidence,
        browser_evidence_collection_error=_browser_error_disclosure(
            start.conversation_id,
            browser,
        ),
    )


async def collect_scenario_run(
    client: ProductClient,
    scenario: dict[str, Any],
    start: ScenarioStart,
    phase: DrivePhaseResult,
    audit: DriveAudit,
) -> CollectedRun:
    """Freeze the durable terminal boundary and assemble the final collected facts."""
    events = client.collect_events(start.conversation_id)
    state_final = await client.get_state(start.conversation_id)
    await _stop_nonterminal_before_inspect(
        client,
        start.conversation_id,
        state_final,
        audit,
    )
    await client.finish_inspect_collection(start.conversation_id)
    inspect_trace = await client.collect_inspect_trace(start.conversation_id)
    boundary = _snapshot_boundary(
        events,
        state_final,
        phase,
        client.live_thrash_monitor,
    )
    workspace = await _capture_workspace(
        client,
        start.conversation_id,
        scenario,
        phase,
        boundary,
        audit,
    )
    browser = _capture_browser(
        client,
        start.conversation_id,
        scenario,
        events,
        workspace,
        boundary,
        phase,
        audit,
    )
    preview = (
        await client.collect_preview(start.conversation_id) if _preview_required(scenario) else None
    )
    monitor = _sample_final_thrash(
        client,
        events,
        inspect_trace,
        state_final,
        workspace,
        browser,
        audit,
    )
    product_evidence = _nonterminal_product_evidence(
        client,
        phase,
        boundary,
        workspace,
    )
    # Fail closed BEFORE any verdict is assembled: an override naming a catalogue
    # key the stack does not carry resolves to the default driver silently, so an
    # unverified pin would yield a green-looking measurement of the wrong window.
    pin_facts = _verify_driver_context_window_pin(scenario, inspect_trace)
    if pin_facts is not None:
        product_evidence["driver_context_window_pin"] = {"schema_version": 1, **pin_facts}
        audit.timeline.append(
            "verified driver context-window pin from this run's own agent.step spans: "
            f"{pin_facts['steps_observed']} step(s), windows={pin_facts['distinct_windows']}"
        )
    await _add_export_evidence(
        client,
        start.conversation_id,
        scenario,
        start.lifecycle,
        events,
        workspace.manifest,
        boundary,
        product_evidence,
        audit,
    )
    run = _assemble_collected_run(
        start,
        audit,
        events,
        state_final,
        inspect_trace,
        workspace,
        browser,
        preview,
        monitor,
        product_evidence,
    )
    if browser.error is not None:
        if not _confirmed_live_thrash_stop(run):
            raise browser.error
        audit.timeline.append(
            "confirmed live-thrash product failure retained despite browser-evidence "
            f"collection error: {type(browser.error).__name__}"
        )
    return run

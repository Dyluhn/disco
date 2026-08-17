"""Initial, terminal-lifecycle, and serialized follow-up phases."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..adapters.disco_api import (
    FOLLOWUP_PICKUP_TIMEOUT,
    INACTIVE_TIMEOUT,
    LIVE_THRASH_STOP,
    DiscoApiClient,
    FollowupPickupError,
    InconclusiveRunError,
)
from ..ports import ProductClient
from .bindings import ScenarioBindings
from .common import _TRIGGER_AFTER_TERMINAL
from .drive_start import DriveAudit, ScenarioStart
from .scenario_io import _declared_workspace_paths
from .triggers import _cancel_at_trigger, _restart_isolated_stack

_TERMINAL_SNAPSHOT_STATES = frozenset({"FINISHED", "VERIFIED", "ERROR", "STUCK"})
TimeoutFreeze = Callable[[InconclusiveRunError], Awaitable[None]]


@dataclass(frozen=True)
class DrivePhaseResult:
    drive_status: str
    terminal_baseline_seq: int | None


async def _initial_phase(
    client: ProductClient,
    start: ScenarioStart,
    audit: DriveAudit,
    runtime: ScenarioBindings,
    *,
    autonomous: bool,
    timeout_s: float,
    hard_cap_s: float,
    freeze_timeout: TimeoutFreeze,
) -> str:
    try:
        return await runtime.drive_to_terminal(
            client,
            start.conversation_id,
            autonomous=autonomous,
            mid_run=start.mid_run,
            cancel_at=start.cancel_at,
            pause_at=start.pause_at,
            timeline=audit.timeline,
            inactivity_s=timeout_s,
            hard_cap_s=hard_cap_s,
            clarification_answer=start.clarification_answer,
            decision_answer=start.decision_answer,
            decisions=audit.decisions,
            injected_user_seqs=audit.injected_user_seqs,
            declared_followup_seqs=audit.declared_followup_seqs,
            declared_followup_requires_revision=audit.declared_followup_requires_revision,
        )
    except InconclusiveRunError as exc:
        await freeze_timeout(exc)
        raise


async def _restore_after_terminal(
    client: ProductClient,
    start: ScenarioStart,
    audit: DriveAudit,
    initial_status: str,
) -> None:
    if initial_status not in _TERMINAL_SNAPSHOT_STATES or not start.lifecycle.get(
        "restore_version"
    ):
        return
    selector = str(start.lifecycle.get("restore_version") or "oldest")
    restored = await client.restore_workspace_version(
        start.conversation_id,
        selector=selector,
    )
    client.scenario_evidence["rollback"] = restored
    audit.timeline.append(
        "restored workspace version after initial terminal: "
        f"selector={selector}, ok={restored.get('ok')}, "
        f"selected={restored.get('selected_seq')}, new={restored.get('new_version')}"
    )


async def _restart_after_terminal(
    client: ProductClient,
    start: ScenarioStart,
    scenario: dict[str, Any],
    audit: DriveAudit,
    initial_status: str,
) -> None:
    if initial_status not in _TERMINAL_SNAPSHOT_STATES or not start.lifecycle.get(
        "restart_after_terminal"
    ):
        return
    declared = _declared_workspace_paths(scenario)
    before_status = DiscoApiClient._status_of(await client.get_state(start.conversation_id))
    before_digest = await client.workspace_snapshot_digest(
        start.conversation_id,
        declared,
    )
    client.note_expected_stack_restart(start.conversation_id)
    restarted = await _restart_isolated_stack()
    after_status = ""
    after_digest: str | None = None
    if restarted.get("ok") is True:
        with contextlib.suppress(Exception):
            after_status = DiscoApiClient._status_of(await client.get_state(start.conversation_id))
            after_digest = await client.workspace_snapshot_digest(
                start.conversation_id,
                declared,
            )
    restart_ok = (
        restarted.get("ok") is True
        and before_status in _TERMINAL_SNAPSHOT_STATES
        and after_status == before_status
        and before_digest is not None
        and after_digest == before_digest
    )
    client.scenario_evidence["restart"] = {
        **restarted,
        "ok": restart_ok,
        "before_status": before_status,
        "after_status": after_status,
        "before_digest": before_digest,
        "after_digest": after_digest,
    }
    audit.timeline.append(
        "restarted isolated App+Agent stack after initial terminal: "
        f"ok={restart_ok}, status={before_status}->{after_status}, "
        f"snapshot_same={before_digest is not None and after_digest == before_digest}"
    )


async def _after_terminal_cancel(
    client: ProductClient,
    start: ScenarioStart,
    audit: DriveAudit,
    initial_status: str,
    hard_cap_s: float,
) -> None:
    if (
        initial_status in _TERMINAL_SNAPSHOT_STATES
        and start.cancel_at
        and start.cancel_at.get("trigger") == _TRIGGER_AFTER_TERMINAL
    ):
        await _cancel_at_trigger(
            client,
            start.conversation_id,
            audit.timeline,
            trigger=_TRIGGER_AFTER_TERMINAL,
            timeout_s=hard_cap_s,
        )


async def _send_serialized_followup(
    client: ProductClient,
    start: ScenarioStart,
    audit: DriveAudit,
    followup: dict[str, Any],
) -> int:
    baseline = await client.capture_followup_baseline(start.conversation_id)
    baseline_seq = int(baseline.get("max_seq", -1))
    before_user_seq = client.latest_user_message_seq(start.conversation_id)
    await client.send_followup(
        start.conversation_id,
        str(followup["text"]),
        kind="message",
    )
    audit.timeline.append(f"sent after-terminal follow-up: {followup['text']!r}")
    pickup = await client.wait_for_followup_pickup(start.conversation_id, baseline)
    if pickup == FOLLOWUP_PICKUP_TIMEOUT:
        audit.timeline.append(
            "follow-up NOT picked up within the bound — SEQUENCING FAILURE (INVALID_RUN); "
            "not re-sending (would duplicate the turn) and not driving on the stale terminal "
            f"(baseline status={baseline.get('status')!r}, "
            f"plan_revision={baseline.get('plan_revision')}, seq={baseline_seq})"
        )
        raise FollowupPickupError(
            "after-terminal follow-up was not picked up by the engine within the bound "
            "— cannot serialize the follow-ups",
            {
                "stage": "followup_pickup",
                "baseline_status": baseline.get("status"),
                "baseline_seq": baseline_seq,
                "baseline_plan_revision": baseline.get("plan_revision"),
                "followup_text": str(followup["text"]),
            },
        )
    audit.timeline.append(f"follow-up picked up by the engine ({pickup}) — now serialized")
    new_user_seq = client.latest_user_message_seq(start.conversation_id)
    if new_user_seq > before_user_seq:
        audit.declared_followup_seqs.append(new_user_seq)
        audit.declared_followup_requires_revision.append(
            bool(followup.get("requires_plan_revision"))
        )
    return baseline_seq


async def _drive_after_terminal(
    client: ProductClient,
    start: ScenarioStart,
    audit: DriveAudit,
    runtime: ScenarioBindings,
    followups: list[dict[str, Any]],
    initial_status: str,
    *,
    autonomous: bool,
    timeout_s: float,
    hard_cap_s: float,
    freeze_timeout: TimeoutFreeze,
) -> DrivePhaseResult:
    drive_status = initial_status
    terminal_baseline: int | None = None
    for followup in followups:
        baseline_seq = await _send_serialized_followup(
            client,
            start,
            audit,
            followup,
        )
        try:
            terminal_baseline = baseline_seq
            drive_status = await runtime.drive_to_terminal(
                client,
                start.conversation_id,
                autonomous=autonomous,
                mid_run=[],
                pause_at=None,
                timeline=audit.timeline,
                inactivity_s=timeout_s,
                hard_cap_s=hard_cap_s,
                clarification_answer=start.clarification_answer,
                decision_answer=start.decision_answer,
                decisions=audit.decisions,
                injected_user_seqs=audit.injected_user_seqs,
                min_seq=baseline_seq,
            )
        except InconclusiveRunError as exc:
            await freeze_timeout(exc)
            raise
        if drive_status in {INACTIVE_TIMEOUT, LIVE_THRASH_STOP}:
            audit.timeline.append(
                "after-terminal phase stopped without a durable work terminal: "
                f"{drive_status}; skipping remaining follow-ups"
            )
            break
    return DrivePhaseResult(drive_status, terminal_baseline)


async def run_drive_phases(
    client: ProductClient,
    start: ScenarioStart,
    scenario: dict[str, Any],
    audit: DriveAudit,
    runtime: ScenarioBindings,
    *,
    autonomous: bool,
    timeout_s: float,
    hard_cap_s: float,
    freeze_timeout: TimeoutFreeze,
) -> DrivePhaseResult:
    """Run the initial build, terminal controls, and serialized follow-up cycles."""
    initial_status = await _initial_phase(
        client,
        start,
        audit,
        runtime,
        autonomous=autonomous,
        timeout_s=timeout_s,
        hard_cap_s=hard_cap_s,
        freeze_timeout=freeze_timeout,
    )
    followups = list(start.after_terminal)
    if initial_status == INACTIVE_TIMEOUT:
        followups = []
    if initial_status == LIVE_THRASH_STOP:
        audit.timeline.append(
            "confirmed live thrash threshold stopped the conversation; "
            "skipping every remaining follow-up"
        )
        followups = []
    await _after_terminal_cancel(
        client,
        start,
        audit,
        initial_status,
        hard_cap_s,
    )
    await _restore_after_terminal(client, start, audit, initial_status)
    await _restart_after_terminal(client, start, scenario, audit, initial_status)
    return await _drive_after_terminal(
        client,
        start,
        audit,
        runtime,
        followups,
        initial_status,
        autonomous=autonomous,
        timeout_s=timeout_s,
        hard_cap_s=hard_cap_s,
        freeze_timeout=freeze_timeout,
    )

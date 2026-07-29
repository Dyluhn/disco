"""Bounded Build Soak triggers owner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ..adapters.disco_api import (
    PAUSED_STATE,
    TERMINAL_STATES,
    DiscoApiClient,
)
from ..ports import ProductClient
from .artifacts import (
    _export_matches_workspace as _export_matches_workspace,
)
from .artifacts import (
    _governed_artifact_paths as _governed_artifact_paths,
)
from .common import (
    _TRIGGER_AFTER_FIRST_FILE_WRITE,
)
from .gates import (
    _dispatch_drive_gate,
    _DriveControl,
    _PauseOwnership,
    _poll_drive_status,
    _raise_if_progressing_timeout,
    _retire_pause_ownership,
)
from .scenario_io import (
    _is_cancel_after_first_write,
)


class CancelMissedWindowError(Exception):
    """The cancel_at watcher found its trigger only after the build had terminalized."""

    def __init__(self, reason: str, facts: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or {}


def _trigger_requested(trigger: object) -> bool:
    return isinstance(trigger, dict) and trigger.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE


def _record_missing_trigger(
    timeline: list[str],
    *,
    has_mid_run: bool,
    wants_cancel: bool,
    wants_pause: bool,
) -> None:
    if has_mid_run:
        timeline.append("mid-run steer skipped: run produced no file write to steer on")
    if wants_cancel:
        timeline.append("cancel_at skipped: run produced no file write to cancel on")
    if wants_pause:
        timeline.append("pause_at skipped: run produced no file write to pause on")


async def _send_mid_run_steers(
    client: ProductClient,
    cid: str,
    mid_run: list[dict[str, Any]],
    *,
    trigger_seq: int,
    timeout_s: float,
    timeline: list[str],
    declared_seqs: list[int] | None,
    declared_requires: list[bool] | None,
) -> None:
    for followup in mid_run:
        before_user_seq = client.latest_user_message_seq(cid)
        await client.send_followup(cid, str(followup["text"]), kind="steer")
        timeline.append(f"steered after first file write (seq={trigger_seq}): {followup['text']!r}")
        if declared_seqs is None or declared_requires is None:
            continue
        new_seq = await client.wait_for_new_user_message_seq(
            cid,
            after_seq=before_user_seq,
            timeout_s=min(timeout_s, 30.0),
        )
        if new_seq is not None:
            declared_seqs.append(new_seq)
            declared_requires.append(bool(followup.get("requires_plan_revision")))


async def _inject_when_writing(
    client: ProductClient,
    cid: str,
    mid_run: list[dict[str, Any]],
    cancel_at: dict[str, Any] | None,
    pause_at: dict[str, Any] | None,
    timeline: list[str],
    timeout_s: float,
    pause_ownership: _PauseOwnership | None = None,
    declared_seqs: list[int] | None = None,
    declared_requires: list[bool] | None = None,
) -> None:
    """Background trigger watcher for after-first-file-write actions.

    It handles all current users of that trigger (mid-run steer follow-ups and the
    REL-6 cancel_at kill) from one task so the runner does not race two independent
    DB pollers against the same first-write boundary.
    """
    wants_cancel = _trigger_requested(cancel_at)
    wants_pause = _trigger_requested(pause_at)
    if not mid_run and not wants_cancel and not wants_pause:
        return

    seq = await client.wait_for_first_file_write(cid, timeout_s=timeout_s)
    if seq is None:
        _record_missing_trigger(
            timeline,
            has_mid_run=bool(mid_run),
            wants_cancel=wants_cancel,
            wants_pause=wants_pause,
        )
        return

    if wants_pause:
        await _pause_resume_at_trigger(
            client,
            cid,
            timeline,
            trigger_seq=seq,
            timeout_s=timeout_s,
            ownership=pause_ownership,
        )

    await _send_mid_run_steers(
        client,
        cid,
        mid_run,
        trigger_seq=seq,
        timeout_s=timeout_s,
        timeline=timeline,
        declared_seqs=declared_seqs,
        declared_requires=declared_requires,
    )

    if wants_cancel:
        await _cancel_at_trigger(
            client,
            cid,
            timeline,
            trigger=_TRIGGER_AFTER_FIRST_FILE_WRITE,
            trigger_seq=seq,
            timeout_s=timeout_s,
        )


async def _wait_for_status(
    client: ProductClient,
    cid: str,
    expected: str,
    *,
    timeout_s: float,
) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = DiscoApiClient._status_of(await client.get_state(cid))
        if last == expected:
            return last
        if last in TERMINAL_STATES:
            return last
        await asyncio.sleep(getattr(client, "_poll", 0.1))
    return last


async def _pause_resume_at_trigger(
    client: ProductClient,
    cid: str,
    timeline: list[str],
    *,
    trigger_seq: int,
    timeout_s: float,
    ownership: _PauseOwnership | None = None,
) -> None:
    if ownership is not None:
        ownership.active = True
        ownership.settled.clear()
    try:
        await client.pause(cid)
        paused = await _wait_for_status(client, cid, PAUSED_STATE, timeout_s=min(timeout_s, 60.0))
        if paused != PAUSED_STATE:
            client.scenario_evidence["pause_resume"] = {
                "ok": False,
                "trigger_seq": trigger_seq,
                "pause_status": paused,
            }
            timeline.append(
                f"pause_at after first write (seq={trigger_seq}) did not settle PAUSED "
                f"(status={paused or 'unknown'})"
            )
            return
        if ownership is not None:
            ownership.observed = True
        response = await client.resume(cid)
        resumed = await client.wait_until_status_leaves(
            cid, PAUSED_STATE, timeout_s=min(timeout_s, 60.0)
        )
        ok = 200 <= int(response.get("http_status", 0)) < 300 and resumed != PAUSED_STATE
        client.scenario_evidence["pause_resume"] = {
            "ok": ok,
            "trigger_seq": trigger_seq,
            "pause_status": paused,
            "resume_status": resumed,
            "resume_http_status": response.get("http_status"),
        }
        timeline.append(
            f"pause_at after first write (seq={trigger_seq}) settled PAUSED then resumed "
            f"to {resumed or 'unknown'} (http {response.get('http_status')})"
        )
    finally:
        if ownership is not None:
            ownership.active = False
            ownership.settled.set()


async def _restart_isolated_stack() -> dict[str, Any]:
    """Restart the disposable App+Agent pair through its private authenticated control."""
    url = os.environ.get("DISCO_RELIABILITY_STACK_CONTROL_URL", "").rstrip("/")
    token = os.environ.get("DISCO_RELIABILITY_STACK_CONTROL_TOKEN", "")
    if not url or not token:
        return {"ok": False, "reason": "isolated_stack_control_unavailable"}

    def _post() -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(
            f"{url}/restart",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                raw = response.read()
                body = json.loads(raw) if raw else {}
                return response.status, body if isinstance(body, dict) else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                body = json.loads(raw)
                if isinstance(body, dict):
                    return exc.code, body
            return exc.code, {}

    try:
        status, body = await asyncio.to_thread(_post)
    except (OSError, TimeoutError, ValueError) as exc:
        return {"ok": False, "reason": type(exc).__name__}
    return {
        "ok": 200 <= status < 300 and body.get("ok") is True,
        "http_status": status,
        "reason": body.get("reason"),
    }


async def _wait_for_cancel_idle(
    client: ProductClient,
    cid: str,
    *,
    previous_status: str,
    timeout_s: float,
) -> str:
    """Wait until a kill settles into the product's resting state.

    The kill route appends `IDLE` with detail `killed`. We require two consecutive IDLE
    reads to avoid racing the state projection immediately after the POST.
    """
    deadline = time.monotonic() + timeout_s
    last = previous_status
    if previous_status and previous_status != "IDLE":
        remaining = max(0.0, deadline - time.monotonic())
        last = await client.wait_until_status_leaves(cid, previous_status, timeout_s=remaining)

    stable_idle_reads = 1 if last == "IDLE" else 0
    while time.monotonic() < deadline:
        last = DiscoApiClient._status_of(await client.get_state(cid))
        if last == "IDLE":
            stable_idle_reads += 1
            if stable_idle_reads >= 2:
                return last
        else:
            stable_idle_reads = 0
        await asyncio.sleep(getattr(client, "_poll", 0.1))
    raise TimeoutError(f"cancel did not settle to stable IDLE (last status={last!r})")


async def _cancel_at_trigger(
    client: ProductClient,
    cid: str,
    timeline: list[str],
    *,
    trigger: str,
    timeout_s: float,
    trigger_seq: int | None = None,
) -> None:
    before_status = ""
    with contextlib.suppress(Exception):
        before_status = DiscoApiClient._status_of(await client.get_state(cid))
    terminal_seq = -1
    with contextlib.suppress(Exception):
        terminal_seq = client._latest_terminal_seq(cid)
    if trigger == _TRIGGER_AFTER_FIRST_FILE_WRITE and (
        (trigger_seq is not None and terminal_seq > trigger_seq)
        or (trigger_seq is None and terminal_seq >= 0)
        or before_status in TERMINAL_STATES
    ):
        reason = (
            "cancel_at after_first_file_write missed the interrupt window: "
            "the build reached a terminal status before the harness could issue kill"
        )
        facts = {
            "trigger": trigger,
            "trigger_seq": trigger_seq,
            "terminal_seq": terminal_seq if terminal_seq >= 0 else None,
            "status_before_kill": before_status or None,
        }
        timeline.append(f"{reason} (facts={facts})")
        raise CancelMissedWindowError(reason, facts)
    resp = await client.kill(cid)
    seq_note = f" (seq={trigger_seq})" if trigger_seq is not None else ""
    timeline.append(
        f"cancel_at fired at {trigger}{seq_note}: POST /conversations/{cid}/kill "
        f"(was {before_status or 'unknown'}, http {resp.get('http_status')})"
    )
    settled = await _wait_for_cancel_idle(
        client,
        cid,
        previous_status=before_status,
        timeout_s=min(timeout_s, 60.0),
    )
    timeline.append(f"cancel_at settled to stable {settled}; continuing scenario")


def _start_trigger_injector(
    client: ProductClient,
    cid: str,
    *,
    mid_run: list[dict[str, Any]],
    cancel_at: dict[str, Any] | None,
    pause_at: dict[str, Any] | None,
    timeline: list[str],
    hard_cap_s: float,
    declared_followup_seqs: list[int] | None,
    declared_followup_requires_revision: list[bool] | None,
) -> tuple[asyncio.Task[None] | None, _PauseOwnership | None]:
    pause_ownership = _PauseOwnership() if pause_at else None
    needed = (
        bool(mid_run) or _is_cancel_after_first_write(cancel_at) or _trigger_requested(pause_at)
    )
    if not needed:
        return None, pause_ownership
    task = asyncio.create_task(
        _inject_when_writing(
            client,
            cid,
            mid_run,
            cancel_at,
            pause_at,
            timeline,
            hard_cap_s,
            pause_ownership=pause_ownership,
            declared_seqs=declared_followup_seqs,
            declared_requires=declared_followup_requires_revision,
        )
    )
    return task, pause_ownership


async def _await_injector(
    injector: asyncio.Task[None] | None,
    cancel_at: dict[str, Any] | None,
    *,
    inactivity_s: float,
) -> None:
    if injector is None:
        return
    if _is_cancel_after_first_write(cancel_at) and not injector.done():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(injector),
                timeout=min(inactivity_s, 60.0),
            )
    if injector.done():
        injector.result()


async def _cancel_injector(injector: asyncio.Task[None] | None) -> None:
    if injector is None:
        return
    if not injector.done():
        injector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await injector
    elif not injector.cancelled():
        injector.result()


async def _drive_to_terminal(
    client: ProductClient,
    cid: str,
    *,
    autonomous: bool,
    mid_run: list[dict[str, Any]],
    cancel_at: dict[str, Any] | None = None,
    pause_at: dict[str, Any] | None = None,
    timeline: list[str],
    inactivity_s: float,
    hard_cap_s: float,
    clarification_answer: str | None = None,
    decision_answer: str | None = None,
    decisions: list[dict[str, Any]] | None = None,
    injected_user_seqs: list[int] | None = None,
    declared_followup_seqs: list[int] | None = None,
    declared_followup_requires_revision: list[bool] | None = None,
    min_seq: int | None = None,
) -> str:
    """Drive gates and triggers to this turn's terminal or bounded stop.

    ``min_seq`` excludes a stale terminal from an earlier follow-up cycle.
    A progressing hard-cap remains inconclusive and raises for INVALID_RUN.
    """
    injector, pause_ownership = _start_trigger_injector(
        client,
        cid,
        mid_run=mid_run,
        cancel_at=cancel_at,
        pause_at=pause_at,
        timeline=timeline,
        hard_cap_s=hard_cap_s,
        declared_followup_seqs=declared_followup_seqs,
        declared_followup_requires_revision=declared_followup_requires_revision,
    )
    control = _DriveControl(
        pause_ownership=pause_ownership,
        decisions=decisions if decisions is not None else [],
        injected_user_seqs=(injected_user_seqs if injected_user_seqs is not None else []),
    )

    try:
        while True:
            status = await _poll_drive_status(
                client,
                cid,
                autonomous=autonomous,
                inactivity_s=inactivity_s,
                hard_cap_s=hard_cap_s,
                min_seq=min_seq,
            )
            _raise_if_progressing_timeout(status, timeline, hard_cap_s=hard_cap_s)
            _retire_pause_ownership(status, control)
            handled, terminal = await _dispatch_drive_gate(
                client,
                cid,
                status,
                control,
                timeline,
                injector,
                inactivity_s=inactivity_s,
                hard_cap_s=hard_cap_s,
                clarification_answer=clarification_answer,
                decision_answer=decision_answer,
            )
            if terminal is not None:
                return terminal
            if handled:
                continue
            await _await_injector(
                injector,
                cancel_at,
                inactivity_s=inactivity_s,
            )
            timeline.append(f"reached terminal/stop status: {status}")
            return status
    finally:
        await _cancel_injector(injector)

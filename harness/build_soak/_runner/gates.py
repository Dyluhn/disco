"""Bounded plan, clarification, decision, and pause gate handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..adapters.disco_api import (
    AWAITING_PLAN_APPROVAL,
    AWAITING_USER_DECISION,
    AWAITING_USER_QUESTION,
    PAUSED_STATE,
    PROGRESSING_TIMEOUT,
    WAITING_FOR_CONFIRMATION,
    InconclusiveRunError,
)
from ..ports import ProductClient
from .common import (
    _GENERIC_CLARIFY_ANSWER,
    _MAX_CLARIFY,
    _MAX_DECISION,
    _MAX_GATES,
    _MAX_RESUMES,
)


class _PauseOwnership:
    """Coordinate the scenario-owned pause with the generic PAUSED driver.

    A lifecycle pause is issued from the background trigger watcher while the
    main driver polls the same conversation.  The main driver must observe that
    pause without consuming it as an actionless/user pause.  This state is local
    to one drive, so parallel conversations cannot interfere with each other.
    """

    def __init__(self) -> None:
        self.active = False
        self.observed = False
        self.consumed = False
        self.settled = asyncio.Event()


@dataclass
class _DriveControl:
    pause_ownership: _PauseOwnership | None
    decisions: list[dict[str, Any]]
    injected_user_seqs: list[int]
    gates: int = 0
    resumes: int = 0
    clarifies: int = 0
    decisions_done: int = 0


async def _poll_drive_status(
    client: ProductClient,
    cid: str,
    *,
    autonomous: bool,
    inactivity_s: float,
    hard_cap_s: float,
    min_seq: int | None,
) -> str:
    poll = client.poll_until_terminal if autonomous else client.poll_until_terminal_or_gate
    return await poll(
        cid,
        inactivity_s=inactivity_s,
        hard_cap_s=hard_cap_s,
        min_terminal_seq=min_seq,
    )


def _raise_if_progressing_timeout(status: str, timeline: list[str], *, hard_cap_s: float) -> None:
    if status != PROGRESSING_TIMEOUT:
        return
    timeline.append(
        "hard-cap reached while build was STILL PROGRESSING — "
        "inconclusive (INVALID_RUN, not a product failure)"
    )
    raise InconclusiveRunError(
        "terminal status not reached before the hard cap while the build "
        "was still actively progressing",
        {"stage": "terminal_wait", "hard_cap_s": hard_cap_s},
    )


def _record_injected_user_turn(
    client: ProductClient,
    cid: str,
    before_seq: int,
    sink: list[int],
) -> None:
    new_seq = client.latest_user_message_seq(cid)
    if new_seq > before_seq:
        sink.append(new_seq)


async def _handle_plan_gate(
    client: ProductClient,
    cid: str,
    status: str,
    control: _DriveControl,
    timeline: list[str],
    *,
    inactivity_s: float,
) -> bool:
    if status != AWAITING_PLAN_APPROVAL or control.gates >= _MAX_GATES:
        return False
    control.gates += 1
    await client.approve_plan(cid)
    timeline.append(f"approved plan (gate {control.gates})")
    await client.wait_until_status_leaves(
        cid,
        AWAITING_PLAN_APPROVAL,
        timeout_s=min(inactivity_s, 60.0),
    )
    return True


async def _handle_clarify_gate(
    client: ProductClient,
    cid: str,
    status: str,
    control: _DriveControl,
    timeline: list[str],
    *,
    inactivity_s: float,
    clarification_answer: str | None,
) -> bool:
    if (
        status not in (AWAITING_USER_QUESTION, WAITING_FOR_CONFIRMATION)
        or control.clarifies >= _MAX_CLARIFY
    ):
        return False
    control.clarifies += 1
    before_user_seq = client.latest_user_message_seq(cid)
    if status == WAITING_FOR_CONFIRMATION:
        await client.confirm(cid)
        timeline.append(f"confirmed pending action (clarify {control.clarifies}/{_MAX_CLARIFY})")
    else:
        answer = clarification_answer or _GENERIC_CLARIFY_ANSWER
        await client.send_followup(cid, answer, kind="message")
        timeline.append(
            f"answered clarifying question (clarify {control.clarifies}/{_MAX_CLARIFY}): {answer!r}"
        )
    await client.wait_until_status_leaves(
        cid,
        status,
        timeout_s=min(inactivity_s, 60.0),
    )
    _record_injected_user_turn(
        client,
        cid,
        before_user_seq,
        control.injected_user_seqs,
    )
    return True


async def _handle_decision_gate(
    client: ProductClient,
    cid: str,
    status: str,
    control: _DriveControl,
    timeline: list[str],
    *,
    inactivity_s: float,
    decision_answer: str | None,
) -> tuple[bool, str | None]:
    if status != AWAITING_USER_DECISION:
        return False, None
    if control.decisions_done >= _MAX_DECISION:
        timeline.append(
            f"AWAITING_USER_DECISION cap (_MAX_DECISION={_MAX_DECISION}) hit — "
            "releasing to honest classification (NOT auto-finishing)"
        )
        return True, status
    before_user_seq = client.latest_user_message_seq(cid)
    resolved = await client.resolve_decision(cid, preferred_option_id=decision_answer)
    if resolved is None:
        timeline.append(
            "AWAITING_USER_DECISION could NOT be auto-resolved (stale/invalid "
            "payload: gate no longer live or no valid option) — releasing to honest "
            "classification (NOT auto-finishing)"
        )
        return True, status
    control.decisions_done += 1
    control.decisions.append({**resolved, "attempt": control.decisions_done})
    timeline.append(
        f"auto-resolved user decision (decision {control.decisions_done}/{_MAX_DECISION}): "
        f"picked option {resolved['option_id']!r} for alternatives "
        f"{resolved['alternatives_id']!r}"
    )
    await client.wait_until_status_leaves(
        cid,
        AWAITING_USER_DECISION,
        timeout_s=min(inactivity_s, 60.0),
    )
    _record_injected_user_turn(
        client,
        cid,
        before_user_seq,
        control.injected_user_seqs,
    )
    return True, None


def _retire_pause_ownership(status: str, control: _DriveControl) -> None:
    ownership = control.pause_ownership
    if (
        status != PAUSED_STATE
        and ownership is not None
        and ownership.observed
        and not ownership.active
    ):
        ownership.consumed = True


async def _handle_owned_pause(
    status: str,
    control: _DriveControl,
    timeline: list[str],
    injector: asyncio.Task[None] | None,
    *,
    hard_cap_s: float,
) -> tuple[bool, str | None]:
    ownership = control.pause_ownership
    if status != PAUSED_STATE or ownership is None:
        return False, None
    if ownership.active:
        try:
            await asyncio.wait_for(
                ownership.settled.wait(),
                timeout=min(hard_cap_s, 125.0),
            )
        except TimeoutError:
            timeline.append(
                "scenario-owned pause did not settle within its bounded control "
                "window — releasing to honest classification"
            )
            return True, status
    if not ownership.observed or ownership.consumed:
        return False, None
    ownership.consumed = True
    if injector is not None and injector.done():
        injector.result()
    timeline.append("scenario-owned PAUSED state was resumed by the lifecycle injector")
    return True, None


async def _handle_resumable_pause(
    client: ProductClient,
    cid: str,
    status: str,
    control: _DriveControl,
    timeline: list[str],
    *,
    inactivity_s: float,
) -> tuple[bool, str | None]:
    if status != PAUSED_STATE or control.resumes >= _MAX_RESUMES:
        return False, None
    control.resumes += 1
    response = await client.resume(cid)
    http_status = int(response.get("http_status", 0))
    timeline.append(f"resumed PAUSED run (resume {control.resumes}, http {http_status})")
    if http_status >= 400:
        timeline.append("resume rejected (not resumable) — stopping")
        return True, status
    await client.wait_until_status_leaves(
        cid,
        PAUSED_STATE,
        timeout_s=min(inactivity_s, 60.0),
    )
    return True, None


async def _dispatch_drive_gate(
    client: ProductClient,
    cid: str,
    status: str,
    control: _DriveControl,
    timeline: list[str],
    injector: asyncio.Task[None] | None,
    *,
    inactivity_s: float,
    hard_cap_s: float,
    clarification_answer: str | None,
    decision_answer: str | None,
) -> tuple[bool, str | None]:
    if await _handle_plan_gate(
        client,
        cid,
        status,
        control,
        timeline,
        inactivity_s=inactivity_s,
    ):
        return True, None
    if await _handle_clarify_gate(
        client,
        cid,
        status,
        control,
        timeline,
        inactivity_s=inactivity_s,
        clarification_answer=clarification_answer,
    ):
        return True, None
    handled, terminal = await _handle_decision_gate(
        client,
        cid,
        status,
        control,
        timeline,
        inactivity_s=inactivity_s,
        decision_answer=decision_answer,
    )
    if handled:
        return True, terminal
    handled, terminal = await _handle_owned_pause(
        status,
        control,
        timeline,
        injector,
        hard_cap_s=hard_cap_s,
    )
    if handled:
        return True, terminal
    return await _handle_resumable_pause(
        client,
        cid,
        status,
        control,
        timeline,
        inactivity_s=inactivity_s,
    )

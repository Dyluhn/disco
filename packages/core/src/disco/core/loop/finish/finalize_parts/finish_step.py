"""Finish-step verify-on-finish disposition.

Sole owner of "what happens after the agent's resolved verify command runs
(or is reused)" policy for ``normalize_finish_step`` — pass / malformed
auto-strip / refuse-and-continue / cap-reached loud release. Extracted from
the ``if verify_cmd:`` block of ``_FinalizeMixin.normalize_finish_step`` in
``finalize.py``, which now delegates here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from ..common import (
    _FINISH_VERIFY_CAP,
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from .plan_verifier_preflight import preflight_failed_plan_verifier
from .verify_receipt import reuse_finish_verify_receipt

if TYPE_CHECKING:
    from ...ports import GateCounterPort, LoopEventPort

    class _LoopFacet(GateCounterPort, LoopEventPort, Protocol):
        """The loop capability this module uses: gate counters, the event log."""


async def _emit_malformed_strip_notice(loop: _LoopFacet, verify_cmd: str) -> None:
    # Broken CHECK, not a failed task → auto-strip and finish.
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"Your verify command `{verify_cmd}` is not runnable "
                    "(syntax error / command-not-found) — that is a broken "
                    "CHECK, not a failed task, so it is "
                    "being ignored and the "
                    "run is finishing. Next time pass a "
                    "valid shell command "
                    "if you want real verification.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )


async def _emit_verify_refusal(loop: _LoopFacet, verify_cmd: str) -> None:
    # Real failure: refuse + keep working (the forcing function).
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"You called finish, but the verify "
                    f"command `{verify_cmd}` "
                    "did not pass (see the result above). The task is NOT "
                    "complete. Fix what it surfaced, then "
                    "finish again — or "
                    "finish without a verify command if "
                    "the check itself is "
                    "wrong.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )


async def _emit_verify_cap_release(loop: _LoopFacet, verify_cmd: str) -> None:
    # Cap reached: LOUD release — don't grind forever on a gate the model
    # can't satisfy (mirrors the browser-verify valve). The failure stays
    # visible (status detail + reminder + summary).
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="finish_verify_release",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"The verify command `{verify_cmd}` has failed "
                    f"{loop._finish_verify_refusals} times. "
                    "Finishing anyway "
                    "so the run does not loop forever — "
                    "but the deliverable "
                    "may be incomplete. Note this clearly "
                    "in your summary.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )
    # ⚠ human-facing: surfaces in the UI as a warning chip so the user
    # knows the run finished with a failing check.
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "⚠ Finished despite the verification check failing "
                    f"{loop._finish_verify_refusals}× — "
                    "the deliverable may "
                    "be incomplete; review it."
                ),
            ),
        )
    )


async def resolve_finish_verify_disposition(
    loop: _LoopFacet,
    verify_cmd: str,
    events: list[Event],
    *,
    finish_verify_passed: Callable[[str], Awaitable[tuple[bool, bool]]],
    finish_dod_gate_passed: Callable[[], Awaitable[bool]],
) -> Disp | None:
    """Run the verify-on-finish disposition for a resolved, non-empty verify command.

    Returns the ``Disp`` the caller must return immediately with, or None
    when the caller should fall through to the DoD gate check and finish.
    """
    # An already-failing authoritative plan gate comes before repeated
    # optional model verification when no trusted repair has landed.
    if await preflight_failed_plan_verifier(events, finish_dod_gate_passed):
        return Disp.CONTINUE

    if await reuse_finish_verify_receipt(loop, verify_cmd, events):
        passed, malformed = True, False
    else:
        passed, malformed = await finish_verify_passed(verify_cmd)

    if passed:
        loop._finish_verify_refusals = 0  # reset streak on clean pass
        return None
    if malformed and loop._finish_verify_strips < 3:
        loop._finish_verify_strips += 1
        await _emit_malformed_strip_notice(loop, verify_cmd)
        return None  # fall through to finish
    if loop._finish_verify_refusals < _FINISH_VERIFY_CAP:
        loop._finish_verify_refusals += 1
        await _emit_verify_refusal(loop, verify_cmd)
        return Disp.CONTINUE

    await _emit_verify_cap_release(loop, verify_cmd)
    loop._finish_verify_refusals = 0
    return None  # fall through to finish

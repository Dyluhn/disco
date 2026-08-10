"""Finish-step advisory verify-on-finish disposition.

Sole owner of "what happens after the agent's resolved verify command runs
(or is reused)" policy for ``normalize_finish_step`` — pass / malformed /
failed advisory evidence. Extracted from
the ``if verify_cmd:`` block of ``_FinalizeMixin.normalize_finish_step`` in
``finalize.py``, which now delegates here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from ..common import (
    ConversationStatus,
    Disp,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from .verify_receipt import reuse_finish_verify_receipt

if TYPE_CHECKING:
    from ...ports import GateCounterPort, LoopEventPort

    class _LoopFacet(GateCounterPort, LoopEventPort, Protocol):
        """The loop capability this module uses: gate counters, the event log."""


async def _emit_malformed_strip_notice(loop: _LoopFacet, verify_cmd: str) -> None:
    # Broken check, not a failed task: ignore it and continue to host authority.
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
                    "being ignored. Host-owned acceptance checks still run. "
                    "Next time pass a "
                    "valid shell command "
                    "if you want real verification.\n"
                    "</system-reminder>"
                ),
            ),
        )
    )


async def _emit_advisory_verify_failure(loop: _LoopFacet, verify_cmd: str) -> None:
    """Record a model-owned check failure without turning it into authority."""
    await loop._emit(
        StatusEvent(
            status=ConversationStatus.RUNNING,
            detail="finish_verify_advisory_failed",
        )
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\n"
                    f"Your advisory verify command `{verify_cmd}` did not pass. "
                    "Its result is recorded as debugging evidence, but a model-authored "
                    "check cannot veto completion. The host-owned acceptance checks now "
                    "determine the final disposition.\n"
                    "</system-reminder>"
                ),
            ),
            meta={"finish_verify_advisory": True, "passed": False},
        )
    )


async def resolve_finish_verify_disposition(
    loop: _LoopFacet,
    verify_cmd: str,
    events: list[Event],
    *,
    finish_verify_passed: Callable[[str], Awaitable[tuple[bool, bool]]],
) -> Disp | None:
    """Run one advisory verify-on-finish command without granting it veto power.

    External DoD and the host-owned final verifier are the completion authorities.
    This model-authored command can add useful diagnostic evidence, but its result
    always falls through to those gates and can never create a repair loop.
    """
    if await reuse_finish_verify_receipt(loop, verify_cmd, events):
        passed, malformed = True, False
    else:
        passed, malformed = await finish_verify_passed(verify_cmd)

    if passed:
        loop._finish_verify_refusals = 0  # reset streak on clean pass
        return None
    if malformed:
        await _emit_malformed_strip_notice(loop, verify_cmd)
        return None
    await _emit_advisory_verify_failure(loop, verify_cmd)
    loop._finish_verify_refusals = 0
    return None

"""Passive finish diagnostics: historical shell commands are never replayed.

An unchanged source tree cannot prove a script or HTTP request safe to repeat.
These observations remain visible; immutable external DoD and typed host checks
retain acceptance authority, independently of model-authored shell diagnostics.
"""

from __future__ import annotations

from ...events import Event, EventSource, LLMMessage, MessageEvent
from ._shell_diagnostics import _already_reported, _shell_caveat
from .common import _FinishGateComponent


class _StaleChecksGateService(_FinishGateComponent):
    async def stale_checks_gate_passed(self, events: list[Event]) -> bool:
        """Continue to required host gates without executing historical commands."""
        self._loop._stale_check_refusals = 0
        caveat = _shell_caveat(events)
        if caveat is not None and not _already_reported(events, caveat):
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=caveat),
                    meta={"diagnostic": "stale_checks_gate", "shell_diagnostics_advisory": True},
                )
            )
        return True

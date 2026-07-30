"""Read/append provenance used by the ordered observation boundary."""

from __future__ import annotations

import logging
from typing import Any

from ..events import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
)

_LOG = logging.getLogger("disco.loop")


def _confirmed_and_failed_call_ids(events: list[Event]) -> tuple[set[str], set[str]]:
    confirmed = {
        event.tool_result.call_id
        for event in events
        if isinstance(event, ObservationEvent) and event.tool_result.success
    }
    failed = {
        event.tool_call_id
        for event in events
        if isinstance(event, AgentErrorEvent) and event.tool_call_id is not None
    }
    return confirmed, failed


def _matching_prior_action(
    events: list[Event],
    path: str,
    *,
    before_id: str | None,
    tool_name: str,
    confirmed: set[str],
    failed: set[str],
) -> str | None:
    seen_current = before_id is None
    for event in reversed(events):
        if not isinstance(event, ActionEvent) or event.tool_call is None:
            continue
        if not seen_current:
            seen_current = event.id == before_id
            continue
        if event.tool_call.tool_name != tool_name:
            continue
        if event.tool_call.arguments.get("path") != path:
            continue
        call_id = event.tool_call.call_id
        if call_id in confirmed and call_id not in failed:
            return call_id
    return None


def _has_confirmed_prior_call(
    events: list[Event],
    path: str,
    *,
    before_id: str | None,
    tool_name: str,
) -> bool:
    if not path:
        return False
    confirmed, failed = _confirmed_and_failed_call_ids(events)
    call_id = _matching_prior_action(
        events,
        path,
        before_id=before_id,
        tool_name=tool_name,
        confirmed=confirmed,
        failed=failed,
    )
    return call_id is not None


def _has_confirmed_prior_read(
    events: list[Event],
    path: str,
    *,
    before_id: str | None,
) -> bool:
    """Return whether the same path has a confirmed earlier ``file_read``."""
    return _has_confirmed_prior_call(
        events,
        path,
        before_id=before_id,
        tool_name="file_read",
    )


def _has_confirmed_prior_append(
    events: list[Event],
    path: str,
    *,
    before_id: str | None,
) -> bool:
    """Return whether the same path has a confirmed earlier ``file_append``."""
    return _has_confirmed_prior_call(
        events,
        path,
        before_id=before_id,
        tool_name="file_append",
    )


async def _redirect_marker_write_with_read_provenance(
    loop: Any,
    action: ActionEvent,
) -> bool:
    """Redirect a marker-only write when its bytes came from an earlier read."""
    assert action.tool_call is not None
    path = action.tool_call.arguments.get("path")
    if not isinstance(path, str) or not path:
        return False
    if not _has_confirmed_prior_read(
        await loop._events(),
        path,
        before_id=action.id,
    ):
        return False
    _LOG.info(
        "K1 redirect: file_write elision copy-back to %s of an elided READ result — "
        "redirect, no execute (call_id=%s)",
        path,
        action.tool_call.call_id,
    )
    await loop._emit(
        MessageEvent(
            source=EventSource.ENVIRONMENT,
            message=LLMMessage(
                role="user",
                content=(
                    "<system-reminder>\nYour last file_write `content` was the engine's"
                    " internal elision placeholder (a context-saving stand-in for content"
                    f" shown to you earlier), NOT real text. {path} was NOT modified — it"
                    f" still holds its actual current bytes. To revise it: file_read {path}"
                    " now, then send file_write with the COMPLETE revised content as real"
                    " text (never the placeholder).\n</system-reminder>"
                ),
            ),
        )
    )
    return True

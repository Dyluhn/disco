"""Shared lookup for the event a gate's own driven probe produced.

`finish_verify_passed`, `_drive_finish_browser_probe`, and `_drive_verify_web_app`
each emit one `ActionEvent` and then need the event correlated to it (robust
against a trailing sandbox-restart notice `_execute_and_observe` may append).
Previously each gate duplicated this exact fold — one shared reader here.
"""

from __future__ import annotations

from typing import Any

from ..common import Event


async def latest_event_for_action(loop: Any, action_id: str) -> Event | None:
    events_after = await loop._events()
    return next(
        (
            event
            for event in reversed(events_after)
            if getattr(event, "action_id", None) == action_id
        ),
        None,
    )

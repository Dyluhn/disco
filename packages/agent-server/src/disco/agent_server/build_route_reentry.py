"""Durable Build route restoration at run-entry boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Literal

from disco.core import (
    AppKitEjectionEvent,
    BuildPlatformAdmissionEvent,
    Event,
    current_build_platform_admission,
)
from disco.core.build_platform import (
    APPKIT_PROFILE_ID,
    FREEFORM_ARTIFACT_PROFILE_ID,
    FREEFORM_PROFILE_ID,
    ComponentId,
)

from .build_platform_shadow import BuildPlatformRouteRecord

BuildRoute = Literal["legacy", "platform"]


@dataclass(frozen=True, slots=True)
class RouteState:
    """In-memory route state reconstructed from durable Build authority."""

    record: BuildPlatformRouteRecord | None = None
    pin: BuildRoute | None = None
    pinned_profile: ComponentId | None = None
    selected_route: BuildRoute | None = None
    selected_profile: ComponentId | None = None
    recomposition_required: bool = False


EMPTY_ROUTE_STATE = RouteState()
_KNOWN_PROFILES = (FREEFORM_PROFILE_ID, FREEFORM_ARTIFACT_PROFILE_ID, APPKIT_PROFILE_ID)
_EventLoader = Callable[[str], Awaitable[list[Event]]]
_EjectionReader = Callable[[Iterable[Event]], AppKitEjectionEvent | None]
_RecordRestorer = Callable[
    [
        str,
        RouteState,
        BuildPlatformAdmissionEvent | None,
        ComponentId | None,
        list[Event],
    ],
    BuildPlatformRouteRecord | None,
]


async def restore_route_pin(
    conversation_id: str,
    *,
    state: RouteState,
    is_build_like: Callable[[str], bool],
    load_events: _EventLoader,
    restore_record: _RecordRestorer,
    read_ejection: _EjectionReader,
) -> tuple[RouteState, bool | None]:
    """Derive live route state and AppKit ejection state from durable events.

    The route pin is durable policy. A selected Platform route is usable only
    while its composition record proves the current admission. The optional
    boolean is the ejection state to record; ``None`` means the surface is not
    Build-like and the ejection ledger must remain untouched.
    """

    had_runtime_selection = any(
        (
            state.pin is not None,
            state.pinned_profile is not None,
            state.selected_route is not None,
            state.selected_profile is not None,
            state.record is not None,
            state.recomposition_required,
        )
    )
    if not is_build_like(conversation_id):
        return (
            replace(state, pin=None, pinned_profile=None, recomposition_required=False),
            None,
        )
    events = await load_events(conversation_id)
    admission = current_build_platform_admission(events)
    restored_profile = next(
        (
            profile
            for profile in _KNOWN_PROFILES
            if admission is not None and profile.canonical == admission.profile_id
        ),
        None,
    )
    reusable_record = restore_record(
        conversation_id,
        state,
        admission,
        restored_profile,
        events,
    )
    selection_ready = (
        admission is not None
        and restored_profile is not None
        and (admission.route == "legacy" or reusable_record is not None)
    )
    requires_recomposition = had_runtime_selection if admission is None else not selection_ready
    return (
        replace(
            state,
            pin=admission.route if admission is not None else None,
            pinned_profile=restored_profile,
            selected_route=(admission.route if admission is not None and selection_ready else None),
            selected_profile=restored_profile if selection_ready else None,
            record=reusable_record,
            recomposition_required=requires_recomposition,
        ),
        read_ejection(events) is not None,
    )

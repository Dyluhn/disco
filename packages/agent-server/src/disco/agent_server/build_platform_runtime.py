"""Runtime admission bridge for Build Platform Core.

The collaborator owns route selection and durable identity bookkeeping. It does
not execute component intents: the existing runtime/executor/workspace services
remain the only effect authorities.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from disco.core import (
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    StatusEvent,
    WorkspaceMutationEvent,
    current_build_platform_admission,
    latest_workspace_run_intent,
)
from disco.core.build_platform import (
    FREEFORM_PROFILE_ID,
    CompositionDigest,
    RunAdmissionAnchor,
    derive_run_admission_identity,
)

from .build_platform_shadow import (
    freeform_platform_route_enabled,
    select_freeform_platform_route,
)

BuildRoute = Literal["legacy", "platform"]


class BuildPlatformRuntime:
    """Per-runtime Platform route state, reconstructed from durable events."""

    def __init__(self, runtime: Any) -> None:
        self._rt = runtime
        self.route_records: dict[str, Any] = {}
        self.route_pins: dict[str, BuildRoute] = {}
        self.selected_routes: dict[str, BuildRoute] = {}

    async def prepare_route_pin(self, conversation_id: str) -> None:
        if self._rt._surface_of(conversation_id) not in self._rt._BUILD_LIKE_SURFACES:
            self.route_pins.pop(conversation_id, None)
            return
        admission = current_build_platform_admission(
            await self._rt._store.get_events(conversation_id)
        )
        if admission is None:
            self.route_pins.pop(conversation_id, None)
        else:
            self.route_pins[conversation_id] = admission.route

    def select_freeform(
        self,
        conversation_id: str,
        *,
        eligible: bool,
        tool_specs: Iterable[Any],
    ) -> None:
        if not eligible:
            self.selected_routes.pop(conversation_id, None)
            return
        pinned = self.route_pins.get(conversation_id)
        platform = pinned == "platform" or (pinned is None and freeform_platform_route_enabled())
        self.selected_routes[conversation_id] = "platform" if platform else "legacy"
        if not platform:
            self.route_records.pop(conversation_id, None)
            return
        try:
            self.route_records[conversation_id] = select_freeform_platform_route(
                tool_specs=tool_specs
            )
        except Exception:
            self._rt._executors.pop(conversation_id, None)
            self.route_records.pop(conversation_id, None)
            self.selected_routes.pop(conversation_id, None)
            raise

    async def record_route_locked(self, conversation_id: str) -> None:
        selected = self.selected_routes.get(conversation_id)
        if selected is None:
            return
        events = await self._rt._store.get_events(conversation_id)
        intent = latest_workspace_run_intent(events)
        released_seq = max(
            (
                event.seq
                for event in events
                if isinstance(event, StatusEvent)
                and type(event.seq) is int
                and event.status
                in {
                    ConversationStatus.FINISHED,
                    ConversationStatus.ERROR,
                    ConversationStatus.STUCK,
                    ConversationStatus.IDLE,
                }
            ),
            default=-1,
        )
        if intent is None or type(intent.seq) is not int or intent.seq <= released_seq:
            stored_intent = await self._rt._store.append(
                conversation_id,
                WorkspaceMutationEvent(
                    operation="agent.run-intent.runtime-kick",
                    run_protocol_version=1,
                ),
            )
            if not isinstance(stored_intent, WorkspaceMutationEvent):
                raise RuntimeError("event store returned a malformed run intent")
            intent = stored_intent
            events = [*events, stored_intent]
        intent_seq = intent.seq
        if type(intent_seq) is not int:
            raise RuntimeError("Build route intent has no canonical sequence")
        existing = next(
            (
                event
                for event in reversed(events)
                if isinstance(event, BuildPlatformAdmissionEvent)
                and event.run_intent_id == intent.id
            ),
            None,
        )
        if existing is not None:
            if existing.route != selected:
                raise RuntimeError("run intent already has a different Build route admission")
            return

        digest: str | None = None
        run_identity: str | None = None
        authority: Literal["legacy", "build_platform_core"] = "legacy"
        if selected == "platform":
            record = self.route_records.get(conversation_id)
            if record is None or not isinstance(record.composition_digest, str):
                raise RuntimeError("Platform route has no valid composition record")
            digest = record.composition_digest
            authority = "build_platform_core"
            run_identity = derive_run_admission_identity(
                CompositionDigest(digest=digest),
                RunAdmissionAnchor(
                    conversation_id=conversation_id,
                    run_intent_event_id=intent.id,
                    run_intent_seq=intent_seq,
                    run_intent_operation=intent.operation,
                ),
            ).value
        await self._rt._store.append(
            conversation_id,
            BuildPlatformAdmissionEvent(
                route=selected,
                profile_id=FREEFORM_PROFILE_ID.canonical,
                run_intent_id=intent.id,
                composition_authority=authority,
                composition_digest=digest,
                run_identity=run_identity,
            ),
        )

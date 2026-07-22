"""Runtime admission bridge for Build Platform Core.

The collaborator owns route selection and durable identity bookkeeping. It does
not execute component intents: the existing runtime/executor/workspace services
remain the only effect authorities.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from disco.core import (
    AppKitEjectionEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    StatusEvent,
    WorkspaceMutationEvent,
    current_appkit_ejection,
    current_build_platform_admission,
    latest_workspace_run_intent,
)
from disco.core.build_platform import (
    APPKIT_PROFILE_ID,
    FREEFORM_PROFILE_ID,
    ComponentId,
    CompositionDigest,
    RunAdmissionAnchor,
    derive_run_admission_identity,
)

from .appkit_ejection import AppKitEjectionService
from .build_platform_shadow import (
    appkit_platform_route_enabled,
    freeform_platform_route_enabled,
    select_appkit_platform_route,
    select_freeform_platform_route,
)

BuildRoute = Literal["legacy", "platform"]


def _record_verification_claims(record: Any | None) -> tuple[Any, ...]:
    if record is None:
        return ()
    return tuple(
        claim
        for check in record.composition.target_plan.verifier.checks
        if check.required
        for claim in check.claims
        if claim.required
    )


class BuildPlatformRuntime:
    """Per-runtime Platform route state, reconstructed from durable events."""

    def __init__(self, runtime: Any) -> None:
        self._rt = runtime
        self.route_records: dict[str, Any] = {}
        self.route_pins: dict[str, BuildRoute] = {}
        self.selected_routes: dict[str, BuildRoute] = {}
        self.selected_profiles: dict[str, ComponentId] = {}
        self.appkit_ejected: dict[str, bool] = {}
        self.ejection = AppKitEjectionService(runtime)

    async def prepare_route_pin(self, conversation_id: str) -> None:
        if self._rt._surface_of(conversation_id) not in self._rt._BUILD_LIKE_SURFACES:
            self.route_pins.pop(conversation_id, None)
            return
        events = await self._rt._store.get_events(conversation_id)
        self.appkit_ejected[conversation_id] = current_appkit_ejection(events) is not None
        admission = current_build_platform_admission(events)
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
            self.selected_profiles.pop(conversation_id, None)
            return
        pinned = self.route_pins.get(conversation_id)
        platform = pinned == "platform" or (pinned is None and freeform_platform_route_enabled())
        self.selected_routes[conversation_id] = "platform" if platform else "legacy"
        self.selected_profiles[conversation_id] = FREEFORM_PROFILE_ID
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
            self.selected_profiles.pop(conversation_id, None)
            raise

    def select_appkit(
        self,
        conversation_id: str,
        *,
        eligible: bool,
        tool_specs: Iterable[Any],
    ) -> None:
        if not eligible:
            self.selected_routes.pop(conversation_id, None)
            self.selected_profiles.pop(conversation_id, None)
            return
        pinned = self.route_pins.get(conversation_id)
        platform = pinned == "platform" or (pinned is None and appkit_platform_route_enabled())
        self.selected_routes[conversation_id] = "platform" if platform else "legacy"
        self.selected_profiles[conversation_id] = APPKIT_PROFILE_ID
        if not platform:
            self.route_records.pop(conversation_id, None)
            return
        try:
            self.route_records[conversation_id] = select_appkit_platform_route(
                tool_specs=tool_specs
            )
        except Exception:
            self._rt._executors.pop(conversation_id, None)
            self.route_records.pop(conversation_id, None)
            self.selected_routes.pop(conversation_id, None)
            self.selected_profiles.pop(conversation_id, None)
            raise

    def select_builtin(
        self,
        conversation_id: str,
        *,
        appkit: bool,
        eligible: bool,
        tool_specs: Iterable[Any],
    ) -> None:
        selector = self.select_appkit if appkit else self.select_freeform
        selector(
            conversation_id,
            eligible=eligible,
            tool_specs=tool_specs,
        )

    async def record_route_locked(self, conversation_id: str) -> None:
        selected = self.selected_routes.get(conversation_id)
        profile = self.selected_profiles.get(conversation_id)
        if selected is None or profile is None:
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
            if existing.route != selected or existing.profile_id != profile.canonical:
                raise RuntimeError("run intent already has a different Build admission")
            return

        digest: str | None = None
        run_identity: str | None = None
        authority: Literal["legacy", "build_platform_core"] = "legacy"
        verification_claims: tuple[Any, ...] = ()
        if selected == "platform":
            record = self.route_records.get(conversation_id)
            if record is None or not isinstance(record.composition_digest, str):
                raise RuntimeError("Platform route has no valid composition record")
            if record.composition.profile.id != profile:
                raise RuntimeError("Platform route resolved a different Build profile")
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
            verification_claims = _record_verification_claims(record)
        await self._rt._store.append(
            conversation_id,
            BuildPlatformAdmissionEvent(
                route=selected,
                profile_id=profile.canonical,
                run_intent_id=intent.id,
                composition_authority=authority,
                composition_digest=digest,
                run_identity=run_identity,
                verification_claims=verification_claims,
            ),
        )

    async def prepare_appkit_ejection_locked(
        self,
        conversation_id: str,
        *,
        tool_specs: Iterable[Any],
    ) -> Any | None:
        """Resolve the replacement composition before any revision is published."""

        if not self._rt.workspace_lock(conversation_id).locked():
            raise RuntimeError("AppKit ejection requires the workspace fence")
        events = await self._rt._store.get_events(conversation_id)
        intent = latest_workspace_run_intent(events)
        if intent is None or type(intent.seq) is not int:
            raise RuntimeError("AppKit ejection has no admitted Build run intent")
        existing = next(
            (
                event
                for event in reversed(events)
                if isinstance(event, BuildPlatformAdmissionEvent)
                and event.run_intent_id == intent.id
            ),
            None,
        )
        if existing is None or existing.profile_id != APPKIT_PROFILE_ID.canonical:
            raise RuntimeError("AppKit ejection requires the current AppKit admission")
        if existing.route == "legacy":
            return None
        record = select_freeform_platform_route(tool_specs=tool_specs)
        if record.composition.profile.id != FREEFORM_PROFILE_ID:
            raise RuntimeError("ejection resolved a non-Freeform composition")
        if not isinstance(record.composition_digest, str):
            raise RuntimeError("ejection composition has no valid digest")
        return record

    async def transition_appkit_ejection_locked(
        self,
        conversation_id: str,
        ejection: AppKitEjectionEvent,
        *,
        prepared_record: Any | None,
    ) -> AppKitEjectionEvent:
        """Atomically supersede the AppKit admission at the revision boundary."""

        if not self._rt.workspace_lock(conversation_id).locked():
            raise RuntimeError("AppKit ejection requires the workspace fence")
        events = await self._rt._store.get_events(conversation_id)
        intent = latest_workspace_run_intent(events)
        if intent is None or type(intent.seq) is not int:
            raise RuntimeError("AppKit ejection has no admitted Build run intent")
        existing = next(
            (
                event
                for event in reversed(events)
                if isinstance(event, BuildPlatformAdmissionEvent)
                and event.run_intent_id == intent.id
            ),
            None,
        )
        if existing is None or existing.profile_id != APPKIT_PROFILE_ID.canonical:
            raise RuntimeError("AppKit ejection requires the current AppKit admission")

        route = existing.route
        digest: str | None = None
        run_identity: str | None = None
        authority: Literal["legacy", "build_platform_core"] = "legacy"
        verification_claims: tuple[Any, ...] = ()
        record: Any | None = None
        if route == "platform":
            record = prepared_record
            if record is None:
                raise RuntimeError("Platform ejection has no prepared composition")
            if record.composition.profile.id != FREEFORM_PROFILE_ID:
                raise RuntimeError("ejection resolved a non-Freeform composition")
            digest = record.composition_digest
            if not isinstance(digest, str):
                raise RuntimeError("ejection composition has no valid digest")
            authority = "build_platform_core"
            run_identity = derive_run_admission_identity(
                CompositionDigest(digest=digest),
                RunAdmissionAnchor(
                    conversation_id=conversation_id,
                    run_intent_event_id=intent.id,
                    run_intent_seq=intent.seq,
                    run_intent_operation=intent.operation,
                ),
            ).value
            verification_claims = _record_verification_claims(record)
        replacement = BuildPlatformAdmissionEvent(
            route=route,
            profile_id=FREEFORM_PROFILE_ID.canonical,
            run_intent_id=intent.id,
            composition_authority=authority,
            composition_digest=digest,
            run_identity=run_identity,
            transition="appkit_ejection",
            supersedes_admission_id=existing.id,
            verification_claims=verification_claims,
        )
        stored = await self._rt._store.append_many(
            conversation_id,
            [ejection, replacement],
        )
        stored_ejection = stored[0]
        if not isinstance(stored_ejection, AppKitEjectionEvent):
            raise RuntimeError("event store returned a malformed AppKit ejection")
        self.route_pins[conversation_id] = route
        self.selected_routes[conversation_id] = route
        self.selected_profiles[conversation_id] = FREEFORM_PROFILE_ID
        if record is None:
            self.route_records.pop(conversation_id, None)
        else:
            self.route_records[conversation_id] = record
        self.appkit_ejected[conversation_id] = True
        return stored_ejection

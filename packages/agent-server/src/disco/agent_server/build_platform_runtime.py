"""Runtime admission and durable identity bridge for Build Platform Core."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from disco.core import (
    AppKitEjectionEvent,
    BuildPlatformAdmissionEvent,
    ConversationStatus,
    Event,
    StatusEvent,
    WorkspaceMutationEvent,
    current_appkit_ejection,
    current_build_platform_admission,
    latest_workspace_run_intent,
)
from disco.core.build_platform import (
    APPKIT_PROFILE_ID,
    FREEFORM_ARTIFACT_PROFILE_ID,
    FREEFORM_PROFILE_ID,
    ComponentId,
    CompositionDigest,
    RunAdmissionAnchor,
    derive_run_admission_identity,
)
from disco.core.llm import ToolSpec
from disco.core.store import EventStore
from disco.core.verification import AdmittedVerificationContract, HostVerificationClaim

from .appkit_ejection import AppKitEjectionLedger
from .build_platform_shadow import (
    BuildPlatformRouteRecord,
    appkit_platform_route_enabled,
    freeform_platform_route_enabled,
    select_appkit_platform_route,
    select_freeform_platform_route,
)
from .build_platform_verification import record_verification_contract
from .workspace_fence import WorkspaceFenceService

BuildRoute = Literal["legacy", "platform"]
CompositionAuthority = Literal["legacy", "build_platform_core"]
_FREEFORM_PROFILES = (FREEFORM_PROFILE_ID, FREEFORM_ARTIFACT_PROFILE_ID)
_KNOWN_PROFILES = (*_FREEFORM_PROFILES, APPKIT_PROFILE_ID)


class BuildSurfaceClassifier(Protocol):
    """Classify whether a conversation uses Build admission."""

    def is_build_like(self, conversation_id: str) -> bool: ...


class BuildRouteRollback(Protocol):
    """Discard a just-composed executor when route resolution fails closed."""

    def discard_composed_executor(self, conversation_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _RouteState:
    record: BuildPlatformRouteRecord | None = None
    pin: BuildRoute | None = None
    pinned_profile: ComponentId | None = None
    selected_route: BuildRoute | None = None
    selected_profile: ComponentId | None = None
    recomposition_required: bool = False


@dataclass(frozen=True, slots=True)
class _AdmissionFields:
    authority: CompositionAuthority
    digest: str | None
    run_identity: str | None
    verification_claims: tuple[HostVerificationClaim, ...]
    verification_contract: AdmittedVerificationContract | None


_EMPTY_ROUTE_STATE = _RouteState()
_LEGACY_ADMISSION = _AdmissionFields(
    authority="legacy",
    digest=None,
    run_identity=None,
    verification_claims=(),
    verification_contract=None,
)


def _released_sequence(events: Iterable[Event]) -> int:
    return max(
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


def _admission_for_intent(
    events: Iterable[Event],
    intent: WorkspaceMutationEvent,
) -> BuildPlatformAdmissionEvent | None:
    return next(
        (
            event
            for event in reversed(list(events))
            if isinstance(event, BuildPlatformAdmissionEvent) and event.run_intent_id == intent.id
        ),
        None,
    )


def _validate_existing_admission(
    existing: BuildPlatformAdmissionEvent,
    *,
    route: BuildRoute,
    profile: ComponentId,
    fields: _AdmissionFields,
) -> None:
    if existing.route != route or existing.profile_id != profile.canonical:
        raise RuntimeError("run intent already has a different Build admission")
    if (
        existing.composition_authority != fields.authority
        or existing.composition_digest != fields.digest
        or existing.run_identity != fields.run_identity
        or existing.verification_claims != fields.verification_claims
        or existing.verification_contract != fields.verification_contract
    ):
        raise RuntimeError("durable Build admission differs from the reconstructed composition")


def _reusable_platform_record(
    conversation_id: str,
    state: _RouteState,
    admission: BuildPlatformAdmissionEvent | None,
    restored_profile: ComponentId | None,
    events: list[Event],
) -> BuildPlatformRouteRecord | None:
    """Retain only a live composition that proves the complete durable admission."""

    record = state.record
    intent = latest_workspace_run_intent(events)
    if (
        admission is None
        or admission.route != "platform"
        or restored_profile is None
        or record is None
        or state.selected_route != admission.route
        or state.selected_profile != restored_profile
        or intent is None
        or admission.run_intent_id != intent.id
    ):
        return None
    try:
        fields = _platform_fields(record, restored_profile, intent, conversation_id)
        _validate_existing_admission(
            admission,
            route="platform",
            profile=restored_profile,
            fields=fields,
        )
    except (RuntimeError, ValueError):
        return None
    return record


def _platform_fields(
    record: BuildPlatformRouteRecord,
    profile: ComponentId,
    intent: WorkspaceMutationEvent,
    conversation_id: str,
) -> _AdmissionFields:
    if record.composition.profile.id != profile:
        raise RuntimeError("Platform route resolved a different Build profile")
    if type(intent.seq) is not int:
        raise RuntimeError("Build route intent has no canonical sequence")
    contract = record_verification_contract(record)
    identity = derive_run_admission_identity(
        CompositionDigest(digest=record.composition_digest),
        RunAdmissionAnchor(
            conversation_id=conversation_id,
            run_intent_event_id=intent.id,
            run_intent_seq=intent.seq,
            run_intent_operation=intent.operation,
        ),
    ).value
    return _AdmissionFields(
        authority="build_platform_core",
        digest=record.composition_digest,
        run_identity=identity,
        verification_claims=contract.required_claims if contract is not None else (),
        verification_contract=contract,
    )


def _transition_fields(
    conversation_id: str,
    intent: WorkspaceMutationEvent,
    route: BuildRoute,
    prepared_record: BuildPlatformRouteRecord | None,
) -> tuple[_AdmissionFields, BuildPlatformRouteRecord | None]:
    if route == "legacy":
        return _LEGACY_ADMISSION, None
    if prepared_record is None:
        raise RuntimeError("Platform ejection has no prepared composition")
    if prepared_record.composition.profile.id != FREEFORM_PROFILE_ID:
        raise RuntimeError("ejection resolved a non-Freeform composition")
    if not isinstance(prepared_record.composition_digest, str):
        raise RuntimeError("ejection composition has no valid digest")
    fields = _platform_fields(
        prepared_record,
        FREEFORM_PROFILE_ID,
        intent,
        conversation_id,
    )
    return fields, prepared_record


def _restored_delivery_kind(state: _RouteState) -> Literal["app", "files"] | None:
    if state.pin is None:
        return None
    profile = state.selected_profile or state.pinned_profile
    if profile == FREEFORM_ARTIFACT_PROFILE_ID:
        return "files"
    return "app" if profile == FREEFORM_PROFILE_ID else None


def _select_freeform_runtime(
    runtime: BuildPlatformRuntime,
    conversation_id: str,
    *,
    eligible: bool,
    tool_specs: Iterable[ToolSpec],
    delivery_kind: Literal["app", "files"] | None,
) -> None:
    if not eligible:
        state = runtime._state(conversation_id)
        runtime._save(
            conversation_id,
            replace(
                state,
                selected_route=None,
                selected_profile=None,
                recomposition_required=False,
            ),
        )
        return
    state = runtime._state(conversation_id)
    restored_kind = _restored_delivery_kind(state)
    if delivery_kind is not None and restored_kind is not None and delivery_kind != restored_kind:
        raise RuntimeError("declared delivery differs from the durable Build admission")
    # Before an artifact exists, retain the historical web contract as a
    # fail-closed provisional floor. This is not an artifact classification:
    # serve derives the real kind and atomically replaces this target before it
    # emits a deliverable or completion can use that handoff.
    effective_delivery = delivery_kind or restored_kind or "app"
    platform = state.pin == "platform" or (
        state.pin is None and freeform_platform_route_enabled()
    )
    route: BuildRoute = "platform" if platform else "legacy"
    selected_profile = (
        FREEFORM_ARTIFACT_PROFILE_ID if effective_delivery == "files" else FREEFORM_PROFILE_ID
    )
    if not platform:
        runtime._set_selection(
            conversation_id,
            route=route,
            profile=selected_profile,
            record=None,
        )
        return
    try:
        record = select_freeform_platform_route(
            tool_specs=tool_specs,
            delivery_kind=effective_delivery,
        )
    except Exception:
        runtime._selection_failed(conversation_id)
        raise
    runtime._set_selection(
        conversation_id,
        route=route,
        profile=record.composition.profile.id,
        record=record,
    )


async def _delivery_selection_authority(
    runtime: BuildPlatformRuntime,
    conversation_id: str,
) -> tuple[WorkspaceMutationEvent, BuildPlatformAdmissionEvent]:
    if not runtime._workspace.lock(conversation_id).locked():
        raise RuntimeError("delivery selection requires the workspace fence")
    if not runtime._workspace.fence_owned_by_current_task(conversation_id):
        raise RuntimeError("delivery selection requires current-task fence ownership")
    runtime._workspace.require_process_fence_locked(conversation_id)
    events = await runtime._store.get_events(conversation_id)
    intent = latest_workspace_run_intent(events)
    admission = current_build_platform_admission(events)
    if (
        intent is None
        or type(intent.seq) is not int
        or admission is None
        or admission.run_intent_id != intent.id
    ):
        raise RuntimeError("delivery selection has no current Build admission")
    return intent, admission


def _admitted_delivery_kind(
    admission: BuildPlatformAdmissionEvent,
) -> Literal["app", "files"] | None:
    contract = admission.verification_contract
    if contract is None:
        return None
    return "app" if contract.delivery.mode == "interactive" else "files"


async def _bind_delivery_runtime(
    runtime: BuildPlatformRuntime,
    conversation_id: str,
    delivery_kind: Literal["app", "files"],
    *,
    tool_specs: Iterable[ToolSpec],
) -> BuildPlatformAdmissionEvent | None:
    intent, admission = await _delivery_selection_authority(runtime, conversation_id)
    if admission.route == "legacy":
        return admission
    if admission.profile_id not in {item.canonical for item in _FREEFORM_PROFILES}:
        return admission
    target_profile = (
        FREEFORM_ARTIFACT_PROFILE_ID if delivery_kind == "files" else FREEFORM_PROFILE_ID
    )
    try:
        record = select_freeform_platform_route(
            tool_specs=tool_specs,
            delivery_kind=delivery_kind,
        )
    except Exception:
        runtime._selection_failed(conversation_id)
        raise
    fields = _platform_fields(record, target_profile, intent, conversation_id)
    if (
        admission.profile_id == target_profile.canonical
        and _admitted_delivery_kind(admission) == delivery_kind
    ):
        _validate_existing_admission(
            admission,
            route="platform",
            profile=target_profile,
            fields=fields,
        )
        runtime._set_selection(
            conversation_id,
            route="platform",
            profile=target_profile,
            record=record,
        )
        return admission
    replacement = BuildPlatformAdmissionEvent(
        route="platform",
        profile_id=target_profile.canonical,
        run_intent_id=intent.id,
        composition_authority=fields.authority,
        composition_digest=fields.digest,
        run_identity=fields.run_identity,
        transition="delivery_selection",
        supersedes_admission_id=admission.id,
        verification_claims=fields.verification_claims,
        verification_contract=fields.verification_contract,
    )
    stored = await runtime._store.append(conversation_id, replacement)
    if not isinstance(stored, BuildPlatformAdmissionEvent):
        raise RuntimeError("event store returned a malformed delivery selection")
    runtime._set_selection(
        conversation_id,
        route="platform",
        profile=target_profile,
        record=record,
    )
    return stored


class BuildPlatformRuntime:
    """Per-runtime Platform route state, reconstructed from durable events."""

    def __init__(
        self,
        *,
        store: EventStore,
        workspace: WorkspaceFenceService,
        surfaces: BuildSurfaceClassifier,
        rollback: BuildRouteRollback,
        ejections: AppKitEjectionLedger,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._surfaces = surfaces
        self._rollback = rollback
        self._ejections = ejections
        self._states: dict[str, _RouteState] = {}

    def _state(self, conversation_id: str) -> _RouteState:
        return self._states.get(conversation_id, _EMPTY_ROUTE_STATE)

    def _save(self, conversation_id: str, state: _RouteState) -> None:
        if state == _EMPTY_ROUTE_STATE:
            self._states.pop(conversation_id, None)
        else:
            self._states[conversation_id] = state

    @property
    def route_records(self) -> dict[str, BuildPlatformRouteRecord]:
        return {
            conversation_id: state.record
            for conversation_id, state in self._states.items()
            if state.record is not None
        }

    @property
    def route_pins(self) -> dict[str, BuildRoute]:
        return {
            conversation_id: state.pin
            for conversation_id, state in self._states.items()
            if state.pin is not None
        }

    @property
    def selected_routes(self) -> dict[str, BuildRoute]:
        return {
            conversation_id: state.selected_route
            for conversation_id, state in self._states.items()
            if state.selected_route is not None
        }

    @property
    def selected_profiles(self) -> dict[str, ComponentId]:
        return {
            conversation_id: state.selected_profile
            for conversation_id, state in self._states.items()
            if state.selected_profile is not None
        }

    async def prepare_route_pin(self, conversation_id: str) -> bool:
        """Restore the durable pin and report whether the loop must recompose.

        A route pin is durable policy.  A selected Platform route is usable
        runtime state only while its composition record proves the current
        admission.  Keeping those states distinct prevents a new run intent
        from inheriting a Platform selection with no composition authority.
        """

        state = self._state(conversation_id)
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
        if not self._surfaces.is_build_like(conversation_id):
            self._save(
                conversation_id,
                replace(state, pin=None, pinned_profile=None, recomposition_required=False),
            )
            return False
        events = await self._store.get_events(conversation_id)
        admission = current_build_platform_admission(events)
        restored_profile = next(
            (
                profile
                for profile in _KNOWN_PROFILES
                if admission is not None and profile.canonical == admission.profile_id
            ),
            None,
        )
        reusable_record = _reusable_platform_record(
            conversation_id,
            state,
            admission,
            restored_profile,
            events,
        )
        self._ejections.record(
            conversation_id,
            ejected=current_appkit_ejection(events) is not None,
        )
        selection_ready = (
            admission is not None
            and restored_profile is not None
            and (admission.route == "legacy" or reusable_record is not None)
        )
        requires_recomposition = (
            had_runtime_selection if admission is None else not selection_ready
        )
        self._save(
            conversation_id,
            replace(
                state,
                pin=admission.route if admission is not None else None,
                pinned_profile=restored_profile,
                selected_route=admission.route if selection_ready else None,
                selected_profile=restored_profile if selection_ready else None,
                record=reusable_record,
                recomposition_required=requires_recomposition,
            ),
        )
        return requires_recomposition

    def _set_selection(
        self,
        conversation_id: str,
        *,
        route: BuildRoute | None,
        profile: ComponentId | None,
        record: BuildPlatformRouteRecord | None,
    ) -> None:
        self._save(
            conversation_id,
            replace(
                self._state(conversation_id),
                selected_route=route,
                selected_profile=profile,
                record=record,
                recomposition_required=False,
            ),
        )

    def _selection_failed(self, conversation_id: str) -> None:
        self._rollback.discard_composed_executor(conversation_id)
        self._set_selection(conversation_id, route=None, profile=None, record=None)

    def select_freeform(
        self,
        conversation_id: str,
        *,
        eligible: bool,
        tool_specs: Iterable[ToolSpec],
        delivery_kind: Literal["app", "files"] | None = None,
    ) -> None:
        _select_freeform_runtime(
            self,
            conversation_id,
            eligible=eligible,
            tool_specs=tool_specs,
            delivery_kind=delivery_kind,
        )

    def select_appkit(
        self,
        conversation_id: str,
        *,
        eligible: bool,
        tool_specs: Iterable[ToolSpec],
    ) -> None:
        if not eligible:
            state = self._state(conversation_id)
            self._save(
                conversation_id,
                replace(
                    state,
                    selected_route=None,
                    selected_profile=None,
                    recomposition_required=False,
                ),
            )
            return
        state = self._state(conversation_id)
        platform = state.pin == "platform" or (
            state.pin is None and appkit_platform_route_enabled()
        )
        route: BuildRoute = "platform" if platform else "legacy"
        if not platform:
            self._set_selection(
                conversation_id,
                route=route,
                profile=APPKIT_PROFILE_ID,
                record=None,
            )
            return
        try:
            record = select_appkit_platform_route(tool_specs=tool_specs)
        except Exception:
            self._selection_failed(conversation_id)
            raise
        self._set_selection(
            conversation_id,
            route=route,
            profile=APPKIT_PROFILE_ID,
            record=record,
        )

    def select_builtin(
        self,
        conversation_id: str,
        *,
        appkit: bool,
        eligible: bool,
        tool_specs: Iterable[ToolSpec],
        delivery_kind: Literal["app", "files"] | None = None,
    ) -> None:
        if appkit:
            self.select_appkit(
                conversation_id,
                eligible=eligible,
                tool_specs=tool_specs,
            )
        else:
            self.select_freeform(
                conversation_id,
                eligible=eligible,
                tool_specs=tool_specs,
                delivery_kind=delivery_kind,
            )

    async def _current_intent(
        self,
        conversation_id: str,
        events: list[Event],
    ) -> tuple[WorkspaceMutationEvent, list[Event]]:
        intent = latest_workspace_run_intent(events)
        if (
            intent is not None
            and type(intent.seq) is int
            and intent.seq > _released_sequence(events)
        ):
            return intent, events
        stored = await self._store.append(
            conversation_id,
            WorkspaceMutationEvent(
                operation="agent.run-intent.runtime-kick",
                run_protocol_version=1,
            ),
        )
        if not isinstance(stored, WorkspaceMutationEvent):
            raise RuntimeError("event store returned a malformed run intent")
        if type(stored.seq) is not int:
            raise RuntimeError("Build route intent has no canonical sequence")
        return stored, [*events, stored]

    def _selected_admission_fields(
        self,
        conversation_id: str,
        profile: ComponentId,
        intent: WorkspaceMutationEvent,
    ) -> _AdmissionFields:
        state = self._state(conversation_id)
        if state.selected_route != "platform":
            return _LEGACY_ADMISSION
        if state.record is None or not isinstance(state.record.composition_digest, str):
            raise RuntimeError("Platform route has no valid composition record")
        return _platform_fields(state.record, profile, intent, conversation_id)

    async def record_route_locked(self, conversation_id: str) -> None:
        state = self._state(conversation_id)
        route = state.selected_route
        profile = state.selected_profile
        if route is None or profile is None:
            if state.recomposition_required:
                raise RuntimeError("Build route requires recomposition before admission")
            return
        events = await self._store.get_events(conversation_id)
        intent, events = await self._current_intent(conversation_id, events)
        existing = _admission_for_intent(events, intent)
        fields = self._selected_admission_fields(conversation_id, profile, intent)
        if existing is not None:
            try:
                _validate_existing_admission(
                    existing,
                    route=route,
                    profile=profile,
                    fields=fields,
                )
            except Exception:
                self._selection_failed(conversation_id)
                raise
            return
        await self._store.append(
            conversation_id,
            BuildPlatformAdmissionEvent(
                route=route,
                profile_id=profile.canonical,
                run_intent_id=intent.id,
                composition_authority=fields.authority,
                composition_digest=fields.digest,
                run_identity=fields.run_identity,
                verification_claims=fields.verification_claims,
                verification_contract=fields.verification_contract,
            ),
        )

    async def bind_delivery_locked(
        self,
        conversation_id: str,
        delivery_kind: Literal["app", "files"],
        *,
        tool_specs: Iterable[ToolSpec],
    ) -> BuildPlatformAdmissionEvent | None:
        """Resolve and durably bind the target selected by host artifact evidence."""
        return await _bind_delivery_runtime(
            self,
            conversation_id,
            delivery_kind,
            tool_specs=tool_specs,
        )

    async def prepare_appkit_ejection_locked(
        self,
        conversation_id: str,
        *,
        tool_specs: Iterable[ToolSpec],
    ) -> BuildPlatformRouteRecord | None:
        """Resolve the replacement composition before any revision is published."""

        if not self._workspace.lock(conversation_id).locked():
            raise RuntimeError("AppKit ejection requires the workspace fence")
        events = await self._store.get_events(conversation_id)
        intent = latest_workspace_run_intent(events)
        if intent is None or type(intent.seq) is not int:
            raise RuntimeError("AppKit ejection has no admitted Build run intent")
        existing = _admission_for_intent(events, intent)
        if existing is None or existing.profile_id != APPKIT_PROFILE_ID.canonical:
            raise RuntimeError("AppKit ejection requires the current AppKit admission")
        if existing.route == "legacy":
            return None
        record = select_freeform_platform_route(tool_specs=tool_specs, delivery_kind="app")
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
        prepared_record: BuildPlatformRouteRecord | None,
    ) -> AppKitEjectionEvent:
        """Atomically supersede the AppKit admission at the revision boundary."""

        if not self._workspace.lock(conversation_id).locked():
            raise RuntimeError("AppKit ejection requires the workspace fence")
        events = await self._store.get_events(conversation_id)
        intent = latest_workspace_run_intent(events)
        if intent is None or type(intent.seq) is not int:
            raise RuntimeError("AppKit ejection has no admitted Build run intent")
        existing = _admission_for_intent(events, intent)
        if existing is None or existing.profile_id != APPKIT_PROFILE_ID.canonical:
            raise RuntimeError("AppKit ejection requires the current AppKit admission")
        fields, record = _transition_fields(
            conversation_id,
            intent,
            existing.route,
            prepared_record,
        )
        replacement = BuildPlatformAdmissionEvent(
            route=existing.route,
            profile_id=FREEFORM_PROFILE_ID.canonical,
            run_intent_id=intent.id,
            composition_authority=fields.authority,
            composition_digest=fields.digest,
            run_identity=fields.run_identity,
            transition="appkit_ejection",
            supersedes_admission_id=existing.id,
            verification_claims=fields.verification_claims,
            verification_contract=fields.verification_contract,
        )
        stored = await self._store.append_many(conversation_id, [ejection, replacement])
        stored_ejection = stored[0]
        if not isinstance(stored_ejection, AppKitEjectionEvent):
            raise RuntimeError("event store returned a malformed AppKit ejection")
        self._ejections.record(conversation_id, ejected=True)
        self._save(
            conversation_id,
            _RouteState(
                record=record,
                pin=existing.route,
                pinned_profile=FREEFORM_PROFILE_ID,
                selected_route=existing.route,
                selected_profile=FREEFORM_PROFILE_ID,
            ),
        )
        return stored_ejection

"""Runtime admission bridge for Build Platform Core.

The collaborator owns route selection and durable identity bookkeeping. It does
not execute component intents: the existing executor and workspace services
remain the only effect authorities.
"""

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
    FREEFORM_PROFILE_ID,
    ComponentId,
    CompositionDigest,
    RunAdmissionAnchor,
    derive_run_admission_identity,
)
from disco.core.llm import ToolSpec
from disco.core.store import EventStore
from disco.core.verification import (
    AdmittedVerificationContract,
    HostVerificationClaim,
    VerificationCheckContract,
    VerificationDeliveryContract,
    VerificationParameter,
)

from .build_platform_shadow import (
    BuildPlatformRouteRecord,
    appkit_platform_route_enabled,
    freeform_platform_route_enabled,
    select_appkit_platform_route,
    select_freeform_platform_route,
)
from .workspace_service import WorkspaceCoordinator

BuildRoute = Literal["legacy", "platform"]
CompositionAuthority = Literal["legacy", "build_platform_core"]


class BuildSurfaceClassifier(Protocol):
    """Classify whether a conversation uses Build admission."""

    def is_build_like(self, conversation_id: str) -> bool: ...


class BuildRouteRollback(Protocol):
    """Discard a just-composed executor when route resolution fails closed."""

    def discard_composed_executor(self, conversation_id: str) -> None: ...


class AppKitEjectionLedger:
    """Live process cache reconstructed from durable ejection events."""

    def __init__(self) -> None:
        self._ejected: set[str] = set()

    def is_appkit_ejected(self, conversation_id: str) -> bool:
        return conversation_id in self._ejected

    def record(self, conversation_id: str, *, ejected: bool) -> None:
        if ejected:
            self._ejected.add(conversation_id)
        else:
            self._ejected.discard(conversation_id)


@dataclass(frozen=True, slots=True)
class _RouteState:
    record: BuildPlatformRouteRecord | None = None
    pin: BuildRoute | None = None
    selected_route: BuildRoute | None = None
    selected_profile: ComponentId | None = None


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


def _record_verification_contract(
    record: BuildPlatformRouteRecord | None,
) -> AdmittedVerificationContract | None:
    if record is None:
        return None
    composition = record.composition
    target_plan = composition.target_plan
    verifier_plan = target_plan.verifier
    delivery = target_plan.delivery
    return AdmittedVerificationContract(
        target_id=target_plan.target.canonical,
        verifier_id=composition.profile.verifier.canonical,
        delivery=VerificationDeliveryContract(
            shape=delivery.shape,
            mode=delivery.mode,
            entry_kind=delivery.entry.kind,
            entry_reference=delivery.entry.reference,
            entry_parameters=tuple(
                VerificationParameter(name=parameter.name, value=parameter.value)
                for parameter in delivery.entry.parameters
            ),
        ),
        preview_modality=target_plan.preview.modality,
        checks=tuple(
            VerificationCheckContract(
                check_id=check.check_id,
                receipt_kind=check.receipt_kind,
                issuer_id=(
                    check.issuer.canonical
                    if check.issuer is not None
                    else composition.profile.verifier.canonical
                ),
                operation=check.intent.operation,
                required_execution_modality=check.required_execution_modality,
                required_artifact_identity_scheme=check.required_artifact_identity_scheme,
                required=check.required,
                delegated_issuer_ids=frozenset(
                    issuer.canonical for issuer in check.delegated_issuers
                ),
                accepted_claim_kinds=(
                    check.accepted_claim_kinds or frozenset(claim.kind for claim in check.claims)
                ),
                claims=check.claims,
            )
            for check in verifier_plan.checks
        ),
        required=verifier_plan.policy.required,
        unavailable=verifier_plan.policy.unavailable,
        unverified_finish=verifier_plan.policy.unverified_finish,
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
) -> None:
    if existing.route != route or existing.profile_id != profile.canonical:
        raise RuntimeError("run intent already has a different Build admission")


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
    contract = _record_verification_contract(record)
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


class BuildPlatformRuntime:
    """Per-runtime Platform route state, reconstructed from durable events."""

    def __init__(
        self,
        *,
        store: EventStore,
        workspace: WorkspaceCoordinator,
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

    async def prepare_route_pin(self, conversation_id: str) -> None:
        state = self._state(conversation_id)
        if not self._surfaces.is_build_like(conversation_id):
            self._save(conversation_id, replace(state, pin=None))
            return
        events = await self._store.get_events(conversation_id)
        admission = current_build_platform_admission(events)
        self._ejections.record(
            conversation_id,
            ejected=current_appkit_ejection(events) is not None,
        )
        self._save(
            conversation_id,
            replace(
                state,
                pin=admission.route if admission is not None else None,
            ),
        )

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
    ) -> None:
        if not eligible:
            state = self._state(conversation_id)
            self._save(
                conversation_id,
                replace(state, selected_route=None, selected_profile=None),
            )
            return
        state = self._state(conversation_id)
        platform = state.pin == "platform" or (
            state.pin is None and freeform_platform_route_enabled()
        )
        route: BuildRoute = "platform" if platform else "legacy"
        if not platform:
            self._set_selection(
                conversation_id,
                route=route,
                profile=FREEFORM_PROFILE_ID,
                record=None,
            )
            return
        try:
            record = select_freeform_platform_route(tool_specs=tool_specs)
        except Exception:
            self._selection_failed(conversation_id)
            raise
        self._set_selection(
            conversation_id,
            route=route,
            profile=FREEFORM_PROFILE_ID,
            record=record,
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
                replace(state, selected_route=None, selected_profile=None),
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
            return
        events = await self._store.get_events(conversation_id)
        intent, events = await self._current_intent(conversation_id, events)
        existing = _admission_for_intent(events, intent)
        if existing is not None:
            _validate_existing_admission(existing, route=route, profile=profile)
            return
        fields = self._selected_admission_fields(conversation_id, profile, intent)
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
                selected_route=existing.route,
                selected_profile=FREEFORM_PROFILE_ID,
            ),
        )
        return stored_ejection

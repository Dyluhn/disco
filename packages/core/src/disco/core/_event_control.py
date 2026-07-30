"""Control events — lifecycle, workspace, and platform admission state.

These models carry status transitions, workspace versioning/restoration/
mutation, build-platform admission, and AppKit ejection.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from ._event_types import (
    APPKIT_EJECTION_LOST_GUARANTEES,
    AdmittedVerificationContract,
    BaseEvent,
    ConversationStatus,
    EventKind,
    EventSource,
    FinalWorkspaceSeal,
    HostVerificationClaim,
    PlanVerificationTransition,
    PlanVerifierFailure,
    PlanVerifierPass,
    RecoveryLeaseTransition,
)


class StatusEvent(BaseEvent):
    """A lifecycle/status transition. NOT LLMConvertible. Drives the UI and the
    loop's state machine reconstruction (§3)."""

    kind: Literal[EventKind.STATUS] = EventKind.STATUS
    source: EventSource = EventSource.SYSTEM
    status: ConversationStatus
    detail: str | None = None
    # A run may fail before it has materialized a model view (driver/sandbox
    # preflight).  Bind that status to the exact durable ingress instead of
    # emitting an unowned ERROR that an older worker could append over a newer
    # run.  Once a view exists, ``agent_view_id`` is the authority instead.
    run_intent_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Host-owned reseals use a separate, explicit authority edge instead of an
    # untagged FINISHED that could be confused with late agent output.
    host_mutation_id: str | None = Field(default=None, min_length=1, max_length=128)
    plan_verification_transition: PlanVerificationTransition | None = None
    plan_verifier_failure: PlanVerifierFailure | None = None
    plan_verifier_pass: PlanVerifierPass | None = None
    # K4 typed recovery state. Historical status events remain valid; ``detail``
    # stays as the legacy UI/debug surface until shadow parity permits removal.
    recovery_lease_transition: RecoveryLeaseTransition | None = None

    @model_validator(mode="after")
    def _one_terminal_authority(self) -> StatusEvent:
        authorities = (
            self.agent_view_id,
            self.host_mutation_id,
            self.run_intent_id,
        )
        if sum(authority is not None for authority in authorities) > 1:
            raise ValueError(
                "status cannot carry more than one of agent-view, host-mutation, "
                "or run-intent authority"
            )
        if self.plan_verifier_pass is not None and (
            self.status != ConversationStatus.RUNNING or self.detail != "plan_verification_passed"
        ):
            raise ValueError("plan_verifier_pass requires RUNNING/plan_verification_passed")
        if self.detail == "plan_verification_passed" and self.plan_verifier_pass is None:
            raise ValueError("RUNNING/plan_verification_passed requires a typed plan_verifier_pass")
        return self


class WorkspaceVersionEvent(BaseEvent):
    """A durable workspace version finished persisting.

    ``FINISHED`` is appended by the loop before the runtime copies the sandbox
    workspace into project storage.  Consumers must therefore use this event,
    rather than the terminal status, as the commit signal for version history.
    NOT LLMConvertible — this is storage/UI synchronization bookkeeping.
    """

    kind: Literal[EventKind.WORKSPACE_VERSION] = EventKind.WORKSPACE_VERSION
    source: EventSource = EventSource.SYSTEM
    version_seq: int
    tree_digest: str
    trigger: str
    # K6 final-state proof. Historical events remain valid with no seal; strict
    # promotion requires this field only after the lifecycle/store migration.
    final_seal: FinalWorkspaceSeal | None = None

    @model_validator(mode="after")
    def _seal_matches_version_event(self) -> WorkspaceVersionEvent:
        seal = self.final_seal
        if seal is None:
            return self
        if self.trigger != "finish":
            raise ValueError("a final workspace seal requires a finish-triggered version event")
        if seal.version_seq != self.version_seq:
            raise ValueError("final seal version sequence does not match event")
        if seal.tree_digest != self.tree_digest:
            raise ValueError("final seal tree digest does not match event")
        if self.seq is not None and seal.terminal_seq >= self.seq:
            raise ValueError("final seal terminal sequence must precede version event")
        return self


class WorkspaceRestoredEvent(BaseEvent):
    """A user-requested workspace rollback was applied.

    NOT LLMConvertible — this is audit/UI bookkeeping. The restored files live in
    the workspace/version store; the model sees the current workspace through the
    fresh per-turn workspace snapshot instead of this event body.
    """

    kind: Literal[EventKind.WORKSPACE_RESTORED] = EventKind.WORKSPACE_RESTORED
    source: EventSource = EventSource.USER
    version_seq: int
    tree_digest: str
    label: str = ""


class WorkspaceMutationEvent(BaseEvent):
    """Conservative fence for a host-owned workspace edit.

    Runtime uploads, editor writes, restores, and imports do not travel through
    the agent Action/Observation envelope. Writers persist this event before
    their first mutation while holding the shared workspace lock. It makes an
    earlier final seal historical without pretending to identify exact bytes;
    those belong to a later immutable final seal.
    """

    kind: Literal[EventKind.WORKSPACE_MUTATION] = EventKind.WORKSPACE_MUTATION
    source: EventSource = EventSource.SYSTEM
    operation: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    paths: tuple[str, ...] = ()
    # Set only on typed ``agent.view-admitted`` records. Sequence order alone
    # cannot prove which user intent a view consumed when workers overlap.
    run_intent_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Strict attribution starts at v1. None preserves the old sequence-only
    # reducer for historical logs until their first typed view admission.
    run_protocol_version: Literal[1] | None = None

    @model_validator(mode="after")
    def _valid_run_protocol_shape(self) -> WorkspaceMutationEvent:
        if self.operation == "agent.view-admitted":
            if (
                self.run_protocol_version != 1
                or self.run_intent_id is None
                or self.agent_view_id is None
            ):
                raise ValueError("typed view admission requires intent, view, and protocol v1")
        elif self.operation.startswith("agent.run-intent."):
            if self.run_intent_id is not None or self.agent_view_id is not None:
                raise ValueError("run intent cannot carry admission authority")
        elif self.run_intent_id is not None or self.run_protocol_version is not None:
            raise ValueError("run protocol fields are valid only on intent/admission events")
        return self

    @field_validator("paths")
    @classmethod
    def _bounded_diagnostic_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 64:
            raise ValueError("workspace mutation diagnostic paths exceed 64")
        normalized = tuple(path.strip() for path in value)
        if any(not path or len(path) > 512 for path in normalized):
            raise ValueError("workspace mutation paths must be non-empty and bounded")
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("workspace mutation paths must be sorted and unique")
        return normalized


def _validate_admission_claims(event: BuildPlatformAdmissionEvent) -> None:
    """Check verification claim/contract consistency for a build admission."""
    claim_ids = [claim.claim_id for claim in event.verification_claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("Build admission verification claim ids must be unique")
    if event.verification_contract is not None:
        if tuple(event.verification_contract.required_claims) != tuple(
            claim for claim in event.verification_claims if claim.required
        ):
            raise ValueError(
                "Build admission flattened claims differ from its verification contract"
            )


def _validate_route_identity(event: BuildPlatformAdmissionEvent) -> None:
    """Check route/composition-authority consistency for a build admission."""
    if event.route == "platform":
        if event.composition_authority != "build_platform_core":
            raise ValueError("Platform route requires Build Platform Core authority")
        if event.composition_digest is None or event.run_identity is None:
            raise ValueError("Platform route requires composition and run identities")
    elif (
        event.composition_authority != "legacy"
        or event.composition_digest is not None
        or event.run_identity is not None
    ):
        raise ValueError("legacy route cannot claim a Platform composition identity")


def _validate_transition(event: BuildPlatformAdmissionEvent) -> None:
    """Check transition/supersession consistency for a build admission."""
    if event.transition == "appkit_ejection":
        if event.profile_id != "disco.freeform_web@1" or event.supersedes_admission_id is None:
            raise ValueError(
                "AppKit ejection must supersede an admission with the Freeform profile"
            )
    elif event.supersedes_admission_id is not None:
        raise ValueError("an initial Build admission cannot supersede another admission")


class BuildPlatformAdmissionEvent(BaseEvent):
    """Durable route/composition identity for one Build run intent."""

    kind: Literal[EventKind.BUILD_PLATFORM_ADMISSION] = EventKind.BUILD_PLATFORM_ADMISSION
    source: EventSource = EventSource.SYSTEM
    route: Literal["legacy", "platform"]
    profile_id: str = Field(
        pattern=r"^[a-z][a-z0-9_-]{0,62}\.[a-z][a-z0-9_-]{0,62}@[1-9][0-9]*(?:\.[0-9]+){0,2}$"
    )
    run_intent_id: str = Field(min_length=1, max_length=160)
    composition_authority: Literal["legacy", "build_platform_core"]
    execution_bridge: Literal["legacy_host"] = "legacy_host"
    composition_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    run_identity: str | None = Field(
        default=None,
        pattern=r"^run:sha256:[0-9a-f]{64}$",
    )
    transition: Literal["initial", "appkit_ejection"] = "initial"
    supersedes_admission_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Exact target proof requirements resolved into this composition. Optional
    # for backward-compatible legacy admissions; platform admissions persist it
    # so restart/code drift cannot silently change the completion authority.
    verification_claims: tuple[HostVerificationClaim, ...] = ()
    verification_contract: AdmittedVerificationContract | None = None

    @model_validator(mode="after")
    def _route_identity_is_exact(self) -> BuildPlatformAdmissionEvent:
        _validate_admission_claims(self)
        _validate_route_identity(self)
        _validate_transition(self)
        return self


class AppKitEjectionEvent(BaseEvent):
    """A confirmed governed-to-Freeform revision boundary.

    The immutable source revision remains available for history/preview. The
    target revision is explicitly not AppKit-verified; later generic verification
    may verify it as Freeform but can never retroactively restore AppKit guarantees.
    """

    kind: Literal[EventKind.APPKIT_EJECTION] = EventKind.APPKIT_EJECTION
    source: EventSource = EventSource.SYSTEM
    action_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(min_length=1, max_length=128)
    source_profile_id: Literal["disco.appkit_web@1"] = "disco.appkit_web@1"
    target_profile_id: Literal["disco.freeform_web@1"] = "disco.freeform_web@1"
    source_version_seq: int = Field(ge=1)
    source_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ejected_version_seq: int = Field(ge=1)
    ejected_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lost_guarantees: tuple[str, ...] = Field(min_length=1)
    appkit_verified: Literal[False] = False

    @model_validator(mode="after")
    def _revision_is_distinct_and_truthful(self) -> AppKitEjectionEvent:
        if self.source_version_seq == self.ejected_version_seq:
            raise ValueError("AppKit ejection must create a distinct workspace revision")
        if self.source_tree_digest == self.ejected_tree_digest:
            raise ValueError("AppKit ejection revision must record the profile-boundary change")
        if self.lost_guarantees != APPKIT_EJECTION_LOST_GUARANTEES:
            raise ValueError("AppKit ejection must disclose the complete lost-guarantee set")
        return self


__all__ = [
    "AppKitEjectionEvent",
    "BuildPlatformAdmissionEvent",
    "StatusEvent",
    "WorkspaceMutationEvent",
    "WorkspaceRestoredEvent",
    "WorkspaceVersionEvent",
]

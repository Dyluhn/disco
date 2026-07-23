"""Versioned, target-neutral Build Platform Core contracts.

The objects in this module are pure immutable values.  They deliberately expose
no runtime, store, sandbox, secret, executor, revision-writer, or terminal-status
handle.  Engines, adapters, exporters, and connectors can describe requested
work, but only the existing host authorities may execute it or publish success.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..verification import HostVerificationClaim, VerificationClaimKind

_ID_PART = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_VERSION = re.compile(r"^[1-9][0-9]*(?:\.[0-9]+){0,2}$")
_OPEN_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")


class FrozenModel(BaseModel):
    """House-style immutable value object with a closed schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ComponentKind(str, Enum):
    PROFILE = "profile"
    ENGINE = "engine"
    TARGET = "target"
    VERIFIER = "verifier"
    PREVIEW = "preview"
    EXPORTER = "exporter"
    CONNECTOR = "connector"
    PROMPT_MODULE = "prompt_module"
    CONTEXT_MODULE = "context_module"


class ComponentId(FrozenModel):
    """A namespaced, explicitly versioned component identity."""

    namespace: str
    name: str
    version: str

    @field_validator("namespace", "name")
    @classmethod
    def _valid_part(cls, value: str) -> str:
        if not _ID_PART.fullmatch(value):
            raise ValueError("component namespace/name must be a lowercase identifier")
        return value

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("component version must be an explicit numeric version")
        return value

    @property
    def canonical(self) -> str:
        return f"{self.namespace}.{self.name}@{self.version}"


class Parameter(FrozenModel):
    """One deterministic scalar parameter; mappings are ordered tuples of these."""

    name: str = Field(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: str | int | float | bool | None


class ComponentRequirement(FrozenModel):
    kind: ComponentKind
    accepted: tuple[ComponentId, ...] = ()
    required_features: frozenset[str] = frozenset()
    operation: str = Field(min_length=1, max_length=96)


class CompatibilityRequirements(FrozenModel):
    components: tuple[ComponentRequirement, ...] = ()
    reference_categories: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset()


class SupportLevel(str, Enum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


class CapabilitySupport(FrozenModel):
    capability: str
    level: SupportLevel
    detail: str = ""

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: str) -> str:
        if not _OPEN_NAME.fullmatch(value):
            raise ValueError("capability must be an open namespaced identifier")
        return value


class CapabilityLayer(FrozenModel):
    """One explicit ceiling in the capability intersection."""

    source: str = Field(min_length=1, max_length=160)
    allowed: frozenset[str] = frozenset()
    denials: tuple[Parameter, ...] = ()


class CapabilityDenial(FrozenModel):
    capability: str
    denied_by: tuple[str, ...]
    reason: str


class EffectiveCapabilityPolicy(FrozenModel):
    """The host-enforced result.  Components may request but never widen it."""

    allowed: frozenset[str]
    denied: tuple[CapabilityDenial, ...] = ()
    supports: tuple[CapabilitySupport, ...] = ()


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyRule(FrozenModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    decision: PolicyDecision
    reason: str = ""


class PolicyLayer(FrozenModel):
    source: str = Field(min_length=1, max_length=160)
    rules: tuple[PolicyRule, ...] = ()


class EffectivePolicy(FrozenModel):
    """One decision per key; a denial at any layer is final."""

    rules: tuple[PolicyRule, ...] = ()


class TrustLevel(str, Enum):
    HOST_POLICY = "host_policy"
    TRUSTED_LOCAL = "trusted_local"
    UNTRUSTED_PORTABLE = "untrusted_portable"


class ModuleRef(FrozenModel):
    component: ComponentId
    trust: TrustLevel
    provenance: str = Field(min_length=1, max_length=240)


class EntryDescriptor(FrozenModel):
    """Opaque adapter-owned entry; Core assigns no filename/transport semantics."""

    kind: str = Field(min_length=1, max_length=96)
    reference: str = Field(min_length=1, max_length=2048)
    parameters: tuple[Parameter, ...] = ()


class DeliveryIntent(FrozenModel):
    shape: str
    entry: EntryDescriptor
    mode: Literal["interactive", "artifact"] = "artifact"

    @field_validator("shape")
    @classmethod
    def _valid_shape(cls, value: str) -> str:
        if not _OPEN_NAME.fullmatch(value):
            raise ValueError("delivery shape must be an open namespaced identifier")
        return value


class ReadinessSignal(FrozenModel):
    kind: str = Field(min_length=1, max_length=96)
    parameters: tuple[Parameter, ...] = ()


class PreviewPolicy(FrozenModel):
    required: bool = False
    unavailable: Literal["block", "degrade"] = "degrade"


class PreviewPlan(FrozenModel):
    modality: str = Field(min_length=1, max_length=96)
    entry: EntryDescriptor | None = None
    readiness: tuple[ReadinessSignal, ...] = ()
    intents: tuple[ComponentIntent, ...] = ()
    required_capabilities: frozenset[str] = frozenset()
    policy: PreviewPolicy = PreviewPolicy()

    @model_validator(mode="after")
    def _none_has_no_work(self) -> PreviewPlan:
        if self.modality == "none" and (self.entry is not None or self.readiness):
            raise ValueError("preview modality 'none' cannot declare entry/readiness work")
        return self


class ComponentIntent(FrozenModel):
    """A requested host effect.  This value is evidence, never an execution handle."""

    operation: str = Field(min_length=1, max_length=128)
    parameters: tuple[Parameter, ...] = ()
    required_capabilities: frozenset[str] = frozenset()


class VerifierCheck(FrozenModel):
    check_id: str = Field(min_length=1, max_length=128)
    intent: ComponentIntent
    required: bool = True
    issuer: ComponentId | None = None
    receipt_kind: str = Field(
        default="disco.verification@1",
        pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+@[1-9][0-9]*(?:\.[0-9]+){0,2}$",
    )
    required_execution_modality: str = Field(
        default="workspace_artifact",
        min_length=1,
        max_length=96,
    )
    required_artifact_identity_scheme: str | None = Field(
        default=None,
        min_length=3,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    delegated_issuers: tuple[ComponentId, ...] = ()
    accepted_claim_kinds: frozenset[VerificationClaimKind] = frozenset()
    claims: tuple[HostVerificationClaim, ...] = ()

    @model_validator(mode="after")
    def _accepted_kinds_cover_claims(self) -> VerifierCheck:
        if len(self.delegated_issuers) != len(
            set(issuer.canonical for issuer in self.delegated_issuers)
        ):
            raise ValueError("verifier check delegated issuers must be unique")
        if self.accepted_claim_kinds and any(
            claim.kind not in self.accepted_claim_kinds for claim in self.claims
        ):
            raise ValueError("verifier check contains a claim kind it does not accept")
        return self


class VerifierPolicy(FrozenModel):
    required: bool = True
    unavailable: Literal["block", "degrade"] = "block"
    unverified_finish: Literal["block", "allow_without_verified_label"] = "block"


class VerifierPlan(FrozenModel):
    checks: tuple[VerifierCheck, ...]
    policy: VerifierPolicy = VerifierPolicy()

    @field_validator("checks")
    @classmethod
    def _checks_are_unique(cls, value: tuple[VerifierCheck, ...]) -> tuple[VerifierCheck, ...]:
        ids = [check.check_id for check in value]
        if len(ids) != len(set(ids)):
            raise ValueError("verifier check ids must be unique")
        claim_ids = [claim.claim_id for check in value for claim in check.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("verifier claim ids must be unique across checks")
        return value


class VerifierVerdictKind(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNVERIFIABLE = "unverifiable"


class TypedVerifierVerdict(FrozenModel):
    """Host-owned verifier result; never a field of engine/adapter plan output."""

    verdict: VerifierVerdictKind
    check_results: tuple[Parameter, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class PackagePlan(FrozenModel):
    package_shape: str
    intents: tuple[ComponentIntent, ...]
    artifact_parameters: tuple[Parameter, ...] = ()

    @field_validator("package_shape")
    @classmethod
    def _valid_package_shape(cls, value: str) -> str:
        if not _OPEN_NAME.fullmatch(value):
            raise ValueError("package shape must be an open namespaced identifier")
        return value


class DeploymentPlan(FrozenModel):
    connector: ComponentId
    environment: str = Field(min_length=1, max_length=128)
    intents: tuple[ComponentIntent, ...]
    confirmation_required: bool = True


class ConstructionRequest(FrozenModel):
    profile: ComponentId
    goal: str
    modules: tuple[ModuleRef, ...] = ()
    reference_ids: tuple[str, ...] = ()


class ConstructionPlan(FrozenModel):
    engine: ComponentId
    intents: tuple[ComponentIntent, ...] = ()
    requested_tools: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset()
    policy: tuple[PolicyRule, ...] = ()


class TargetRequest(FrozenModel):
    profile: ComponentId
    engine: ComponentId
    goal: str


class TargetPlan(FrozenModel):
    target: ComponentId
    intents: tuple[ComponentIntent, ...] = ()
    delivery: DeliveryIntent
    preview: PreviewPlan
    verifier: VerifierPlan
    package: PackagePlan | None = None
    deployments: tuple[DeploymentPlan, ...] = ()
    required_capabilities: frozenset[str] = frozenset()
    policy: tuple[PolicyRule, ...] = ()


class PackageRequest(FrozenModel):
    profile: ComponentId
    target: ComponentId
    delivery: DeliveryIntent
    revision_ref: str


class DeploymentRequest(FrozenModel):
    profile: ComponentId
    target: ComponentId
    package_digest_ref: str
    environment: str


@runtime_checkable
class ConstructionEngine(Protocol):
    @property
    def id(self) -> ComponentId: ...

    def plan(self, request: ConstructionRequest) -> ConstructionPlan: ...


@runtime_checkable
class TargetAdapter(Protocol):
    @property
    def id(self) -> ComponentId: ...

    def plan(self, request: TargetRequest) -> TargetPlan: ...


@runtime_checkable
class PackageExporter(Protocol):
    @property
    def id(self) -> ComponentId: ...

    def plan(self, request: PackageRequest) -> PackagePlan: ...


@runtime_checkable
class DeploymentConnector(Protocol):
    @property
    def id(self) -> ComponentId: ...

    def plan(self, request: DeploymentRequest) -> DeploymentPlan: ...


class BuildProfile(FrozenModel):
    """The small user-facing choice resolved into one component composition."""

    id: ComponentId
    label: str = Field(min_length=1, max_length=96)
    engine: ComponentId
    target: ComponentId
    verifier: ComponentId
    preview: ComponentId
    exporter: ComponentId | None = None
    connector: ComponentId | None = None
    prompt_modules: tuple[ModuleRef, ...] = ()
    context_modules: tuple[ModuleRef, ...] = ()
    compatible_reference_categories: frozenset[str] = frozenset()
    capabilities: CapabilityLayer
    policy: PolicyLayer
    requirements: CompatibilityRequirements = CompatibilityRequirements()

    @model_validator(mode="after")
    def _module_ids_are_unique(self) -> BuildProfile:
        module_ids = [
            module.component.canonical for module in self.prompt_modules + self.context_modules
        ]
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("profile module component IDs must be unique")
        return self

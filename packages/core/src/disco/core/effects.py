"""Target-agnostic tool behavior and effect-receipt contracts.

These models are deliberately independent of tool names, sandboxes, and product
shapes.  A tool declares what it *may* do through :class:`ToolBehavior`; a
validated call may narrow that declaration to an :class:`ActionProfile`; and
the host records what it actually observed through immutable effect receipts.

K1 introduces this vocabulary in shadow mode only.  Existing loop policy still
uses its legacy signals until the later migration epics activate the event
reducer.  The schemas nevertheless fail safely today: a receipt outside the
declared action profile can be converted only to an opaque receipt, which is not
exact resource evidence and therefore cannot earn progress.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EffectCapability(str, Enum):
    """Behavioral capabilities used by reliability policy.

    These are intentionally separate from execution-environment permissions
    such as filesystem/network access.  A verifier may execute a process, for
    example, while its reliability behavior remains ``artifact.verify``.
    """

    WORKSPACE_CONTENT_READ = "workspace.content.read"
    WORKSPACE_INVENTORY_READ = "workspace.inventory.read"
    WORKSPACE_MUTATE = "workspace.mutate"
    OPAQUE_EXECUTE = "opaque.execute"
    PROCESS_OUTPUT_READ = "process.output.read"
    PROCESS_CONTROL = "process.control"
    WEB_OBSERVE = "web.observe"
    EXTERNAL_OBSERVE = "external.observe"
    ARTIFACT_VERIFY = "artifact.verify"
    PLAN_CONTROL = "plan.control"
    RUN_FINALIZE = "run.finalize"


class ToolBehavior(BaseModel):
    """Exhaustive static behavior declaration for one callable tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planner_safe: bool
    possible_capabilities: frozenset[EffectCapability]

    def static_profile(self) -> ActionProfile:
        """Return the broad profile used when a tool has no argument classifier."""

        return ActionProfile(capabilities=self.possible_capabilities)


class ActionProfile(BaseModel):
    """Capabilities a single validated invocation may exercise.

    A mixed tool's argument-aware classifier narrows its static behavior to this
    shape.  It may never add a capability absent from ``ToolBehavior``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: frozenset[EffectCapability]


def validate_action_profile(behavior: ToolBehavior, profile: ActionProfile) -> ActionProfile:
    """Return ``profile`` iff it is a narrowing of ``behavior``.

    This is kept as a small pure function so executors and registry conformance
    tests enforce exactly the same subset rule.
    """

    undeclared = profile.capabilities - behavior.possible_capabilities
    if undeclared:
        names = ", ".join(sorted(cap.value for cap in undeclared))
        raise ValueError(f"action profile contains undeclared capabilities: {names}")
    return profile


class ResourceKey(BaseModel):
    """Stable identity of a resource independent of its current contents.

    ``namespace`` supplies the ownership domain (for example ``workspace`` or
    ``process``); ``identifier`` is canonical within that namespace.  Core does
    not interpret paths, URLs, framework names, or target shapes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    identifier: str = Field(min_length=1)


class ResourceRevision(BaseModel):
    """A content-addressed revision of one resource.

    SHA-256 is the shared byte-identity primitive for this campaign.  Other
    semantic versions remain domain payloads until they are bound to bytes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource: ResourceKey
    algorithm: Literal["sha256"] = "sha256"
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("digest")
    @classmethod
    def _canonical_digest(cls, value: str) -> str:
        if value != value.lower():
            raise ValueError("revision digest must use canonical lowercase hex")
        return value


class CoverageUnit(str, Enum):
    """Unit used by an exact half-open coverage interval."""

    BYTES = "bytes"
    LINES = "lines"
    ITEMS = "items"


class CoverageSpan(BaseModel):
    """A zero-based half-open interval ``[start, end)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> CoverageSpan:
        if self.end <= self.start:
            raise ValueError("coverage span end must be greater than start")
        return self


class ResourceCoverage(BaseModel):
    """Canonical, exact coverage physically delivered for a resource revision.

    Spans must be sorted and disjoint.  Adjacent spans are allowed because they
    may originate from distinct rendered segments; reducers may merge them.
    ``total`` is the total count in the same unit when known.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: CoverageUnit
    spans: tuple[CoverageSpan, ...] = ()
    total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _canonical_spans(self) -> ResourceCoverage:
        previous_end = -1
        for span in self.spans:
            if span.start < previous_end:
                raise ValueError("coverage spans must be sorted and non-overlapping")
            if self.total is not None and span.end > self.total:
                raise ValueError("coverage span exceeds the declared total")
            previous_end = span.end
        return self

    def covers_total(self) -> bool:
        """Whether the spans cover every unit from zero through ``total``."""

        if self.total is None:
            return False
        if self.total == 0:
            return not self.spans
        cursor = 0
        for span in self.spans:
            if span.start > cursor:
                return False
            cursor = max(cursor, span.end)
        return cursor == self.total


class EffectReceiptKind(str, Enum):
    OBSERVATION = "observation"
    MUTATION = "mutation"
    VERIFICATION = "verification"
    CONTROL = "control"
    OPAQUE = "opaque"


_OBSERVATION_CAPABILITIES = frozenset(
    {
        EffectCapability.WORKSPACE_CONTENT_READ,
        EffectCapability.WORKSPACE_INVENTORY_READ,
        EffectCapability.PROCESS_OUTPUT_READ,
        EffectCapability.WEB_OBSERVE,
        EffectCapability.EXTERNAL_OBSERVE,
    }
)


class ObservationReceipt(BaseModel):
    """Exact bytes/ranges delivered by a host-observed call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[EffectReceiptKind.OBSERVATION] = EffectReceiptKind.OBSERVATION
    capability: EffectCapability
    revision: ResourceRevision
    coverage: ResourceCoverage
    complete: bool = False
    raw_size_bytes: int | None = Field(default=None, ge=0)
    rendered_size_bytes: int | None = Field(default=None, ge=0)
    rendered_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_observation(self) -> ObservationReceipt:
        if self.capability not in _OBSERVATION_CAPABILITIES:
            raise ValueError(f"{self.capability.value} is not an observation capability")
        if self.complete and not self.coverage.covers_total():
            raise ValueError("complete observation must cover the declared total exactly")
        if (self.rendered_size_bytes is None) != (self.rendered_sha256 is None):
            raise ValueError("rendered size and digest must be declared together")
        return self


class MutationReceipt(BaseModel):
    """Exact before/after identity for one host-observed resource mutation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[EffectReceiptKind.MUTATION] = EffectReceiptKind.MUTATION
    capability: Literal[EffectCapability.WORKSPACE_MUTATE] = EffectCapability.WORKSPACE_MUTATE
    resource: ResourceKey
    before: ResourceRevision | None = None
    after: ResourceRevision | None = None
    after_size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _valid_mutation(self) -> MutationReceipt:
        if self.before is None and self.after is None:
            raise ValueError("mutation receipt needs a before or after revision")
        if self.before is not None and self.before.resource != self.resource:
            raise ValueError("before revision resource does not match mutation resource")
        if self.after is not None and self.after.resource != self.resource:
            raise ValueError("after revision resource does not match mutation resource")
        if self.before is not None and self.after is not None and self.before == self.after:
            raise ValueError("no-op writes are not mutation progress")
        if self.after is None and self.after_size_bytes is not None:
            raise ValueError("deleted resources cannot declare an after size")
        if self.after is not None and self.after_size_bytes is None:
            raise ValueError("created or updated resources must declare an after size")
        return self


class VerificationReceipt(BaseModel):
    """A verifier claim bound to exact subject and requirement revisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[EffectReceiptKind.VERIFICATION] = EffectReceiptKind.VERIFICATION
    capability: Literal[EffectCapability.ARTIFACT_VERIFY] = EffectCapability.ARTIFACT_VERIFY
    verifier_id: str = Field(min_length=1)
    subject: ResourceRevision
    requirement_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    failure_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_verdict(self) -> VerificationReceipt:
        if self.passed and self.failure_fingerprint is not None:
            raise ValueError("passing verification cannot carry a failure fingerprint")
        if not self.passed and self.failure_fingerprint is None:
            raise ValueError("failed verification requires a failure fingerprint")
        return self


class ControlReceipt(BaseModel):
    """An authorized plan/run state transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[EffectReceiptKind.CONTROL] = EffectReceiptKind.CONTROL
    capability: Literal[EffectCapability.PLAN_CONTROL, EffectCapability.RUN_FINALIZE]
    transition_kind: str = Field(min_length=1)
    prior_state: str = Field(min_length=1)
    new_state: str = Field(min_length=1)


class OpaqueEffectReceipt(BaseModel):
    """Honest evidence that a capability ran without exact resource attribution.

    Opaque receipts are intentionally never exact progress evidence.  They also
    carry ``trusted=False`` as a structural guard against treating a downgrade
    from an invalid exact receipt as authorization or proof.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[EffectReceiptKind.OPAQUE] = EffectReceiptKind.OPAQUE
    capability: EffectCapability
    reason: str = Field(min_length=1)
    trusted: Literal[False] = False


class FinalWorkspaceSeal(BaseModel):
    """Host-owned proof of the immutable workspace delivered after completion.

    Per-action mutation receipts answer *which observed call changed a resource*.
    This seal answers the separate question *which exact tree was persisted and
    delivered*.  Keeping those claims separate lets arbitrary compilers and
    shell-heavy custom targets remain usable without pretending that command
    strings provide byte attribution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    scope: ResourceKey
    terminal_seq: int = Field(ge=1)
    latest_effect_seq: int | None = Field(default=None, ge=1)
    version_seq: int = Field(ge=1)
    tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered_fence(self) -> FinalWorkspaceSeal:
        if self.latest_effect_seq is not None and self.latest_effect_seq >= self.terminal_seq:
            raise ValueError("latest effect sequence must precede terminal sequence")
        return self


type EffectReceipt = Annotated[
    ObservationReceipt
    | MutationReceipt
    | VerificationReceipt
    | ControlReceipt
    | OpaqueEffectReceipt,
    Field(discriminator="kind"),
]


def receipt_capability(receipt: EffectReceipt) -> EffectCapability:
    """Return the capability claimed by a typed receipt."""

    return receipt.capability


def validate_effect_receipts(
    profile: ActionProfile,
    receipts: tuple[EffectReceipt, ...],
) -> tuple[EffectReceipt, ...]:
    """Validate that every exact receipt is permitted by ``profile``.

    Opaque receipts are retained as diagnostics but confer no exact evidence.
    Their capability must still be declared so a tool cannot smuggle a policy
    claim through the downgrade type.
    """

    undeclared = {
        receipt_capability(receipt)
        for receipt in receipts
        if receipt_capability(receipt) not in profile.capabilities
    }
    if undeclared:
        names = ", ".join(sorted(cap.value for cap in undeclared))
        raise ValueError(f"effect receipts contain undeclared capabilities: {names}")
    return receipts


def downgrade_effect_receipts(
    receipts: tuple[EffectReceipt, ...],
    *,
    reason: str,
) -> tuple[OpaqueEffectReceipt, ...]:
    """Replace untrusted receipts with explicit, non-progress opaque evidence."""

    return tuple(
        OpaqueEffectReceipt(capability=receipt_capability(receipt), reason=reason)
        for receipt in receipts
    )

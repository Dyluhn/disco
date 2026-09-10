"""Typed host-owned verification claims and receipts.

The builder may request verification and may describe what it believes it saw,
but only a host verifier can construct these values.  A receipt is deliberately
bound to one conversation/run, one workspace revision, one artifact/URL, and an
exact set of claims.  Evidence modalities are explicit so DOM facts cannot be
mistaken for visual-semantic inspection (or vice versa).

This module is the state-free public compatibility/export facade.  Cohesive
private implementation lives under :mod:`disco.core.verification_parts` and is
re-imported here so every public symbol keeps its import path.
"""

from __future__ import annotations

import posixpath
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from .effects import VerificationReceipt


class VerificationClaimKind(str, Enum):
    ARTIFACT_IDENTITY = "artifact_identity"
    APPLICATION_IDENTITY = "application_identity"
    HTTP_READY = "http_ready"
    RENDERED_CONTENT = "rendered_content"
    VISIBLE_TEXT = "visible_text"
    CONSOLE_CLEAN = "console_clean"
    NETWORK_CLEAN = "network_clean"
    INTERACTION = "interaction"
    ROUTE = "route"
    CONTRACT_SEMANTIC = "contract_semantic"
    VISUAL_SEMANTIC = "visual_semantic"
    TARGET_SPECIFIC = "target_specific"


_STRUCTURED_BROWSER_RUNTIME_CLAIM_KINDS = frozenset(
    {
        VerificationClaimKind.HTTP_READY,
        VerificationClaimKind.RENDERED_CONTENT,
        VerificationClaimKind.VISIBLE_TEXT,
        VerificationClaimKind.CONSOLE_CLEAN,
        VerificationClaimKind.NETWORK_CLEAN,
        VerificationClaimKind.INTERACTION,
        VerificationClaimKind.ROUTE,
    }
)


def requires_structured_browser_runtime(
    claims: tuple[HostVerificationClaim, ...] | list[HostVerificationClaim],
) -> bool:
    """Whether mandatory target claims require a runnable browser surface.

    This is deliberately claim-driven. Artifact identity, target-specific
    verification, and semantic/visual claims do not independently imply a web
    runtime; ordinary web targets persist HTTP/render/console/network claims.
    """

    return any(
        claim.required and claim.kind in _STRUCTURED_BROWSER_RUNTIME_CLAIM_KINDS for claim in claims
    )


def normalized_required_text(value: str) -> str:
    """Fold a quoted user PHRASE for a presence check.

    PROD-3: a phrase lifted out of the user's brief ("...a 'beans of the month'
    section...") is COPY, not an identifier. Comparing it byte-exactly turned
    ordinary title-casing in the rendered page ("Beans of the Month") into an
    unmet acceptance check, and the agent burned end-of-run turns grepping for
    the lowercase spelling and editing prose to smuggle it in. So presence
    checks fold case and collapse every run of whitespace (newlines included) —
    a phrase that wrapped across two lines of markup is the same phrase.

    Deliberately NOT applied to identity or source-text literals: an app title
    the user dictated "exactly", a filename, or a code identifier is a name,
    where case and spacing are the content.
    """

    return " ".join(value.split()).casefold()


class VerificationClaimStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


class VerificationParameter(BaseModel):
    """One opaque scalar retained from a target-owned entry descriptor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=96, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: str | int | float | bool | None


class VerificationDeliveryContract(BaseModel):
    """Target-owned delivery identity with no transport or filename semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$",
    )
    mode: Literal["interactive", "artifact"] = "artifact"
    entry_kind: str = Field(min_length=1, max_length=96)
    entry_reference: str = Field(min_length=1, max_length=2048)
    entry_parameters: tuple[VerificationParameter, ...] = ()

    @model_validator(mode="after")
    def _parameters_are_unique(self) -> VerificationDeliveryContract:
        names = [parameter.name for parameter in self.entry_parameters]
        if len(names) != len(set(names)):
            raise ValueError("delivery entry parameter names must be unique")
        return self


class VerificationExecutionIdentity(BaseModel):
    """Opaque host-observed runtime/artifact instance bound to a receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    modality: str = Field(min_length=1, max_length=96)
    instance_id: str = Field(min_length=1, max_length=256)
    generation: str = Field(min_length=1, max_length=256)
    locator: str = Field(default="", max_length=2048)


class VerificationArtifactIdentity(BaseModel):
    """Immutable target-owned identity of the exact artifact closure inspected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme: str = Field(
        min_length=3,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    entry_reference: str = Field(min_length=1, max_length=2048)
    producer_id: str = Field(min_length=1, max_length=160)


class VerificationEvidenceModality(str, Enum):
    ARTIFACT_BINDING = "artifact_binding"
    HTTP = "http"
    DOM_ACCESSIBILITY = "dom_accessibility"
    RUNTIME_CONSOLE = "runtime_console"
    RUNTIME_NETWORK = "runtime_network"
    INTERACTION = "interaction"
    NAVIGATION = "navigation"
    SCREENSHOT_PIXELS = "screenshot_pixels"
    TARGET_SPECIFIC = "target_specific"


class HostVerificationClaim(BaseModel):
    """One host-authored requirement, never a model-authored assertion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_.:-]*$",
    )
    kind: VerificationClaimKind
    required: bool = True
    expected: str = Field(default="", max_length=2048)
    source_authority: str = Field(min_length=1, max_length=160)
    reference_image_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _expected_value_is_present_when_required(self) -> HostVerificationClaim:
        if (
            self.kind
            in {
                VerificationClaimKind.VISIBLE_TEXT,
                VerificationClaimKind.INTERACTION,
                VerificationClaimKind.ROUTE,
                VerificationClaimKind.APPLICATION_IDENTITY,
                VerificationClaimKind.CONTRACT_SEMANTIC,
                VerificationClaimKind.VISUAL_SEMANTIC,
                VerificationClaimKind.TARGET_SPECIFIC,
            }
            and not self.expected
        ):
            raise ValueError(f"{self.kind.value} claim requires an expected value")
        if (
            self.reference_image_sha256 is not None
            and self.kind is not VerificationClaimKind.VISUAL_SEMANTIC
        ):
            raise ValueError("only visual-semantic claims may bind a reference image")
        return self


class VerificationCheckContract(BaseModel):
    """One admitted verifier operation and the exact claims it owns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.:-]*$",
    )
    receipt_kind: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+@[1-9][0-9]*(?:\.[0-9]+){0,2}$",
    )
    issuer_id: str = Field(min_length=1, max_length=160)
    operation: str = Field(min_length=1, max_length=128)
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
    delegated_issuer_ids: frozenset[str] = frozenset()
    required: bool = True
    accepted_claim_kinds: frozenset[VerificationClaimKind] = frozenset()
    claims: tuple[HostVerificationClaim, ...] = ()

    @field_serializer("delegated_issuer_ids", "accepted_claim_kinds")
    def _serialize_unordered_contract_fields(
        self,
        value: frozenset[str] | frozenset[VerificationClaimKind],
    ) -> list[str]:
        """Keep durable contract bytes stable across independent host objects."""

        return sorted(
            item.value if isinstance(item, VerificationClaimKind) else item for item in value
        )

    @model_validator(mode="before")
    @classmethod
    def _migrate_builtin_execution_modality(cls, value: Any) -> Any:
        """Read pre-field durable built-in contracts without guessing target names."""

        if not isinstance(value, dict) or "required_execution_modality" in value:
            return value
        migrated = dict(value)
        receipt_kind = migrated.get("receipt_kind")
        if receipt_kind == "disco.web_functional@1":
            migrated["required_execution_modality"] = "managed_preview"
        elif receipt_kind == "disco.appkit_strict@1":
            migrated["required_execution_modality"] = "appkit_strict_runtime"
        return migrated

    @model_validator(mode="after")
    def _claim_contract_is_exact(self) -> VerificationCheckContract:
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("verification check claim ids must be unique")
        if self.accepted_claim_kinds and any(
            claim.kind not in self.accepted_claim_kinds for claim in self.claims
        ):
            raise ValueError("verification check contains an unsupported claim kind")
        if self.required and not any(claim.required for claim in self.claims):
            raise ValueError("required verification check needs a mandatory claim")
        return self


class AdmittedVerificationContract(BaseModel):
    """Durable target-neutral completion policy for one resolved Build run.

    This is a pure snapshot of the resolved target/verifier plan.  It contains
    no executable handle; the host dispatcher remains the sole authority that
    may run a named operation or mint a receipt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1, 2, 3] = 3
    target_id: str = Field(min_length=1, max_length=160)
    verifier_id: str = Field(min_length=1, max_length=160)
    delivery: VerificationDeliveryContract
    preview_modality: str = Field(min_length=1, max_length=96)
    checks: tuple[VerificationCheckContract, ...]
    required: bool = True
    unavailable: Literal["block", "degrade"] = "block"
    unverified_finish: Literal["block", "allow_without_verified_label"] = "block"

    @model_validator(mode="after")
    def _checks_are_complete_and_unique(self) -> AdmittedVerificationContract:
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("admitted verification check ids must be unique")
        claim_ids = [claim.claim_id for check in self.checks for claim in check.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("admitted verification claim ids must be unique across checks")
        if self.required and not any(check.required for check in self.checks):
            raise ValueError("required verification contract needs a required check")
        if self.delivery.mode == "interactive" and any(
            check.required and check.required_execution_modality == "workspace_artifact"
            for check in self.checks
        ):
            raise ValueError("interactive verification checks require an explicit runtime modality")
        return self

    @property
    def required_claims(self) -> tuple[HostVerificationClaim, ...]:
        return tuple(
            claim
            for check in self.checks
            if check.required
            for claim in check.claims
            if claim.required
        )

    @property
    def digest(self) -> str:
        import hashlib
        import json

        payload = self.model_dump(mode="json")
        if self.schema_version == 1:
            # Version 1 predated explicit execution modalities. Replaying those
            # durable built-in contracts migrates their runtime requirement in
            # memory, but linked historical handoffs/receipts must retain the
            # canonical digest of the bytes that were originally admitted.
            for check in payload["checks"]:
                check.pop("required_execution_modality", None)
        if self.schema_version < 3:
            for check in payload["checks"]:
                check.pop("required_artifact_identity_scheme", None)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


class HostVerificationClaimResult(BaseModel):
    """The exact evidence-supported result for one requirement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1, max_length=160)
    kind: VerificationClaimKind
    required: bool = True
    expected: str = Field(default="", max_length=2048)
    source_authority: str = Field(min_length=1, max_length=160)
    reference_image_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    status: VerificationClaimStatus
    reason: str = Field(min_length=1, max_length=2048)
    verifier_id: str = Field(min_length=1, max_length=160)
    capability_basis: str = Field(min_length=1, max_length=240)
    evidence_modalities: tuple[VerificationEvidenceModality, ...] = ()
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _visual_pass_has_real_vision_authority(self) -> HostVerificationClaimResult:
        if (
            self.kind is VerificationClaimKind.VISUAL_SEMANTIC
            and self.status is VerificationClaimStatus.PASS
            and (
                VerificationEvidenceModality.SCREENSHOT_PIXELS not in self.evidence_modalities
                or "vision" not in self.capability_basis.casefold()
            )
        ):
            raise ValueError(
                "visual-semantic PASS requires a vision-capable pixel-inspection receipt"
            )
        return self


class HostVerificationObservedFact(BaseModel):
    """One bounded host observation, distinct from a requirement judgement.

    Claims say what must be true. Facts retain what a trusted target adapter
    actually observed so a deterministic consumer may evaluate an additional
    exact assertion without falling back to source files or model prose.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9_.:-]*$")
    kind: VerificationClaimKind
    value: str = Field(min_length=1, max_length=131_072, repr=False)
    verifier_id: str = Field(min_length=1, max_length=160)
    capability_basis: str = Field(min_length=1, max_length=240)
    evidence_modalities: tuple[VerificationEvidenceModality, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _fact_is_observation_not_visual_judgement(self) -> HostVerificationObservedFact:
        if self.kind is VerificationClaimKind.VISUAL_SEMANTIC:
            raise ValueError("visual semantics require a claim judgement, not an observed fact")
        return self


class VerifierReferenceImage(BaseModel):
    """One user-authored visual reference retained behind the verifier boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_event_id: str = Field(min_length=1, max_length=128)
    source_index: int = Field(ge=0, le=31)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(default="", max_length=80)
    instruction: str = Field(default="", max_length=8192)
    instruction_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instruction_complete: bool
    image_data_url: str = Field(default="", repr=False, max_length=20_000_000)

    @model_validator(mode="after")
    def _content_matches_declared_identity(self) -> VerifierReferenceImage:
        import hashlib

        if self.instruction_complete:
            if hashlib.sha256(self.instruction.encode()).hexdigest() != self.instruction_sha256:
                raise ValueError("reference instruction digest mismatch")
        elif self.instruction:
            raise ValueError("incomplete reference instruction must not expose partial text")
        if not self.image_data_url:
            if self.media_type:
                raise ValueError("unavailable reference pixels cannot declare a media type")
            return self
        validated = validated_image_data_url(self.image_data_url)
        if validated != (self.media_type, self.sha256):
            raise ValueError("reference image bytes do not match declared identity")
        return self


class VerificationRequestedClaim(BaseModel):
    """Client/target request for proof, never itself a verifier result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9_.:-]*$")
    kind: VerificationClaimKind
    required: bool = True
    expected: str = Field(default="", max_length=2048)
    reference_image_index: int | None = Field(default=None, ge=0, le=31)

    @model_validator(mode="after")
    def _valid_requested_claim(self) -> VerificationRequestedClaim:
        HostVerificationClaim(
            claim_id=self.claim_id,
            kind=self.kind,
            required=self.required,
            expected=self.expected,
            source_authority="requested.requirement",
            reference_image_sha256=("0" * 64 if self.reference_image_index is not None else None),
        )
        if (
            self.reference_image_index is not None
            and self.kind is not VerificationClaimKind.VISUAL_SEMANTIC
        ):
            raise ValueError("only visual-semantic requirements may name a reference image")
        return self


class VerificationRequirementsDirective(BaseModel):
    """Complete user/scenario requirement snapshot attached to one user turn.

    Replacement is causal: a later snapshot must name the event carrying the
    prior snapshot.  Images are verifier-only inputs and do not imply visual
    requirements unless an exact claim references their local index.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: tuple[VerificationRequestedClaim, ...] = Field(default=(), max_length=32)
    reference_images: tuple[str, ...] = Field(default=(), max_length=32, repr=False)
    supersedes_event_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _bounded_exact_snapshot(self) -> VerificationRequirementsDirective:
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("verification requirement claim ids must be unique")
        referenced = {
            claim.reference_image_index
            for claim in self.claims
            if claim.reference_image_index is not None
        }
        if any(index >= len(self.reference_images) for index in referenced):
            raise ValueError("verification requirement references a missing image")
        if set(range(len(self.reference_images))) != referenced:
            raise ValueError("every verifier reference image must be bound to an exact claim")
        if any(validated_image_data_url(image) is None for image in self.reference_images):
            raise ValueError("verification reference image is not a valid bounded image")
        return self


class PreviewSelectionIdentity(BaseModel):
    """Exact host preview generation selected for one deliverable handoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projection_id: str = Field(pattern=r"^pv_[0-9a-f]{32}$")
    session_name: str = Field(min_length=1, max_length=128)
    port: int = Field(ge=1, le=65535)
    url: str = Field(default="", max_length=2048)
    launch_kind: str = Field(min_length=1, max_length=64)
    intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sandbox_instance_id: str = Field(min_length=1, max_length=256)
    sandbox_generation: int = Field(ge=1)
    static_serve_dir: str | None = Field(default=None, max_length=512)
    source_action_id: str = Field(min_length=1, max_length=128)
    source_action_seq: int = Field(ge=1)
    source_observation_id: str = Field(min_length=1, max_length=128)
    source_observation_seq: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered_and_safe(self) -> PreviewSelectionIdentity:
        if self.source_action_seq >= self.source_observation_seq:
            raise ValueError("preview selection observation must follow its action")
        if self.static_serve_dir is not None:
            normalized = posixpath.normpath(self.static_serve_dir or ".")
            if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
                raise ValueError("static preview serve directory must remain workspace-relative")
        return self

    def contains_artifact(self, artifact_path: str) -> bool:
        if self.static_serve_dir is None:
            return True
        root = posixpath.normpath(self.static_serve_dir or ".")
        artifact = posixpath.normpath(artifact_path)
        return root == "." or artifact == root or artifact.startswith(f"{root}/")

    @property
    def operational_identity(self) -> tuple[str, str, int, str, str, str, int]:
        """Live selection identity excluding replay provenance and host locator."""

        return (
            self.projection_id,
            self.session_name,
            self.port,
            self.launch_kind,
            self.intent_digest,
            self.sandbox_instance_id,
            self.sandbox_generation,
        )

    def verification_target_url(self, artifact_path: str) -> str | None:
        """Exact in-sandbox URL for this selected application/artifact."""

        base = f"http://127.0.0.1:{self.port}"
        if self.static_serve_dir is None:
            return f"{base}/"
        if not self.contains_artifact(artifact_path):
            return None
        root = posixpath.normpath(self.static_serve_dir or ".")
        artifact = posixpath.normpath(artifact_path)
        relative = artifact if root == "." else posixpath.relpath(artifact, root)
        if relative == "index.html":
            return f"{base}/"
        encoded = "/".join(quote(part, safe="") for part in relative.split("/"))
        return f"{base}/{encoded}"

    @classmethod
    def from_structured(
        cls,
        *,
        action_id: str,
        action_seq: int,
        observation_id: str,
        observation_seq: int,
        structured: dict[str, Any],
    ) -> PreviewSelectionIdentity | None:
        fields = _structured_preview_identity(
            action_id=action_id,
            action_seq=action_seq,
            observation_id=observation_id,
            observation_seq=observation_seq,
            structured=structured,
        )
        if fields is None:
            return None
        (
            projection_id,
            session_name,
            port,
            url,
            launch_kind,
            intent_digest,
            sandbox_instance_id,
            sandbox_generation,
            static_serve_dir,
            src_action_id,
            src_action_seq,
            src_observation_id,
            src_observation_seq,
        ) = fields
        try:
            return cls(
                projection_id=projection_id,
                session_name=session_name,
                port=port,
                url=url,
                launch_kind=launch_kind,
                intent_digest=intent_digest,
                sandbox_instance_id=sandbox_instance_id,
                sandbox_generation=sandbox_generation,
                static_serve_dir=static_serve_dir,
                source_action_id=src_action_id,
                source_action_seq=src_action_seq,
                source_observation_id=src_observation_id,
                source_observation_seq=src_observation_seq,
            )
        except Exception:
            return None


class HostVerificationResult(BaseModel):
    """A current host verification receipt for one exact execution authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    conversation_id: str = Field(min_length=1, max_length=160)
    run_intent_id: str | None = Field(default=None, min_length=1, max_length=160)
    run_identity: str | None = Field(
        default=None,
        pattern=r"^run:sha256:[0-9a-f]{64}$",
    )
    target_id: str = Field(default="", max_length=160)
    delivery_shape: str = Field(default="", max_length=160)
    delivery_entry_reference: str = Field(default="", max_length=2048)
    verification_contract_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    check_id: str = Field(default="", max_length=128)
    receipt_kind: str = Field(default="", max_length=160)
    issuer_id: str = Field(default="", max_length=160)
    operation: str = Field(default="", max_length=128)
    delegated_issuer_ids: frozenset[str] = frozenset()
    execution_identity: VerificationExecutionIdentity | None = None
    artifact_identity: VerificationArtifactIdentity | None = None
    effect_receipt: VerificationReceipt | None = None
    agent_view_id: str | None = Field(default=None, min_length=1, max_length=128)
    deliverable_event_id: str | None = Field(default=None, min_length=1, max_length=128)
    artifact_path: str = Field(min_length=1, max_length=512)
    artifact_kind: str = Field(min_length=1, max_length=80)
    observed_url: str = Field(default="", max_length=2048)
    preview_selection: PreviewSelectionIdentity | None = None
    workspace_revision: int = Field(ge=0)
    workspace_generation: str = Field(default="", max_length=128)
    workspace_epoch: int | None = Field(default=None, ge=1)
    observed_after_seq: int = Field(ge=0)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    verifier_id: str = Field(min_length=1, max_length=160)
    tool_id: str = Field(min_length=1, max_length=160)
    status: VerificationClaimStatus
    reason: str = Field(min_length=1, max_length=2048)
    claim_results: tuple[HostVerificationClaimResult, ...] = Field(min_length=1)
    observed_facts: tuple[HostVerificationObservedFact, ...] = ()
    screenshot_path: str = Field(default="", max_length=512)
    screenshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _status_matches_required_claims(self) -> HostVerificationResult:
        status_matches_required_claims(self)
        return self

    @property
    def passed(self) -> bool:
        return self.status is VerificationClaimStatus.PASS

    def covers(self, claims: tuple[HostVerificationClaim, ...]) -> bool:
        expected = {
            (
                claim.claim_id,
                claim.kind,
                claim.required,
                claim.expected,
                claim.source_authority,
                claim.reference_image_sha256,
            )
            for claim in claims
        }
        actual = {
            (
                result.claim_id,
                result.kind,
                result.required,
                result.expected,
                result.source_authority,
                result.reference_image_sha256,
            )
            for result in self.claim_results
        }
        return expected == actual

    def authority_clauses(
        self, deliverable: Any, *, observed_url: str
    ) -> tuple[tuple[str, bool], ...]:
        """Every authority comparison, each paired with the fact name it binds.

        ``is_current_authority_for`` is the conjunction of these; ``first_authority_mismatch``
        reads the same list to NAME the one that failed. A receipt that binds ~30 facts
        can fail for ~30 reasons, and a caller told only "mismatched" — an agent above
        all — has no move but to guess. Guessing is indistinguishable from degeneracy,
        so the name is not a nicety: it is the difference between a fixable report and
        an unfixable one. Keep this the single source of the comparison; a predicate
        that duplicates a clause here will drift out of the name space.
        """

        return authority_clauses(self, deliverable, observed_url=observed_url)

    def is_current_authority_for(self, deliverable: Any, *, observed_url: str) -> bool:
        """Compare execution authority independently of one receipt's claim subset."""

        return all(ok for _, ok in self.authority_clauses(deliverable, observed_url=observed_url))

    def first_authority_mismatch(self, deliverable: Any, *, observed_url: str) -> str | None:
        """Name the first authority fact this receipt does not bind, or None if current."""

        for name, ok in self.authority_clauses(deliverable, observed_url=observed_url):
            if not ok:
                return name
        return None

    def is_current_for(self, deliverable: Any, *, observed_url: str) -> bool:
        """Exact authority and claim comparison; no fuzzy or name-derived matching."""

        claims = deliverable.required_claims or default_structured_web_claims()
        return self.covers(claims) and self.is_current_authority_for(
            deliverable, observed_url=observed_url
        )

    def first_currency_mismatch(self, deliverable: Any, *, observed_url: str) -> str | None:
        """Name why this receipt is not current for `deliverable`, or None if it is."""

        claims = deliverable.required_claims or default_structured_web_claims()
        if not self.covers(claims):
            return "required_claims"
        return self.first_authority_mismatch(deliverable, observed_url=observed_url)


def default_structured_web_claims() -> tuple[HostVerificationClaim, ...]:
    """The ordinary functional-web floor; deliberately contains no visual claim."""

    specs = (
        ("web.artifact_identity", VerificationClaimKind.ARTIFACT_IDENTITY),
        ("web.http_ready", VerificationClaimKind.HTTP_READY),
        ("web.rendered_content", VerificationClaimKind.RENDERED_CONTENT),
        ("web.console_clean", VerificationClaimKind.CONSOLE_CLEAN),
        ("web.network_clean", VerificationClaimKind.NETWORK_CLEAN),
    )
    return tuple(
        HostVerificationClaim(
            claim_id=claim_id,
            kind=kind,
            source_authority="target.freeform_web.functional_floor@1",
        )
        for claim_id, kind in specs
    )


# ---- private implementation re-imports -------------------------------------
# The cohesive private helpers live under ``verification_parts`` and are imported
# here AFTER all public models/enums are defined, so the private modules can
# import the public types without a circular-dependency error.  These names are
# the runtime implementation behind the public facade; external callers must
# continue to import them from ``disco.core.verification``.

from .verification_parts._aggregation import (  # noqa: E402
    VerificationCoverage,
    aggregate_verification_receipts,
)
from .verification_parts._authority import (  # noqa: E402
    authority_clauses,
    status_matches_required_claims,
    with_verification_effect_receipt,
)
from .verification_parts._image import validated_image_data_url  # noqa: E402
from .verification_parts._preview import _structured_preview_identity  # noqa: E402
from .verification_parts._results import (  # noqa: E402
    preview_binding_failure_result,
    target_verification_result,
    unavailable_verification_result,
)
from .verification_parts._structured_web import (  # noqa: E402
    apply_semantic_verifier_result,
    structured_web_verification_result,
)

__all__ = [
    "AdmittedVerificationContract",
    "HostVerificationClaim",
    "HostVerificationClaimResult",
    "HostVerificationResult",
    "PreviewSelectionIdentity",
    "VerifierReferenceImage",
    "VerificationCheckContract",
    "VerificationClaimKind",
    "VerificationClaimStatus",
    "VerificationCoverage",
    "VerificationDeliveryContract",
    "VerificationEvidenceModality",
    "VerificationExecutionIdentity",
    "VerificationParameter",
    "VerificationRequestedClaim",
    "VerificationRequirementsDirective",
    "aggregate_verification_receipts",
    "apply_semantic_verifier_result",
    "default_structured_web_claims",
    "preview_binding_failure_result",
    "requires_structured_browser_runtime",
    "structured_web_verification_result",
    "target_verification_result",
    "unavailable_verification_result",
    "validated_image_data_url",
    "with_verification_effect_receipt",
]

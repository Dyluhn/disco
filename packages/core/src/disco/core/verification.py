"""Typed host-owned verification claims and receipts.

The builder may request verification and may describe what it believes it saw,
but only a host verifier can construct these values.  A receipt is deliberately
bound to one conversation/run, one workspace revision, one artifact/URL, and an
exact set of claims.  Evidence modalities are explicit so DOM facts cannot be
mistaken for visual-semantic inspection (or vice versa).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import posixpath
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from io import BytesIO
from typing import Any, Literal
from urllib.parse import quote

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from .effects import ResourceKey, ResourceRevision, VerificationReceipt

_MAX_VERIFIER_IMAGE_BYTES = 20_000_000
_MAX_VERIFIER_IMAGE_PIXELS = 50_000_000
_IMAGE_FORMAT_MEDIA_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def validated_image_data_url(value: str) -> tuple[str, str] | None:
    """Return the decoded image's authoritative media type and digest.

    A magic prefix is not pixel evidence.  Pillow must parse and verify the
    complete bounded image before host verification is allowed to retain it.
    """

    header, separator, payload = value.partition(",")
    if not separator or not header.startswith("data:image/") or not header.endswith(";base64"):
        return None
    declared_media_type = header[5:-7].lower()
    if declared_media_type not in set(_IMAGE_FORMAT_MEDIA_TYPES.values()):
        return None
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > _MAX_VERIFIER_IMAGE_BYTES:
        return None
    try:
        with Image.open(BytesIO(raw)) as image:
            actual_media_type = _IMAGE_FORMAT_MEDIA_TYPES.get(str(image.format or "").upper())
            width, height = image.size
            if (
                actual_media_type != declared_media_type
                or width < 1
                or height < 1
                or width * height > _MAX_VERIFIER_IMAGE_PIXELS
            ):
                return None
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return None
    return declared_media_type, hashlib.sha256(raw).hexdigest()


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
    def operational_identity(self) -> tuple[str, str, int, str, str, str, int, str]:
        """Live selection identity excluding replay-event provenance."""

        return (
            self.projection_id,
            self.session_name,
            self.port,
            self.launch_kind,
            self.intent_digest,
            self.sandbox_instance_id,
            self.sandbox_generation,
            self.url,
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
        intent = structured.get("intent")
        if not isinstance(intent, dict):
            return None
        command = structured.get("command")
        exec_dir = structured.get("exec_dir")
        name = structured.get("name")
        port = structured.get("port")
        projection_id = structured.get("projection_id")
        sandbox_instance_id = structured.get("sandbox_instance_id")
        sandbox_generation = structured.get("sandbox_generation")
        launch_kind = structured.get("launch_kind")
        if (
            not isinstance(command, str)
            or not isinstance(name, str)
            or type(port) is not int
            or not isinstance(projection_id, str)
            or not isinstance(sandbox_instance_id, str)
            or type(sandbox_generation) is not int
            or launch_kind not in {"static", "custom", "framework"}
            or intent.get("launch_kind") != launch_kind
        ):
            return None
        digest = hashlib.sha256(
            json.dumps(
                {
                    "command": command,
                    "exec_dir": exec_dir,
                    "intent": intent,
                    "name": name,
                    "port": port,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if structured.get("intent_digest") != digest:
            return None
        static_serve_dir: str | None = None
        if launch_kind == "static":
            normalized_serve_dir = posixpath.normpath(str(intent.get("serve_dir") or "."))
            if normalized_serve_dir == "/workspace":
                static_serve_dir = "."
            elif normalized_serve_dir.startswith("/workspace/"):
                static_serve_dir = normalized_serve_dir.removeprefix("/workspace/")
            elif not normalized_serve_dir.startswith("/"):
                static_serve_dir = normalized_serve_dir
            else:
                return None
        try:
            return cls(
                projection_id=projection_id,
                session_name=name,
                port=port,
                url=str(structured.get("url") or ""),
                launch_kind=launch_kind,
                intent_digest=digest,
                sandbox_instance_id=sandbox_instance_id,
                sandbox_generation=sandbox_generation,
                static_serve_dir=static_serve_dir,
                source_action_id=action_id,
                source_action_seq=action_seq,
                source_observation_id=observation_id,
                source_observation_seq=observation_seq,
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
    screenshot_path: str = Field(default="", max_length=512)
    screenshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _status_matches_required_claims(self) -> HostVerificationResult:
        ids = [result.claim_id for result in self.claim_results]
        if len(ids) != len(set(ids)):
            raise ValueError("verification claim result ids must be unique")
        required = [result for result in self.claim_results if result.required]
        if not required:
            raise ValueError("verification result needs at least one required claim")
        expected_status = (
            VerificationClaimStatus.FAIL
            if any(result.status is VerificationClaimStatus.FAIL for result in required)
            else VerificationClaimStatus.UNAVAILABLE
            if any(result.status is VerificationClaimStatus.UNAVAILABLE for result in required)
            else VerificationClaimStatus.PASS
        )
        if self.status is not expected_status:
            raise ValueError("overall verification status does not match required claims")
        if self.observed_after_seq < self.workspace_revision:
            raise ValueError("verification cannot precede the workspace revision it covers")
        if self.screenshot_sha256 is not None and not self.screenshot_path:
            raise ValueError("screenshot digest requires a screenshot path")
        if self.effect_receipt is not None:
            effect = self.effect_receipt
            if effect.verifier_id != self.verifier_id:
                raise ValueError("effect receipt verifier does not match result")
            if effect.passed is not (self.status is VerificationClaimStatus.PASS):
                raise ValueError("effect receipt pass state does not match result")
            if effect.subject.digest != _result_authority_digest(self):
                raise ValueError("effect receipt subject does not match result authority")
            if effect.requirement_fingerprint != _result_requirement_fingerprint(self):
                raise ValueError("effect receipt requirement does not match result claims")
        if self.verification_contract_digest is not None and self.passed:
            if self.verifier_id != self.issuer_id or self.tool_id != self.operation:
                raise ValueError("governed PASS must be issued by the admitted verifier operation")
            allowed_claim_issuers = {self.issuer_id, *self.delegated_issuer_ids}
            if any(
                result.verifier_id not in allowed_claim_issuers for result in self.claim_results
            ):
                raise ValueError("governed PASS claim comes from an unadmitted evidence issuer")
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

    def is_current_authority_for(self, deliverable: Any, *, observed_url: str) -> bool:
        """Compare execution authority independently of one receipt's claim subset."""

        contract = getattr(deliverable, "verification_contract", None)
        check = getattr(deliverable, "verification_check", None)
        expected_contract_digest = contract.digest if contract is not None else None
        expected_target_id = contract.target_id if contract is not None else ""
        expected_delivery_shape = contract.delivery.shape if contract is not None else ""
        expected_delivery_entry = contract.delivery.entry_reference if contract is not None else ""
        expected_check_id = check.check_id if check is not None else ""
        expected_receipt_kind = check.receipt_kind if check is not None else ""
        expected_issuer_id = check.issuer_id if check is not None else ""
        return bool(
            (expected_contract_digest is None or self.effect_receipt is not None)
            and self.conversation_id == deliverable.conversation_id
            and self.run_intent_id == deliverable.run_intent_id
            and self.run_identity == deliverable.run_identity
            and self.target_id == expected_target_id
            and self.delivery_shape == expected_delivery_shape
            and self.delivery_entry_reference == expected_delivery_entry
            and self.verification_contract_digest == expected_contract_digest
            and self.check_id == expected_check_id
            and self.receipt_kind == expected_receipt_kind
            and self.issuer_id == expected_issuer_id
            and self.operation == (check.operation if check is not None else "")
            and (
                check is None
                or (self.verifier_id == expected_issuer_id and self.tool_id == check.operation)
            )
            and self.delegated_issuer_ids
            == (check.delegated_issuer_ids if check is not None else frozenset())
            and self.execution_identity == getattr(deliverable, "execution_identity", None)
            and self.artifact_identity == getattr(deliverable, "artifact_identity", None)
            and self.agent_view_id == deliverable.agent_view_id
            and self.deliverable_event_id == deliverable.deliverable_event_id
            and self.artifact_path == deliverable.artifact_path
            and self.artifact_kind == deliverable.artifact_kind
            and self.preview_selection == deliverable.preview_selection
            and (
                deliverable.preview_selection is not None
                or not deliverable.deployment_url
                or self.observed_url.rstrip("/") == deliverable.deployment_url.rstrip("/")
            )
            and self.workspace_revision == deliverable.workspace_revision
            and (
                not deliverable.workspace_generation
                or self.workspace_generation == deliverable.workspace_generation
            )
            and (
                deliverable.workspace_epoch is None
                or self.workspace_epoch == deliverable.workspace_epoch
            )
            and self.observed_after_seq == deliverable.observed_after_seq
            and self.observed_url == observed_url
        )

    def is_current_for(self, deliverable: Any, *, observed_url: str) -> bool:
        """Exact authority and claim comparison; no fuzzy or name-derived matching."""

        claims = deliverable.required_claims or default_structured_web_claims()
        return self.covers(claims) and self.is_current_authority_for(
            deliverable, observed_url=observed_url
        )


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _result_authority_payload(result: HostVerificationResult) -> dict[str, Any]:
    return {
        "conversation_id": result.conversation_id,
        "run_intent_id": result.run_intent_id,
        "run_identity": result.run_identity,
        "target_id": result.target_id,
        "delivery_shape": result.delivery_shape,
        "delivery_entry_reference": result.delivery_entry_reference,
        "verification_contract_digest": result.verification_contract_digest,
        "check_id": result.check_id,
        "receipt_kind": result.receipt_kind,
        "issuer_id": result.issuer_id,
        "operation": result.operation,
        "delegated_issuer_ids": sorted(result.delegated_issuer_ids),
        "execution_identity": (
            result.execution_identity.model_dump(mode="json")
            if result.execution_identity is not None
            else None
        ),
        "artifact_identity": (
            result.artifact_identity.model_dump(mode="json")
            if result.artifact_identity is not None
            else None
        ),
        "agent_view_id": result.agent_view_id,
        "deliverable_event_id": result.deliverable_event_id,
        "artifact_path": result.artifact_path,
        "artifact_kind": result.artifact_kind,
        "observed_url": result.observed_url,
        "preview_selection": (
            result.preview_selection.model_dump(mode="json")
            if result.preview_selection is not None
            else None
        ),
        "workspace_revision": result.workspace_revision,
        "workspace_generation": result.workspace_generation,
        "workspace_epoch": result.workspace_epoch,
        "observed_after_seq": result.observed_after_seq,
    }


def _result_authority_digest(result: HostVerificationResult) -> str:
    return _canonical_digest(_result_authority_payload(result))


def _result_requirement_fingerprint(result: HostVerificationResult) -> str:
    return _canonical_digest(
        {
            "verification_contract_digest": result.verification_contract_digest,
            "check_id": result.check_id,
            "receipt_kind": result.receipt_kind,
            "issuer_id": result.issuer_id,
            "operation": result.operation,
            "delegated_issuer_ids": sorted(result.delegated_issuer_ids),
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "kind": claim.kind.value,
                    "required": claim.required,
                    "expected": claim.expected,
                    "source_authority": claim.source_authority,
                    "reference_image_sha256": claim.reference_image_sha256,
                }
                for claim in result.claim_results
            ],
        }
    )


def with_verification_effect_receipt(
    result: HostVerificationResult,
) -> HostVerificationResult:
    """Anchor a governed high-level result to the existing effect receipt root."""

    if result.verification_contract_digest is None:
        return result
    failed_payload = {
        "status": result.status.value,
        "reason": result.reason,
        "claim_statuses": [(claim.claim_id, claim.status.value) for claim in result.claim_results],
    }
    receipt = VerificationReceipt(
        verifier_id=result.verifier_id,
        subject=ResourceRevision(
            resource=ResourceKey(
                namespace="verification.subject",
                identifier=f"{result.target_id}:{result.check_id}",
            ),
            digest=_result_authority_digest(result),
        ),
        requirement_fingerprint=_result_requirement_fingerprint(result),
        passed=result.status is VerificationClaimStatus.PASS,
        failure_fingerprint=(
            None
            if result.status is VerificationClaimStatus.PASS
            else _canonical_digest(failed_payload)
        ),
    )
    return result.model_copy(update={"effect_receipt": receipt})


class VerificationCoverage(BaseModel):
    """Pure aggregate of the latest current result for every required claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: VerificationClaimStatus
    claim_results: tuple[HostVerificationClaimResult, ...]
    missing_claim_ids: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status is VerificationClaimStatus.PASS


def aggregate_verification_receipts(
    *,
    deliverables: tuple[Any, ...],
    receipts: tuple[HostVerificationResult, ...],
) -> VerificationCoverage:
    """Evaluate ordered trusted receipts across target-owned verifier checks.

    Each deliverable is one admitted check with its exact authority and claim
    subset. Receipts are chronological; a later current result for the same
    exact claim replaces an earlier result. Foreign, stale, or cross-check
    receipts contribute no coverage.
    """

    required: dict[tuple[str, str], HostVerificationClaim] = {}
    for deliverable in deliverables:
        check = getattr(deliverable, "verification_check", None)
        check_id = check.check_id if check is not None else ""
        for claim in deliverable.required_claims:
            if claim.required:
                required[(check_id, claim.claim_id)] = claim

    shared_authorities = {
        _canonical_digest(
            {
                "conversation_id": getattr(deliverable, "conversation_id", None),
                "run_intent_id": getattr(deliverable, "run_intent_id", None),
                "run_identity": getattr(deliverable, "run_identity", None),
                "agent_view_id": getattr(deliverable, "agent_view_id", None),
                "deliverable_event_id": getattr(deliverable, "deliverable_event_id", None),
                "artifact_path": getattr(deliverable, "artifact_path", None),
                "artifact_kind": getattr(deliverable, "artifact_kind", None),
                "workspace_revision": getattr(deliverable, "workspace_revision", None),
                "workspace_generation": getattr(deliverable, "workspace_generation", None),
                "workspace_epoch": getattr(deliverable, "workspace_epoch", None),
                "execution_identity": (
                    {
                        "instance_id": identity.instance_id,
                        "generation": identity.generation,
                        "locator": identity.locator,
                    }
                    if (identity := getattr(deliverable, "execution_identity", None)) is not None
                    else None
                ),
                "contract_digest": (
                    contract.digest
                    if (contract := getattr(deliverable, "verification_contract", None)) is not None
                    else None
                ),
            }
        )
        for deliverable in deliverables
    }
    if len(shared_authorities) > 1:
        return VerificationCoverage(
            status=VerificationClaimStatus.UNAVAILABLE,
            claim_results=(),
            missing_claim_ids=tuple(claim.claim_id for claim in required.values()),
        )

    selected: dict[tuple[str, str], HostVerificationClaimResult] = {}
    for deliverable in deliverables:
        check = getattr(deliverable, "verification_check", None)
        check_id = check.check_id if check is not None else ""
        for receipt in receipts:
            if not receipt.is_current_authority_for(
                deliverable,
                observed_url=receipt.observed_url,
            ):
                continue
            by_id = {result.claim_id: result for result in receipt.claim_results}
            for claim in deliverable.required_claims:
                result = by_id.get(claim.claim_id)
                if result is None:
                    continue
                exact = (
                    result.claim_id == claim.claim_id
                    and result.kind is claim.kind
                    and result.required is claim.required
                    and result.expected == claim.expected
                    and result.source_authority == claim.source_authority
                    and result.reference_image_sha256 == claim.reference_image_sha256
                )
                if exact:
                    selected[(check_id, claim.claim_id)] = result

    missing = tuple(claim.claim_id for key, claim in required.items() if key not in selected)
    ordered_results = tuple(selected[key] for key in required if key in selected)
    status = (
        VerificationClaimStatus.FAIL
        if any(result.status is VerificationClaimStatus.FAIL for result in ordered_results)
        else VerificationClaimStatus.UNAVAILABLE
        if missing
        or any(result.status is VerificationClaimStatus.UNAVAILABLE for result in ordered_results)
        else VerificationClaimStatus.PASS
    )
    return VerificationCoverage(
        status=status,
        claim_results=ordered_results,
        missing_claim_ids=missing,
    )


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


def _result(
    claim: HostVerificationClaim,
    status: VerificationClaimStatus,
    reason: str,
    *,
    verifier_id: str,
    basis: str,
    modalities: tuple[VerificationEvidenceModality, ...] = (),
    refs: tuple[str, ...] = (),
) -> HostVerificationClaimResult:
    return HostVerificationClaimResult(
        claim_id=claim.claim_id,
        kind=claim.kind,
        required=claim.required,
        expected=claim.expected,
        source_authority=claim.source_authority,
        reference_image_sha256=claim.reference_image_sha256,
        status=status,
        reason=reason,
        verifier_id=verifier_id,
        capability_basis=basis,
        evidence_modalities=modalities,
        evidence_refs=refs,
    )


def _target_binding_fields(deliverable: Any) -> dict[str, Any]:
    contract = getattr(deliverable, "verification_contract", None)
    check = getattr(deliverable, "verification_check", None)
    return {
        "target_id": contract.target_id if contract is not None else "",
        "delivery_shape": contract.delivery.shape if contract is not None else "",
        "delivery_entry_reference": (
            contract.delivery.entry_reference if contract is not None else ""
        ),
        "verification_contract_digest": contract.digest if contract is not None else None,
        "check_id": check.check_id if check is not None else "",
        "receipt_kind": check.receipt_kind if check is not None else "",
        "issuer_id": check.issuer_id if check is not None else "",
        "operation": check.operation if check is not None else "",
        "delegated_issuer_ids": (check.delegated_issuer_ids if check is not None else frozenset()),
        "execution_identity": getattr(deliverable, "execution_identity", None),
        "artifact_identity": getattr(deliverable, "artifact_identity", None),
    }


def preview_binding_failure_result(
    *,
    deliverable: Any,
    observed_url: str,
    reason: str = "selected preview generation is absent, changed, or foreign",
    verifier_id: str = "host.preview_binding@1",
    tool_id: str = "host.preview_binding_preflight@1",
) -> HostVerificationResult:
    """Build an exact, host-owned receipt for a failed Preview preflight.

    Preview binding is checked before a browser is allowed to inspect the page.
    The receipt therefore proves one negative fact only: the requested artifact
    was not bound to the selected live Preview generation.  It is structurally
    incapable of returning PASS.  Claims that would require HTTP/browser
    evidence remain UNAVAILABLE instead of being inferred from empty lists.

    Authority is copied from the host-built deliverable, not from browser
    freshness and never from model prose.  The normal exact ``is_current_for``
    comparator consequently applies without relaxing PASS freshness rules.
    """

    claims = deliverable.required_claims or default_structured_web_claims()
    refs = tuple(
        ref
        for ref in (
            f"artifact:{deliverable.artifact_path}",
            f"url:{observed_url}" if observed_url else "",
            (
                f"preview:{deliverable.preview_selection.projection_id}"
                if deliverable.preview_selection is not None
                else ""
            ),
        )
        if ref
    )
    results = tuple(
        _result(
            claim,
            (
                VerificationClaimStatus.FAIL
                if claim.kind is VerificationClaimKind.ARTIFACT_IDENTITY
                else VerificationClaimStatus.UNAVAILABLE
            ),
            (
                reason
                if claim.kind is VerificationClaimKind.ARTIFACT_IDENTITY
                else "claim was not evaluated because canonical Preview binding failed"
            ),
            verifier_id=verifier_id,
            basis="deterministic host Preview-binding preflight",
            modalities=(
                (VerificationEvidenceModality.ARTIFACT_BINDING,)
                if claim.kind is VerificationClaimKind.ARTIFACT_IDENTITY
                else ()
            ),
            refs=refs,
        )
        for claim in claims
    )
    status = (
        VerificationClaimStatus.FAIL
        if any(
            result.required and result.status is VerificationClaimStatus.FAIL for result in results
        )
        else VerificationClaimStatus.UNAVAILABLE
    )
    overall_reason = next(
        result.reason for result in results if result.required and result.status is status
    )
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url=observed_url,
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=deliverable.workspace_generation,
        workspace_epoch=deliverable.workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=verifier_id,
        tool_id=tool_id,
        status=status,
        reason=overall_reason,
        claim_results=results,
    )
    return with_verification_effect_receipt(receipt)


def unavailable_verification_result(
    *,
    deliverable: Any,
    reason: str,
    verifier_id: str = "host.verifier_dispatcher@1",
    tool_id: str = "host.verifier_dispatcher@1",
) -> HostVerificationResult:
    """Mint an exact non-authorizing receipt when no target verifier can run."""

    claims = deliverable.required_claims
    if not claims:
        raise ValueError("unavailable verification receipt needs exact required claims")
    results = tuple(
        _result(
            claim,
            VerificationClaimStatus.UNAVAILABLE,
            reason,
            verifier_id=verifier_id,
            basis="host verifier dispatcher capability registry",
        )
        for claim in claims
    )
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url="",
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=deliverable.workspace_generation,
        workspace_epoch=deliverable.workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=verifier_id,
        tool_id=tool_id,
        status=VerificationClaimStatus.UNAVAILABLE,
        reason=reason,
        claim_results=results,
    )
    return with_verification_effect_receipt(receipt)


def target_verification_result(
    *,
    deliverable: Any,
    claim_results: tuple[HostVerificationClaimResult, ...],
    verifier_id: str,
    tool_id: str,
    reason: str,
    observed_url: str = "",
    screenshot_path: str = "",
    screenshot_sha256: str | None = None,
) -> HostVerificationResult:
    """Build a target-neutral host result from adapter-owned exact evidence."""

    check = getattr(deliverable, "verification_check", None)
    if check is not None and (verifier_id != check.issuer_id or tool_id != check.operation):
        raise ValueError("target verifier issuer/operation differs from the admitted check")
    expected = {
        (
            claim.claim_id,
            claim.kind,
            claim.required,
            claim.expected,
            claim.source_authority,
            claim.reference_image_sha256,
        )
        for claim in deliverable.required_claims
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
        for result in claim_results
    }
    if expected != actual:
        raise ValueError("target verifier results do not match the admitted check claims")
    required = tuple(result for result in claim_results if result.required)
    status = (
        VerificationClaimStatus.FAIL
        if any(result.status is VerificationClaimStatus.FAIL for result in required)
        else VerificationClaimStatus.UNAVAILABLE
        if any(result.status is VerificationClaimStatus.UNAVAILABLE for result in required)
        else VerificationClaimStatus.PASS
    )
    if (
        status is VerificationClaimStatus.PASS
        and check is not None
        and check.required_artifact_identity_scheme is not None
    ):
        artifact_identity = getattr(deliverable, "artifact_identity", None)
        if (
            artifact_identity is None
            or artifact_identity.scheme != check.required_artifact_identity_scheme
        ):
            raise ValueError(
                "admitted verifier PASS requires the exact immutable artifact identity scheme"
            )
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url=observed_url,
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=deliverable.workspace_generation,
        workspace_epoch=deliverable.workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=verifier_id,
        tool_id=tool_id,
        status=status,
        reason=reason,
        claim_results=claim_results,
        screenshot_path=screenshot_path,
        screenshot_sha256=screenshot_sha256,
    )
    return with_verification_effect_receipt(receipt)


@dataclass(frozen=True)
class _StructuredWebEvidence:
    deliverable: Any
    verdict: dict[str, Any]
    verifier_id: str
    basis: str
    refs: tuple[str, ...]
    deterministic_pass: bool
    url: str
    http_status: Any
    rendered_text: Any
    console_errors: Any
    network_failures: Any
    meaningful: bool


def _basic_structured_claim_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
) -> HostVerificationClaimResult | None:
    common = {
        "verifier_id": evidence.verifier_id,
        "basis": evidence.basis,
        "refs": evidence.refs,
    }
    if claim.kind is VerificationClaimKind.ARTIFACT_IDENTITY:
        identity = evidence.verdict.get("artifact_identity")
        selected = evidence.deliverable.preview_selection
        ok = bool(
            isinstance(identity, dict)
            and identity.get("conversation_id") == evidence.deliverable.conversation_id
            and identity.get("artifact_path") == evidence.deliverable.artifact_path
            and identity.get("artifact_kind") == evidence.deliverable.artifact_kind
            and identity.get("requested_url") == evidence.deliverable.deployment_url
            and identity.get("observed_url") == evidence.url
            and identity.get("preview_selection")
            == (selected.model_dump(mode="json") if selected is not None else None)
            and identity.get("preview_live_match") is True
            and (not evidence.deliverable.preview_binding_required or selected is not None)
            and (selected is None or selected.contains_artifact(evidence.deliverable.artifact_path))
            and (
                selected is None
                or selected.verification_target_url(evidence.deliverable.artifact_path)
                == evidence.url
            )
            and evidence.url
            and evidence.deterministic_pass
        )
        return _result(
            claim,
            VerificationClaimStatus.PASS if ok else VerificationClaimStatus.FAIL,
            "host bound the selected artifact to the observed preview URL"
            if ok
            else "host could not bind the artifact to an observed preview URL",
            modalities=(VerificationEvidenceModality.ARTIFACT_BINDING,),
            **common,
        )
    if claim.kind is VerificationClaimKind.HTTP_READY:
        ok = (
            isinstance(evidence.http_status, int)
            and not isinstance(evidence.http_status, bool)
            and 200 <= evidence.http_status < 400
        )
        return _result(
            claim,
            VerificationClaimStatus.PASS if ok else VerificationClaimStatus.FAIL,
            f"host HTTP probe returned {evidence.http_status!r}",
            modalities=(VerificationEvidenceModality.HTTP,),
            **common,
        )
    if claim.kind is VerificationClaimKind.RENDERED_CONTENT:
        return _result(
            claim,
            VerificationClaimStatus.PASS if evidence.meaningful else VerificationClaimStatus.FAIL,
            "rendered DOM contained meaningful visible content"
            if evidence.meaningful
            else "rendered DOM did not prove meaningful visible content",
            modalities=(VerificationEvidenceModality.DOM_ACCESSIBILITY,),
            **common,
        )
    if claim.kind is VerificationClaimKind.VISIBLE_TEXT:
        text = evidence.rendered_text if isinstance(evidence.rendered_text, str) else None
        ok = text is not None and claim.expected in text
        return _result(
            claim,
            VerificationClaimStatus.PASS
            if ok
            else VerificationClaimStatus.FAIL
            if text is not None
            else VerificationClaimStatus.UNAVAILABLE,
            f"rendered DOM contains required text {claim.expected!r}"
            if ok
            else f"rendered DOM does not contain required text {claim.expected!r}"
            if text is not None
            else "structured browser receipt omitted rendered DOM text",
            modalities=(VerificationEvidenceModality.DOM_ACCESSIBILITY,) if text else (),
            **common,
        )
    if claim.kind is VerificationClaimKind.CONSOLE_CLEAN:
        available = isinstance(evidence.console_errors, list)
        ok = available and not evidence.console_errors
        return _result(
            claim,
            VerificationClaimStatus.PASS
            if ok
            else VerificationClaimStatus.FAIL
            if available
            else VerificationClaimStatus.UNAVAILABLE,
            "browser runtime reported zero console errors"
            if ok
            else "browser runtime reported console errors"
            if available
            else "structured browser receipt omitted console diagnostics",
            modalities=(VerificationEvidenceModality.RUNTIME_CONSOLE,) if available else (),
            **common,
        )
    if claim.kind is VerificationClaimKind.NETWORK_CLEAN:
        available = isinstance(evidence.network_failures, list)
        ok = available and not evidence.network_failures
        return _result(
            claim,
            VerificationClaimStatus.PASS
            if ok
            else VerificationClaimStatus.FAIL
            if available
            else VerificationClaimStatus.UNAVAILABLE,
            "browser runtime reported zero critical network failures"
            if ok
            else "browser runtime reported critical network failures"
            if available
            else "structured browser receipt omitted network diagnostics",
            modalities=(VerificationEvidenceModality.RUNTIME_NETWORK,) if available else (),
            **common,
        )
    return None


def _extended_structured_claim_result(
    claim: HostVerificationClaim,
    evidence: _StructuredWebEvidence,
) -> HostVerificationClaimResult:
    common = {
        "verifier_id": evidence.verifier_id,
        "basis": evidence.basis,
        "refs": evidence.refs,
    }
    if claim.kind is VerificationClaimKind.INTERACTION:
        interactions = evidence.verdict.get("interaction_claims")
        interaction = interactions.get(claim.claim_id) if isinstance(interactions, dict) else None
        interaction_data = interaction if isinstance(interaction, dict) else {}
        steps = interaction_data.get("steps")
        available = bool(interaction_data) and isinstance(steps, list)
        exact_contract = available and interaction_data.get("expected") == claim.expected
        ok = (
            available
            and exact_contract
            and interaction_data.get("passed") is True
            and bool(steps)
            and all(
                isinstance(step, dict) and step.get("success") is True and not step.get("error")
                for step in steps
            )
        )
        return _result(
            claim,
            VerificationClaimStatus.PASS
            if ok
            else VerificationClaimStatus.FAIL
            if available
            else VerificationClaimStatus.UNAVAILABLE,
            "host interaction sequence completed without errors"
            if ok
            else "host interaction evidence did not prove the exact required outcome"
            if available
            else "no host-owned interaction evidence was available",
            modalities=(VerificationEvidenceModality.INTERACTION,) if available else (),
            **common,
        )
    if claim.kind is VerificationClaimKind.ROUTE:
        ok = evidence.url.rstrip("/") == claim.expected.rstrip("/")
        return _result(
            claim,
            VerificationClaimStatus.PASS if ok else VerificationClaimStatus.FAIL,
            f"observed route was {evidence.url!r}",
            modalities=(VerificationEvidenceModality.NAVIGATION,),
            **common,
        )
    visual = claim.kind is VerificationClaimKind.VISUAL_SEMANTIC
    return _result(
        claim,
        VerificationClaimStatus.UNAVAILABLE,
        "visual-semantic claim requires a configured vision-capable verifier receipt"
        if visual
        else "claim requires an independent target/semantic verifier receipt",
        **common,
    )


def structured_web_verification_result(
    *,
    deliverable: Any,
    verdict: dict[str, Any],
    verifier_id: str | None = None,
    tool_id: str | None = None,
) -> HostVerificationResult:
    """Convert trusted structured browser output into exact claim results.

    Missing fields are unavailable or failed; they are never filled from model
    prose.  Screenshot provenance is retained, but visual claims stay unavailable
    until a separate vision-capable verifier explicitly judges the pixels.
    """

    check = getattr(deliverable, "verification_check", None)
    resolved_verifier_id = verifier_id or (
        check.issuer_id if check is not None else "host.verify_web_app@1"
    )
    resolved_tool_id = tool_id or (check.operation if check is not None else "verify_web_app@1")
    label = str(verdict.get("verdict") or "").lower()
    infrastructure_unavailable = label in {"unavailable", "unverifiable"}
    deterministic_pass = verdict.get("passed") is True and label == "pass"
    url = str(verdict.get("url") or "")
    http_status = verdict.get("http_status")
    rendered_text = verdict.get("rendered_text")
    console_errors = verdict.get("console_errors")
    network_failures = verdict.get("network_failures")
    meaningful = verdict.get("meaningful_content") is True
    raw_freshness = verdict.get("freshness")
    freshness: dict[str, Any] = dict(raw_freshness) if isinstance(raw_freshness, dict) else {}
    basis = "deterministic host structured-browser receipt"
    raw_screenshot_path = str(verdict.get("screenshot_path") or "")
    screenshot_path = (
        raw_screenshot_path
        if len(raw_screenshot_path) <= 512
        and not any(ord(char) < 32 or ord(char) == 127 for char in raw_screenshot_path)
        else ""
    )
    refs = tuple(
        ref
        for ref in (
            f"artifact:{deliverable.artifact_path}",
            f"url:{url}" if url else "",
            f"screenshot:{screenshot_path}" if screenshot_path else "",
        )
        if ref
    )

    claims = deliverable.required_claims or default_structured_web_claims()
    evidence = _StructuredWebEvidence(
        deliverable=deliverable,
        verdict=verdict,
        verifier_id=resolved_verifier_id,
        basis=basis,
        refs=refs,
        deterministic_pass=deterministic_pass,
        url=url,
        http_status=http_status,
        rendered_text=rendered_text,
        console_errors=console_errors,
        network_failures=network_failures,
        meaningful=meaningful,
    )
    results: list[HostVerificationClaimResult] = []
    for claim in claims:
        if infrastructure_unavailable:
            results.append(
                _result(
                    claim,
                    VerificationClaimStatus.UNAVAILABLE,
                    str(verdict.get("summary") or "structured browser verification unavailable"),
                    verifier_id=resolved_verifier_id,
                    basis=basis,
                    refs=refs,
                )
            )
            continue
        result = _basic_structured_claim_result(claim, evidence)
        results.append(result or _extended_structured_claim_result(claim, evidence))

    status = (
        VerificationClaimStatus.FAIL
        if any(
            result.required and result.status is VerificationClaimStatus.FAIL for result in results
        )
        else VerificationClaimStatus.UNAVAILABLE
        if any(
            result.required and result.status is VerificationClaimStatus.UNAVAILABLE
            for result in results
        )
        else VerificationClaimStatus.PASS
    )
    screenshot_b64 = verdict.get("screenshot_b64")
    screenshot_sha256 = None
    if isinstance(screenshot_b64, str) and screenshot_b64:
        validated = validated_image_data_url(f"data:image/png;base64,{screenshot_b64}")
        if validated is not None:
            _, screenshot_sha256 = validated
    reason = (
        "all mandatory verification claims are covered by current trusted receipts"
        if status is VerificationClaimStatus.PASS
        else next(
            result.reason for result in results if result.required and result.status is status
        )
    )
    raw_workspace_epoch = freshness.get("synchronized_epoch")
    workspace_epoch = (
        raw_workspace_epoch
        if isinstance(raw_workspace_epoch, int)
        and not isinstance(raw_workspace_epoch, bool)
        and raw_workspace_epoch > 0
        else None
    )
    receipt = HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
        **_target_binding_fields(deliverable),
        agent_view_id=deliverable.agent_view_id,
        deliverable_event_id=deliverable.deliverable_event_id,
        artifact_path=deliverable.artifact_path,
        artifact_kind=deliverable.artifact_kind,
        observed_url=url,
        preview_selection=deliverable.preview_selection,
        workspace_revision=deliverable.workspace_revision,
        workspace_generation=str(freshness.get("executor_generation") or ""),
        workspace_epoch=workspace_epoch,
        observed_after_seq=deliverable.observed_after_seq,
        verifier_id=resolved_verifier_id,
        tool_id=resolved_tool_id,
        status=status,
        reason=reason,
        claim_results=tuple(results),
        screenshot_path=screenshot_path,
        screenshot_sha256=screenshot_sha256,
    )
    return with_verification_effect_receipt(receipt)


def apply_semantic_verifier_result(
    receipt: HostVerificationResult,
    *,
    verified: bool,
    verdict: str,
    detail: str,
    verifier_id: str = "model_role.verifier@1",
    claim_id: str | None = None,
) -> HostVerificationResult:
    """Apply one bounded independent judge to one exact semantic claim.

    The no-id form is retained for the standalone one-claim adapter.  It refuses
    to fan one aggregate judgement across multiple semantic requirements.
    """

    semantic_kinds = {
        VerificationClaimKind.CONTRACT_SEMANTIC,
        VerificationClaimKind.VISUAL_SEMANTIC,
    }
    label = str(verdict).lower()
    status = (
        VerificationClaimStatus.PASS
        if verified and label == "pass"
        else VerificationClaimStatus.UNAVAILABLE
        if label in {"unavailable", "unverifiable"}
        else VerificationClaimStatus.FAIL
    )
    semantic_results = [result for result in receipt.claim_results if result.kind in semantic_kinds]
    target_id = claim_id or (semantic_results[0].claim_id if len(semantic_results) == 1 else None)
    updated: list[HostVerificationClaimResult] = []
    for result in receipt.claim_results:
        if result.kind not in semantic_kinds or result.claim_id != target_id:
            updated.append(result)
            continue
        visual = result.kind is VerificationClaimKind.VISUAL_SEMANTIC
        applied_status = (
            VerificationClaimStatus.UNAVAILABLE
            if visual
            and status is VerificationClaimStatus.PASS
            and receipt.screenshot_sha256 is None
            else status
        )
        updated.append(
            result.model_copy(
                update={
                    "status": applied_status,
                    "reason": (
                        "vision verifier cannot pass without captured pixel evidence"
                        if applied_status is VerificationClaimStatus.UNAVAILABLE
                        and status is VerificationClaimStatus.PASS
                        else detail or f"independent verifier returned {label}"
                    ),
                    "verifier_id": verifier_id,
                    "capability_basis": (
                        "configured independent vision-capable verifier"
                        if visual
                        else "configured independent semantic verifier"
                    ),
                    "evidence_modalities": (
                        (VerificationEvidenceModality.SCREENSHOT_PIXELS,)
                        if visual and applied_status is not VerificationClaimStatus.UNAVAILABLE
                        else ()
                    ),
                }
            )
        )
    overall = (
        VerificationClaimStatus.FAIL
        if any(item.required and item.status is VerificationClaimStatus.FAIL for item in updated)
        else VerificationClaimStatus.UNAVAILABLE
        if any(
            item.required and item.status is VerificationClaimStatus.UNAVAILABLE for item in updated
        )
        else VerificationClaimStatus.PASS
    )
    reason = (
        "all mandatory verification claims are covered by current trusted receipts"
        if overall is VerificationClaimStatus.PASS
        else next(item.reason for item in updated if item.required and item.status is overall)
    )
    updated_receipt = receipt.model_copy(
        update={
            "status": overall,
            "reason": reason,
            "claim_results": tuple(updated),
            "effect_receipt": None,
        }
    )
    return with_verification_effect_receipt(updated_receipt)


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

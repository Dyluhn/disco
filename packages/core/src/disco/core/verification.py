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
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
                static_serve_dir=(
                    str(intent.get("serve_dir") or ".") if launch_kind == "static" else None
                ),
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

    def is_current_for(self, deliverable: Any, *, observed_url: str) -> bool:
        """Exact authority comparison; no fuzzy or name-derived matching."""

        claims = deliverable.required_claims or default_structured_web_claims()
        return bool(
            self.covers(claims)
            and self.conversation_id == deliverable.conversation_id
            and self.run_intent_id == deliverable.run_intent_id
            and self.run_identity == deliverable.run_identity
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
    verifier_id: str = "host.verify_web_app@1",
    tool_id: str = "verify_web_app@1",
) -> HostVerificationResult:
    """Convert trusted structured browser output into exact claim results.

    Missing fields are unavailable or failed; they are never filled from model
    prose.  Screenshot provenance is retained, but visual claims stay unavailable
    until a separate vision-capable verifier explicitly judges the pixels.
    """

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
        verifier_id=verifier_id,
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
                    verifier_id=verifier_id,
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
    return HostVerificationResult(
        conversation_id=deliverable.conversation_id,
        run_intent_id=deliverable.run_intent_id,
        run_identity=deliverable.run_identity,
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
        verifier_id=verifier_id,
        tool_id=tool_id,
        status=status,
        reason=reason,
        claim_results=tuple(results),
        screenshot_path=screenshot_path,
        screenshot_sha256=screenshot_sha256,
    )


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
    return receipt.model_copy(
        update={"status": overall, "reason": reason, "claim_results": tuple(updated)}
    )


__all__ = [
    "HostVerificationClaim",
    "HostVerificationClaimResult",
    "HostVerificationResult",
    "PreviewSelectionIdentity",
    "VerifierReferenceImage",
    "VerificationClaimKind",
    "VerificationClaimStatus",
    "VerificationEvidenceModality",
    "VerificationRequestedClaim",
    "VerificationRequirementsDirective",
    "apply_semantic_verifier_result",
    "default_structured_web_claims",
    "requires_structured_browser_runtime",
    "structured_web_verification_result",
    "validated_image_data_url",
]

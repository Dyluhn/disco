"""Typed result models for release detection: the public verdict shapes plus
the private internal findings the detectors pass between themselves.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple

from disco.core.release.spec import (
    EnvVarDecl,
    ReleaseAssessment,
    ReleaseService,
    ResourceDecl,
    ServiceRole,
)
from pydantic import BaseModel, Field

from ._constants import _STRICT


class Provenance(BaseModel):
    """How the workspace arrived, as TYPED data the detector may branch on.

    `imported` is the only decision bit (an imported repo we could not detect
    deterministically needs owner review); `evidence` carries human strings that
    are reported in the result but never change the verdict."""

    model_config = _STRICT

    imported: bool = False
    evidence: tuple[str, ...] = Field(default_factory=tuple)


class MissingField(BaseModel):
    """One release-contract field a `needs_review` result could not establish —
    the machine-readable half of the repairable diagnostic (`field` is a contract
    field name; `detail` explains why it is missing/unverifiable)."""

    model_config = _STRICT

    field: str
    detail: str


class DetectionBlocker(BaseModel):
    """A fine-grained, TYPED reason a workspace fails closed to `needs_review`.

    `code` is a stable, machine-readable code the release API returns verbatim on
    the blocker's `code` field (`required_env_unresolved`, `port_contract_unresolved`,
    `entrypoint_unresolved`, `toolchain_unsupported`, `output_dir_unresolved`,
    `health_path_unresolved`, `runtime_conflict`, `persistent_path_unbackable`); `field`
    optionally names the
    release-contract field an owner must declare; `path` optionally names a
    workspace path the finding is about. Distinct from `MissingField`: a
    `DetectionBlocker` carries the EXACT typed code (not the coarse
    `release_field_unresolved`), so a predictable defect is diagnosable."""

    model_config = _STRICT

    code: str
    message: str
    field: str | None = None
    path: str | None = None


class DetectionResult(BaseModel):
    """The detector's verdict as immutable DATA.

    Carries the `assessment` plus the raw findings a caller assembles into a full
    `ReleaseSpec` (the derived `services` / `resources` / `env`), and the human
    `evidence` / `reasons` / `missing` diagnostic. It stops short of a full
    `ReleaseSpec` on purpose: the detector has the contents but NOT the source
    binding (`version_seq` / `tree_digest`) a spec must pin to, so a later work
    order stitches these findings to the committed snapshot."""

    model_config = _STRICT

    assessment: ReleaseAssessment
    services: tuple[ReleaseService, ...] = Field(default_factory=tuple)
    resources: tuple[ResourceDecl, ...] = Field(default_factory=tuple)
    env: tuple[EnvVarDecl, ...] = Field(default_factory=tuple)
    evidence: tuple[str, ...] = Field(default_factory=tuple)
    reasons: tuple[str, ...] = Field(default_factory=tuple)
    missing: tuple[MissingField, ...] = Field(default_factory=tuple)
    blockers: tuple[DetectionBlocker, ...] = Field(default_factory=tuple)

    @property
    def ingress(self) -> ReleaseService | None:
        """The single ingress-role service, if one was derived (else `None`)."""
        for service in self.services:
            if service.role is ServiceRole.ingress:
                return service
        return None


class _DbKind(Enum):
    none = "none"
    sqlite = "sqlite"
    unknown = "unknown"


class _DbFinding(NamedTuple):
    kind: _DbKind
    engine: str
    evidence: tuple[str, ...]
    has_url: bool


class _DetectBlocker(NamedTuple):
    """An internal fail-closed outcome from a runtime detector: the workspace has
    THIS runtime's signature but a predictable, unrepairable-without-declaration
    defect, so detection fails closed with the exact typed `code`. A detector
    returns `None` (signature absent — try the next runtime), a `ReleaseService`
    (a resolved candidate), or a `_DetectBlocker` (signature present but unreleasable)."""

    code: str
    message: str
    field: str
    evidence: tuple[str, ...]

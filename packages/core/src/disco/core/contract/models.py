"""Core Build Artifact Contract models (CONTRACT-1).

Pure, frozen, serializable Pydantic v2 value objects (house style: `BaseModel` +
`ConfigDict(frozen=True, extra="forbid")`, `model_dump(mode="json")` round-trip). No
runtime/tool/frontend imports.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# Host finalizers are the ready_for_*_verification family (§3.2). Pinning the shape
# keeps minimal()/manual contracts from naming a finalizer the platform can't route.
_FINALIZER_RE = re.compile(r"^ready_for_[a-z0-9_]+_verification$")


class ContractKind(str, Enum):
    """The artifact kinds a Build run can declare. Values are the on-the-wire ids."""

    APPKIT_LEADGEN = "appkit.leadgen"
    STATIC_SITE = "static.site"
    INTERACTIVE_PROTOTYPE = "interactive.prototype"
    DECK = "deck"
    DOCUMENT = "document"
    WORKFLOW_OUTPUT = "workflow.output"
    CUSTOM = "custom"


class VerificationLevel(str, Enum):
    """How hard the finalizer verifies before a contract is 'done'."""

    LOAD_ONLY = "load_only"  # small copy change → just confirm it still loads
    STANDARD = "standard"  # feature/data change → route + console + verify
    STRICT = "strict"  # export/deploy → full check


class ToolPack(BaseModel):
    """A named, ordered allowlist of tool names available in a contract phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    tools: tuple[str, ...] = ()
    description: str = ""


class EditContract(BaseModel):
    """How a contract's artifact may be MUTATED. Targeted, semantic edits are the
    norm; a raw rewrite is available only when ``rewrite_allowed`` (repair/custom)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edit_tools: tuple[str, ...] = ()
    repair_tools: tuple[str, ...] = ()
    rewrite_allowed: bool = False


class VerificationContract(BaseModel):
    """The host-owned finalizer that decides a contract is done (NOT the model)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finalizer: str  # e.g. "ready_for_app_verification"
    level: VerificationLevel = VerificationLevel.STANDARD

    @field_validator("finalizer")
    @classmethod
    def _finalizer_shape(cls, v: str) -> str:
        if not _FINALIZER_RE.match(v):
            raise ValueError(
                f"finalizer {v!r} must match the host ready_for_*_verification convention"
            )
        return v


class ExportContract(BaseModel):
    """A host-owned export pipeline (preflight → bundle → validate → deliver)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    pipeline: tuple[str, ...] = ()


class ArtifactContract(BaseModel):
    """WHAT the deliverable is: its kind, the files it must produce, and the starter
    kit it scaffolds from (host-owned, so the model doesn't hand-draw common frames)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ContractKind
    required_files: tuple[str, ...] = ()
    starter_kit: str | None = None


class BuildContract(BaseModel):
    """The full contract a Build run declares before execution. Binds the artifact to
    its bootstrap/edit/verify/export rails + the prompt pack and UI card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ContractKind
    artifact: ArtifactContract
    bootstrap: ToolPack
    edit: EditContract
    verify: VerificationContract
    export: ExportContract | None = None
    prompt_pack: str | None = None  # workflow prompt pack id (P3)
    ui_card: str | None = None  # the UI card that renders this contract's state

    @model_validator(mode="after")
    def _kind_coherent(self) -> BuildContract:
        if self.kind != self.artifact.kind:
            raise ValueError(
                f"BuildContract.kind ({self.kind.value}) != artifact.kind ({self.artifact.kind.value})"
            )
        return self

    @classmethod
    def minimal(cls, kind: ContractKind, *, finalizer: str = "ready_for_artifact_verification") -> BuildContract:
        """A minimal well-formed contract for ``kind`` (no required files, empty packs).
        Real contracts come from the BuildContractRegistry (CONTRACT-2)."""
        return cls(
            kind=kind,
            artifact=ArtifactContract(kind=kind),
            bootstrap=ToolPack(name=f"{kind.value}.bootstrap"),
            edit=EditContract(),
            verify=VerificationContract(finalizer=finalizer),
        )

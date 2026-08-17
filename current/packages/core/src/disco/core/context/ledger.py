"""ContextLedger + its small reference value objects.

The ledger is the durable, in-memory representation of a Build run's context:
goal, contract, version, the live todo pointer, resolved (omittable) ranges,
unresolved verifier failures, user direct edits, imported resources, and handoff
packages. CXT-2 populates it from ``.disco/context/*`` files; CXT-4 projects it
into a model-facing ContextPack. This module is pure value objects — no I/O, no
event/runtime imports.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._util import _mk_id, _now
from .artifact_memory import ArtifactMemoryRef


class Severity(str, Enum):
    BLOCKER = "blocker"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class DirectEditKind(str, Enum):
    TEXT = "text"
    STYLE = "style"
    STRUCTURE = "structure"
    DELETE = "delete"


class ResolvedContextRange(BaseModel):
    """A span of event history marked resolved → omittable from the model view
    (but never deleted from the audit log; that invariant is enforced in CXT-3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    range_id: str = Field(default_factory=lambda: _mk_id("cxr_"))
    reason: str
    event_ids: tuple[str, ...] = ()
    summary_ref: ArtifactMemoryRef | None = None


class DirectEditRef(BaseModel):
    """A user's direct edit to an artifact. ``target_id`` is a caller-supplied
    stable anchor (e.g. a ``data-disco-*`` id), NOT auto-generated, so the agent
    can reconcile against the same target later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str
    rel_path: str
    kind: DirectEditKind
    summary: str = ""
    at: datetime = Field(default_factory=_now)


class VerifierFailureRef(BaseModel):
    """A compact, actionable verifier failure surfaced to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_id: str = Field(default_factory=lambda: _mk_id("vf_"))
    kind: str
    message: str
    rel_path: str | None = None
    severity: Severity = Severity.ERROR
    resolved: bool = False


class ResourceRef(BaseModel):
    """An external resource copied into the workspace, with provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rel_path: str
    source: str
    sha256: str | None = None
    license: str | None = None
    copied_at: datetime | None = None


class ArtifactRecord(BaseModel):
    """[REL-2a] One record in the shared per-artifact runtime manifest — the single folded truth
    for an OUTPUT artifact. Frozen: an upsert reads the whole list, replaces the matching record,
    and writes it back (no in-place mutation), so each record stays immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: str = "files"  # app | files | deck | sheet | pdf | audio | ...
    sha256: str | None = None
    size_bytes: int | None = None
    shown: bool = False
    verified: bool = False
    verify_verdict: str | None = None
    export: dict[str, str] = Field(default_factory=dict)  # {fmt: iso_ts} — when each export landed
    preview_status: str | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_verified(cls, data: Any) -> Any:
        """Accept the REL-2a shadow-era tri-state string if an old manifest has it."""
        if not isinstance(data, dict):
            return data
        raw = data.get("verified")
        if not isinstance(raw, str):
            return data
        out = dict(data)
        normalized = raw.strip().lower()
        out["verified"] = normalized == "passed"
        out.setdefault("verify_verdict", None if normalized in ("", "unverified") else raw)
        return out


class HandoffRef(BaseModel):
    """A pointer to a produced handoff package (owner/developer/deployment)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    rel_path: str


class ContextLedger(BaseModel):
    """The durable, structured context for a single Build conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: str
    workspace_root: str | None = None
    active_goal: str | None = None
    active_contract: str | None = None
    current_version: int = 0
    todo_ref: ArtifactMemoryRef | None = None
    retained_refs: tuple[ArtifactMemoryRef, ...] = ()
    resolved_ranges: tuple[ResolvedContextRange, ...] = ()
    latest_verifier_failures: tuple[VerifierFailureRef, ...] = ()
    unresolved_comments: tuple[str, ...] = ()
    direct_edits: tuple[DirectEditRef, ...] = ()
    resource_manifest: tuple[ResourceRef, ...] = ()
    handoff_refs: tuple[HandoffRef, ...] = ()

    @classmethod
    def empty(cls, conversation_id: str, workspace_root: str | None = None) -> ContextLedger:
        return cls(conversation_id=conversation_id, workspace_root=workspace_root)

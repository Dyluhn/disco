"""WO-7 — the agent-server release API + the assessment behind the Download upgrade.

`GET /api/projects/{cid}/release` reads a project's committed workspace + its
host-owned release-intent sidecar (WO-5), runs the PURE detector (WO-3) +
validator (WO-2), and returns a JSON verdict: can this app be self-hosted, what
env NAMES it needs, its ingress, and a stable `spec_digest`. The verdict is
computed ON REQUEST — nothing runs at finish time, and NOTHING here spawns a
process, touches a container engine, or reaches the network. Detection,
validation, and overlay emission are pure functions over the immutable contents.

The assessment is IMMUTABLY source-bound (WO-C2): it reads a real committed
version workspace (hash-verified against its `VersionRecord` digest), never the
mutable live mirror, and a tree that matches no committed version fails closed
with a `source_not_snapshotted` blocker rather than inventing a sequence. The same
per-version assessment (`assess_version`) powers the bound self-host Download
(`/download?version_seq=N&spec_digest=D`): when a version assesses `candidate` AND
validation passes, the streamed zip additionally carries the WO-4 self-host overlay
(`compose.yaml`, `Dockerfile`(s), `.dockerignore`, `.env.example`, `SELFHOST.md`,
`release.json`). A workspace file always wins a path collision — the overlay entry
is dropped and reported as a blocker on `/release` (never a false affordance).

Secret hygiene is inherited, not re-invented: the workspace view is read through
the store's `iter_workspace`, which already excludes every runtime-secret path
(`.env`, `.dev.vars`, …). The detector, the validator, the emitted overlay, and
this endpoint's JSON therefore NEVER observe secret material — the release
contract records env-var NAMES only, and the host injects the values out-of-band.

Layering: `agent_server` may import `disco.core` (the detector / validator /
emitter / spec) and `disco.tools` (the project store) — both are legal downward
edges. There is no mode / lane branching here: the verdict is a pure function of
the committed contents + the typed intent, nothing else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from disco.core.release.detect import DetectionResult, Provenance, detect_release
from disco.core.release.local_compose import emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseSpec,
    SecretClass,
    spec_digest,
)
from disco.core.release.validate import validate_release
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.projects import ProjectRecord, ProjectStore, StorageError, is_runtime_secret_path
from disco.tools.projects.store import VersionRecord
from disco.tools.projects.store import tree_digest as compute_tree_digest
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from ..runtime import ConversationRuntime
from .projects import _resolve_project_for_read

# The single, provider-neutral run command every local self-host bundle uses (the
# WO-4 adapter documents the same command). Constant, never computed per request.
_RELEASE_COMMAND = "docker compose up -d --build"

# Detector identity recorded in the spec provenance so an assessment is auditable
# without re-running detection. A version tag, not a semantic gate.
_DETECTOR_NAME = "disco.core.release.detect"
_DETECTOR_VERSION = "1"

# `DetectorProvenance` caps: evidence strings are _ShortStr (<=120), the reason is
# _ReasonStr (<=2000). Detection strings are short in practice; clamp defensively
# so a long DB-path evidence line can never make spec construction raise.
_EVIDENCE_MAX = 120
_REASON_MAX = 2000


# ---- response models ----------------------------------------------------------


class _ResponseBlocker(BaseModel):
    """One reason a release is not (fully) ready, as flat DATA. `field` names a
    release-contract field an owner must declare (a repairable diagnostic); `path`
    names a workspace/overlay path a finding is about. Both optional so detection
    diagnostics, validation blockers, and overlay-collision reports share ONE
    shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    field: str | None = None
    path: str | None = None


class _ResponseEnv(BaseModel):
    """A declared env var the release reads — its NAME + metadata, NEVER a value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    scope: str
    required: bool
    secret: bool


class _IngressInfo(BaseModel):
    """The single public entrypoint of a detected release. `port` is the env-var
    NAME the ingress binds (the `$PORT` contract) — the neutral spec carries no
    literal port number; the host owns the concrete port at deploy time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    port: str
    health_path: str | None = None


class ReleaseResponse(BaseModel):
    """The `/release` verdict. The key set is IDENTICAL for every assessment
    (`candidate` / `needs_review` / `not_web`): non-applicable fields are `null`
    or empty rather than absent, so a consumer parses one stable shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment: str
    reasons: list[str]
    blockers: list[_ResponseBlocker]
    required_env: list[_ResponseEnv]
    command: str
    ingress: _IngressInfo | None
    self_host: bool
    spec_digest: str | None
    # Source binding — mirrors `store.VersionRecord`. NULLABLE: null when no exact
    # committed source can be named (a workspace never snapshotted). Whenever these
    # are non-null they ALWAYS resolve to a real `VersionRecord`; a hypothetical
    # next sequence is never fabricated (locked semantics §2 #4).
    version_seq: int | None
    tree_digest: str | None


@dataclass(frozen=True)
class AssessedRelease:
    """The complete assessment: the JSON `response` for `/release`, plus the
    `overlay_files` (`{path: text}`) the Download endpoint injects into the zip.
    `overlay_files` is EMPTY unless the project is a self-hostable `candidate`
    whose validation passed and whose overlay paths do not collide with existing
    workspace files (a colliding overlay entry is dropped — the workspace wins)."""

    response: ReleaseResponse
    overlay_files: dict[str, str]


# ---- spec assembly (stitch detection findings to the source binding) ----------


def _clamp(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _build_spec(
    detection: DetectionResult,
    *,
    name: str,
    version_seq: int,
    tree_digest: str,
) -> ReleaseSpec:
    """Stitch a `DetectionResult`'s findings to the committed source binding
    (`version_seq` + `tree_digest`) into a full, validated `ReleaseSpec`. The
    detector deliberately stops short of a spec (it has the contents but not the
    binding); this is where the two are joined."""
    ingress = detection.ingress
    reason = detection.reasons[0] if detection.reasons else None
    provenance = DetectorProvenance(
        detector=_DETECTOR_NAME,
        detector_version=_DETECTOR_VERSION,
        assessment=detection.assessment,
        evidence=tuple(_clamp(signal, _EVIDENCE_MAX) for signal in detection.evidence),
        reason=_clamp(reason, _REASON_MAX) if reason is not None else None,
    )
    kind = ingress.runtime.value if ingress is not None else "web"
    return ReleaseSpec(
        kind=kind,
        name=_clamp(name, 200) or "app",
        version_seq=version_seq,
        tree_digest=tree_digest,
        services=detection.services,
        env=detection.env,
        resources=detection.resources,
        provenance=provenance,
    )


# ---- the pure assessment ------------------------------------------------------


def _required_env_view(detection: DetectionResult) -> list[_ResponseEnv]:
    return [
        _ResponseEnv(
            name=var.name,
            scope=var.scope.value,
            required=var.required,
            secret=var.secret is SecretClass.secret,
        )
        for var in detection.env
    ]


def _fail_closed_assessment(
    detection: DetectionResult,
    *,
    version_seq: int | None,
    tree_digest: str | None,
    source_blocker: _ResponseBlocker | None,
) -> AssessedRelease:
    """The verdict when the assessed tree is NOT a verified committed version.

    No self-host bundle is ever offered here (`self_host=False`, empty overlay,
    `spec_digest=None`). A statically plausible web app is DOWNGRADED from
    `candidate` to `needs_review`, and `source_blocker` (the unsnapshotted-drift or
    integrity diagnostic) explains why it cannot be self-hosted. A tree that is not
    a web app keeps its honest `not_web` / `needs_review` verdict and its repairable
    field diagnostics — snapshotting would not change that. The source fields carry
    whatever real record could be named (or null), never a fabricated sequence."""
    if detection.assessment is ReleaseAssessment.candidate:
        assessment = ReleaseAssessment.needs_review
        blockers = [source_blocker] if source_blocker is not None else []
    else:
        assessment = detection.assessment
        blockers = [
            _ResponseBlocker(code="release_field_unresolved", field=item.field, message=item.detail)
            for item in detection.missing
        ]
    response = ReleaseResponse(
        assessment=assessment.value,
        reasons=list(detection.reasons),
        blockers=blockers,
        required_env=_required_env_view(detection),
        command=_RELEASE_COMMAND,
        ingress=None,
        self_host=False,
        spec_digest=None,
        version_seq=version_seq,
        tree_digest=tree_digest,
    )
    return AssessedRelease(response=response, overlay_files={})


def assess_release(
    files: Mapping[str, bytes],
    *,
    intent: ReleaseIntent | None,
    project_name: str,
    version_seq: int | None,
    tree_digest: str | None,
    source_snapshotted: bool,
    imported: bool = False,
    source_blocker: _ResponseBlocker | None = None,
) -> AssessedRelease:
    """Assess a workspace's release readiness — PURE and deterministic.

    Reads ONLY the immutable file view + the typed intent + the `imported`
    provenance bit: runs `detect_release`, and (for a `candidate` with a valid spec)
    `validate_release` + the WO-4 overlay emission. Returns the JSON verdict and the
    (possibly empty) overlay to inject. No process, no container, no network, no
    clock, no randomness — the same inputs return an EQUAL result every time.

    `source_snapshotted` is the fail-closed gate: only a tree that is a VERIFIED
    committed version (its stored workspace re-hashes to its recorded digest, and its
    digest equals the live tree) may become a self-host `candidate`. When it is
    False, `_fail_closed_assessment` is returned instead — no bundle, and a
    would-be candidate is downgraded to `needs_review` with `source_blocker`.

    `imported=True` (a project whose workspace was seeded by a code import) makes an
    UNRECOGNIZED stack fail closed to `needs_review` rather than `not_web` (WO-3 rung
    4): an imported tree we can't shape still needs an owner declaration.
    """
    detection = detect_release(files, intent=intent, provenance=Provenance(imported=imported))

    if not source_snapshotted:
        return _fail_closed_assessment(
            detection,
            version_seq=version_seq,
            tree_digest=tree_digest,
            source_blocker=source_blocker,
        )
    # A verified committed source: `version_seq` / `tree_digest` are guaranteed to
    # name that real record, so the spec can be pinned to it.
    assert version_seq is not None and tree_digest is not None

    ingress = detection.ingress
    assessment = detection.assessment
    spec: ReleaseSpec | None = None
    ingress_info: _IngressInfo | None = None
    digest: str | None = None
    spec_error: str | None = None
    if ingress is not None:
        try:
            spec = _build_spec(
                detection, name=project_name, version_seq=version_seq, tree_digest=tree_digest
            )
        except ValidationError as exc:
            # A SCHEMA-valid intent can still describe an INCONSISTENT release — e.g. a
            # resource naming a consumer service the release does not define — which
            # only trips the ReleaseSpec cross-field validators at assembly. That is a
            # repairable owner problem, not a server fault: fail closed to needs_review
            # with a typed blocker rather than letting a 500 escape. The spec models set
            # `hide_input_in_errors`, so the message names FIELDS/ids only — never a
            # secret VALUE — and it is length-clamped defensively.
            assessment = ReleaseAssessment.needs_review
            spec_error = _clamp(str(exc), _REASON_MAX)
        else:
            digest = spec_digest(spec)
            ingress_info = _IngressInfo(
                service=ingress.id, port=ingress.port_env, health_path=ingress.health_path
            )

    blockers: list[_ResponseBlocker] = []
    overlay_files: dict[str, str] = {}
    self_host = False

    if spec_error is not None:
        blockers.append(
            _ResponseBlocker(
                code="release_spec_invalid",
                message=(
                    "the declared release intent could not be assembled into a valid "
                    f"release spec and needs owner review: {spec_error}"
                ),
            )
        )
    elif assessment is ReleaseAssessment.candidate and spec is not None:
        validation = validate_release(spec, files)
        if validation.ok:
            self_host = True
            overlay = emit_local_compose(spec)
            present = set(files)
            for path in sorted(overlay):
                if path in present:
                    # The workspace already has a file here — it wins; the generated
                    # overlay entry is dropped and reported so the choice is legible.
                    blockers.append(
                        _ResponseBlocker(
                            code="overlay_suppressed_by_workspace_file",
                            message=(
                                f"the generated {path!r} is suppressed because the workspace "
                                "already contains a file at that path (the workspace file is kept)."
                            ),
                            path=path,
                        )
                    )
                elif not is_runtime_secret_path(path):
                    overlay_files[path] = overlay[path]
        else:
            blockers.extend(
                _ResponseBlocker(
                    code=blocker.code.value, message=blocker.message, path=blocker.path
                )
                for blocker in validation.blockers
            )
    else:
        blockers.extend(
            _ResponseBlocker(code="release_field_unresolved", field=item.field, message=item.detail)
            for item in detection.missing
        )

    response = ReleaseResponse(
        assessment=assessment.value,
        reasons=list(detection.reasons),
        blockers=blockers,
        required_env=_required_env_view(detection),
        command=_RELEASE_COMMAND,
        ingress=ingress_info,
        self_host=self_host,
        spec_digest=digest,
        version_seq=version_seq,
        tree_digest=tree_digest,
    )
    return AssessedRelease(response=response, overlay_files=overlay_files)


def _read_tree(root: Path) -> dict[str, bytes]:
    """The immutable file view of a workspace directory: sorted workspace-relative
    POSIX path → bytes, with symlinks and runtime-secret paths excluded — the exact
    file set `store.tree_digest` hashes, so the read tree's digest equals the
    matching `VersionRecord.tree_digest`."""
    files: dict[str, bytes] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if is_runtime_secret_path(rel):
            continue
        files[rel] = path.read_bytes()
    return files


def _integrity_blocker(detail: str) -> _ResponseBlocker:
    return _ResponseBlocker(
        code="source_integrity_failed",
        message=(
            "the committed version workspace could not be verified against its "
            f"recorded digest ({detail}); its source integrity is untrustworthy."
        ),
    )


@dataclass(frozen=True)
class _ResolvedSource:
    """Which immutable committed version an assessment binds to.

    `verified` + `workspace` are set ONLY when the live tree exactly matches a
    committed version whose STORED workspace re-hashes to its recorded digest — the
    sole path that may yield a self-host candidate, and the tree the assessment then
    reads from (never the mutable live mirror). Otherwise the tree is not a
    trustworthy snapshot: `blocker` explains why (unsnapshotted drift / integrity),
    and `version_seq` / `tree_digest` name the newest REAL record if one exists (or
    are null when none does) — never a fabricated `max+1` sequence."""

    verified: VersionRecord | None
    workspace: Path | None
    version_seq: int | None
    tree_digest: str | None
    blocker: _ResponseBlocker | None


def _resolve_source(ps: ProjectStore, conversation_id: str, workspace: Path) -> _ResolvedSource:
    """Bind the live workspace to an immutable committed version, hash-verifying it.

    The live tree is hashed and matched against the committed versions (newest
    match wins). A match's stored workspace is re-hashed and required to equal its
    recorded digest before it is trusted (a tampered/corrupt version fails closed
    with a source-integrity blocker). With no exact match the current tree was never
    snapshotted: fail closed with `source_not_snapshotted`, naming the newest real
    record's `(seq, digest)` if one exists, else null — the route never invents a
    sequence for a tree that was never committed."""
    live_digest = compute_tree_digest(workspace)
    try:
        versions = ps.list_versions(conversation_id)  # newest first
    except StorageError:
        versions = []

    exact = next((record for record in versions if record.tree_digest == live_digest), None)
    if exact is not None:
        try:
            version_ws = ps.version_workspace_path(conversation_id, exact.seq)
            rehashed = compute_tree_digest(version_ws)
        except StorageError:
            return _ResolvedSource(
                None,
                None,
                exact.seq,
                exact.tree_digest,
                _integrity_blocker("missing or unreadable"),
            )
        if rehashed != exact.tree_digest:
            return _ResolvedSource(
                None,
                None,
                exact.seq,
                exact.tree_digest,
                _integrity_blocker("stored bytes no longer match"),
            )
        return _ResolvedSource(exact, version_ws, exact.seq, exact.tree_digest, None)

    not_snapshotted = _ResponseBlocker(
        code="source_not_snapshotted",
        message=(
            "the current workspace has not been captured as a committed version, so "
            "it cannot be pinned to an immutable source; commit a version first."
        ),
    )
    newest = versions[0] if versions else None
    if newest is not None:
        return _ResolvedSource(None, None, newest.seq, newest.tree_digest, not_snapshotted)
    return _ResolvedSource(None, None, None, None, not_snapshotted)


def assess_version(
    ps: ProjectStore,
    record: ProjectRecord,
    conversation_id: str,
    version_record: VersionRecord,
    version_workspace: Path,
) -> AssessedRelease:
    """Assess ONE named committed version (its immutable stored workspace), pinned to
    that version's `(seq, tree_digest)`. The download binding uses this to assess the
    exact requested version — independent of the mutable live mirror."""
    intent = ps.read_release_intent(conversation_id)
    files = _read_tree(version_workspace)
    return assess_release(
        files,
        intent=intent,
        project_name=record.title or conversation_id,
        version_seq=version_record.seq,
        tree_digest=version_record.tree_digest,
        source_snapshotted=True,
        imported=record.imported,
    )


def assess_project(
    ps: ProjectStore, record: ProjectRecord, workspace: Path, conversation_id: str
) -> AssessedRelease:
    """Read a project's host-owned intent sidecar and assess its source-bound release
    readiness. The tree assessed is the VERIFIED committed version that matches the
    live workspace (read from the immutable version store, not the mutable mirror);
    when the live tree matches no committed version the assessment fails closed. Raises
    `StorageError` on a corrupt intent sidecar (an honest failure, never a silent
    'no intent')."""
    intent = ps.read_release_intent(conversation_id)
    name = record.title or conversation_id
    source = _resolve_source(ps, conversation_id, workspace)
    if source.verified is not None and source.workspace is not None:
        return assess_release(
            _read_tree(source.workspace),
            intent=intent,
            project_name=name,
            version_seq=source.version_seq,
            tree_digest=source.tree_digest,
            source_snapshotted=True,
            imported=record.imported,
        )
    return assess_release(
        _read_tree(workspace),
        intent=intent,
        project_name=name,
        version_seq=source.version_seq,
        tree_digest=source.tree_digest,
        source_snapshotted=False,
        imported=record.imported,
        source_blocker=source.blocker,
    )


# ---- the router ---------------------------------------------------------------


def make_release_router(store: SqliteEventStore, runtime: ConversationRuntime | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{conversation_id}/release")
    async def project_release(conversation_id: str, request: Request) -> ReleaseResponse:
        """Assess whether a project's committed workspace can be released for
        self-hosting. Owner-scoped exactly like the download endpoint (403
        `project_forbidden` / 404 `project_not_found` / 404 `storage_unavailable`).
        Computed on request from the immutable contents + the typed release intent —
        no process is spawned and nothing runs at finish time."""
        ps, record, workspace = await _resolve_project_for_read(
            request, store, runtime, conversation_id
        )
        try:
            # Offload the workspace read + hash-verify (potentially many MB) to a
            # worker thread so it never blocks the async event loop (WO-C2 §6.11).
            assessed = await asyncio.to_thread(
                assess_project, ps, record, workspace, record.conversation_id
            )
        except StorageError as exc:
            raise HTTPException(
                status_code=500,
                detail={"reason": "release_intent_unreadable", "message": str(exc)},
            ) from exc
        return assessed.response

    return router


__all__ = [
    "AssessedRelease",
    "ReleaseResponse",
    "assess_project",
    "assess_release",
    "assess_version",
    "make_release_router",
]

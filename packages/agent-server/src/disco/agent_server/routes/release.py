"""WO-7 — the agent-server release API + the assessment behind the Download upgrade.

`GET /api/projects/{cid}/release` reads a project's committed workspace + its
host-owned release-intent sidecar (WO-5), runs the PURE detector (WO-3) +
validator (WO-2), and returns a JSON verdict: can this app be self-hosted, what
env NAMES it needs, its ingress, and a stable `spec_digest`. The verdict is
computed ON REQUEST — nothing runs at finish time, and NOTHING here spawns a
process, touches a container engine, or reaches the network. Detection,
validation, and overlay emission are pure functions over the immutable contents.

The same pure assessment (`assess_project`) powers the upgraded Download: when a
project assesses `candidate` AND validation passes, the streamed zip additionally
carries the WO-4 self-host overlay (`compose.yaml`, `Dockerfile`(s),
`.dockerignore`, `.env.example`, `SELFHOST.md`, `release.json`). A workspace file
always wins a path collision — the overlay entry is dropped and reported as a
blocker on `/release` (never a false affordance: a non-`candidate` project's
download is byte-for-byte the plain filtered zip, no overlay).

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
    version_seq: int
    tree_digest: str


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


def assess_release(
    files: Mapping[str, bytes],
    *,
    intent: ReleaseIntent | None,
    project_name: str,
    version_seq: int,
    tree_digest: str,
    imported: bool = False,
) -> AssessedRelease:
    """Assess a workspace's release readiness — PURE and deterministic.

    Reads ONLY the immutable file view + the typed intent + the `imported`
    provenance bit: runs `detect_release`, and (for a `candidate` with a valid spec)
    `validate_release` + the WO-4 overlay emission. Returns the JSON verdict and the
    (possibly empty) overlay to inject. No process, no container, no network, no
    clock, no randomness — the same inputs return an EQUAL result every time.

    `imported=True` (a project whose workspace was seeded by a code import) makes an
    UNRECOGNIZED stack fail closed to `needs_review` rather than `not_web` (WO-3 rung
    4): an imported tree we can't shape still needs an owner declaration.
    """
    detection = detect_release(files, intent=intent, provenance=Provenance(imported=imported))
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
            overlay = emit_local_compose(spec)
            present = set(files)
            collided = False
            for path in sorted(overlay):
                if path in present:
                    # The workspace already has a file here — it wins; the generated
                    # overlay entry is dropped and reported so the choice is legible.
                    # An overlay collision means the bundle is NOT internally complete
                    # (a generated file could not be placed), so it forces
                    # `self_host:false`: the API never advertises a self-hostable
                    # candidate while ALSO reporting a blocker (plan §7.6 / §2 #3).
                    collided = True
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
            self_host = not collided
        else:
            blockers.extend(
                _ResponseBlocker(
                    code=blocker.code.value, message=blocker.message, path=blocker.path
                )
                for blocker in validation.blockers
            )
    elif detection.blockers:
        # A detector fail-closed with EXACT typed codes (required_env_unresolved,
        # port_contract_unresolved, entrypoint_unresolved, toolchain_unsupported,
        # output_dir_unresolved, health_path_unresolved, runtime_conflict). Surface
        # each verbatim so a predictable defect is precisely diagnosable.
        blockers.extend(
            _ResponseBlocker(code=item.code, message=item.message, field=item.field, path=item.path)
            for item in detection.blockers
        )
    else:
        blockers.extend(
            _ResponseBlocker(code="release_field_unresolved", field=item.field, message=item.detail)
            for item in detection.missing
        )

    required_env = [
        _ResponseEnv(
            name=var.name,
            scope=var.scope.value,
            required=var.required,
            secret=var.secret is SecretClass.secret,
        )
        for var in detection.env
    ]

    response = ReleaseResponse(
        assessment=assessment.value,
        reasons=list(detection.reasons),
        blockers=blockers,
        required_env=required_env,
        command=_RELEASE_COMMAND,
        ingress=ingress_info,
        self_host=self_host,
        spec_digest=digest,
        version_seq=version_seq,
        tree_digest=tree_digest,
    )
    return AssessedRelease(response=response, overlay_files=overlay_files)


def _source_binding(ps: ProjectStore, conversation_id: str, workspace: Path) -> tuple[int, str]:
    """The (version_seq, tree_digest) pair that pins the assessment to ONE tree — the
    LIVE workspace being assessed — so the two fields can never disagree.

    `tree_digest` is always the live workspace's digest (the tree whose files are
    assessed). For `version_seq`: if the live tree exactly matches a committed
    version, that version's seq is reported (they describe the identical tree); if it
    matches none — uncommitted edits, or no versions yet — the seq it WOULD receive on
    the next cut (`max existing + 1`, i.e. `1` when there are none) is reported. Either
    way the returned pair describes the same tree, never a saved version's seq paired
    with a different tree's digest."""
    digest = compute_tree_digest(workspace)
    try:
        versions = ps.list_versions(conversation_id)  # newest first
    except StorageError:
        return 1, digest
    for record in versions:  # newest first — report the most recent EXACT match
        if record.tree_digest == digest:
            return record.seq, digest
    next_seq = max((record.seq for record in versions), default=0) + 1
    return next_seq, digest


def assess_project(
    ps: ProjectStore, record: ProjectRecord, workspace: Path, conversation_id: str
) -> AssessedRelease:
    """Read a project's committed workspace + host-owned intent sidecar and assess
    it. Reads the tree through `iter_workspace`, so runtime-secret files are already
    excluded from every downstream input. Raises `StorageError` on a corrupt intent
    sidecar (an honest failure, never a silent 'no intent')."""
    files: dict[str, bytes] = {}
    for path in ps.iter_workspace(conversation_id):
        files[path.relative_to(workspace).as_posix()] = path.read_bytes()
    intent = ps.read_release_intent(conversation_id)
    version_seq, digest = _source_binding(ps, conversation_id, workspace)
    name = record.title or conversation_id
    return assess_release(
        files,
        intent=intent,
        project_name=name,
        version_seq=version_seq,
        tree_digest=digest,
        imported=record.imported,
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
            assessed = assess_project(ps, record, workspace, record.conversation_id)
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
    "make_release_router",
]

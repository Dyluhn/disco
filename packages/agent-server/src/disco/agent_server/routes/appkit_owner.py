"""Owner-only canonical AppKit controls, fenced against model execution."""

from typing import Any
from uuid import uuid4

from disco.core import ConversationStatus
from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
    spec_digest,
)
from disco.core.appkit.owner_controls import apply_owner_tweak, owner_tweak_spec, owner_tweak_values
from disco.core.appkit.spec import AppSpec, DesignSpec
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.app_kit_parts.errors import _AppKitError
from disco.tools.builtin.app_kit_parts.set_tweak import commit_owner_tweaks, prepare_owner_tweaks
from disco.tools.builtin.verify_appkit_parts.preview_lifecycle import PreviewLifecycle
from disco.tools.builtin.verify_appkit_parts.sandbox_reader import SandboxReader
from disco.tools.projects import StorageError, StorageStatus
from disco.tools.secrets import CapabilityBroker
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from ..auth import current_owner_id
from ..preview_sealed_restore import replace_with_sealed_workspace
from ..runtime import ConversationRuntime
from ..workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from ._common import _reject_if_imported, require_owned_conversation
from .files_support import ArtifactRejected, _committed_artifact, _read_artifact_bytes


class OwnerTweaksBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_spec_digest: str = Field(min_length=1, max_length=100)
    values: dict[str, StrictStr | StrictInt | StrictFloat | StrictBool] = Field(
        min_length=1,
        max_length=6,
    )


def _payload(app: AppSpec, design: DesignSpec, *, editable: bool) -> dict[str, Any]:
    return {
        "title": app.name,
        "fields": owner_tweak_spec(app, design).model_dump(mode="json")["fields"],
        "values": owner_tweak_values(app, design),
        "spec_digest": spec_digest(app, design),
        "editable": editable,
    }


def _require_appkit(runtime: ConversationRuntime, cid: str) -> None:
    try:
        enabled = runtime.settings._effective_appkit_mode(cid)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    if not enabled:
        raise HTTPException(409, detail="Owner controls require an unejected AppKit conversation.")


async def _read_controls(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    cid: str,
) -> dict[str, Any] | None:
    if not store.conversation_appkit_mode_sync(cid):
        return None
    _require_appkit(runtime, cid)
    state = await store.get_state(cid)
    finished = state.execution_status == ConversationStatus.FINISHED
    events = await store.get_events(cid) if finished else []
    raw = []
    for path in (APPSPEC_RELPATH, DESIGNSPEC_RELPATH):
        if finished:
            try:
                data = await _committed_artifact(runtime, cid, path, events)
            except ArtifactRejected as exc:
                if exc.status_code == 404:
                    return None
                raise HTTPException(exc.status_code, detail=exc.detail) from exc
        else:
            data = await _read_artifact_bytes(runtime, cid, path)
        if data is None:
            return None
        raw.append(data)
    try:
        app, design = load_app_spec_from_bytes(raw[0]), load_design_spec_from_bytes(raw[1])
    except ValueError as exc:
        raise HTTPException(409, detail="AppKit specs are not currently readable.") from exc
    return _payload(app, design, editable=finished) if app.owner_controls is not None else None


async def _apply_controls(
    store: SqliteEventStore,
    runtime: ConversationRuntime,
    cid: str,
    owner_id: str,
    body: OwnerTweaksBody,
) -> dict[str, Any]:
    _require_appkit(runtime, cid)
    state = await store.get_state(cid)
    if state.execution_status != ConversationStatus.FINISHED:
        raise HTTPException(409, detail="Finish the AppKit build before editing owner controls.")
    # A unique authority prevents another owner save from being mistaken for
    # this edit if it arrives between releasing the fence and sealing.
    operation = f"appkit.owner-tweaks.{uuid4().hex}"
    async with runtime.workspace.fence(cid):
        _require_appkit(runtime, cid)
        state = await store.get_state(cid)
        if state.execution_status != ConversationStatus.FINISHED or runtime.workspace.has_run_claim(
            cid
        ):
            raise HTTPException(
                409, detail="The run changed; wait for it to finish and reload controls."
            )
        files = await _sealed_edit_files(store, runtime, cid, body)
        session = runtime.live_sessions.live_session(cid)
        if session is None:
            runtime._loop_for(cid)
            session = runtime.live_sessions.live_session(cid)
        if session is None:
            raise HTTPException(409, detail="The AppKit sandbox is unavailable.")
        try:
            with runtime.lifecycle.restoring_workspace(cid):
                await replace_with_sealed_workspace(session, files)
        except Exception as exc:
            raise HTTPException(
                503,
                detail="The saved AppKit source could not be restored; saved files are preserved.",
            ) from exc
        ctx = ToolContext(
            sandbox=session,
            workspace_path=".",
            timeout_s=300,
            capabilities=CapabilityBroker().grant(frozenset()),
            conversation_id=cid,
            owner_id=owner_id,
        )
        try:
            app, design, tree = await prepare_owner_tweaks(ctx, body.values)
        except (ValueError, _AppKitError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        await runtime.workspace.record_mutation_locked(
            cid,
            operation,
            paths=tuple(sorted({*tree, APPSPEC_RELPATH, DESIGNSPEC_RELPATH, "dist"})),
        )
        outcome = await commit_owner_tweaks(ctx, app, design, tree)
        if not outcome.success:
            raise HTTPException(503, detail=outcome.content)
        # Sources and downloadable dist must agree before a new FINISHED seal.
        build = await PreviewLifecycle(SandboxReader()).ensure_build(ctx, tree["package.json"])
        if not build.ok:
            raise HTTPException(
                503, detail=f"Controls changed but the app could not rebuild: {build.evidence}"
            )
    try:
        await runtime.workspace.finalize_sandbox_change(cid, operation)
    except Exception as exc:
        raise HTTPException(
            503, detail="Controls changed but the new workspace could not be sealed."
        ) from exc
    return _payload(app, design, editable=True)


async def _sealed_edit_files(
    store: SqliteEventStore, runtime: ConversationRuntime, cid: str, body: OwnerTweaksBody
) -> tuple[tuple[str, bytes], ...]:
    """Read only the committed immutable source, while the caller holds its fence."""
    project_store = runtime.projects.current_project_store()
    if project_store is None or project_store.status() != StorageStatus.OK:
        raise HTTPException(503, detail="The saved AppKit source is unavailable.")
    try:
        committed = resolve_committed_workspace(await store.get_events(cid), project_store, cid)
        with project_store.open_verified_version(cid, committed.event.version_seq) as version:
            files = tuple((entry.path, version.read_bytes(entry.path)) for entry in version.files)
    except (WorkspaceCommitUnavailable, StorageError) as exc:
        raise HTTPException(
            409, detail="The AppKit workspace is not sealed; reload controls."
        ) from exc
    source = dict(files)
    try:
        app = load_app_spec_from_bytes(source[APPSPEC_RELPATH])
        design = load_design_spec_from_bytes(source[DESIGNSPEC_RELPATH])
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, detail="The saved AppKit specs are not readable.") from exc
    if spec_digest(app, design) != body.expected_spec_digest:
        raise HTTPException(409, detail="This app changed; reload controls before saving.")
    try:
        for key, value in sorted(body.values.items()):
            app, design = apply_owner_tweak(app, design, key, value)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return files


def make_appkit_owner_router(
    store: SqliteEventStore, runtime: ConversationRuntime | None
) -> APIRouter:
    router = APIRouter()

    @router.get("/conversations/{conversation_id}/appkit/controls")
    async def get_controls(conversation_id: str, request: Request) -> dict[str, Any]:
        cid = await require_owned_conversation(request, store, conversation_id)
        return {"controls": await _read_controls(store, runtime, cid) if runtime else None}

    @router.patch("/conversations/{conversation_id}/appkit/controls")
    async def patch_controls(
        conversation_id: str, request: Request, body: OwnerTweaksBody
    ) -> dict[str, Any]:
        cid = await require_owned_conversation(request, store, conversation_id)
        _reject_if_imported(store, cid)
        if runtime is None:
            raise HTTPException(503, detail="AppKit runtime is unavailable.")
        return {
            "controls": await _apply_controls(store, runtime, cid, current_owner_id(request), body)
        }

    return router

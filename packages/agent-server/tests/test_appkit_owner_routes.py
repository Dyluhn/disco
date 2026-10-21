"""Owner edits use the workspace authority, preserve gates, and reject stale input."""

import asyncio
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from disco.agent_server.lifecycle_command_service import LifecycleCommandService
from disco.agent_server.lifecycle_rehydration import Rehydration
from disco.agent_server.routes import appkit_owner as owner
from disco.agent_server.workspace_finalization import seal_event
from disco.core import ConversationStatus, WorkspaceMutationEvent, derive_final_workspace_fence
from disco.core.appkit import load_app_spec_from_bytes, load_design_spec_from_bytes, spec_digest
from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.app_kit import AppCreateArgs, AppCreateTool
from disco.tools.builtin.verify_appkit_parts.preview_lifecycle import _BuildResult
from disco.tools.projects import ProjectStore
from disco.tools.sandbox.base import SandboxFileNotFoundError
from disco.tools.secrets import CapabilityBroker
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

CID = "conv_owner_app"


class FakeSandboxInstance:
    def __init__(self):
        self._fs = {}

    async def read_file(self, path):
        if path not in self._fs:
            raise SandboxFileNotFoundError(path)
        return self._fs[path]

    async def write_file(self, path, data):
        self._fs[path] = data

    async def atomic_write(self, path, data):
        await self.write_file(path, data)

    async def resolve_relpath(self, path):
        return path

    async def list_dir(self, _path):
        return sorted(self._fs)

    async def file_exists(self, path):
        return path in self._fs

    async def exec_shell(self, command, **kwargs):
        assert command == "find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +"
        self._fs.clear()
        return SimpleNamespace(exit_code=0, timed_out=False)


@pytest.fixture
async def env(monkeypatch, tmp_path):
    sandbox = FakeSandboxInstance()
    context = ToolContext(
        sandbox=sandbox,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        conversation_id=CID,
        owner_id="alice",
    )
    assert (await AppCreateTool().run(AppCreateArgs(recipe_id="editorial-ledger"), context)).success
    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="alice", appkit_mode=True)
    state = SimpleNamespace(execution_status=ConversationStatus.FINISHED)
    monkeypatch.setattr(store, "get_state", AsyncMock(return_value=state))
    sealed = dict(sandbox._fs)
    lock = asyncio.Lock()
    order = []
    authorities = []
    mutations = []
    ps = ProjectStore(str(tmp_path / "projects"))
    rehydration = Rehydration(None, None, None, None)

    async def seal():
        workspace = ps.path_for(CID)
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        for path, data in sealed.items():
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        ps.write_manifest(
            CID,
            owner_id="alice",
            title="Owner fixture",
            created_at=None,
            file_count=len(sealed),
            total_bytes=sum(map(len, sealed.values())),
        )
        version = ps.cut_verified_version(CID, trigger="finish", pin=True)
        await store.append(
            CID,
            LifecycleCommandService.build_status(
                ConversationStatus.FINISHED,
                host_mutation_id=mutations[-1].id if mutations else None,
            ),
        )
        fence = derive_final_workspace_fence(await store.get_events(CID))
        await store.append(CID, seal_event(CID, fence, version))

    await seal()

    @asynccontextmanager
    async def fence(_cid):
        async with lock:
            yield

    async def record(_cid, operation, **_kwargs):
        assert lock.locked() and operation.startswith("appkit.owner-tweaks.")
        authorities.append(operation)
        order.append("intent")
        mutations.append(await store.append(CID, WorkspaceMutationEvent(operation=operation)))

    async def build(_self, _ctx, _package):
        assert lock.locked() and order[-1] == "intent"
        order.append("build")
        await sandbox.write_file("dist/index.html", b"rebuilt")
        return _BuildResult(True)

    async def finalize(_cid, operation):
        assert not lock.locked() and order[-1] == "build"
        assert operation == authorities[-1]
        order.append("seal")
        sealed.clear()
        sealed.update(sandbox._fs)
        await seal()

    monkeypatch.setattr(owner.PreviewLifecycle, "ensure_build", build)
    runtime = SimpleNamespace(
        settings=SimpleNamespace(_effective_appkit_mode=Mock(return_value=True)),
        preview=SimpleNamespace(ensure_preview=AsyncMock(return_value=True)),
        workspace=SimpleNamespace(
            fence=fence,
            has_run_claim=Mock(return_value=False),
            record_mutation_locked=record,
            finalize_sandbox_change=AsyncMock(side_effect=finalize),
        ),
        live_sessions=SimpleNamespace(live_session=lambda _cid: sandbox),
        projects=SimpleNamespace(current_project_store=lambda: ps),
        lifecycle=SimpleNamespace(restoring_workspace=rehydration.restoring),
        _loop_for=Mock(),
    )
    app = load_app_spec_from_bytes(sealed[".disco/appspec.json"])
    design = load_design_spec_from_bytes(sealed[".disco/designspec.json"])
    return SimpleNamespace(
        sandbox=sandbox,
        sealed=sealed,
        runtime=runtime,
        store=store,
        state=state,
        digest=spec_digest(app, design),
        order=order,
        authorities=authorities,
        project_store=ps,
        rehydration=rehydration,
    )


async def test_controls_commit_rebuild_and_seal_then_read_sealed_values(env):
    result = await owner._apply_controls(
        env.store,
        env.runtime,
        CID,
        "alice",
        owner.OwnerTweaksBody(
            expected_spec_digest=env.digest,
            values={"brand.name": "Owner revision", "layout.width": 1280},
        ),
    )
    assert env.order == ["intent", "build", "seal"]
    assert result["values"]["brand.name"] == "Owner revision"
    assert result["spec_digest"] != env.digest
    # A later live-sandbox divergence cannot contaminate the FINISHED read path.
    env.sandbox._fs[".disco/appspec.json"] = b"untrusted mutable bytes"
    reread = await owner._read_controls(env.store, env.runtime, CID)
    assert reread == result


async def test_cold_owner_edit_restores_verified_host_source_without_preview(env):
    original = dict(env.sealed)
    env.sandbox._fs.clear()
    live = {"session": None}
    env.runtime.live_sessions.live_session = lambda _cid: live["session"]
    env.runtime._loop_for.side_effect = lambda _cid: live.update(session=env.sandbox)
    env.runtime.preview.ensure_preview.return_value = False
    # Neither a stale mutable mirror nor preview availability governs owner edits.
    mirror = env.project_store.path_for(CID)
    (mirror / ".disco/appspec.json").write_bytes(b"invalid mutable source")
    result = await owner._apply_controls(
        env.store,
        env.runtime,
        CID,
        "alice",
        owner.OwnerTweaksBody(expected_spec_digest=env.digest, values={"brand.name": "Cold edit"}),
    )
    env.runtime._loop_for.assert_called_once_with(CID)
    env.runtime.preview.ensure_preview.assert_not_awaited()
    assert result["values"]["brand.name"] == "Cold edit"
    assert env.sandbox._fs[".dev.vars.example"] == original[".dev.vars.example"]
    assert env.sealed == env.sandbox._fs
    assert env.rehydration.snapshot_allowed(CID)
    assert (await owner._read_controls(env.store, env.runtime, CID)) == result


@pytest.mark.parametrize("failure", [OSError("broken transport"), asyncio.CancelledError()])
async def test_interrupted_host_restore_keeps_original_seal_and_blocks_partial_snapshot(
    env,
    monkeypatch,
    failure,
):
    restore = owner.replace_with_sealed_workspace
    original = dict(env.sealed)
    versions = env.project_store.list_versions(CID)

    async def partial(session, files):
        session._fs.clear()
        await session.write_file("partial.txt", b"incomplete")
        # A sandbox transport recreation can reset the normal session markers.
        env.rehydration.clear(CID)
        raise failure

    monkeypatch.setattr(owner, "replace_with_sealed_workspace", partial)
    body = owner.OwnerTweaksBody(expected_spec_digest=env.digest, values={"layout.width": 1280})
    expected = (
        asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else HTTPException
    )
    with pytest.raises(expected):
        await owner._apply_controls(env.store, env.runtime, CID, "alice", body)
    assert not env.order
    assert not env.rehydration.snapshot_allowed(CID)
    assert env.sealed == original
    assert env.project_store.list_versions(CID) == versions
    env.runtime.workspace.finalize_sandbox_change.assert_not_awaited()

    monkeypatch.setattr(owner, "replace_with_sealed_workspace", restore)
    result = await owner._apply_controls(env.store, env.runtime, CID, "alice", body)
    assert result["values"]["layout.width"] == 1280
    assert "partial.txt" not in env.sandbox._fs
    assert env.rehydration.snapshot_allowed(CID)


async def test_each_save_finalizes_its_own_mutation_authority(env):
    first = await owner._apply_controls(
        env.store,
        env.runtime,
        CID,
        "alice",
        owner.OwnerTweaksBody(
            expected_spec_digest=env.digest,
            values={"layout.width": 1280},
        ),
    )
    await owner._apply_controls(
        env.store,
        env.runtime,
        CID,
        "alice",
        owner.OwnerTweaksBody(
            expected_spec_digest=first["spec_digest"],
            values={"layout.width": 1440},
        ),
    )
    assert len(set(env.authorities)) == 2
    calls = env.runtime.workspace.finalize_sandbox_change.await_args_list
    assert [call.args[1] for call in calls] == env.authorities


@pytest.mark.parametrize(
    "values,digest",
    [
        ({"layout.width": 1280}, "stale"),
        ({"brand.name": "Valid", "layout.width": 1281}, None),
        ({"lead.include_phone": True}, None),
    ],
)
async def test_invalid_or_stale_patch_changes_no_files_or_authority(env, values, digest):
    before = dict(env.sandbox._fs)
    with pytest.raises(HTTPException) as raised:
        await owner._apply_controls(
            env.store,
            env.runtime,
            CID,
            "alice",
            owner.OwnerTweaksBody(
                expected_spec_digest=digest or env.digest,
                values=values,
            ),
        )
    assert raised.value.status_code in {409, 422}
    assert env.order == [] and env.sandbox._fs == before
    env.runtime.workspace.finalize_sandbox_change.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    [
        ConversationStatus.RUNNING,
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_USER_DECISION,
    ],
)
async def test_active_and_pending_approval_states_do_not_enter_owner_edit(env, status):
    env.state.execution_status = status
    with pytest.raises(HTTPException, match="Finish"):
        await owner._apply_controls(
            env.store,
            env.runtime,
            CID,
            "alice",
            owner.OwnerTweaksBody(
                expected_spec_digest=env.digest,
                values={"layout.width": 1280},
            ),
        )
    env.runtime.preview.ensure_preview.assert_not_awaited()
    assert not env.order


async def test_run_starting_before_owner_fence_cannot_be_edited(env):
    env.store.get_state.side_effect = [
        SimpleNamespace(execution_status=ConversationStatus.FINISHED),
        SimpleNamespace(execution_status=ConversationStatus.RUNNING),
    ]
    with pytest.raises(HTTPException, match="run changed"):
        await owner._apply_controls(
            env.store,
            env.runtime,
            CID,
            "alice",
            owner.OwnerTweaksBody(
                expected_spec_digest=env.digest,
                values={"layout.width": 1280},
            ),
        )
    assert not env.order


async def test_two_saves_of_one_revision_cannot_overwrite_each_other(env):
    results = await asyncio.gather(
        *(
            owner._apply_controls(
                env.store,
                env.runtime,
                CID,
                "alice",
                owner.OwnerTweaksBody(
                    expected_spec_digest=env.digest,
                    values={"brand.name": name},
                ),
            )
            for name in ("First owner edit", "Second owner edit")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    refusal = next(result for result in results if isinstance(result, HTTPException))
    assert refusal.status_code == 409
    assert env.order == ["intent", "build", "seal"]


async def test_failed_build_does_not_claim_a_finished_revision(env, monkeypatch):
    monkeypatch.setattr(
        owner.PreviewLifecycle,
        "ensure_build",
        AsyncMock(return_value=_BuildResult(False, "broken")),
    )
    before = dict(env.sealed)
    with pytest.raises(HTTPException, match="could not rebuild"):
        await owner._apply_controls(
            env.store,
            env.runtime,
            CID,
            "alice",
            owner.OwnerTweaksBody(
                expected_spec_digest=env.digest,
                values={"layout.width": 1280},
            ),
        )
    assert env.sealed == before and env.order == ["intent"]
    env.runtime.workspace.finalize_sandbox_change.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, RuntimeError("AppKit disabled")])
async def test_ejected_or_disabled_mode_cannot_use_owner_ingress(env, enabled):
    if isinstance(enabled, Exception):
        env.runtime.settings._effective_appkit_mode.side_effect = enabled
    else:
        env.runtime.settings._effective_appkit_mode.return_value = enabled
    with pytest.raises(HTTPException) as raised:
        await owner._apply_controls(
            env.store,
            env.runtime,
            CID,
            "alice",
            owner.OwnerTweaksBody(
                expected_spec_digest=env.digest,
                values={"layout.width": 1280},
            ),
        )
    assert raised.value.status_code == 409
    env.runtime.preview.ensure_preview.assert_not_awaited()
    assert not env.order


async def test_failed_final_seal_is_reported_as_failure(env):
    env.runtime.workspace.finalize_sandbox_change.side_effect = RuntimeError("disk full")
    before = dict(env.sealed)
    with pytest.raises(HTTPException, match="could not be sealed"):
        await owner._apply_controls(
            env.store,
            env.runtime,
            CID,
            "alice",
            owner.OwnerTweaksBody(
                expected_spec_digest=env.digest,
                values={"layout.width": 1280},
            ),
        )
    assert env.order == ["intent", "build"]
    assert env.sealed == before


@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_controls_routes_enforce_conversation_owner(env, method):
    app = FastAPI()

    @app.middleware("http")
    async def session(request, call_next):
        request.state.auth_session = AuthSession("mallory", "csrf", "real-session", 9999999999)
        return await call_next(request)

    app.include_router(owner.make_appkit_owner_router(env.store, env.runtime))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://owner.test"
    ) as client:
        response = await client.request(
            method,
            f"/conversations/{CID}/appkit/controls",
            json={
                "expected_spec_digest": env.digest,
                "values": {"layout.width": 1280},
            },
        )
    assert response.status_code == 403
    assert not env.order


async def test_unsealed_owner_source_is_rejected_before_sandbox_mutation(env):
    await env.store.append(CID, WorkspaceMutationEvent(operation="unsealed.change"))
    before = dict(env.sandbox._fs)
    with pytest.raises(HTTPException, match="not sealed"):
        await owner._apply_controls(
            env.store,
            env.runtime,
            CID,
            "alice",
            owner.OwnerTweaksBody(expected_spec_digest=env.digest, values={"layout.width": 1280}),
        )
    assert env.sandbox._fs == before
    assert not env.order
    env.runtime._loop_for.assert_not_called()

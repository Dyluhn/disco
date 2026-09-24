"""Owner edits use the workspace authority, preserve gates, and reject stale input."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from disco.agent_server.routes import appkit_owner as owner
from disco.core import ConversationStatus
from disco.core.appkit import load_app_spec_from_bytes, load_design_spec_from_bytes, spec_digest
from disco.core.auth import AuthSession
from disco.core.store.sqlite import SqliteEventStore
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.app_kit import AppCreateArgs, AppCreateTool
from disco.tools.builtin.verify_appkit_parts.preview_lifecycle import _BuildResult
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


@pytest.fixture
async def env(monkeypatch):
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

    @asynccontextmanager
    async def fence(_cid):
        async with lock:
            yield

    async def record(_cid, operation, **_kwargs):
        assert lock.locked() and operation.startswith("appkit.owner-tweaks.")
        authorities.append(operation)
        order.append("intent")

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

    async def read_sealed(_rt, _cid, path, _events):
        return sealed[path]

    monkeypatch.setattr(owner.PreviewLifecycle, "ensure_build", build)
    monkeypatch.setattr(owner, "_committed_artifact", read_sealed)
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


async def test_run_starting_during_preview_restore_cannot_be_edited(env):
    async def wake(*_args, **_kwargs):
        env.state.execution_status = ConversationStatus.RUNNING
        return True

    env.runtime.preview.ensure_preview.side_effect = wake
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

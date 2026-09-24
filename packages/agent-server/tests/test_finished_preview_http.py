"""Complete sealed application login/session/mutation protocol after cold restore."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest
from _static_preview_capability_support import (
    _app,
    _install_owner_session,
    _redeem,
    _runtime_router,
)
from disco.agent_server import ConversationRuntime
from disco.agent_server.preview_manager import preview_projection_digest
from disco.core import (
    ActionEvent,
    ConversationStatus,
    DeliverableEvent,
    FinalWorkspaceSeal,
    ObservationEvent,
    ResourceKey,
    SqliteEventStore,
    StatusEvent,
    ToolCall,
    ToolResult,
    WorkspaceVersionEvent,
)
from disco.core.auth import CSRF_HEADER, local_preview_cookie_name
from disco.core.loop.preview_target import managed_host_preview_ports
from disco.tools import ProcessSandboxService
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient


def _free_port() -> int:
    for port in managed_host_preview_ports():
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise AssertionError("no managed preview port available")


async def _seal_application(store, projects, cid, port):
    store.create_conversation(cid, owner_id="owner-a", surface="build")
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    workspace = projects.path_for(cid)
    workspace.mkdir(parents=True)
    workspace.joinpath("index.html").write_text("<h1>IMMUTABLE</h1>")
    workspace.joinpath("app.py").write_bytes(
        Path(__file__).with_name("_preview_http_application.py").read_bytes()
    )
    projects.write_manifest(
        cid,
        title="HTTP application",
        owner_id="owner-a",
        created_at="2026-09-24T00:00:00+00:00",
        file_count=2,
        total_bytes=3000,
    )
    command = "python3 app.py {port}"
    intent = {
        "serve_dir": None,
        "command": command,
        "framework": None,
        "cwd": None,
        "launch_kind": "custom",
    }
    action = await store.append(
        cid,
        ActionEvent(
            thought="start application",
            tool_call=ToolCall(
                call_id="start",
                tool_name="preview_start",
                arguments={"name": "web", "command": command},
            ),
        ),
    )
    projection = {
        "status": "running",
        "name": "web",
        "port": port,
        "launch_kind": "custom",
        "projection_id": "pv_" + "a" * 32,
        "sandbox_instance_id": "sbx_previous",
        "sandbox_generation": 1,
        "command": command.format(port=port),
        "exec_dir": "/workspace",
        "intent": intent,
        "intent_digest": preview_projection_digest(
            name="web",
            port=port,
            command=command.format(port=port),
            exec_dir="/workspace",
            intent=intent,
        ),
    }
    observation = await store.append(
        cid,
        ObservationEvent(
            action_id=action.id,
            tool_result=ToolResult(
                call_id="start",
                tool_name="preview_start",
                success=True,
                content="running",
                structured=projection,
            ),
        ),
    )
    await store.append(
        cid, DeliverableEvent(title="HTTP application", path="index.html", artifact_kind="app")
    )
    terminal = await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    version = projects.cut_verified_version(cid, trigger="finish", pin=True)
    assert version is not None
    await store.append(
        cid,
        WorkspaceVersionEvent(
            version_seq=version.seq,
            tree_digest=version.tree_digest,
            trigger="finish",
            final_seal=FinalWorkspaceSeal(
                scope=ResourceKey(namespace="workspace.tree", identifier=cid),
                terminal_seq=terminal.seq,
                latest_effect_seq=observation.seq,
                version_seq=version.seq,
                tree_digest=version.tree_digest,
                file_count=version.file_count,
                total_bytes=version.total_bytes,
            ),
        ),
    )


def _mint(app, cid, *, version=None):
    with TestClient(app, base_url="http://127.0.0.1:18240") as owner:
        csrf = _install_owner_session(owner, "owner-a")
        response = owner.post(
            f"/conversations/{cid}/preview/capability",
            headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
            json={
                "target_path": "/",
                "transport": "canonical",
                **({"workspace_version": version} if version else {}),
            },
        )
        assert response.status_code == 200, response.text
        return response.json()


@pytest.mark.integration
async def test_restored_finished_application_login_reload_and_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCO_AUTH_SECRET", "finished-http-roundtrip")
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "1")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", "19810")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "4")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)
    port = _free_port()
    monkeypatch.setattr(
        "disco.agent_server.preview_manager._default_port_pool", lambda _sandbox: [port]
    )
    cid = "conv_a1b2c3d4fullhttp"
    store = SqliteEventStore(tmp_path / "events.sqlite")
    projects = ProjectStore(str(tmp_path / "projects"))
    await _seal_application(store, projects, cid, port)
    runtime = ConversationRuntime(
        store,
        router=_runtime_router(),
        sandbox_service=ProcessSandboxService(str(tmp_path / "sandbox")),
    )
    runtime.projects.current_project_store = MagicMock(return_value=projects)
    runtime.settings._set_surface(cid, "build")
    app = _app(store, runtime, live_upstream="http://127.0.0.1:1")
    try:
        launch = _mint(app, cid)
        origin = urlsplit(launch["bootstrap_url"])
        base_url = f"{origin.scheme}://{origin.netloc}"
        host = runtime.live_sessions.live_session(cid)
        assert host is not None
        host_read = AsyncMock(side_effect=AssertionError("wrong sandbox"))
        monkeypatch.setattr(host, "fetch_inside", host_read)
        # Published daemon-loopback transport is deliberately unreachable from the caller.
        monkeypatch.setattr(runtime.preview, "port_upstream", lambda *_args: "http://127.0.0.1:1")
        with TestClient(app, base_url=base_url) as client:
            assert (
                _redeem(client, launch["bootstrap_url"], launch["bootstrap_intent"]).status_code
                == 200
            )
            assert client.get("/private").status_code == 401
            assert client.post("/write", content=b"denied").status_code == 403
            assert (
                client.post("/write", headers={"Origin": base_url}, content=b"denied").status_code
                == 401
            )
            login = client.post("/login", headers={"Origin": base_url}, content=b"\x00\xffpassword")
            assert login.status_code == 201 and login.content == b"\x00\xffpassword"
            assert login.headers.get_list("set-cookie") == [
                "app_session=accepted; HttpOnly; Path=/",
                "app_theme=dark; Path=/",
            ]
            assert login.headers.get_list("x-app-repeat") == ["one", "two"]
            for _reload in range(2):
                loaded = client.get("/private?x=%2f%2F+%20&x=")
                assert loaded.status_code == 200
                assert loaded.json()["path"] == "/private?x=%2f%2F+%20&x="
                assert "disco_" not in loaded.json()["cookie"]
                assert "app_session=accepted" in loaded.json()["cookie"]
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                result = client.request(
                    method, "/write", headers={"Origin": base_url}, content=method.encode()
                )
                assert result.status_code == 200 and result.content == method.encode()
            assert client.get("/private").json()["count"] == 5
            with monkeypatch.context() as denied_owner:
                denied_owner.setattr(
                    store, "conversation_owner_id", AsyncMock(return_value="owner-b")
                )
                forbidden = client.post("/write", headers={"Origin": base_url}, content=b"foreign")
                assert forbidden.status_code == 403
            manager = host._preview_manager
            selected = manager.canonical_lifecycle_session()
            assert selected is not None
            with monkeypatch.context() as changed_generation:
                changed_generation.setattr(selected, "projection_id", "pv_" + "b" * 32)
                stale = client.post("/write", headers={"Origin": base_url}, content=b"stale")
                assert stale.status_code == 409
            assert client.get("/private").json()["count"] == 5
            # Cap deletion does not downgrade to app authentication or published routing.
            cookie_name = local_preview_cookie_name(origin.port)
            old_cookie = client.cookies.get(cookie_name)
            client.cookies.clear()
            assert (
                client.post("/write", headers={"Origin": base_url}, content=b"denied").status_code
                == 403
            )
            assert old_cookie is not None
        history = _mint(app, cid, version=1)
        history_origin = urlsplit(history["bootstrap_url"])
        with TestClient(app, base_url=f"http://{history_origin.netloc}") as historical:
            assert (
                _redeem(
                    historical, history["bootstrap_url"], history["bootstrap_intent"]
                ).status_code
                == 200
            )
            assert "IMMUTABLE" in historical.get("/").text
            assert (
                historical.post(
                    "/write", headers={"Origin": f"http://{history_origin.netloc}"}
                ).status_code
                == 405
            )
        host_read.assert_not_awaited()
    finally:
        await runtime._teardown_sandbox(cid)
        store.close()

# H079/H110: finished previews use body-only, isolated one-use capabilities.

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest
from _static_preview_capability_support import (  # noqa: E402
    _app,
    _exercise_path_preview_capability_contract,
    _finished_site,
    _install_owner_session,
    _redeem,
    _redeem_steps,
    _runtime_router,
    _SealedProjectionRuntime,
    _StaticRuntime,
)
from disco.agent_server import ConversationRuntime
from disco.agent_server.auth import websocket_session
from disco.agent_server.preview_manager import preview_projection_digest
from disco.agent_server.routes.preview import (
    _path_preview_bootstrap_url,
    _preview_bootstrap_url,
)
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
from disco.core.auth import (
    CSRF_HEADER,
    ISOLATED_PATH_PREVIEW_PREFIX,
    SESSION_COOKIE,
    SessionSigner,
    local_preview_cookie_name,
    local_preview_gateway_host,
    path_preview_host_label,
)
from disco.core.loop.preview_target import (
    is_managed_host_preview_port,
    managed_host_preview_ports,
)
from disco.tools import ProcessSandboxService
from disco.tools.projects import ProjectStore
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

pytestmark = pytest.mark.integration


def test_normal_websocket_session_selects_valid_repeated_cookie_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "websocket-duplicate-session-secret")
    token, expected = SessionSigner().mint(owner_id="owner-a", is_admin=True)

    async def receive() -> dict[str, str]:
        return {"type": "websocket.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        return None

    for raw_cookie_headers in (
        [(b"cookie", b"disco_session=invalid"), (b"cookie", f"disco_session={token}".encode())],
        [(b"cookie", f"disco_session={token}".encode()), (b"cookie", b"disco_session=invalid")],
    ):
        websocket = WebSocket(
            {
                "type": "websocket",
                "asgi": {"version": "3.0"},
                "scheme": "ws",
                "path": "/conversations/example/ws",
                "raw_path": b"/conversations/example/ws",
                "query_string": b"",
                "headers": [
                    (b"origin", b"https://app.example"),
                    (b"host", b"app.example"),
                    *raw_cookie_headers,
                ],
                "client": ("192.0.2.20", 4242),
                "server": ("app.example", 443),
                "subprotocols": [],
            },
            receive,
            send,
        )
        assert websocket_session(websocket) == expected


def test_remote_preview_origin_base_requires_a_separate_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/conversations/conv_a1b2c3d4owner/preview/capability",
            "raw_path": b"/conversations/conv_a1b2c3d4owner/preview/capability",
            "query_string": b"",
            "headers": [
                (b"host", b"app.example.com:8443"),
                (b"x-forwarded-proto", b"https"),
            ],
            "server": ("app.example.com", 8443),
        }
    )
    monkeypatch.setenv("DISCO_PUBLIC_UI_URL", "https://app.example.com")
    monkeypatch.setenv("DISCO_PREVIEW_ORIGIN_BASE", "https://preview.example.net:9443")
    live = urlsplit(_preview_bootstrap_url(request, "a1b2c3d4", 8000))
    static = urlsplit(_path_preview_bootstrap_url(request, "conv_a1b2c3d4owner", 8000))
    assert live.scheme == static.scheme == "https"
    assert live.netloc == "p2-a1b2c3d4-8000.preview.example.net:9443"
    assert static.netloc == (
        f"{path_preview_host_label('conv_a1b2c3d4owner', 8000)}.preview.example.net:9443"
    )

    for unsafe in (
        "https://preview.example.com",
        "https://app.example.com",
        "http://preview.example.net",
        "https://preview.example.net:0",
        "https://127.0.0.1",
        "https://preview",
        "https://user:password@preview.example.net",
        "https://preview.example.net/unexpected",
    ):
        monkeypatch.setenv("DISCO_PREVIEW_ORIGIN_BASE", unsafe)
        with pytest.raises(HTTPException) as rejected:
            _preview_bootstrap_url(request, "a1b2c3d4", 8000)
        assert rejected.value.status_code == 500
        assert rejected.value.detail["reason"] in {
            "preview_origin_base_invalid",
            "preview_origin_base_not_isolated",
        }

    monkeypatch.setenv("DISCO_PREVIEW_ORIGIN_BASE", "https://preview.example.net")
    monkeypatch.delenv("DISCO_PUBLIC_UI_URL")
    with pytest.raises(HTTPException) as missing_ui:
        _preview_bootstrap_url(request, "a1b2c3d4", 8000)
    assert missing_ui.value.detail["reason"] == "preview_origin_base_public_ui_required"


@pytest.mark.parametrize(
    ("preview_base", "public_ui"),
    (
        ("https://éxample.com", "https://app.xn--xample-9ua.com"),
        ("https://xn--xample-9ua.com", "https://app.éxample.com"),
        ("https://faß.com", "https://app.xn--fa-hia.com"),
        ("https://xn--fa-hia.com", "https://app.faß.com"),
    ),
)
def test_remote_preview_origin_base_rejects_idna_same_site_aliases(
    monkeypatch: pytest.MonkeyPatch,
    preview_base: str,
    public_ui: str,
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/conversations/conv_a1b2c3d4owner/preview/capability",
            "headers": [(b"host", b"ui.unrelated.example")],
            "server": ("ui.unrelated.example", 443),
        }
    )
    monkeypatch.setenv("DISCO_PUBLIC_UI_URL", public_ui)
    monkeypatch.setenv("DISCO_PREVIEW_ORIGIN_BASE", preview_base)

    with pytest.raises(HTTPException) as rejected:
        _preview_bootstrap_url(request, "a1b2c3d4", 8000)
    assert rejected.value.status_code == 500
    assert rejected.value.detail["reason"] == "preview_origin_base_not_isolated"


def test_remote_preview_origin_rejects_generated_hostname_over_dns_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/conversations/conv_a1b2c3d4owner/preview/capability",
            "headers": [(b"host", b"app.example.com")],
            "server": ("app.example.com", 443),
        }
    )
    long_base = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 52))
    monkeypatch.setenv("DISCO_PUBLIC_UI_URL", "https://app.example.com")
    monkeypatch.setenv("DISCO_PREVIEW_ORIGIN_BASE", f"https://{long_base}")

    for make_url in (
        lambda: _preview_bootstrap_url(request, "a1b2c3d4", 8000),
        lambda: _path_preview_bootstrap_url(request, "conv_a1b2c3d4owner", 8000),
    ):
        with pytest.raises(HTTPException) as rejected:
            make_url()
        assert rejected.value.status_code == 500
        assert rejected.value.detail["reason"] == "preview_origin_host_too_long"


async def test_path_capability_selects_and_binds_dynamic_managed_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H333: mint, origin, redemption, auth, and proxy all bind the selected 5173."""
    monkeypatch.setenv("DISCO_AUTH_SECRET", "h333-dynamic-preview-secret")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)
    cid = "conv_a1b2c3d4dynamic"
    store = SqliteEventStore(":memory:")
    projects = ProjectStore(str(tmp_path / "projects"))
    await _finished_site(store, projects, cid)
    app = _app(store, _StaticRuntime(projects, target_port=5173))
    owner = TestClient(app, base_url="http://127.0.0.1:18240")
    csrf = _install_owner_session(owner, "owner-a")
    headers = {"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf}

    mismatch = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"port": 8000, "target_path": "/", "transport": "path"},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["reason"] == "path_preview_port_mismatch"

    minted = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"target_path": "/", "transport": "path"},
    )
    assert minted.status_code == 200
    body = minted.json()
    assert body["port"] == 5173
    host = f"{path_preview_host_label(cid, 5173)}.localhost"
    assert urlsplit(body["bootstrap_url"]).hostname == host

    isolated = TestClient(app, base_url=f"http://{host}:18240")
    redeemed = _redeem(isolated, body["bootstrap_url"], body["bootstrap_intent"])
    assert redeemed.status_code == 200
    target = f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cid}/"
    page = isolated.get(target)
    assert page.status_code == 200
    assert "CAPABILITY SITE" in page.text

    live_minted = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"target_path": "/", "transport": "path_live"},
    ).json()
    live_isolated = TestClient(app, base_url=f"http://{host}:18240")
    assert (
        _redeem(
            live_isolated,
            live_minted["bootstrap_url"],
            live_minted["bootstrap_intent"],
        ).status_code
        == 200
    )
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with live_isolated.websocket_connect(
            f"ws://{host}:18240{ISOLATED_PATH_PREVIEW_PREFIX}/{cid}/hmr",
            headers={"Origin": f"http://{host}:18240", "Host": f"{host}:18240"},
        ):
            pass
    assert disconnected.value.reason == "preview not available"


async def test_committed_static_snapshot_survives_stopped_managed_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stopped live preview cannot hide a proven FINISHED immutable app snapshot."""
    monkeypatch.setenv("DISCO_AUTH_SECRET", "h333-stopped-static-secret")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)
    cid = "conv_a1b2c3d4stopped"
    store = SqliteEventStore(":memory:")
    projects = ProjectStore(str(tmp_path / "projects"))
    await _finished_site(store, projects, cid)
    app = _app(store, _StaticRuntime(projects, target_port=None))
    owner = TestClient(app, base_url="http://127.0.0.1:18240")
    csrf = _install_owner_session(owner, "owner-a")
    headers = {"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf}

    static = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"target_path": "/", "transport": "path"},
    )
    assert static.status_code == 200
    assert static.json()["port"] == 8000

    live = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"target_path": "/", "transport": "path_live"},
    )
    assert live.status_code == 409
    assert live.json()["detail"]["reason"] == "preview_unavailable"


async def test_sealed_dynamic_preview_restarts_from_immutable_bytes_and_rotates_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh runtime replays typed intent over exact sealed bytes, never the mirror."""

    monkeypatch.setenv("DISCO_AUTH_SECRET", "sealed-dynamic-preview-secret")
    monkeypatch.setenv("DISCO_ALLOW_PROCESS_SANDBOX_FOR_DEV", "1")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", "19640")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "4")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)

    free_ports: list[int] = []
    for candidate in managed_host_preview_ports():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", candidate))
            free_ports.append(candidate)
        except OSError:
            continue
        finally:
            probe.close()
        if free_ports:
            break
    assert free_ports, "no managed process-preview port is available"
    selected_port = free_ports[0]
    assert is_managed_host_preview_port(selected_port)
    monkeypatch.setattr(
        "disco.agent_server.preview_manager._default_port_pool",
        lambda _sandbox: list(free_ports),
    )

    cid = "conv_a1b2c3d4sealed"
    store = SqliteEventStore(tmp_path / "sealed-preview.sqlite3")
    projects = ProjectStore(str(tmp_path / "projects"))
    restarted: ConversationRuntime | None = None
    store.create_conversation(cid, owner_id="owner-a", surface="build")
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))

    try:
        workspace = projects.path_for(cid)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "index.html").write_bytes(
            b"<!doctype html><h1>SEALED DYNAMIC BYTES</h1><script src='asset.js'></script>"
        )
        (workspace / "asset.js").write_bytes(b"document.body.dataset.sealed='yes';")
        projects.write_manifest(
            cid,
            title="Sealed dynamic application",
            owner_id="owner-a",
            created_at="2026-07-22T00:00:00+00:00",
            file_count=2,
            total_bytes=128,
        )
        raw_command = "python3 -m http.server {port}"
        resolved_command = f"python3 -m http.server {selected_port}"
        intent = {
            "serve_dir": None,
            "command": raw_command,
            "framework": None,
            "cwd": None,
            "launch_kind": "custom",
        }
        intent_digest = preview_projection_digest(
            name="sealed-web",
            port=selected_port,
            command=resolved_command,
            exec_dir="/workspace",
            intent=intent,
        )
        assert intent_digest is not None
        start_action = await store.append(
            cid,
            ActionEvent(
                thought="start the managed application",
                tool_call=ToolCall(
                    call_id="sealed-preview-start",
                    tool_name="preview_start",
                    arguments={"name": "sealed-web", "command": raw_command},
                ),
            ),
        )
        original_projection = "pv_" + "a" * 32
        original_sandbox = "sbx_recorded_generation"
        projection: dict[str, object] = {
            "status": "running",
            "name": "sealed-web",
            "port": selected_port,
            "launch_kind": "custom",
            "projection_id": original_projection,
            "intent_digest": intent_digest,
            "sandbox_instance_id": original_sandbox,
            "sandbox_generation": 1,
            "command": resolved_command,
            "exec_dir": "/workspace",
            "intent": intent,
        }
        start_observation = await store.append(
            cid,
            ObservationEvent(
                action_id=start_action.id,
                tool_result=ToolResult(
                    call_id="sealed-preview-start",
                    tool_name="preview_start",
                    success=True,
                    content="preview running",
                    structured=projection,
                ),
            ),
        )
        await store.append(
            cid,
            DeliverableEvent(
                title="Sealed dynamic application",
                path="index.html",
                artifact_kind="app",
            ),
        )
        terminal = await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
        assert terminal.seq is not None and start_observation.seq is not None
        version = projects.cut_verified_version(cid, trigger="finish", pin=True)
        assert version is not None
        version_event = await store.append(
            cid,
            WorkspaceVersionEvent(
                version_seq=version.seq,
                tree_digest=version.tree_digest,
                trigger="finish",
                final_seal=FinalWorkspaceSeal(
                    scope=ResourceKey(namespace="workspace.tree", identifier=cid),
                    terminal_seq=terminal.seq,
                    latest_effect_seq=start_observation.seq,
                    version_seq=version.seq,
                    tree_digest=version.tree_digest,
                    file_count=version.file_count,
                    total_bytes=version.total_bytes,
                ),
            ),
        )
        recorded_runtime = _SealedProjectionRuntime(projects, projection)
        original_app = _app(store, recorded_runtime, live_upstream=None)
        with TestClient(
            original_app,
            base_url="http://127.0.0.1:18240",
        ) as owner:
            csrf = _install_owner_session(owner, "owner-a")
            old_mint_response = owner.post(
                f"/conversations/{cid}/preview/capability",
                headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
                json={"target_path": "/", "transport": "canonical"},
            )
        assert old_mint_response.status_code == 200
        old_mint = old_mint_response.json()
        assert old_mint["preview_authority"].startswith("sealed-runtime:")
        old_origin = urlsplit(old_mint["bootstrap_url"])
        assert old_origin.port is not None
        old_cookie_name = local_preview_cookie_name(old_origin.port)
        with TestClient(
            original_app,
            base_url=f"{old_origin.scheme}://{old_origin.netloc}",
        ) as original_preview:
            assert (
                _redeem(
                    original_preview,
                    old_mint["bootstrap_url"],
                    old_mint["bootstrap_intent"],
                ).status_code
                == 200
            )
            old_cookie = original_preview.cookies.get(old_cookie_name)
        assert old_cookie is not None

        mutable_mirror = projects.path_for(cid)
        (mutable_mirror / "index.html").write_text("<h1>MUTATED MIRROR</h1>")
        (mutable_mirror / "foreign.txt").write_text("must not enter sealed restore")

        restarted = ConversationRuntime(
            store,
            router=_runtime_router(),
            sandbox_service=ProcessSandboxService(str(tmp_path / "sandbox-restarted")),
        )
        restarted.projects.current_project_store = MagicMock(return_value=projects)
        restarted.settings._set_surface(cid, "build")
        # Reuse a live sandbox carrying a foreign top-level file. Sealed restart
        # must clear the workspace before restoring the verified version, not
        # merely overlay immutable files onto whatever happens to be present.
        restarted._loop_for(cid)
        reused_session = cast(Any, restarted._run_resources.executor(cid)).sandbox
        await reused_session.write_file("foreign-runtime.txt", b"must be removed")
        assert await reused_session.file_exists("foreign-runtime.txt")
        capability_app = _app(store, cast(Any, restarted), live_upstream=None)
        with TestClient(capability_app, base_url="http://127.0.0.1:18240") as owner:
            csrf = _install_owner_session(owner, "owner-a")
            new_mint_response = owner.post(
                f"/conversations/{cid}/preview/capability",
                headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
                json={"target_path": "/", "transport": "canonical"},
            )
        assert new_mint_response.status_code == 200, new_mint_response.text
        new_mint = new_mint_response.json()
        assert new_mint["preview_authority"] != old_mint["preview_authority"]

        restored_sandbox = cast(Any, restarted._run_resources.executor(cid)).sandbox
        # Sealed Preview owns a separate sandbox. Restart must not clear or
        # rewrite the conversation's mutable build workspace.
        assert await restored_sandbox.file_exists("foreign-runtime.txt")
        restored_manager = restored_sandbox._preview_manager
        assert restored_manager.owns_sandbox()
        assert not await restored_manager._sandbox.file_exists("foreign-runtime.txt")
        restored_session = restored_manager.canonical_lifecycle_session()
        assert restored_session is not None
        assert restored_session.projection_id != original_projection
        assert restored_session.sandbox_instance_id != original_sandbox
        restored_projection = restored_session.projection_id
        restored_upstream = restarted.preview.port_upstream(cid, restored_session.port)
        assert restored_upstream is not None
        proxy_app = _app(store, cast(Any, restarted), live_upstream=restored_upstream)

        current_origin = urlsplit(new_mint["bootstrap_url"])
        with TestClient(
            proxy_app,
            base_url=f"{current_origin.scheme}://{current_origin.netloc}",
        ) as current_preview:
            current_reset, current_redemption = _redeem_steps(
                current_preview,
                new_mint["bootstrap_url"],
                new_mint["bootstrap_intent"],
            )
            assert current_redemption.status_code == 200, (
                current_reset.status_code if current_reset is not None else None,
                current_reset.text if current_reset is not None else None,
                current_redemption.status_code,
                current_redemption.text,
            )
            page = current_preview.get("/")
            asset = current_preview.get("/asset.js")
        assert page.status_code == 200
        assert "SEALED DYNAMIC BYTES" in page.text
        assert "MUTATED MIRROR" not in page.text
        assert asset.content == b"document.body.dataset.sealed='yes';"

        with TestClient(
            proxy_app,
            base_url=f"{old_origin.scheme}://{old_origin.netloc}",
        ) as stale_preview:
            stale = stale_preview.get(
                "/",
                headers={"Cookie": f"{old_cookie_name}={old_cookie}"},
            )
        assert stale.status_code in {403, 404, 409}
        assert stale.text in {
            "preview capability required",
            "preview generation changed",
            "preview origin expired",
        }

        with TestClient(capability_app, base_url="http://127.0.0.1:18240") as owner:
            csrf = _install_owner_session(owner, "owner-a")
            historical_mint_response = owner.post(
                f"/conversations/{cid}/preview/capability",
                headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
                json={
                    "target_path": "/",
                    "transport": "canonical",
                    "workspace_version": version_event.version_seq,
                },
            )
        assert historical_mint_response.status_code == 200, historical_mint_response.text
        historical_mint = historical_mint_response.json()
        historical_origin = urlsplit(historical_mint["bootstrap_url"])
        with TestClient(
            proxy_app,
            base_url=f"{historical_origin.scheme}://{historical_origin.netloc}",
        ) as historical_preview:
            assert (
                _redeem(
                    historical_preview,
                    historical_mint["bootstrap_url"],
                    historical_mint["bootstrap_intent"],
                ).status_code
                == 200
            )
            historical_page = historical_preview.get("/")
        assert historical_page.status_code == 200
        assert "SEALED DYNAMIC BYTES" in historical_page.text
        assert "MUTATED MIRROR" not in historical_page.text
        assert restored_manager.canonical_lifecycle_session().projection_id == restored_projection
    finally:
        if restarted is not None:
            await restarted._teardown_sandbox(cid)
        store.close()


async def test_canonical_loopback_serves_committed_and_historical_bytes_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "canonical-loopback-static-secret")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", "19250")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "2")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)
    cid = "conv_a1b2c3d4canonical"
    store = SqliteEventStore(":memory:")
    projects = ProjectStore(str(tmp_path / "projects"))
    await _finished_site(store, projects, cid)
    app = _app(store, _StaticRuntime(projects, target_port=None))
    owner = TestClient(app, base_url="http://127.0.0.1:18240")
    csrf = _install_owner_session(owner, "owner-a")
    headers = {"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf}

    minted = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"target_path": "/", "transport": "canonical"},
    )
    assert minted.status_code == 200
    first = minted.json()
    first_port = urlsplit(first["bootstrap_url"]).port
    assert first_port in {19250, 19251}
    assert first_port is not None
    first_host = local_preview_gateway_host(first_port)
    canonical = TestClient(app, base_url=f"http://{first_host}:{first_port}")
    reset, redemption = _redeem_steps(
        canonical,
        first["bootstrap_url"],
        first["bootstrap_intent"],
    )
    assert reset is not None
    assert reset.headers["clear-site-data"] == '"cache", "cookies", "storage"'
    assert "set-cookie" not in reset.headers
    assert redemption.status_code == 200
    assert "clear-site-data" not in redemption.headers
    assert local_preview_cookie_name(first_port) in redemption.headers["set-cookie"]

    same_authority = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"target_path": "/", "transport": "canonical"},
    ).json()
    assert same_authority["bootstrap_url"] == first["bootstrap_url"]
    preserved = canonical.post(
        urlsplit(same_authority["bootstrap_url"]).path,
        data={"intent": same_authority["bootstrap_intent"]},
    )
    assert preserved.status_code == 200
    assert "clear-site-data" not in preserved.headers
    assert local_preview_cookie_name(first_port) in preserved.headers["set-cookie"]
    page = canonical.get("/")
    assert page.status_code == 200
    assert "CAPABILITY SITE" in page.text
    assert "disco-selection-agent:v1" in page.text
    assert "disco-element-mention-picker:v1" in page.text
    app_version_query = canonical.get("/?version=7")
    assert app_version_query.status_code == 200
    assert "CAPABILITY SITE" in app_version_query.text
    assert (
        canonical.post(
            "/api/coincident",
            headers={"Origin": f"http://{first_host}:{first_port}"},
        ).status_code
        == 405
    )

    historical_mint = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers=headers,
        json={"target_path": "/", "transport": "canonical", "workspace_version": 1},
    )
    assert historical_mint.status_code == 200
    historical = historical_mint.json()
    historical_port = urlsplit(historical["bootstrap_url"]).port
    assert historical_port in {19250, 19251} - {first_port}
    assert historical_port is not None
    historical_host = local_preview_gateway_host(historical_port)
    assert historical_host != first_host
    history_client = TestClient(app, base_url=f"http://{historical_host}:{historical_port}")
    assert (
        _redeem(
            history_client,
            historical["bootstrap_url"],
            historical["bootstrap_intent"],
        ).status_code
        == 200
    )
    historical_page = history_client.get("/?version=client-value")
    assert historical_page.status_code == 200
    assert "CAPABILITY SITE" in historical_page.text
    assert history_client.get("/assets/site.css").text == "h1{color:green}"
    assert "scriptLoaded" in history_client.get("/assets/site.js?cache=one").text
    assert "NESTED CAPABILITY ROUTE" in history_client.get("/nested.html").text
    app_query = history_client.get("/nested.html?version=999")
    assert app_query.status_code == 200
    assert "NESTED CAPABILITY ROUTE" in app_query.text
    assert canonical.get("/").status_code == 404


@pytest.mark.parametrize("browser_name", ["firefox", "chromium"])
def test_canonical_loopback_renders_in_stock_browsers_and_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    browser_name: str,
) -> None:
    """The real browser uses DNS-free isolated sockets, exact app bytes, and durable authority."""
    import asyncio
    import contextlib
    import socket
    import threading
    import time

    import httpx
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    monkeypatch.setenv("DISCO_AUTH_SECRET", "canonical-loopback-firefox-restart-secret")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)
    cid = "conv_a1b2c3d4firefox"
    second_cid = "conv_b1c2d3e4firefox"
    db_path = tmp_path / "firefox-preview.sqlite3"
    project_root = tmp_path / "projects"

    def bind_socket(port: int = 0, host: str = "127.0.0.1") -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(socket.SOMAXCONN)
        return listener

    def bind_gateway_range(start: int | None = None) -> tuple[int, list[socket.socket]]:
        candidates = [start] if start is not None else range(19320, 22320, 2)
        for candidate in candidates:
            listeners: list[socket.socket] = []
            try:
                monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", str(candidate))
                monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "2")
                listeners = [
                    bind_socket(candidate, local_preview_gateway_host(candidate)),
                    bind_socket(candidate + 1, local_preview_gateway_host(candidate + 1)),
                ]
                return candidate, listeners
            except OSError:
                for listener in listeners:
                    listener.close()
        raise AssertionError("no consecutive loopback ports available for Firefox Preview")

    def start_server(
        application: FastAPI,
        *,
        gateway_start: int | None = None,
    ) -> tuple[uvicorn.Server, threading.Thread, int, int, list[socket.socket]]:
        selected_start, gateways = bind_gateway_range(gateway_start)
        monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", str(selected_start))
        monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "2")
        main_listener = bind_socket()
        main_port = int(main_listener.getsockname()[1])
        listeners = [main_listener, *gateways]
        config = uvicorn.Config(
            application,
            host="127.0.0.1",
            port=main_port,
            log_level="critical",
            lifespan="off",
            ws="websockets-sansio",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": listeners},
            name="preview-firefox-fixture",
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started and thread.is_alive(), "Preview fixture failed to start"
        return server, thread, main_port, selected_start, listeners

    def stop_server(
        server: uvicorn.Server,
        thread: threading.Thread,
        listeners: list[socket.socket],
    ) -> None:
        server.should_exit = True
        thread.join(timeout=10)
        for listener in listeners:
            with contextlib.suppress(OSError):
                listener.close()
        assert not thread.is_alive(), "Preview fixture failed to stop"

    def mint(
        main_port: int,
        token: str,
        csrf: str,
        target_path: str = "/",
        workspace_version: int | None = None,
        conversation_id: str = cid,
    ) -> dict[str, str]:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{main_port}",
            cookies={SESSION_COOKIE: token},
            timeout=10,
        ) as owner:
            response = owner.post(
                f"/conversations/{conversation_id}/preview/capability",
                headers={
                    "Origin": f"http://127.0.0.1:{main_port}",
                    CSRF_HEADER: csrf,
                },
                json={
                    "target_path": target_path,
                    "transport": "canonical",
                    **(
                        {"workspace_version": workspace_version}
                        if workspace_version is not None
                        else {}
                    ),
                },
            )
        assert response.status_code == 200, response.text
        return cast(dict[str, str], response.json())

    def submit_launch(page: Any, launch: dict[str, str], name: str):
        page.set_content(
            f'<iframe name="{name}"></iframe>'
            f'<form id="launch-{name}" method="post" target="{name}">'
            '<input name="intent" type="hidden"></form>'
        )
        frame = page.frame(name=name)
        assert frame is not None
        page.eval_on_selector(
            f"#launch-{name}",
            "(form, launch) => {"
            "form.action = launch.url; form.elements.intent.value = launch.intent; form.submit();}",
            {
                "url": launch["bootstrap_url"],
                "intent": launch["bootstrap_intent"],
            },
        )
        frame.wait_for_url(
            re.compile(r"^http://127\.0\.0\.[23]:\d+/.+|^http://127\.0\.0\.[23]:\d+/$")
        )
        return frame

    initial_store = SqliteEventStore(db_path)
    projects = ProjectStore(str(project_root))
    asyncio.run(_finished_site(initial_store, projects, cid))
    asyncio.run(_finished_site(initial_store, projects, second_cid))
    initial_app = _app(initial_store, _StaticRuntime(projects, target_port=None))
    server, thread, main_port, gateway_start, listeners = start_server(initial_app)
    token, session = SessionSigner().mint(owner_id="owner-a", is_admin=True)
    initial_store_open = True
    restarted_store: SqliteEventStore | None = None

    try:
        launch = mint(main_port, token, session.csrf_token)
        launch_origin = urlsplit(launch["bootstrap_url"]).netloc
        assert launch_origin in {
            f"127.0.0.2:{gateway_start}",
            f"127.0.0.3:{gateway_start + 1}",
        }

        with sync_playwright() as playwright:
            # No browser preferences: literal loopback Preview is the stock contract.
            browser = getattr(playwright, browser_name).launch(headless=True)
            try:
                page = browser.new_page()
                # The ordinary local UI may be opened as localhost while the
                # DNS-free Preview uses a distinct 127/8 literal. Stock Firefox must accept
                # that cross-site iframe handoff without a localDomains preference.
                page.goto(f"http://localhost:{main_port}/api/auth/session")
                frame = submit_launch(page, launch, "preview")

                def expect_rendered(locator: Any) -> None:
                    expect(locator).to_be_attached()
                    if browser_name == "firefox":
                        expect(locator).to_be_visible()
                        return
                    # This host's Playwright Chromium has no installed font metrics
                    # (even a bare local h1 has zero line-box height). Structural
                    # layout/CSS plus forced pointer activation still proves the
                    # storage protocol without calling that host limitation visual.
                    layout = locator.evaluate(
                        "element => { const style = getComputedStyle(element); "
                        "const rect = element.getBoundingClientRect(); return {"
                        "display: style.display, visibility: style.visibility, "
                        "width: rect.width}; }"
                    )
                    assert layout["display"] == "block"
                    assert layout["visibility"] == "visible"
                    assert layout["width"] > 0

                def activate(locator: Any) -> None:
                    if browser_name == "chromium":
                        locator.evaluate("element => element.click()")
                    else:
                        locator.click()

                heading = frame.get_by_role("heading", name="CAPABILITY SITE")
                expect_rendered(heading)
                expect(frame.locator("h1")).to_have_css("color", "rgb(0, 128, 0)")
                expect(frame.locator("body")).to_have_attribute("data-script-loaded", "true")

                counter = frame.get_by_role("button", name="Count 0")
                expect(counter).to_be_attached()
                counter.click(force=browser_name == "chromium")
                expect(frame.get_by_role("button", name="Count 1")).to_be_attached()
                assert "disco-selection-agent:v1" in frame.content()
                assert "disco-element-mention-picker:v1" in frame.content()

                activate(frame.get_by_role("link", name="Open nested route"))
                frame.wait_for_url(re.compile(r"/nested\.html$"))
                expect_rendered(frame.get_by_role("heading", name="NESTED CAPABILITY ROUTE"))
                assert frame.url.startswith(f"http://{launch_origin}/nested.html")
                frame.evaluate("localStorage.setItem('preview-state', 'old-authority')")
                if browser_name == "firefox":
                    frame.evaluate("document.cookie='app_session=current-preview; Path=/'")
                page.context.add_cookies(
                    [
                        {
                            "name": "app_session",
                            "value": "current-preview",
                            "url": f"http://{launch_origin}/",
                        },
                        {
                            "name": "http_only_app",
                            "value": "old-authority",
                            "url": f"http://{launch_origin}/",
                            "httpOnly": True,
                        },
                    ]
                )
                if browser_name == "firefox":
                    assert "app_session=current-preview" in frame.evaluate("document.cookie")

                # A process restart reconstructs the lease and committed target from
                # durable events. The same isolated page and capability remain exact.
                stop_server(server, thread, listeners)
                initial_store.close()
                initial_store_open = False
                restarted_store = SqliteEventStore(db_path)
                restarted_projects = ProjectStore(str(project_root))
                restarted_app = _app(
                    restarted_store,
                    _StaticRuntime(restarted_projects, target_port=None),
                )
                server, thread, restarted_main_port, _, listeners = start_server(
                    restarted_app,
                    gateway_start=gateway_start,
                )
                assert restarted_main_port > 0
                frame.goto(frame.url, wait_until="domcontentloaded")
                expect_rendered(frame.get_by_role("heading", name="NESTED CAPABILITY ROUTE"))
                expect(frame.locator("h1")).to_have_css("color", "rgb(0, 128, 0)")
                expect(frame.locator("body")).to_have_attribute("data-script-loaded", "true")

                history_launch = mint(
                    restarted_main_port,
                    token,
                    session.csrf_token,
                    "/",
                    1,
                )
                history_origin = urlsplit(history_launch["bootstrap_url"]).netloc
                assert history_origin != launch_origin
                page.goto(f"http://localhost:{restarted_main_port}/api/auth/session")
                history_frame = submit_launch(page, history_launch, "history-preview")
                expect_rendered(history_frame.get_by_role("heading", name="CAPABILITY SITE"))
                assert "app_session=current-preview" not in history_frame.evaluate(
                    "document.cookie"
                )
                expect(history_frame.locator("h1")).to_have_css("color", "rgb(0, 128, 0)")
                expect(history_frame.locator("body")).to_have_attribute(
                    "data-script-loaded", "true"
                )
                activate(history_frame.get_by_role("link", name="Open nested route"))
                history_frame.wait_for_url(re.compile(r"/nested\.html$"))
                expect_rendered(
                    history_frame.get_by_role("heading", name="NESTED CAPABILITY ROUTE")
                )

                # The two-slot pool now reassigns the first origin to another
                # conversation. Its two-stage handoff must remove old JS-visible
                # state and HttpOnly app cookies before installing the new cap.
                second_launch = mint(
                    restarted_main_port,
                    token,
                    session.csrf_token,
                    conversation_id=second_cid,
                )
                assert urlsplit(second_launch["bootstrap_url"]).netloc == launch_origin
                page.goto(f"http://localhost:{restarted_main_port}/api/auth/session")
                second_frame = submit_launch(page, second_launch, "second-preview")
                expect_rendered(second_frame.get_by_role("heading", name="CAPABILITY SITE"))
                assert second_frame.evaluate("localStorage.getItem('preview-state')") is None
                cookie_names = {
                    cookie["name"] for cookie in page.context.cookies(f"http://{launch_origin}/")
                }
                assert "app_session" not in cookie_names
                assert "http_only_app" not in cookie_names
            finally:
                browser.close()
    finally:
        if thread.is_alive():
            stop_server(server, thread, listeners)
        if restarted_store is not None:
            restarted_store.close()
        if initial_store_open:
            initial_store.close()


async def test_canonical_loopback_preserves_route_during_real_vite_react_hmr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stock Firefox frame receives React HMR over the canonical isolated origin."""
    import asyncio
    import contextlib
    import os
    import socket

    import httpx
    import uvicorn
    from playwright.async_api import async_playwright, expect

    monkeypatch.setenv("DISCO_AUTH_SECRET", "canonical-loopback-vite-hmr-secret")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)
    cid = "conv_a1b2c3d4vitehmr"

    def bind_socket(port: int = 0, host: str = "127.0.0.1") -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(socket.SOMAXCONN)
        return listener

    gateway_start = 0
    gateways: list[socket.socket] = []
    for candidate in range(22320, 25320, 2):
        try:
            monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", str(candidate))
            monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "2")
            gateways = [
                bind_socket(candidate, local_preview_gateway_host(candidate)),
                bind_socket(candidate + 1, local_preview_gateway_host(candidate + 1)),
            ]
            gateway_start = candidate
            break
        except OSError:
            for listener in gateways:
                listener.close()
            gateways = []
    assert gateway_start and len(gateways) == 2
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", str(gateway_start))
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "2")

    probe = bind_socket()
    vite_port = int(probe.getsockname()[1])
    probe.close()
    vite_root = tmp_path / "vite-react"
    (vite_root / "src").mkdir(parents=True)
    (vite_root / "node_modules").symlink_to(
        (Path.cwd() / "frontend" / "node_modules").resolve(),
        target_is_directory=True,
    )
    (vite_root / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Canonical Vite React</title>'
        '<style>h1{color:rgb(82,45,145)}</style><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script>'
    )

    def react_source(label: str) -> str:
        return (
            'import React from "react";\n'
            'import {createRoot} from "react-dom/client";\n'
            "const root=globalThis.__discoTestRoot ??= "
            'createRoot(document.getElementById("root"));\n'
            'function App(){return React.createElement("main",null,'
            f'React.createElement("h1",null,{label!r}),'
            'React.createElement("button",{onClick:()=>history.pushState({},"",'
            '"/work/item?tab=details#inspector")},"Open work item"));}\n'
            "root.render(React.createElement(App));\n"
            "if(import.meta.hot) import.meta.hot.accept();\n"
        )

    source_path = vite_root / "src" / "main.jsx"
    source_path.write_text(react_source("VITE REACT ONE"))
    vite_process = await asyncio.create_subprocess_exec(
        str((Path.cwd() / "frontend" / "node_modules" / ".bin" / "vite").resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        str(vite_port),
        "--strictPort",
        cwd=vite_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    store = SqliteEventStore(tmp_path / "vite-preview.sqlite3")
    projects = ProjectStore(str(tmp_path / "vite-projects"))
    store.create_conversation(cid, owner_id="owner-a", surface="build")
    await store.append(
        cid,
        DeliverableEvent(title="Vite React app", path="index.html", artifact_kind="app"),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.RUNNING))
    vite_origin = f"http://127.0.0.1:{vite_port}"

    class LiveViteRuntime(_StaticRuntime):
        def preview_target_port(self, _conversation_id: str) -> int | None:
            return 8000

        async def preview(self, _conversation_id: str) -> dict[str, object]:
            return {
                "available": True,
                "generation": "vite-react-generation-1",
                "port": 8000,
                "status": "ready",
                "launch_kind": "framework",
                "reload_strategy": "hmr",
            }

        async def wake_for_preview(
            self,
            _cid8: str,
            port: int,
            *,
            owner_id: str | None = None,
        ) -> str | None:
            return vite_origin if port == 8000 and owner_id == "owner-a" else None

    runtime = LiveViteRuntime(projects, target_port=8000)
    app = _app(store, runtime, live_upstream=vite_origin)
    main_listener = bind_socket()
    main_port = int(main_listener.getsockname()[1])
    listeners = [main_listener, *gateways]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=main_port,
        log_level="critical",
        lifespan="off",
        ws="websockets-sansio",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(sockets=listeners))

    try:
        deadline = asyncio.get_running_loop().time() + 15
        async with httpx.AsyncClient(timeout=1) as probe_client:
            while True:
                if vite_process.returncode is not None:
                    output = (
                        await vite_process.stdout.read() if vite_process.stdout is not None else b""
                    )
                    raise AssertionError(f"Vite exited before readiness: {output.decode()}")
                try:
                    if (await probe_client.get(vite_origin)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert asyncio.get_running_loop().time() < deadline, "Vite did not become ready"
                await asyncio.sleep(0.05)
        while not server.started and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert server.started and not server_task.done()

        token, session = SessionSigner().mint(owner_id="owner-a", is_admin=True)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{main_port}",
            cookies={SESSION_COOKIE: token},
            timeout=10,
        ) as owner:
            minted = await owner.post(
                f"/conversations/{cid}/preview/capability",
                headers={
                    "Origin": f"http://127.0.0.1:{main_port}",
                    CSRF_HEADER: session.csrf_token,
                },
                json={"target_path": "/", "transport": "canonical"},
            )
        assert minted.status_code == 200, minted.text
        launch = cast(dict[str, str], minted.json())
        assert urlsplit(launch["bootstrap_url"]).hostname in {"127.0.0.2", "127.0.0.3"}

        async with async_playwright() as playwright:
            browser = await playwright.firefox.launch(headless=True)
            try:
                page = await browser.new_page()
                browser_errors: list[str] = []
                page.on("pageerror", lambda error: browser_errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        browser_errors.append(message.text) if message.type == "error" else None
                    ),
                )
                await page.goto(f"http://127.0.0.1:{main_port}/api/auth/session")
                await page.set_content(
                    '<iframe name="vite-preview"></iframe>'
                    '<form id="vite-launch" method="post" target="vite-preview">'
                    '<input name="intent" type="hidden"></form>'
                )
                frame = page.frame(name="vite-preview")
                assert frame is not None
                await page.eval_on_selector(
                    "#vite-launch",
                    "(form, launch) => {"
                    "form.action=launch.url;form.elements.intent.value=launch.intent;form.submit();}",
                    {
                        "url": launch["bootstrap_url"],
                        "intent": launch["bootstrap_intent"],
                    },
                )
                await frame.wait_for_url(re.compile(r"^http://127\.0\.0\.[23]:\d+/$"))
                await expect(frame.get_by_role("heading", name="VITE REACT ONE")).to_be_visible()
                await expect(frame.locator("h1")).to_have_css("color", "rgb(82, 45, 145)")
                assert "disco-selection-agent:v1" in await frame.content()

                await frame.get_by_role("button", name="Open work item").click()
                await frame.wait_for_url(re.compile(r"/work/item\?tab=details#inspector$"))
                navigation_count = 0

                def count_frame_navigation(navigated: Any) -> None:
                    nonlocal navigation_count
                    if navigated is frame:
                        navigation_count += 1

                page.on("framenavigated", count_frame_navigation)
                source_path.write_text(react_source("VITE REACT TWO"))
                os.utime(source_path, None)
                await expect(frame.get_by_role("heading", name="VITE REACT TWO")).to_be_visible(
                    timeout=15_000
                )
                assert frame.url.endswith("/work/item?tab=details#inspector")
                assert navigation_count == 0
                assert browser_errors == []
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=10)
        for listener in listeners:
            with contextlib.suppress(OSError):
                listener.close()
        if vite_process.returncode is None:
            vite_process.terminate()
            try:
                await asyncio.wait_for(vite_process.wait(), timeout=5)
            except TimeoutError:
                vite_process.kill()
                await vite_process.wait()
        store.close()


async def test_path_preview_capability_loads_asset_graph_without_app_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolated frame gets selected-preview bytes, never owner APIs."""
    await _exercise_path_preview_capability_contract(tmp_path, monkeypatch)

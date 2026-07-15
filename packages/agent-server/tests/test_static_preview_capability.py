"""H079/H110: finished previews use body-only, isolated one-use capabilities."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from disco.agent_server.auth import AgentAuthMiddleware, make_auth_router
from disco.agent_server.host_proxy import HostPreviewProxyMiddleware
from disco.agent_server.routes.preview import (
    _preview_websocket_origin_allowed,
    make_preview_router,
)
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceVersionEvent,
)
from disco.core.auth import (
    CSRF_HEADER,
    ISOLATED_PATH_PREVIEW_PREFIX,
    PATH_PREVIEW_ISOLATION_COOKIE,
    SESSION_COOKIE,
    SessionSigner,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.integration


class _StaticRuntime:
    def __init__(self, project_store: ProjectStore) -> None:
        self._project_store = project_store

    def project_store(self) -> ProjectStore:
        return self._project_store

    def live_session(self, _conversation_id: str) -> None:
        return None

    async def wake_for_preview(
        self, _cid8: str, _port: int, *, owner_id: str | None = None
    ) -> None:
        del owner_id
        return None


def _app(store: SqliteEventStore, runtime: _StaticRuntime) -> FastAPI:
    app = FastAPI()
    app.add_middleware(AgentAuthMiddleware, store=store)
    app.add_middleware(
        HostPreviewProxyMiddleware,
        upstream_resolver=lambda *_args: None,
        require_capability=True,
        redemption_store=store,
    )
    app.include_router(make_auth_router())
    app.include_router(make_preview_router(store, cast(Any, runtime)))

    @app.get("/conversations/{conversation_id}/events")
    async def owner_events(conversation_id: str) -> dict[str, object]:
        return {"conversation_id": conversation_id, "events": ["OWNER-BYTES"]}

    @app.get("/api/mcp/test-admin")
    async def admin_settings() -> dict[str, bool]:
        return {"admin": True}

    return app


def _install_owner_session(client: TestClient, owner_id: str) -> str:
    token, session = SessionSigner().mint(owner_id=owner_id, is_admin=True)
    client.cookies.set(SESSION_COOKIE, token)
    client.headers.update({"Cookie": f"{SESSION_COOKIE}={token}"})
    return session.csrf_token


def _redeem(
    client: TestClient,
    bootstrap_url: str,
    intent: str,
    *,
    headers: dict[str, str] | None = None,
):
    return client.post(
        urlsplit(bootstrap_url).path,
        headers=headers,
        data={"intent": intent},
    )


async def _finished_site(store: SqliteEventStore, projects: ProjectStore, cid: str) -> None:
    store.create_conversation(cid, owner_id="owner-a", surface="build")
    workspace = projects.path_for(cid)
    (workspace / "release" / "assets").mkdir(parents=True)
    (workspace / "release" / "index.html").write_text(
        '<link rel="stylesheet" href="assets/site.css">'
        '<script src="assets/site.js"></script><h1>CAPABILITY SITE</h1>'
    )
    (workspace / "release" / "assets" / "site.css").write_text("h1{color:green}")
    (workspace / "release" / "assets" / "site.js").write_text(
        'document.body.dataset.scriptLoaded="true"'
    )
    projects.write_manifest(
        cid,
        title="Capability site",
        owner_id="owner-a",
        created_at="2026-07-14T00:00:00+00:00",
        file_count=3,
        total_bytes=100,
    )
    await store.append(
        cid,
        DeliverableEvent(
            title="Selected capability site",
            path="release/index.html",
            artifact_kind="app",
        ),
    )
    await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
    version = projects.cut_version(cid, trigger="finish")
    assert version is not None
    await store.append(
        cid,
        WorkspaceVersionEvent(
            version_seq=version.seq,
            tree_digest=version.tree_digest,
            trigger="finish",
        ),
    )


async def test_path_preview_capability_loads_asset_graph_without_app_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolated frame gets selected-preview bytes, never owner APIs."""
    monkeypatch.setenv("DISCO_AUTH_SECRET", "h079-static-preview-secret")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)

    cid = "conv_a1b2c3d4selected"
    other_cid = "conv_deadbeefother"
    store = SqliteEventStore(":memory:")
    projects = ProjectStore(str(tmp_path / "projects"))
    await _finished_site(store, projects, cid)
    store.create_conversation(other_cid, owner_id="owner-a", surface="build")
    app = _app(store, _StaticRuntime(projects))

    owner = TestClient(app, base_url="http://127.0.0.1:18240")
    csrf = _install_owner_session(owner, "owner-a")
    capability = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/", "transport": "path"},
    )
    assert capability.status_code == 200
    assert capability.headers["cache-control"] == "no-store"
    body = capability.json()
    assert body["transport"] == "path"
    assert "path_bootstrap_url" not in body
    def redemption_count() -> int:
        return int(
            store._conn.execute("SELECT COUNT(*) FROM preview_redemptions").fetchone()[0]
        )

    assert redemption_count() == 1

    # Invalid/unredeemable targets fail before durable JTI registration.
    for invalid_target in (
        "/%2e%2e/escape",
        "/assets%2Fescape.js",
        "/assets%5cescape.js",
        "/assets%25escape.js",
        "/bad%zzescape",
    ):
        invalid = owner.post(
            f"/conversations/{cid}/preview/capability",
            headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
            json={"port": 8000, "target_path": invalid_target, "transport": "path"},
        )
        assert invalid.status_code == 400
        assert redemption_count() == 1
    too_long = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/" + "x" * 4096, "transport": "path"},
    )
    assert too_long.status_code == 422
    assert redemption_count() == 1
    expanded_too_long = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/" + "x" * 4095, "transport": "path"},
    )
    assert expanded_too_long.status_code == 400
    assert expanded_too_long.json()["detail"]["reason"] == "preview_target_too_long"
    assert redemption_count() == 1
    benign_query_encoding = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/?next=a%2Fb", "transport": "path"},
    )
    assert benign_query_encoding.status_code == 200
    assert redemption_count() == 2

    for reserved_target in (
        "/__disco/preview-auth",
        "/__disco/path-preview-auth/a1b2c3d4",
        f"/conversations/{cid}/preview-app/",
    ):
        reserved = owner.post(
            f"/conversations/{cid}/preview/capability",
            headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
            json={"port": 8000, "target_path": reserved_target, "transport": "host"},
        )
        assert reserved.status_code == 400
        assert reserved.json()["detail"]["reason"] == "reserved_preview_path"
        assert redemption_count() == 2

    host_capability = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/", "transport": "host"},
    )
    assert host_capability.status_code == 200
    assert urlsplit(host_capability.json()["bootstrap_url"]).hostname == (
        "p2-a1b2c3d4-8000.localhost"
    )
    assert redemption_count() == 3

    bare_ip_owner = TestClient(app, base_url="http://192.0.2.10:18240")
    bare_ip_csrf = _install_owner_session(bare_ip_owner, "owner-a")
    for bare_ip_transport in ("host", "path"):
        bare_ip = bare_ip_owner.post(
            f"/conversations/{cid}/preview/capability",
            headers={"Origin": "http://192.0.2.10:18240", CSRF_HEADER: bare_ip_csrf},
            json={"port": 8000, "target_path": "/", "transport": bare_ip_transport},
        )
        assert bare_ip.status_code == 409
        assert bare_ip.json()["detail"]["reason"] == "preview_wildcard_dns_required"
        assert redemption_count() == 3
    bootstrap_url = body["bootstrap_url"]
    intent = body["bootstrap_intent"]
    parsed = urlsplit(bootstrap_url)
    assert parsed.hostname == "localhost"
    assert parsed.port == 18240
    assert parsed.query == parsed.fragment == ""
    assert intent not in bootstrap_url

    isolated_origin = "http://localhost:18240"
    isolated = TestClient(app, base_url=isolated_origin)
    target = f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cid}/"
    bootstrap_path = urlsplit(bootstrap_url).path
    assert (
        isolated.get(
            bootstrap_path,
            headers={"Origin": "http://127.0.0.1:18240"},
        ).status_code
        == 403
    )
    assert (
        isolated.post(
            bootstrap_path + "/lookalike",
            headers={"Origin": "http://127.0.0.1:18240"},
            data={"intent": intent},
        ).status_code
        == 403
    )
    redeem = _redeem(
        isolated,
        bootstrap_url,
        intent,
        headers={"Origin": "http://127.0.0.1:18240"},
    )
    assert redeem.status_code == 200
    assert "location" not in redeem.headers
    assert "window.location.replace" in redeem.text
    assert target in redeem.text
    assert intent not in redeem.text
    assert "default-src 'none'" in redeem.headers["content-security-policy"]
    nonce_match = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', redeem.text)
    assert nonce_match is not None
    assert (
        f"script-src 'nonce-{nonce_match.group(1)}'"
        in redeem.headers["content-security-policy"]
    )
    assert redeem.headers["x-content-type-options"] == "nosniff"
    assert redeem.headers["clear-site-data"] == '"storage"'
    set_cookie = redeem.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    assert f"Path={target}" in set_cookie
    assert f"{PATH_PREVIEW_ISOLATION_COOKIE}=1" in set_cookie
    assert SESSION_COOKIE not in set_cookie

    # H110: cookie posture comes from the one real browser form navigation,
    # where fetch metadata still describes the cross-site iframe embedding.
    iframe_capability = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/", "transport": "path"},
    )
    iframe_body = iframe_capability.json()
    iframe_isolated = TestClient(app, base_url=isolated_origin)
    iframe_redeem = _redeem(
        iframe_isolated,
        iframe_body["bootstrap_url"],
        iframe_body["bootstrap_intent"],
        headers={
            "Origin": "http://127.0.0.1:18240",
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    iframe_cookie = iframe_redeem.headers["set-cookie"]
    assert "HttpOnly" in iframe_cookie
    assert "; Secure" in iframe_cookie
    assert "samesite=none" in iframe_cookie.lower()
    assert "Partitioned" in iframe_cookie
    assert "samesite=strict" not in iframe_cookie.lower()

    # Durable JTI exchange is one-time; replay never sets a cookie.
    replay = _redeem(
        iframe_isolated,
        iframe_body["bootstrap_url"],
        iframe_body["bootstrap_intent"],
        headers={"Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site"},
    )
    assert replay.status_code == 403
    assert "set-cookie" not in replay.headers

    document = isolated.get(target)
    css = isolated.get(f"{target}assets/site.css")
    script = isolated.get(f"{target}assets/site.js")
    assert document.status_code == 200 and "CAPABILITY SITE" in document.text
    assert document.headers["cache-control"] == "private, no-store"
    assert document.headers["pragma"] == "no-cache"
    assert css.status_code == 200 and css.text == "h1{color:green}"
    assert css.headers["cache-control"] == "private, no-store"
    assert script.status_code == 200 and "scriptLoaded" in script.text
    assert script.headers["cache-control"] == "private, no-store"

    for service_worker_headers in (
        {"Sec-Fetch-Dest": "serviceworker"},
        {"Service-Worker": "script"},
    ):
        service_worker = isolated.get(target + "sw.js", headers=service_worker_headers)
        assert service_worker.status_code == 403
        assert service_worker.text == "preview service workers disabled"
        assert service_worker.headers["cache-control"] == "private, no-store"

    preauthenticated_capability = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/", "transport": "path"},
    ).json()
    preauthenticated_isolated = TestClient(app, base_url=isolated_origin)
    _install_owner_session(preauthenticated_isolated, "owner-a")
    rejected_session_redeem = _redeem(
        preauthenticated_isolated,
        preauthenticated_capability["bootstrap_url"],
        preauthenticated_capability["bootstrap_intent"],
    )
    assert rejected_session_redeem.status_code == 403
    assert "set-cookie" not in rejected_session_redeem.headers
    clean_after_session_rejection = TestClient(app, base_url=isolated_origin)
    clean_redeem = _redeem(
        clean_after_session_rejection,
        preauthenticated_capability["bootstrap_url"],
        preauthenticated_capability["bootstrap_intent"],
    )
    assert clean_redeem.status_code == 200

    # A full app session deliberately planted on the isolated host cannot
    # replace the path capability, reach owner APIs, or use public credential
    # grants. This remains true after the path capability is absent/expired.
    isolated_with_session = TestClient(app, base_url=isolated_origin)
    isolated_with_session.cookies.set(PATH_PREVIEW_ISOLATION_COOKIE, "1")
    isolated_with_session.headers.update(
        {"Cookie": f"{PATH_PREVIEW_ISOLATION_COOKIE}=1"}
    )
    _install_owner_session(isolated_with_session, "owner-a")
    isolated_with_session.headers.update(
        {
            "Cookie": (
                f"{PATH_PREVIEW_ISOLATION_COOKIE}=1; "
                f"{SESSION_COOKIE}={isolated_with_session.cookies.get(SESSION_COOKIE)}"
            )
        }
    )
    denied_preview = isolated_with_session.get(target)
    assert denied_preview.status_code == 403
    assert denied_preview.text == "preview capability required"
    for denied_path in (
        f"/conversations/{cid}/events",
        "/api/auth/pairing-token",
        "/api/auth/mint",
    ):
        if denied_path.endswith("/mint"):
            denied = isolated_with_session.post(
                denied_path,
                headers={"Origin": isolated_origin},
                json={"pairing_token": None},
            )
        else:
            denied = isolated_with_session.get(
                denied_path, headers={"Origin": isolated_origin}
            )
        assert denied.status_code == 403
    session_worker = isolated_with_session.get(
        target + "sw.js", headers={"Sec-Fetch-Dest": "serviceworker"}
    )
    assert session_worker.status_code == 403
    assert session_worker.text == "preview service workers disabled"

    # Only path_live tokens carry the signed HMR websocket claim. The isolated
    # origin has no app session; reaching "preview not available" proves the
    # capability authenticated before this fixture's intentionally absent runtime.
    live_capability = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": "/", "transport": "path_live"},
    )
    live_body = live_capability.json()
    live_isolated = TestClient(app, base_url=isolated_origin)
    live_redeem = _redeem(
        live_isolated, live_body["bootstrap_url"], live_body["bootstrap_intent"]
    )
    assert live_redeem.status_code == 200
    with pytest.raises(WebSocketDisconnect) as live_disconnect:
        with live_isolated.websocket_connect(
            f"ws://localhost:18240{ISOLATED_PATH_PREVIEW_PREFIX}/{cid}/hmr",
            headers={"Origin": isolated_origin, "Host": "localhost:18240"},
        ):
            pass
    assert live_disconnect.value.reason == "preview not available"
    with pytest.raises(WebSocketDisconnect) as wrong_origin_disconnect:
        with live_isolated.websocket_connect(
            f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cid}/hmr",
            headers={"Origin": "https://evil.example", "Host": "localhost:18240"},
        ):
            pass
    assert wrong_origin_disconnect.value.reason == "preview origin required"
    assert _preview_websocket_origin_allowed(
        "http://localhost:18240", "localhost:18240", websocket_scheme="ws"
    )
    assert not _preview_websocket_origin_allowed(
        "https://localhost:18240", "localhost:18240", websocket_scheme="ws"
    )
    assert _preview_websocket_origin_allowed(
        "https://localhost:18240",
        "localhost:18240",
        websocket_scheme="ws",
        forwarded_proto="https",
    )
    assert not _preview_websocket_origin_allowed(
        "http://localhost:18240",
        "localhost:18240",
        websocket_scheme="ws",
        forwarded_proto="https",
    )

    # A signed query remains inert script data; closing tags are escaped.
    hostile_target = "/?next=</script><script>globalThis.pwned=1</script>"
    hostile_capability = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": hostile_target, "transport": "path"},
    )
    hostile_body = hostile_capability.json()
    hostile_redeem = _redeem(
        isolated, hostile_body["bootstrap_url"], hostile_body["bootstrap_intent"]
    )
    assert hostile_redeem.status_code == 200
    assert "</script><script>globalThis.pwned" not in hostile_redeem.text
    assert r"\u003c/script\u003e\u003cscript\u003e" in hostile_redeem.text

    # The capability is exact to this conversation and route family.
    other = isolated.get(f"/conversations/{other_cid}/preview-app/")
    events = isolated.get(f"/conversations/{cid}/events")
    session = isolated.get("/api/auth/session")
    pairing = isolated.get("/api/auth/pairing-token", headers={"Origin": isolated_origin})
    assert other.status_code == 403
    assert events.status_code == 403
    assert session.status_code == 403
    assert pairing.status_code == 403

    # SECURITY: the isolated localhost preview cannot target the operator's
    # original 127 alias and regain the full HostOnly session cookie. The guard
    # runs before public session/pairing/mint routes and applies to WebSockets.
    cross_alias_headers = {
        "Origin": isolated_origin,
        "Host": "127.0.0.1:18240",
    }
    for denied_path in (
        "/api/auth/session",
        "/api/auth/pairing-token",
        f"/conversations/{cid}/events",
        "/api/mcp/test-admin",
    ):
        denied = owner.get(denied_path, headers=cross_alias_headers)
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "private, no-store"
    denied_mint = owner.post(
        "/api/auth/mint",
        headers=cross_alias_headers,
        json={"pairing_token": None},
    )
    assert denied_mint.status_code == 403
    with pytest.raises(WebSocketDisconnect) as cross_alias_ws:
        with owner.websocket_connect(
            f"/conversations/{cid}/preview-app/hmr",
            headers=cross_alias_headers,
        ):
            pass
    assert cross_alias_ws.value.reason == "preview origin required"

    # Normal split-origin development remains valid when the hostname is exact
    # and only the port differs.
    same_host_session = owner.get(
        "/api/auth/session",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Host": "127.0.0.1:18240",
        },
    )
    assert same_host_session.status_code == 200
    assert same_host_session.json()["authenticated"] is True
    assert (
        owner.get(
            f"/conversations/{cid}/events",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Host": "127.0.0.1:18240",
            },
        ).json()["events"]
        == ["OWNER-BYTES"]
    )

    remote_owner = TestClient(app, base_url="https://mybox.example")
    remote_csrf = _install_owner_session(remote_owner, "owner-a")
    remote_capability = remote_owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "https://mybox.example", CSRF_HEADER: remote_csrf},
        json={"port": 8000, "target_path": "/", "transport": "path"},
    )
    remote_body = remote_capability.json()
    remote_url = remote_body["bootstrap_url"]
    assert urlsplit(remote_url).hostname == "p2-a1b2c3d4-8000.mybox.example"
    remote_isolated = TestClient(app, base_url="https://p2-a1b2c3d4-8000.mybox.example")
    remote_redeem = _redeem(
        remote_isolated, remote_url, remote_body["bootstrap_intent"]
    )
    assert remote_redeem.status_code == 200
    assert "; Secure" in remote_redeem.headers["set-cookie"]
    remote_document = remote_isolated.get(target)
    assert remote_document.status_code == 200
    assert "CAPABILITY SITE" in remote_document.text
    store.close()

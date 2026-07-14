"""H079: finished multi-file previews use an isolated, preview-only origin."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from disco.agent_server.auth import AgentAuthMiddleware, make_auth_router
from disco.agent_server.host_proxy import HostPreviewProxyMiddleware
from disco.agent_server.routes.preview import make_preview_router
from disco.core import (
    ConversationStatus,
    DeliverableEvent,
    SqliteEventStore,
    StatusEvent,
    WorkspaceVersionEvent,
)
from disco.core.auth import CSRF_HEADER, SESSION_COOKIE, SessionSigner
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    )
    app.include_router(make_auth_router())
    app.include_router(make_preview_router(store, cast(Any, runtime)))
    return app


def _install_owner_session(client: TestClient, owner_id: str) -> str:
    token, session = SessionSigner().mint(owner_id=owner_id, is_admin=True)
    client.cookies.set(SESSION_COOKIE, token)
    client.headers.update({"Cookie": f"{SESSION_COOKIE}={token}"})
    return session.csrf_token


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
    """The isolated frame gets only selected-preview bytes, never owner APIs."""
    monkeypatch.setenv("DISCO_AUTH_SECRET", "h079-static-preview-secret")
    # Exercise real cookie authentication, not AgentAuthMiddleware's pytest shortcut.
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
        json={"port": 8000, "target_path": "/"},
    )
    assert capability.status_code == 200
    path_bootstrap_url = capability.json()["path_bootstrap_url"]
    parsed = urlsplit(path_bootstrap_url)
    # 127.0.0.1 and localhost are distinct browser origins, but both resolve
    # without wildcard-localhost DNS support (notably in stock Firefox).
    assert parsed.hostname == "localhost"
    assert parsed.port == 18240

    isolated = TestClient(app, base_url="http://localhost:18240")
    bootstrap = isolated.get(path_bootstrap_url, follow_redirects=False)
    target = f"/conversations/{cid}/preview-app/"
    # H084: neither a redirect nor a single 200 document can install a Strict
    # cookie from the original 127.0.0.1 -> localhost cross-site iframe
    # navigation. The first locked-down document must move to a second URL on
    # localhost without setting any cookie. Only that same-site redemption
    # response may install the capability and navigate to the target.
    assert bootstrap.status_code == 200
    assert "location" not in bootstrap.headers
    assert "set-cookie" not in bootstrap.headers
    redeem_match = re.search(r"window\.location\.replace\((\"[^\"]+\")\)", bootstrap.text)
    assert redeem_match is not None
    redeem_target = json.loads(redeem_match.group(1))
    redeem_parts = urlsplit(redeem_target)
    assert redeem_parts.path == f"/__disco/path-preview-auth/{cid.removeprefix('conv_')[:8]}"
    assert parse_qs(redeem_parts.query).get("stage") == ["redeem"]

    redeem = isolated.get(redeem_target, follow_redirects=False)
    assert redeem.status_code == 200
    assert "location" not in redeem.headers
    assert f"window.location.replace({json.dumps(target)})" in redeem.text
    assert "default-src 'none'" in bootstrap.headers["content-security-policy"]
    nonce_match = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', redeem.text)
    assert nonce_match is not None
    assert (
        f"script-src 'nonce-{nonce_match.group(1)}'"
        in redeem.headers["content-security-policy"]
    )
    assert redeem.headers["x-content-type-options"] == "nosniff"
    set_cookie = redeem.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    assert f"Path=/conversations/{cid}/preview-app/" in set_cookie
    assert SESSION_COOKIE not in set_cookie

    document = isolated.get(target)
    css = isolated.get(f"/conversations/{cid}/preview-app/assets/site.css")
    script = isolated.get(f"/conversations/{cid}/preview-app/assets/site.js")
    assert document.status_code == 200 and "CAPABILITY SITE" in document.text
    assert css.status_code == 200 and css.text == "h1{color:green}"
    assert script.status_code == 200 and "scriptLoaded" in script.text

    # The signed target may contain an arbitrary query. It must remain data in
    # the bootstrap script rather than breaking out of the JSON string or CSP.
    hostile_target = "/?next=</script><script>globalThis.pwned=1</script>"
    hostile_capability = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"port": 8000, "target_path": hostile_target},
    )
    assert hostile_capability.status_code == 200
    hostile_bootstrap = isolated.get(
        hostile_capability.json()["path_bootstrap_url"], follow_redirects=False
    )
    assert hostile_bootstrap.status_code == 200
    hostile_redeem_match = re.search(
        r"window\.location\.replace\((\"[^\"]+\")\)", hostile_bootstrap.text
    )
    assert hostile_redeem_match is not None
    hostile_redeem = isolated.get(
        json.loads(hostile_redeem_match.group(1)), follow_redirects=False
    )
    assert hostile_redeem.status_code == 200
    assert "</script><script>" not in hostile_redeem.text
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in hostile_redeem.text

    # The preview capability is exact to this conversation and preview route.
    other = isolated.get(f"/conversations/{other_cid}/preview-app/")
    events = isolated.get(f"/conversations/{cid}/events")
    session = isolated.get("/api/auth/session")
    assert other.status_code in {401, 403}
    assert events.status_code == 401
    assert session.status_code == 200
    assert session.json() == {"authenticated": False}

    # Remote front doors use their existing capability-gated wildcard host. The
    # host proxy must pass this static path family to the snapshot route instead
    # of attempting the live port upstream.
    remote_owner = TestClient(app, base_url="https://mybox.example")
    remote_csrf = _install_owner_session(remote_owner, "owner-a")
    remote_capability = remote_owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "https://mybox.example", CSRF_HEADER: remote_csrf},
        json={"port": 8000, "target_path": "/"},
    )
    assert remote_capability.status_code == 200
    remote_url = remote_capability.json()["path_bootstrap_url"]
    assert urlsplit(remote_url).hostname == "a1b2c3d4-8000.mybox.example"
    remote_isolated = TestClient(
        app, base_url="https://a1b2c3d4-8000.mybox.example"
    )
    remote_bootstrap = remote_isolated.get(remote_url, follow_redirects=False)
    assert remote_bootstrap.status_code == 200
    assert "location" not in remote_bootstrap.headers
    assert "set-cookie" not in remote_bootstrap.headers
    remote_redeem_match = re.search(
        r"window\.location\.replace\((\"[^\"]+\")\)", remote_bootstrap.text
    )
    assert remote_redeem_match is not None
    remote_redeem = remote_isolated.get(
        json.loads(remote_redeem_match.group(1)), follow_redirects=False
    )
    assert remote_redeem.status_code == 200
    assert f"window.location.replace({json.dumps(target)})" in remote_redeem.text
    assert "; Secure" in remote_redeem.headers["set-cookie"]
    remote_document = remote_isolated.get(target)
    assert remote_document.status_code == 200
    assert "CAPABILITY SITE" in remote_document.text
    store.close()

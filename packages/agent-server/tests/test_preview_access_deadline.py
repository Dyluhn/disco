"""Access deadlines describe existing authority; they never renew authorization."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from _static_preview_capability_support import (
    _app,
    _finished_site,
    _install_owner_session,
    _redeem_steps,
    _StaticRuntime,
)
from disco.agent_server.preview_bootstrap import (
    preview_navigation_document,
    preview_storage_reset_document,
)
from disco.core import SqliteEventStore
from disco.core.auth import CSRF_HEADER, PreviewCapabilitySigner, local_preview_cookie_name
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


async def test_launch_deadline_uses_actual_lease_and_verified_delayed_redemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCO_AUTH_SECRET", "preview-expiry-test-secret")
    monkeypatch.setenv("DISCO_AUTH_PREVIEW_TTL_S", "900")
    monkeypatch.setenv("DISCO_AUTH_PREVIEW_INTENT_TTL_S", "30")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_START", "19250")
    monkeypatch.setenv("DISCO_LOCAL_PREVIEW_PORT_COUNT", "2")
    monkeypatch.setattr("disco.agent_server.auth._is_testclient", lambda _request: False)
    now = [1_800_000_000]
    monkeypatch.setattr("time.time", lambda: now[0])
    cid = "conv_a1b2c3d4deadline"
    store = SqliteEventStore(":memory:")
    projects = ProjectStore(str(tmp_path / "projects"))
    await _finished_site(store, projects, cid)
    app = _app(store, _StaticRuntime(projects, target_port=None))
    owner = TestClient(app, base_url="http://127.0.0.1:18240")
    csrf = _install_owner_session(owner, "owner-a")
    response = owner.post(
        f"/conversations/{cid}/preview/capability",
        headers={"Origin": "http://127.0.0.1:18240", CSRF_HEADER: csrf},
        json={"target_path": "/", "transport": "canonical"},
    )
    assert response.status_code == 200
    minted = response.json()
    port = urlsplit(minted["bootstrap_url"]).port
    assert port is not None
    lease = store.resolve_local_preview_lease(port, now=now[0])
    assert lease is not None
    assert minted["access_expires_at"] == lease.expires_at
    assert minted["server_time"] == now[0]
    assert minted["access_expires_at"] - now[0] == 900  # Not the 30-second intent TTL.
    now[0] += 12
    browser = TestClient(app, base_url=f"http://{urlsplit(minted['bootstrap_url']).netloc}")
    reset, redemption = _redeem_steps(browser, minted["bootstrap_url"], minted["bootstrap_intent"])
    assert reset is not None, (redemption.status_code, redemption.text)
    assert redemption.status_code == 200
    payload = redemption.json()
    token = browser.cookies.get(local_preview_cookie_name(port))
    assert token is not None
    cap = PreviewCapabilitySigner(redemption_store=store).verify(
        token, cid8="a1b2c3d4", port=lease.target_port, method="GET", path="/"
    )
    assert cap is not None
    assert payload == {"target": "/", "access_expires_at": cap.expires_at, "server_time": now[0]}
    assert payload["access_expires_at"] == now[0] + 900
    assert token not in redemption.text
    assert store.resolve_local_preview_lease(port, now=now[0]) == replace(
        lease, storage_reset_required=False
    )


def test_navigation_metadata_precedes_legacy_ready_and_contains_no_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.time", lambda: 1000.25)
    body, headers = preview_navigation_document("/app?view=1", access_expires_at=1100)
    text = body.decode()
    match = re.search(r"postMessage\((\{.*?\}),\"\*\"\)", text)
    assert match is not None
    assert json.loads(match.group(1)) == {
        "channel": "disco-preview-access",
        "expires_at": 1100,
        "server_time": 1000.25,
    }
    assert text.index("disco-preview-access") < text.index("disco-preview-bootstrap-ready")
    assert text.index("disco-preview-bootstrap-ready") < text.index("window.location.replace")
    assert "default-src 'none'" in headers["content-security-policy"]
    legacy, _ = preview_navigation_document("/")
    assert b"disco-preview-access" not in legacy
    assert b"disco-preview-bootstrap-ready" in legacy


def test_storage_reset_waits_for_verified_handoff_before_deadline_and_ready() -> None:
    body, headers = preview_storage_reset_document("one-use-handoff")
    text = body.decode()
    assert text.index("return response.json()") < text.index("disco-preview-access")
    assert text.index("disco-preview-access") < text.index("disco-preview-bootstrap-ready")
    assert text.index("disco-preview-bootstrap-ready") < text.index("window.location.replace")
    assert text.count("disco-preview-bootstrap-ready") == 1
    message = text.split("window.parent.postMessage(", 1)[1].split('"*")', 1)[0]
    assert "one-use-handoff" not in message
    assert headers["clear-site-data"] == '"cache", "cookies", "storage"'

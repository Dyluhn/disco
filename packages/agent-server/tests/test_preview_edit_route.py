"""A1.1b — the click-to-edit preview-edit route.

GET /conversations/{cid}/preview-edit/{path} serves a workspace HTML file:
  - stamped with data-oid (the keystone stamp_oids),
  - with the in-frame selection agent injected as a nonce'd <script>,
  - under a tailored CSP that permits exactly that nonce'd script.

A `..` traversal, a missing file, a non-HTML file, and a SYMLINK escaping the
workspace each 404. A relative sibling asset (css/js) is served as-is.

The route reads SYMLINK-SAFELY from the host ProjectStore snapshot (NOT the live
container session, whose `cat -- target` would follow a planted symlink), so the
tests seed a real workspace dir on disk under ProjectStore.path_for(cid).
"""

from __future__ import annotations

import re

from disco.agent_server import create_app
from disco.core import SqliteEventStore
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

_HTML = """<!DOCTYPE html>
<html>
<head><title>Demo</title><link rel="stylesheet" href="style.css"></head>
<body>
  <h1>Hello</h1>
  <p class="lead">A built static page.</p>
  <script src="app.js"></script>
</body>
</html>
"""

_CSS = "h1 { color: rebeccapurple; }\n"


class _Runtime:
    """Reads come from the host snapshot (project_store), never the live session —
    the route is snapshot-only for symlink safety."""

    def __init__(self, ps: ProjectStore) -> None:
        self._ps = ps

    def set_surface(self, cid: str, surface: object) -> None: ...
    def set_model_override(self, cid: str, model: object) -> None: ...
    def set_depth(self, cid: str, tier: object) -> None: ...
    def get_last_selected_model(self) -> str | None:
        return None
    def sandbox_backend_name(self) -> str | None:
        return "process"
    def live_session(self, cid: str) -> None:
        return None

    def project_store(self) -> ProjectStore:
        return self._ps


def _client(tmp_path, files: dict[str, bytes]) -> tuple[TestClient, str]:
    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))
    client = TestClient(create_app(store, runtime=_Runtime(ps)))  # type: ignore[arg-type]
    cid = client.post("/conversations", json={"owner_id": "local"}).json()["conversation_id"]
    ws = ps.path_for(cid)
    ws.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return client, cid


def test_serves_stamped_html_with_injected_nonce_script_and_csp(tmp_path) -> None:
    client, cid = _client(tmp_path, {"index.html": _HTML.encode(), "style.css": _CSS.encode()})
    r = client.get(f"/conversations/{cid}/preview-edit/index.html")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    body = r.text

    # 1) Stamped: data-oid attributes present.
    assert 'data-oid="index.html:' in body
    # 2) The selection agent is injected as <script nonce="...">.
    m = re.search(r'<script nonce="([^"]+)">', body)
    assert m is not None, "selection script not injected with a nonce"
    nonce = m.group(1)
    assert "disco:overlay:arm" in body
    assert "disco-select" in body
    # 3) CSP permits EXACTLY the nonce'd script; NO frame-ancestors (split origins —
    # the UI frames the agent cross-origin, so 'self' would reject the iframe).
    csp = r.headers.get("content-security-policy")
    assert csp is not None
    assert f"script-src 'nonce-{nonce}'" in csp
    assert "default-src 'self'" in csp
    assert "default-src 'none'" not in csp
    assert "frame-ancestors" not in csp


def test_symlink_escape_is_rejected_404(tmp_path) -> None:
    """The load-bearing security test: a workspace symlink pointing OUTSIDE the
    workspace (the classic container `cat -- leak.html` → /etc/passwd escape) must
    NOT be served. The snapshot read resolves the real path and rejects the escape."""
    client, cid = _client(tmp_path, {"index.html": _HTML.encode()})
    # Plant leak.html -> /etc/passwd inside the workspace.
    from disco.tools.projects import ProjectStore as _PS  # workspace path

    ws = _PS(str(tmp_path)).path_for(cid)
    (ws / "leak.html").symlink_to("/etc/passwd")
    r = client.get(f"/conversations/{cid}/preview-edit/leak.html")
    assert r.status_code == 404  # escape rejected — never serves host files


def test_traversal_path_404(tmp_path) -> None:
    client, cid = _client(tmp_path, {"index.html": _HTML.encode()})
    r = client.get(f"/conversations/{cid}/preview-edit/../etc/passwd")
    assert r.status_code == 404


def test_missing_file_404(tmp_path) -> None:
    client, cid = _client(tmp_path, {"index.html": _HTML.encode()})
    assert client.get(f"/conversations/{cid}/preview-edit/nope.html").status_code == 404


def test_non_html_unknown_type_404(tmp_path) -> None:
    client, cid = _client(tmp_path, {"data.bin": b"\x00\x01binary"})
    assert client.get(f"/conversations/{cid}/preview-edit/data.bin").status_code == 404


def test_sibling_css_served_as_is_no_stamp_no_script(tmp_path) -> None:
    client, cid = _client(tmp_path, {"index.html": _HTML.encode(), "style.css": _CSS.encode()})
    r = client.get(f"/conversations/{cid}/preview-edit/style.css")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/css")
    assert r.text == _CSS
    assert "data-oid" not in r.text
    assert "disco-select" not in r.text


def test_no_runtime_404(tmp_path) -> None:
    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=None))
    cid = client.post("/conversations", json={"owner_id": "local"}).json()["conversation_id"]
    assert client.get(f"/conversations/{cid}/preview-edit/index.html").status_code == 404

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

import asyncio
import re
import uuid
from types import SimpleNamespace

from disco.agent_server.routes.preview_edit import make_preview_edit_router
from disco.core import (
    ConversationStatus,
    FinalWorkspaceSeal,
    ResourceKey,
    SqliteEventStore,
    StatusEvent,
    WorkspaceVersionEvent,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
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

    def __set_surface(self, cid: str, surface: object) -> None: ...
    def _set_model_override(self, cid: str, model: object) -> None: ...
    def set_depth(self, cid: str, tier: object) -> None: ...
    def _get_last_selected_model(self) -> str | None:
        return None

    def _backend_name(self) -> str | None:
        return "process"

    def live_session(self, cid: str) -> None:
        return None

    def _current_project_store(self) -> ProjectStore:
        return self._ps

    @property
    def projects(self) -> SimpleNamespace:
        return SimpleNamespace(current_project_store=self._current_project_store)

    @property
    def sandbox(self) -> SimpleNamespace:
        return SimpleNamespace(backend_name=self._backend_name)

    @property
    def settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            _set_surface=self.__set_surface,
            set_model_override=self._set_model_override,
            model_binding=SimpleNamespace(get_last_selected_model=self._get_last_selected_model),
        )


def _client(tmp_path, files: dict[str, bytes]) -> tuple[TestClient, str]:
    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))
    app = FastAPI()
    app.include_router(make_preview_edit_router(store, _Runtime(ps)))  # type: ignore[arg-type]
    client = TestClient(app)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    ws = ps.path_for(cid)
    ws.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return client, cid


def _finished_client(
    tmp_path,
    *,
    sealed: bool = True,
) -> tuple[TestClient, str, ProjectStore, int | None]:
    store = SqliteEventStore(":memory:")
    ps = ProjectStore(str(tmp_path))
    app = FastAPI()
    app.include_router(make_preview_edit_router(store, _Runtime(ps)))  # type: ignore[arg-type]
    client = TestClient(app)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    ws = ps.path_for(cid)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "index.html").write_text(_HTML)
    ps.write_manifest(
        cid,
        title="Finished edit preview",
        owner_id="local",
        created_at="2026-07-18T00:00:00+00:00",
        file_count=1,
        total_bytes=len(_HTML.encode()),
    )

    async def finish() -> int | None:
        terminal = await store.append(cid, StatusEvent(status=ConversationStatus.FINISHED))
        assert terminal.seq is not None
        if not sealed:
            return None
        version = ps.cut_verified_version(cid, trigger="finish", pin=True)
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
                    latest_effect_seq=None,
                    version_seq=version.seq,
                    tree_digest=version.tree_digest,
                    file_count=version.file_count,
                    total_bytes=version.total_bytes,
                ),
            ),
        )
        return version.seq

    return client, cid, ps, asyncio.run(finish())


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


def test_finished_edit_preview_reads_immutable_version_not_mutable_mirror(tmp_path) -> None:
    client, cid, ps, _version = _finished_client(tmp_path)
    (ps.path_for(cid) / "index.html").write_text("MUTATED MIRROR")

    response = client.get(f"/conversations/{cid}/preview-edit/index.html")

    assert response.status_code == 200
    assert "A built static page." in response.text
    assert "MUTATED MIRROR" not in response.text


def test_finished_unsealed_edit_preview_fails_closed(tmp_path) -> None:
    client, cid, _ps, _version = _finished_client(tmp_path, sealed=False)

    response = client.get(f"/conversations/{cid}/preview-edit/index.html")

    assert response.status_code == 503


def test_finished_corrupt_version_edit_preview_fails_closed(tmp_path) -> None:
    client, cid, ps, version = _finished_client(tmp_path)
    assert version is not None
    (ps.version_workspace_path(cid, version) / "index.html").write_text("CORRUPTED")

    response = client.get(f"/conversations/{cid}/preview-edit/index.html")

    assert response.status_code == 503


def test_no_runtime_404(tmp_path) -> None:
    store = SqliteEventStore(":memory:")
    app = FastAPI()
    app.include_router(make_preview_edit_router(store, runtime=None))
    client = TestClient(app)
    cid = f"conv_{uuid.uuid4().hex}"
    store.create_conversation(cid, owner_id="local")
    assert client.get(f"/conversations/{cid}/preview-edit/index.html").status_code == 404

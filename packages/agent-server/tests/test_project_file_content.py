"""Files inspector reads bytes from the same authority as project download."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest
from disco.agent_server import create_app
from disco.core import ConversationStatus, SqliteEventStore, StatusEvent
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient
from test_projects_endpoints import _runtime, _seal_finished, _zip_bytes


def _import(tmp_path):
    store = SqliteEventStore(":memory:")
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    body = client.post(
        "/api/projects/import",
        files={
            "file": (
                "app.zip",
                _zip_bytes(
                    {"index.html": b"<h1>Imported app</h1>", "image.png": b"\x89PNG\x00\xff"}
                ),
                "application/zip",
            ),
        },
    ).json()
    return store, runtime, client, body["conversation_id"]


def test_imported_file_content_and_binary_metadata(tmp_path):
    _, _, client, cid = _import(tmp_path)
    res = client.get(f"/api/projects/{cid}/files/index.html")
    assert res.status_code == 200
    assert res.json() == {
        "path": "index.html",
        "content": "<h1>Imported app</h1>",
        "binary": False,
        "bytes": 21,
        "version_seq": None,
    }
    assert res.headers["cache-control"] == "private, no-store"
    assert client.get(f"/api/projects/{cid}/files/image.png").json()["binary"] is True


def test_current_file_uses_sealed_restored_bytes_despite_stale_live_mirror(tmp_path):
    store, _, client, cid = _import(tmp_path)
    ps = ProjectStore(str(tmp_path))
    workspace = ps.path_for(cid)
    original = b"<h1>Imported app</h1>"
    original_version = ps.list_versions(cid)[0]
    (workspace / "index.html").write_bytes(b"<h1>New edited app</h1>")
    _seal_finished(store, ps, cid)
    assert (
        client.get(f"/api/projects/{cid}/files/index.html").json()["content"]
        == "<h1>New edited app</h1>"
    )
    # The restore coordinator publishes restored bytes then seals them. Reproduce
    # that durable boundary, retaining a stale mirror to catch wrong authority.
    asyncio.run(store.append(cid, StatusEvent(status=ConversationStatus.PAUSED)))
    with ps.open_verified_version(cid, original_version.seq) as verified:
        (workspace / "index.html").write_bytes(verified.read_bytes("index.html"))
    restored_version = _seal_finished(store, ps, cid)
    (workspace / "index.html").write_bytes(b"stale edited mirror")
    response = client.get(f"/api/projects/{cid}/files/index.html")
    assert response.status_code == 200
    assert response.json()["content"].encode() == original
    assert response.json()["version_seq"] == restored_version
    exported = client.get(f"/api/projects/{cid}/download")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("index.html") == response.json()["content"].encode()


def test_finished_without_valid_seal_never_falls_back_to_mirror(tmp_path):
    store, _, client, cid = _import(tmp_path)
    asyncio.run(store.append(cid, StatusEvent(status=ConversationStatus.FINISHED)))
    response = client.get(f"/api/projects/{cid}/files/index.html")
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "workspace_unsealed"


@pytest.mark.parametrize(
    "path", [".env", "missing.txt", "escape/secret.txt", "link.txt", "directory"]
)
def test_file_path_secrets_symlinks_and_nonfiles_are_not_served(tmp_path, path):
    _, _, client, cid = _import(tmp_path)
    workspace = ProjectStore(str(tmp_path)).path_for(cid)
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_bytes(b"private")
    (workspace / ".env").write_bytes(b"private env")
    (workspace / "escape").symlink_to(external, target_is_directory=True)
    (workspace / "link.txt").symlink_to(external / "secret.txt")
    (workspace / "directory").mkdir()
    assert client.get(f"/api/projects/{cid}/files/{path}").status_code == 404


def test_large_file_is_bounded_and_other_owner_is_denied(tmp_path):
    store, _, client, cid = _import(tmp_path)
    workspace = ProjectStore(str(tmp_path)).path_for(cid)
    (workspace / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    assert client.get(f"/api/projects/{cid}/files/large.txt").status_code == 413
    other = "conv_other_owner"
    store.create_conversation(other, owner_id="someone-else", surface="build")
    assert client.get(f"/api/projects/{other}/files/index.html").status_code == 404

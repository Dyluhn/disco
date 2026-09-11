"""Reference Packs: the store (atomic creation, bounds, owner isolation, digests)
and the owner-scoped routes (list/get/patch/files/delete)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from disco.agent_server.auth import AgentAuthMiddleware
from disco.agent_server.reference_pack_store import (
    MAX_FILE_BYTES,
    JsonReferencePackStore,
    ReferencePackError,
    classify_file,
    safe_filename,
)
from disco.agent_server.routes.reference_packs import make_reference_packs_router
from disco.core import SqliteEventStore
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _store(tmp_path: Path) -> JsonReferencePackStore:
    return JsonReferencePackStore(tmp_path / "projects")


# ---- store -------------------------------------------------------------------


def test_create_copies_exact_bytes_in_order_and_survives_reload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create(
        owner_id="local",
        name="  Brand   kit ",
        description="Logos and copy",
        files=[("b.md", b"# B"), ("a.txt", b"alpha"), ("logo.png", b"\x89PNG\r\n\x1a\n...")],
    )
    assert record.name == "Brand kit"
    assert [f.name for f in record.files] == ["b.md", "a.txt", "logo.png"]
    assert [f.state for f in record.files] == ["ready", "ready", "asset_only"]
    reloaded = JsonReferencePackStore(tmp_path / "projects").get(record.pack_id, "local")
    assert reloaded == record
    assert store.read_file(record.pack_id, "local", "a.txt") == b"alpha"


def test_create_is_atomic_when_a_file_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ReferencePackError):
        store.create(
            owner_id="local",
            name="too big",
            files=[("ok.txt", b"fine"), ("huge.bin", b"x" * (MAX_FILE_BYTES + 1))],
        )
    assert store.list("local") == []
    assert (
        not any(p.name.startswith(".staging") for p in store.root.iterdir())
        if store.root.is_dir()
        else True
    )


def test_names_are_sanitised_and_traversal_is_impossible(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create(owner_id="local", name="p", files=[("../../etc/passwd", b"root:x")])
    assert record.files[0].name == "passwd"
    assert (store.root / record.pack_id / "files" / "passwd").is_file()
    assert safe_filename("  ..hidden  file.txt ") == "hidden-file.txt"
    assert safe_filename("...") is None
    with pytest.raises(ReferencePackError):
        store.create(owner_id="local", name="p", files=[("manifest.json", b"{}")])


def test_owner_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    mine = store.create(owner_id="alice", name="mine", files=[("a.txt", b"a")])
    assert store.get(mine.pack_id, "bob") is None
    assert store.list("bob") == []
    assert store.read_file(mine.pack_id, "bob", "a.txt") is None
    assert store.update_meta(mine.pack_id, "bob", name="stolen") is None
    assert store.delete(mine.pack_id, "bob") is False
    assert store.get(mine.pack_id, "alice") == mine


def test_file_edits_move_the_digest_and_keep_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create(owner_id="local", name="p", files=[("a.txt", b"1"), ("b.txt", b"2")])
    replaced = store.put_file(record.pack_id, "local", "a.txt", b"one")
    assert replaced is not None and replaced.digest != record.digest
    assert [f.name for f in replaced.files] == ["a.txt", "b.txt"]
    added = store.put_file(record.pack_id, "local", "c.txt", b"3")
    assert added is not None and [f.name for f in added.files] == ["a.txt", "b.txt", "c.txt"]
    removed = store.remove_file(record.pack_id, "local", "b.txt")
    assert removed is not None and [f.name for f in removed.files] == ["a.txt", "c.txt"]
    assert not (store.root / record.pack_id / "files" / "b.txt").exists()
    with pytest.raises(ReferencePackError):
        store.remove_file(record.pack_id, "local", "a.txt")
        store.remove_file(record.pack_id, "local", "c.txt")
    assert store.delete(record.pack_id, "local") is True
    assert store.get(record.pack_id, "local") is None


def test_readability_states() -> None:
    assert classify_file("notes.md", b"# hi") == "ready"
    assert classify_file("data.csv", b"a,b\n1,2") == "ready"
    assert classify_file("photo.jpg", b"\xff\xd8\xff") == "asset_only"
    assert classify_file("blob.bin", b"\x00\x01") == "unreadable"
    assert classify_file("broken.txt", b"\xff\xfe\x00bad") == "unreadable"
    assert classify_file("scan.pdf", b"%PDF-1.4 no text objects") == "asset_only"


# ---- routes ------------------------------------------------------------------


class _FakeRuntime:
    def __init__(self, root: Path) -> None:
        self._project_store = ProjectStore(str(root))

    @property
    def projects(self) -> SimpleNamespace:
        return SimpleNamespace(current_project_store=lambda: self._project_store)


def _client(tmp_path: Path) -> tuple[TestClient, JsonReferencePackStore]:
    root = tmp_path / "projects"
    root.mkdir()
    store = SqliteEventStore(":memory:")
    app = FastAPI()
    app.add_middleware(AgentAuthMiddleware, store=store)
    app.include_router(make_reference_packs_router(_FakeRuntime(root)))  # type: ignore[arg-type]
    return TestClient(app), JsonReferencePackStore(root)


def test_routes_list_get_patch_files_delete(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    record = store.create(
        owner_id="local", name="Brand", description="d", files=[("a.txt", b"alpha")]
    )
    listed = client.get("/api/reference-packs").json()["packs"]
    assert [p["pack_id"] for p in listed] == [record.pack_id]
    assert listed[0]["file_count"] == 1 and listed[0]["total_bytes"] == 5

    detail = client.get(f"/api/reference-packs/{record.pack_id}").json()["pack"]
    assert detail["files"][0] == {
        "name": "a.txt",
        "media_type": "text/plain",
        "bytes": 5,
        "sha256": record.files[0].sha256,
        "state": "ready",
    }

    patched = client.patch(
        f"/api/reference-packs/{record.pack_id}", json={"name": "Brand v2"}
    ).json()["pack"]
    assert patched["name"] == "Brand v2" and patched["description"] == "d"
    assert (
        client.patch(f"/api/reference-packs/{record.pack_id}", json={"name": ""}).status_code == 400
    )

    with_files = client.post(
        f"/api/reference-packs/{record.pack_id}/files",
        files=[
            ("files", ("b.md", b"# B", "text/markdown")),
            ("files", ("a.txt", b"ALPHA", "text/plain")),
        ],
    ).json()["pack"]
    assert [f["name"] for f in with_files["files"]] == ["a.txt", "b.md"]
    assert with_files["digest"] != record.digest
    assert client.get(f"/api/reference-packs/{record.pack_id}/files/a.txt").content == b"ALPHA"

    removed = client.delete(f"/api/reference-packs/{record.pack_id}/files/b.md").json()["pack"]
    assert [f["name"] for f in removed["files"]] == ["a.txt"]
    assert client.get(f"/api/reference-packs/{record.pack_id}/files/b.md").status_code == 404

    assert client.delete(f"/api/reference-packs/{record.pack_id}").json()["deleted"] is True
    assert client.get(f"/api/reference-packs/{record.pack_id}").status_code == 404
    assert client.get("/api/reference-packs/rp_notarealid").status_code == 404
    assert client.get("/api/reference-packs/../etc").status_code == 404

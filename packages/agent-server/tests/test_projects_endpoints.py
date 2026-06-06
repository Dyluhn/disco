"""Agent-server projects endpoints — list / download / delete + the storage
browse picker. Headless: drives the ASGI app via Starlette's TestClient with a
runtime that has no real sandbox, so the endpoints' GRACEFUL-FAILURE branches
are exactly what the tests exercise."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from perpleximanus.agent_server import ConversationRuntime, create_app
from perpleximanus.core import SqliteEventStore
from perpleximanus.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from perpleximanus.tools.projects import ProjectStore


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


def _runtime(store: SqliteEventStore, *, root: str | None) -> ConversationRuntime:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg_store = ConfigStore(path=Path("/dev/null"))
    if root is not None:
        cfg = cfg.model_copy(
            update={"projects": ProjectStorageSettings(projects_root=root)}
        )
    # Save the config so _project_store_now reads it back from the in-memory copy.
    # We don't actually write to disk; the cfg_store is overridden below.
    cfg_store.load = lambda: cfg  # type: ignore[method-assign]
    return ConversationRuntime(store, config=cfg, config_store=cfg_store)


def test_list_projects_returns_unset_when_path_not_configured(store):
    runtime = _runtime(store, root=None)
    client = TestClient(create_app(store, runtime=runtime))
    res = client.get("/api/projects").json()
    assert res["projects"] == []
    assert res["status"] == "unset"


def test_list_projects_reports_not_found_for_missing_path(store):
    runtime = _runtime(store, root="/definitely/not/a/real/path/here")
    client = TestClient(create_app(store, runtime=runtime))
    res = client.get("/api/projects").json()
    assert res["projects"] == []
    assert res["status"] == "not_found"


def test_list_projects_joins_manifests_with_conversation_metadata(store, tmp_path):
    """A project on disk shows up in the list with title from the conversations
    table — the load-bearing "list-and-reopen" property mirrored from History."""
    runtime = _runtime(store, root=str(tmp_path))
    # write a project to disk: manifest + workspace tree
    ps = ProjectStore(str(tmp_path))
    cid = "conv_listme"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "index.html").write_bytes(b"<h1>hi</h1>")
    ps.write_manifest(
        cid,
        title="(manifest title — overridden by conversations table)",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=1,
        total_bytes=12,
    )
    # AND register the conversation row, so the join works
    store.create_conversation(cid, owner_id="local", title="Snake game prototype")

    client = TestClient(create_app(store, runtime=runtime))
    res = client.get("/api/projects").json()
    assert res["status"] == "ok"
    assert len(res["projects"]) == 1
    row = res["projects"][0]
    assert row["id"] == cid
    # title from the conversations table wins (authoritative for the row)
    assert row["title"] == "Snake game prototype"
    assert row["file_count"] == 1
    assert row["files_missing"] is False


def test_files_missing_flag_surfaces_when_workspace_deleted(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_orphan"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_bytes(b"x")
    ps.write_manifest(
        cid, title="o", owner_id="local",
        created_at="2026-06-06T00:00:00Z", file_count=1, total_bytes=1,
    )
    # nuke the workspace, leave the manifest
    import shutil
    shutil.rmtree(workspace)
    store.create_conversation(cid, owner_id="local", title="Orphan")

    client = TestClient(create_app(store, runtime=runtime))
    rows = client.get("/api/projects").json()["projects"]
    assert len(rows) == 1
    assert rows[0]["files_missing"] is True


def test_download_returns_zip_with_real_files(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_zip"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_bytes(b"alpha")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.txt").write_bytes(b"beta")
    ps.write_manifest(
        cid, title="z", owner_id="local",
        created_at="2026-06-06T00:00:00Z", file_count=2, total_bytes=9,
    )

    client = TestClient(create_app(store, runtime=runtime))
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert f'attachment; filename="{cid}.zip"' in res.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/b.txt"]
        assert zf.read("sub/b.txt") == b"beta"


def test_download_files_missing_returns_404_with_reason(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_gone"
    # manifest only — no workspace dir
    (tmp_path / cid).mkdir()
    ps.write_manifest(
        cid, title="g", owner_id="local",
        created_at="2026-06-06T00:00:00Z", file_count=0, total_bytes=0,
    )

    client = TestClient(create_app(store, runtime=runtime))
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "files_missing"


def test_delete_removes_project_from_disk(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_kill"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "x").write_bytes(b"x")
    ps.write_manifest(
        cid, title="k", owner_id="local",
        created_at="2026-06-06T00:00:00Z", file_count=1, total_bytes=1,
    )

    client = TestClient(create_app(store, runtime=runtime))
    res = client.delete(f"/api/projects/{cid}")
    assert res.status_code == 200
    assert res.json() == {"id": cid, "deleted": True}
    assert not (tmp_path / cid).exists()


def test_browse_storage_lists_directories(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    (tmp_path / "sub_a").mkdir()
    (tmp_path / "sub_b").mkdir()
    (tmp_path / "a_file.txt").write_text("x")

    client = TestClient(create_app(store, runtime=runtime))
    res = client.get(f"/api/storage/browse?path={tmp_path}")
    assert res.status_code == 200
    body = res.json()
    assert body["path"] == str(tmp_path)
    names = {e["name"]: e["is_dir"] for e in body["entries"]}
    assert names["sub_a"] is True
    assert names["sub_b"] is True
    assert names["a_file.txt"] is False
    # the picker tells the UI whether the *current* path is selectable
    assert body["selectable"] == "ok"


def test_browse_storage_not_found_returns_404(store):
    runtime = _runtime(store, root=None)
    client = TestClient(create_app(store, runtime=runtime))
    res = client.get("/api/storage/browse?path=/definitely/not/a/real/path")
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "not_found"


def test_surface_recovery_treats_project_with_manifest_as_build(store, tmp_path):
    """After a server restart, the in-memory _surface dict is empty. If a
    project manifest exists for the conversation, _surface_of must recover
    "build" so the loop composes Build (not Research with no tools) and the
    rehydrate / snapshot hooks fire. Found in live e2e — guard against
    regression."""
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_recover"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_bytes(b"x")
    ps.write_manifest(
        cid, title="r", owner_id="local",
        created_at="2026-06-06T00:00:00Z", file_count=1, total_bytes=1,
    )
    # set_surface was NEVER called on this runtime — exactly the post-restart shape.
    assert cid not in runtime._surface
    # the recovery method should return "build" because a manifest exists
    assert runtime._surface_of(cid) == "build"
    # and the lookup caches the recovery so subsequent calls don't restat
    assert runtime._surface[cid] == "build"


def test_surface_recovery_defaults_to_research_when_no_manifest(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    # no project on disk → defaults to research, doesn't pollute _surface
    assert runtime._surface_of("conv_unknown") == "research"
    assert "conv_unknown" not in runtime._surface

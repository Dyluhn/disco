"""Agent-server projects endpoints — list / download / delete + the storage
browse picker. Headless: drives the ASGI app via Starlette's TestClient with a
runtime that has no real sandbox, so the endpoints' GRACEFUL-FAILURE branches
are exactly what the tests exercise."""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.agent_server.routes import projects as projects_routes
from disco.core import EventSource, MessageEvent, SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient


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
        cfg = cfg.model_copy(update={"projects": ProjectStorageSettings(projects_root=root)})
    # Save the config so _project_store_now reads it back from the in-memory copy.
    # We don't actually write to disk; the cfg_store is overridden below.
    cfg_store.load = lambda: cfg  # type: ignore[method-assign]
    return ConversationRuntime(store, config=cfg, config_store=cfg_store)


def test_list_projects_ok_with_auto_default_when_path_not_configured(store, tmp_path, monkeypatch):
    # E4 zero-config: an unconfigured root auto-resolves to a writable default (here
    # isolated to a tmp data dir), so the list is OK + empty — not "unset".
    monkeypatch.setenv("DISCO_DATA_DIR", str(tmp_path))
    runtime = _runtime(store, root=None)
    client = TestClient(create_app(store, runtime=runtime))
    res = client.get("/api/projects").json()
    assert res["projects"] == []
    assert res["status"] == "ok"


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


def test_list_projects_carries_surface_for_resume_routing(store, tmp_path):
    """The row must carry `surface` so the Projects list resumes each project on the
    right surface (an "agent" project → /agent/:cid, not the build-framed /build/:cid).
    A build project (or a legacy row with no surface) defaults to "build"."""
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    for cid, surface in (("conv_agent", "agent"), ("conv_build", "build")):
        ws = ps.path_for(cid)
        ws.mkdir(parents=True)
        (ws / "f.txt").write_bytes(b"x")
        ps.write_manifest(
            cid,
            title=None,
            owner_id="local",
            created_at="2026-06-06T00:00:00Z",
            file_count=1,
            total_bytes=1,
        )
        store.create_conversation(cid, owner_id="local", surface=surface)

    client = TestClient(create_app(store, runtime=runtime))
    rows = {r["id"]: r for r in client.get("/api/projects").json()["projects"]}
    assert rows["conv_agent"]["surface"] == "agent"
    assert rows["conv_build"]["surface"] == "build"


def test_files_missing_flag_surfaces_when_workspace_deleted(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_orphan"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_bytes(b"x")
    ps.write_manifest(
        cid,
        title="o",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=1,
        total_bytes=1,
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
        cid,
        title="z",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=2,
        total_bytes=9,
    )

    client = TestClient(create_app(store, runtime=runtime))
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert f'attachment; filename="{cid}.zip"' in res.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert sorted(zf.namelist()) == ["a.txt", "sub/b.txt"]
        assert zf.read("sub/b.txt") == b"beta"


def test_download_and_manifest_exclude_legacy_runtime_secret_files(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_secret_hygiene"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "index.html").write_bytes(b"ok")
    (workspace / ".dev.vars").write_bytes(b"STRIPE_WEBHOOK_SECRET=whsec_real")
    (workspace / ".dev.vars.example").write_bytes(b"STRIPE_WEBHOOK_SECRET=replace-me")
    ps.write_manifest(
        cid,
        title="safe export",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=3,
        total_bytes=100,
    )
    store.create_conversation(cid, owner_id="local", title="safe export")
    client = TestClient(create_app(store, runtime=runtime))

    download = client.get(f"/api/projects/{cid}/download")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
        names = set(zf.namelist())
        # Secret hygiene (the point of this test): the runtime-secret file is
        # excluded; the template + source survive. (WO-7: a root index.html is a
        # static release candidate, so the download also carries the generated
        # self-host overlay — hence a membership check, not exact equality.)
        assert ".dev.vars" not in names
        assert {".dev.vars.example", "index.html"} <= names
        # The real secret VALUE never appears in ANY zip entry (incl. the overlay).
        for name in names:
            assert b"whsec_real" not in zf.read(name)

    manifest = client.get(f"/api/projects/{cid}/manifest")
    assert manifest.status_code == 200
    assert {item["path"] for item in manifest.json()["files"]} == {
        ".dev.vars.example",
        "index.html",
    }


def test_download_files_missing_returns_404_with_reason(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_gone"
    # manifest only — no workspace dir
    (tmp_path / cid).mkdir()
    ps.write_manifest(
        cid,
        title="g",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=0,
        total_bytes=0,
    )

    client = TestClient(create_app(store, runtime=runtime))
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "files_missing"


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_import_zip_creates_build_project_and_seed_message(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    payload = _zip_bytes(
        {
            "package.json": b'{"scripts":{"dev":"vite"}}',
            "src/App.tsx": b"export function App() { return null }",
        }
    )

    res = client.post(
        "/api/projects/import",
        files={"file": ("sample-app.zip", payload, "application/zip")},
    )

    assert res.status_code == 200
    body = res.json()
    cid = body["conversation_id"]
    assert body["files"] == 2
    assert body["bytes"] == len(b'{"scripts":{"dev":"vite"}}') + len(
        b"export function App() { return null }"
    )
    assert body["title"] == "sample-app"
    assert runtime._surface_of(cid) == "build"

    ps = ProjectStore(str(tmp_path))
    workspace = ps.path_for(cid)
    assert (workspace / "package.json").read_bytes() == b'{"scripts":{"dev":"vite"}}'
    assert (workspace / "src" / "App.tsx").read_bytes() == b"export function App() { return null }"
    record = ps.get(cid)
    assert record is not None
    assert record.file_count == 2
    assert record.files_missing is False

    events = asyncio.run(store.get_events(cid))
    env = [e for e in events if isinstance(e, MessageEvent) and e.source is EventSource.ENVIRONMENT]
    assert len(env) == 1
    assert "Imported 2 files from sample-app.zip" in env[0].message.content
    assert "largest: src/App.tsx" in env[0].message.content

    rows = {p["id"]: p for p in client.get("/api/projects").json()["projects"]}
    assert rows[cid]["surface"] == "build"
    assert rows[cid]["file_count"] == 2


def test_import_zip_rejects_zip_slip(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    payload = _zip_bytes({"../evil.txt": b"nope"})

    res = client.post(
        "/api/projects/import",
        files={"file": ("evil.zip", payload, "application/zip")},
    )

    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "zip_slip"
    assert client.get("/api/projects").json()["projects"] == []


@pytest.mark.parametrize("secret_path", [".env", ".env.local", "nested/.dev.vars.prod"])
def test_import_zip_rejects_runtime_secret_files(store, tmp_path, secret_path):
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    payload = _zip_bytes({"index.html": b"ok", secret_path: b"SECRET=real"})

    res = client.post(
        "/api/projects/import",
        files={"file": ("unsafe.zip", payload, "application/zip")},
    )

    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "runtime_secret_file"
    assert client.get("/api/projects").json()["projects"] == []


def test_import_zip_keeps_runtime_secret_template(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    payload = _zip_bytes({".dev.vars.example": b"SECRET=replace-me"})

    res = client.post(
        "/api/projects/import",
        files={"file": ("template.zip", payload, "application/zip")},
    )

    assert res.status_code == 200
    workspace = ProjectStore(str(tmp_path)).path_for(res.json()["conversation_id"])
    assert (workspace / ".dev.vars.example").read_bytes() == b"SECRET=replace-me"


def test_import_zip_enforces_file_count_cap(store, tmp_path, monkeypatch):
    monkeypatch.setattr(projects_routes, "_MAX_IMPORT_FILES", 1)
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    payload = _zip_bytes({"a.txt": b"a", "b.txt": b"b"})

    res = client.post(
        "/api/projects/import",
        files={"file": ("too-many.zip", payload, "application/zip")},
    )

    assert res.status_code == 413
    assert res.json()["detail"]["reason"] == "too_many_files"
    assert client.get("/api/projects").json()["projects"] == []


def test_import_zip_enforces_tree_size_cap(store, tmp_path, monkeypatch):
    monkeypatch.setattr(projects_routes, "_MAX_IMPORT_TREE_BYTES", 3)
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    payload = _zip_bytes({"a.txt": b"abcd"})

    res = client.post(
        "/api/projects/import",
        files={"file": ("too-big.zip", payload, "application/zip")},
    )

    assert res.status_code == 413
    assert res.json()["detail"]["reason"] == "tree_too_large"
    assert client.get("/api/projects").json()["projects"] == []


def test_import_local_path_requires_directory(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path / "projects"))
    client = TestClient(create_app(store, runtime=runtime))
    source_file = tmp_path / "not-a-dir.txt"
    source_file.write_text("x")

    res = client.post("/api/projects/import", json={"path": str(source_file)})

    assert res.status_code == 400
    assert res.json()["detail"]["reason"] == "path_not_directory"


def test_import_git_clones_shallow_and_skips_git_dir(store, tmp_path, monkeypatch):
    async def fake_clone(git_url: str, dest: Path) -> None:
        assert git_url == "https://example.com/acme/widgets.git"
        (dest / ".git").mkdir(parents=True)
        (dest / ".git" / "config").write_text("secretish")
        (dest / "README.md").write_text("# Widgets")

    monkeypatch.setattr(projects_routes, "_clone_git_url", fake_clone)
    runtime = _runtime(store, root=str(tmp_path / "projects"))
    client = TestClient(create_app(store, runtime=runtime))

    res = client.post(
        "/api/projects/import",
        json={"git_url": "https://example.com/acme/widgets.git"},
    )

    assert res.status_code == 200
    cid = res.json()["conversation_id"]
    workspace = ProjectStore(str(tmp_path / "projects")).path_for(cid)
    assert (workspace / "README.md").read_text() == "# Widgets"
    assert not (workspace / ".git").exists()
    events = asyncio.run(store.get_events(cid))
    env = [e for e in events if isinstance(e, MessageEvent) and e.source is EventSource.ENVIRONMENT]
    assert "https://example.com/acme/widgets.git" in env[0].message.content


def test_delete_removes_project_from_disk(store, tmp_path):
    runtime = _runtime(store, root=str(tmp_path))
    ps = ProjectStore(str(tmp_path))
    cid = "conv_kill"
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True)
    (workspace / "x").write_bytes(b"x")
    ps.write_manifest(
        cid,
        title="k",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=1,
        total_bytes=1,
    )

    client = TestClient(create_app(store, runtime=runtime))
    res = client.delete(f"/api/projects/{cid}")
    assert res.status_code == 200
    assert res.json() == {"id": cid, "deleted": True}
    assert not (tmp_path / cid).exists()


def test_browse_storage_lists_directories(store, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
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
    missing = Path.home() / "definitely-not-a-real-disco-browse-path"
    assert not missing.exists()
    res = client.get("/api/storage/browse", params={"path": str(missing)})
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "not_found"


def test_browse_storage_hides_and_refuses_escaping_symlink(store, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = _runtime(store, root=str(tmp_path))
    escape = tmp_path / "etc-link"
    escape.symlink_to("/etc", target_is_directory=True)

    client = TestClient(create_app(store, runtime=runtime))
    listing = client.get("/api/storage/browse", params={"path": str(tmp_path)})
    assert listing.status_code == 200, listing.text
    assert "etc-link" not in {entry["name"] for entry in listing.json()["entries"]}

    escaped = client.get("/api/storage/browse", params={"path": str(escape)})
    assert escaped.status_code == 403
    assert escaped.json()["detail"]["reason"] == "outside_allowed_roots"


def test_browse_storage_emits_allow_and_deny_audit_records(store, tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = _runtime(store, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    caplog.set_level("INFO", logger="disco.agent_server.routes.storage")

    assert client.get("/api/storage/browse", params={"path": str(tmp_path)}).status_code == 200
    assert client.get("/api/storage/browse", params={"path": "/etc"}).status_code == 403

    records = [
        record
        for record in caplog.records
        if getattr(record, "audit_event", None) == "storage_browse"
    ]
    assert [record.audit_decision for record in records[-2:]] == ["allowed", "denied"]
    assert [record.audit_reason for record in records[-2:]] == [
        "listed",
        "outside_allowed_roots",
    ]


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
        cid,
        title="r",
        owner_id="local",
        created_at="2026-06-06T00:00:00Z",
        file_count=1,
        total_bytes=1,
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


def test_surface_persists_across_restart_without_project_manifest(store, tmp_path, monkeypatch):
    """DC-05 re-run #7 (2026-06-11): with NO projects_root configured, the
    manifest-based recovery ladder can never derive "build" — so a server
    restart silently demoted a Build conversation to the research surface,
    composing _NoToolExecutor. The model's only offered tool became `finish`
    and every post-resume turn was a doomed finish→verify-probe loop. The
    surface is now PERSISTED to a sidecar (B0 model-override pattern) so the
    restart keeps it even with zero durable project state on disk."""
    monkeypatch.setenv("PMX_DB", str(tmp_path / "events.db"))
    cid = "conv_restart_survivor"
    first = _runtime(store, root=None)  # ← no projects root: the Phase-B shape
    first.set_surface(cid, "build")
    assert (tmp_path / "events.db.surfaces.json").exists()

    # A fresh runtime = a restarted server: in-memory dict starts from the sidecar.
    second = _runtime(store, root=None)
    assert second._surface_of(cid) == "build"


def test_surface_sidecar_drops_unknown_values(store, tmp_path, monkeypatch):
    """A hand-edited / future-versioned sidecar must not compose an invalid
    loop: unknown surface strings are treated as never-set (ladder applies)."""
    db = tmp_path / "events.db"
    monkeypatch.setenv("PMX_DB", str(db))
    (tmp_path / "events.db.surfaces.json").write_text(
        '{"conv_ok": "deep_research", "conv_bad": "warp_core"}'
    )
    runtime = _runtime(store, root=None)
    assert runtime._surface_of("conv_ok") == "deep_research"
    # the invalid entry fell back to the ladder → research default
    assert runtime._surface_of("conv_bad") == "research"

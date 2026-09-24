"""Storage browse unifies around the validated configured projects_root.

Settings could mark `/data/projects` Valid while Browse refused it with
`outside_allowed_roots` (the allowlist only knew home/mnt/media). A
Settings-valid root outside those prefixes must browse AND report selectable,
while invalid settings, parent escape, traversal, and symlinks stay blocked.
"""

from __future__ import annotations

from pathlib import Path

from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from fastapi.testclient import TestClient


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
    cfg_store.load = lambda: cfg  # type: ignore[method-assign]
    return ConversationRuntime(store, config=cfg, config_store=cfg_store)


def test_configured_root_outside_home_browses_and_is_selectable(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    configured = tmp_path / "data-projects"
    configured.mkdir()
    (configured / "sub").mkdir()
    (configured / "note.txt").write_text("x")

    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=_runtime(store, root=str(configured))))

    res = client.get("/api/storage/browse", params={"path": str(configured)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["path"] == str(configured.resolve())
    names = {e["name"]: e["is_dir"] for e in body["entries"]}
    assert names["sub"] is True
    assert names["note.txt"] is False
    assert body["selectable"] == "ok"  # Choose is enabled for the valid root

    child = client.get("/api/storage/browse", params={"path": str(configured / "sub")})
    assert child.status_code == 200, child.text


def test_configured_root_does_not_open_parents_or_etc(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    configured = tmp_path / "data-projects"
    configured.mkdir()

    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=_runtime(store, root=str(configured))))

    # The parent of the configured root is NOT browsable (no broadened access).
    parent = client.get("/api/storage/browse", params={"path": str(tmp_path)})
    assert parent.status_code == 403
    assert parent.json()["detail"]["reason"] == "outside_allowed_roots"
    # Traversal out of the root is resolved before the allow check.
    traversal = client.get("/api/storage/browse", params={"path": str(configured / "..")})
    assert traversal.status_code == 403
    assert traversal.json()["detail"]["reason"] == "outside_allowed_roots"
    # And /etc stays refused.
    assert client.get("/api/storage/browse", params={"path": "/etc"}).status_code == 403


def test_invalid_configured_root_broadens_nothing(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A path that can never validate: creating it would require descending
    # through a regular file, so `resolve_projects_root`'s mkdir fails and
    # the setting stays NOT_FOUND (a merely-missing path would be auto-created
    # and legitimately become browsable).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    missing = blocker / "child"

    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=_runtime(store, root=str(missing))))

    res = client.get("/api/storage/browse", params={"path": str(missing)})
    assert res.status_code == 403
    assert res.json()["detail"]["reason"] == "outside_allowed_roots"
    # An unrelated directory outside the static roots is still refused.
    other = tmp_path / "other"
    other.mkdir()
    res = client.get("/api/storage/browse", params={"path": str(other)})
    assert res.status_code == 403


def test_symlink_escape_inside_configured_root_stays_blocked(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    configured = tmp_path / "data-projects"
    configured.mkdir()
    (configured / "etc-link").symlink_to("/etc", target_is_directory=True)

    store = SqliteEventStore(":memory:")
    client = TestClient(create_app(store, runtime=_runtime(store, root=str(configured))))

    listing = client.get("/api/storage/browse", params={"path": str(configured)})
    assert listing.status_code == 200, listing.text
    assert "etc-link" not in {e["name"] for e in listing.json()["entries"]}
    escaped = client.get("/api/storage/browse", params={"path": str(configured / "etc-link")})
    assert escaped.status_code == 403
    assert escaped.json()["detail"]["reason"] == "outside_allowed_roots"

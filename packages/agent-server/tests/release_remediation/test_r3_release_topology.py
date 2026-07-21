"""R3 (G07) at the PUBLIC boundary — real GET /release + /download.

Drives the REAL FastAPI ``/release`` + ``/download`` routes through the ASGI app, a real
``ProjectStore`` on a real on-disk workspace, a real committed version cut, and the real
host-owned ``release-intent`` sidecar; the emitted ``compose.yaml`` / ``release.json``
bytes are parsed IN-PROCESS. No release/detect/emit/route function is mocked; the ONLY
``monkeypatch`` is ``ConfigStore.load`` (the config seam a settings PUT performs). The
boundary harness (``_client`` / ``_seed`` / ``_download_zip``) mirrors the frozen C7/G07
harness. OUTSIDE the frozen closeout dirs (no ``export_track1_closeout`` marker), so it
never perturbs the acceptance manifest.

Proves, end to end:
  * a filesystem-ROOT persistent path FAILS CLOSED — ``self_host:false`` /
    ``needs_review`` with the typed ``persistent_path_unbackable`` blocker and NO overlay;
  * a NON-root persistent path is a self-host CANDIDATE whose emitted compose mounts a
    volume at the file's REAL parent dir that BACKS it, and the parsed compose mount, the
    runtime DATABASE_URL, the release.json resource url + persistent_path all identify the
    SAME normalized location.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import COMPOSE_PATH, RELEASE_JSON_PATH
from disco.core.release.spec import (
    EnvScope,
    EnvVarDecl,
    LocalResourceProfile,
    ReleaseIntent,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def _store() -> Iterator[SqliteEventStore]:
    yield SqliteEventStore(":memory:")


def _app_and_store(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FastAPI, ProjectStore]:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg = cfg.model_copy(update={"projects": ProjectStorageSettings(projects_root=str(tmp_path))})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    monkeypatch.setattr(cfg_store, "load", lambda: cfg)
    runtime = ConversationRuntime(store, config=cfg, config_store=cfg_store)
    return create_app(store, runtime=runtime), ProjectStore(str(tmp_path))


def _client(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
    app, ps = _app_and_store(store, tmp_path, monkeypatch)
    return TestClient(app), ps


def _seed(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    *,
    files: dict[str, bytes],
    intent: ReleaseIntent,
    owner_id: str = "local",
) -> None:
    workspace = ps.path_for(cid)
    workspace.mkdir(parents=True, exist_ok=True)
    total = 0
    for rel, data in files.items():
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        total += len(data)
    ps.write_manifest(
        cid,
        title=cid,
        owner_id=owner_id,
        created_at="2026-06-06T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=False,
    )
    ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id=owner_id, title=cid, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1


def _download_zip(client: TestClient, cid: str) -> dict[str, bytes]:
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _mount_targets(compose: dict[str, object], sid: str) -> dict[str, str]:
    services = compose["services"]
    assert isinstance(services, dict)
    block = services[sid]
    assert isinstance(block, dict)
    vols = block.get("volumes", [])
    assert isinstance(vols, list)
    out: dict[str, str] = {}
    for entry in vols:
        assert isinstance(entry, str)
        name, sep, target = entry.partition(":")
        assert sep, entry
        out[target] = name
    return out


def _env_of(compose: dict[str, object], sid: str) -> dict[str, str]:
    services = compose["services"]
    assert isinstance(services, dict)
    block = services[sid]
    assert isinstance(block, dict)
    env = block.get("environment", {})
    assert isinstance(env, dict)
    return {str(k): str(v) for k, v in env.items()}


def _resource_intent(path: str) -> ReleaseIntent:
    return ReleaseIntent(
        start_cmd=("node", "server.js"),
        env=(EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime, binding="db"),),
        resources=(
            ResourceDecl(
                id="db",
                kind=ResourceKind.sqlite,
                persistent_path=path,
                profiles=ResourceProfiles(
                    local=LocalResourceProfile(url=f"file:{path}", volume="app-data")
                ),
                consumers=("web",),
            ),
        ),
    )


# ---- root persistent path fails closed ----------------------------------------


@pytest.mark.parametrize("path", ["/app.db", "/db.sqlite"])
def test_public_root_persistent_path_fails_closed(
    path: str, _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root-level persistent path is refused at ``/release``: ``self_host:false`` /
    ``needs_review`` with the typed ``persistent_path_unbackable`` blocker, no ingress, no
    ``spec_digest`` — and ``/download`` ships NO self-host overlay (no compose.yaml)."""
    cid = "conv_r3root"
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(
        ps,
        _store,
        cid,
        files={"package.json": b'{"name":"svc"}', "server.js": b"x\n"},
        intent=_resource_intent(path),
    )

    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["self_host"] is False
    assert body["assessment"] == "needs_review"
    assert body["ingress"] is None
    assert body["spec_digest"] is None
    codes = [b["code"] for b in body["blockers"]]
    assert codes == ["persistent_path_unbackable"], body["blockers"]

    # No self-host overlay is offered — the download is the plain source zip.
    names = set(_download_zip(client, cid))
    assert COMPOSE_PATH not in names and RELEASE_JSON_PATH not in names


# ---- non-root persistent path: backed + normalized-location agreement ---------


@pytest.mark.parametrize("path", ["/data/app.db", "/var/lib/x/db.sqlite"])
def test_public_nonroot_persistent_path_is_backed_and_agrees(
    path: str, _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-root persistent path is a self-host candidate; the emitted compose mounts a
    volume at the file's REAL parent dir that BACKS it, and the parsed compose mount, the
    runtime DATABASE_URL, and release.json's resource url + persistent_path all identify
    the SAME normalized location."""
    parent = path.rsplit("/", 1)[0]
    cid = "conv_r3ok"
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(
        ps,
        _store,
        cid,
        files={"package.json": b'{"name":"svc"}', "server.js": b"x\n"},
        intent=_resource_intent(path),
    )

    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["self_host"] is True and body["assessment"] == "candidate", body

    zip_files = _download_zip(client, cid)
    assert COMPOSE_PATH in zip_files and RELEASE_JSON_PATH in zip_files
    compose = yaml.safe_load(zip_files[COMPOSE_PATH])
    assert isinstance(compose, dict)

    # 1) the compose mount backs the file exactly (mount = the file's real parent dir).
    targets = _mount_targets(compose, "web")
    assert targets == {parent: "app-data"}, targets
    assert path == parent or path.startswith(parent + "/")

    # 2) the runtime env DATABASE_URL is the resource's file: url.
    assert _env_of(compose, "web").get("DATABASE_URL") == f"file:{path}"

    # 3) release.json's resource identifies the SAME normalized location.
    release = yaml.safe_load(zip_files[RELEASE_JSON_PATH])
    resource = release["resources"][0]
    assert resource["persistent_path"] == path
    assert resource["profiles"]["local"]["url"] == f"file:{path}"

    # 4) all four surfaces agree: url path == persistent_path == a file under the mount dir.
    url_path = resource["profiles"]["local"]["url"][len("file:") :]
    assert url_path == path == resource["persistent_path"]
    assert next(iter(targets)) == parent

"""R2 (crit "wire through … response, bundle") — where the new intent fields surface.

Confirms, at the REAL `/release` + `/download` boundary, exactly WHERE each new typed
intent field appears:

* the `/release` RESPONSE summary carries the ingress `runtime` strategy (the label
  R4's SelfHostPanel renders the outcome from) alongside the existing
  assessment/self_host/ingress/required_env/spec_digest;
* the emitted BUNDLE `release.json` (the authoritative neutral contract) carries the
  FULL build detail — `runtime`, `output_dir`, `package_manager`, `lockfile`, and the
  install/build argv (install provenance) — so nothing declared is silently dropped.

Outside the frozen closeout dirs (no `export_track1_closeout` marker).
"""

from __future__ import annotations

import io
import itertools
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import DOCKERFILE_PATH, RELEASE_JSON_PATH
from disco.core.release.spec import ReleaseIntent
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

_ID = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}_{next(_ID)}"


@pytest.fixture
def _store() -> Iterator[SqliteEventStore]:
    yield SqliteEventStore(":memory:")


def _client(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
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
    return TestClient(create_app(store, runtime=runtime)), ProjectStore(str(tmp_path))


def _seed(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    files: dict[str, bytes],
    intent: ReleaseIntent,
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
        owner_id="local",
        created_at="2026-07-14T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=False,
    )
    ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id="local", title=cid, surface="build")
    cut = ps.cut_version(cid, trigger="r2")
    assert cut is not None and cut.seq == 1


def _release_json_service(client: TestClient, cid: str) -> dict[str, object]:
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        spec = json.loads(zf.read(RELEASE_JSON_PATH).decode("utf-8"))
    services = spec["services"]
    assert isinstance(services, list) and len(services) == 1
    service = services[0]
    assert isinstance(service, dict)
    return service


_VITE = {
    "index.html": b"<!doctype html><div id=a></div><script type=module src=/src/m.js></script>",
    "package.json": (
        b'{"name":"spa","scripts":{"build":"vite build"},"devDependencies":{"vite":"^5"}}'
    ),
    "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
    "src/m.js": b"1;\n",
}


def test_static_runtime_surfaces_in_response_and_full_detail_in_release_json(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _uid("conv_r2surf")
    client, ps = _client(_store, tmp_path, monkeypatch)
    intent = ReleaseIntent(
        build_cmd=("npm", "run", "build"),
        output_dir="dist",
        package_manager="npm",
        health_path="/",
    )
    _seed(ps, _store, cid, dict(_VITE), intent)

    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    # RESPONSE: the ingress runtime strategy surfaces so R4 can label the outcome.
    assert body["assessment"] == "candidate" and body["self_host"] is True
    assert body["ingress"]["runtime"] == "static", body["ingress"]
    assert body["ingress"]["health_path"] == "/"

    # BUNDLE (release.json): the FULL declared build detail is conserved.
    service = _release_json_service(client, cid)
    assert service["runtime"] == "static"
    assert service["output_dir"] == "dist"
    assert service["package_manager"] == "npm"
    assert service["build_cmd"] == ["npm", "run", "build"]
    assert service["install_cmd"] == ["npm", "install"]  # derived install provenance


def test_node_runtime_and_explicit_install_surface_in_release_json(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _uid("conv_r2surfnode")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {
        "package.json": (
            b'{"name":"svc","dependencies":{"express":"1"},"scripts":{"start":"node a.js"}}'
        ),
        "a.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    }
    intent = ReleaseIntent(
        start_cmd=("node", "a.js"), install_cmd=("npm", "ci"), package_manager="npm"
    )
    _seed(ps, _store, cid, files, intent)

    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["ingress"]["runtime"] == "node"

    service = _release_json_service(client, cid)
    assert service["runtime"] == "node"
    assert service["package_manager"] == "npm"
    assert service["install_cmd"] == ["npm", "ci"]  # explicit install provenance conserved
    assert service["start_cmd"] == ["node", "a.js"]
    # The emitted Dockerfile actually runs the explicit install (end-to-end, not just JSON).
    res = client.get(f"/api/projects/{cid}/download")
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        dockerfile = zf.read(DOCKERFILE_PATH).decode("utf-8")
    assert '"npm", "ci"' in dockerfile

"""WO-10 — self-host bundle E2E: route-level proof (unit lane) + [LIVE] docker
lifecycle (integration lane, deferred).

Two lanes live in this one module:

* UNIT LANE (unmarked — runs in the required `-m "not integration"` job): drives
  the real `/api/projects/{id}/release` + `/download` routes over the ASGI app
  for FOUR fixture shapes — a free-form Build node/express app, an Agent-surface
  python fastapi app, an imported plain-node http server (with a lockfile), and an
  AppKit app GENERATED in-test via `disco.core.appkit.generate` (never a committed
  tree). It proves ONE identical response schema + ONE identical action set across
  all four (the no-mode-branching acceptance), that each candidate download carries
  the full self-host overlay, that the two negative shapes (unknown-stack /
  docs-only) fail closed yet stay downloadable as plain source, that a planted
  `.env`/`.dev.vars` secret never reaches a bundle or a `/release` body, and — as
  the non-LIVE proxy for the docker lane — that every emitted `compose.yaml` parses
  and has the expected structure.

* INTEGRATION LANE (`@pytest.mark.integration`, and `skipif` no docker): the [LIVE]
  bundle lifecycle on a real docker host — `docker compose config` → `up -d
  --build` → health 200 + meaningful body → DB write persisted across `restart` →
  `down`, plus migration idempotence. DEFERRED: this host has no compose provider,
  so these SKIP cleanly (an honest deferral, never a fake pass).

Every fixture workspace is materialized in a `tmp_path` at test time; the committed
fixtures under `tests/fixtures/release_e2e/` are git-tracked source ONLY (no `.env`,
no `*.db`). The planted secret lives solely in a tmp-written file, never the repo.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
import yaml
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.appkit import (
    APPSPEC_RELPATH,
    default_records_app_spec,
    generate,
    get_recipe,
    serialize_app_spec,
)
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

# The four fixture shapes. `appkit` is generated in-test; the other three are
# committed source trees under tests/fixtures/release_e2e/.
_FIXTURES = ("build_express", "agent_fastapi", "imported_node", "appkit")

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "release_e2e"

# The stable `/release` response key set — identical for every assessment.
_RESPONSE_KEYS = {
    "assessment",
    "reasons",
    "blockers",
    "required_env",
    "command",
    "ingress",
    "self_host",
    "spec_digest",
    "version_seq",
    "tree_digest",
}

# The self-host overlay the candidate download injects (the "action set").
_OVERLAY_NAMES = {
    "compose.yaml",
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    "SELFHOST.md",
    "release.json",
}

# One representative source file per fixture, asserted byte-preserved in the zip.
_SOURCE_SAMPLE = {
    "build_express": "server.js",
    "agent_fastapi": "main.py",
    "imported_node": "index.js",
    "appkit": "worker/index.ts",
}

_RELEASE_COMMAND = "docker compose up -d --build"

# A distinctive planted secret. Written ONLY into a tmp workspace at test time.
_SENTINEL = b"PLANTED-WO10-a1b2c3d4-SECRET"

# Negative shapes (built inline in tmp — no committed Dockerfile/opaque tree).
_OPAQUE_FILES = {
    "Dockerfile": b"FROM scratch\n",
    "main.go": b"package main\nfunc main() {}\n",
}
_DOCS_FILES = {
    "README.md": b"# Research Notes\n\nNo application here \xe2\x80\x94 a document workspace.\n",
    "LICENSE": b"MIT\n",
}


# ---- fixture materialization --------------------------------------------------


def _cid(name: str) -> str:
    return f"conv_{name}"


def _read_committed_fixture(name: str) -> dict[str, bytes]:
    """Read a committed fixture workspace into a `{relpath: bytes}` map."""
    root = _FIXTURES_DIR / name
    if not root.is_dir():
        raise AssertionError(f"missing committed fixture workspace: {root}")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _appkit_records_app():  # AppSpec type is internal detail here
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return default_records_app_spec("Acme Records", recipe), recipe


def _appkit_fixture() -> dict[str, bytes]:
    """Generate an AppKit records app in-test and add the `.disco/appspec.json`
    contract file (generation saves it separately), so the tree carries the full
    four-file AppKit shape the detector keys off."""
    app, recipe = _appkit_records_app()
    tree = generate(app, recipe.to_design_spec())
    tree[APPSPEC_RELPATH] = serialize_app_spec(app)
    return {rel: content.encode("utf-8") for rel, content in tree.items()}


def _fixture_files(name: str) -> dict[str, bytes]:
    if name == "appkit":
        return _appkit_fixture()
    return _read_committed_fixture(name)


# ---- ASGI harness (mirrors the WO-7 route-test harness) -----------------------


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


def _runtime(
    store: SqliteEventStore, monkeypatch: pytest.MonkeyPatch, *, root: str
) -> ConversationRuntime:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg = cfg.model_copy(update={"projects": ProjectStorageSettings(projects_root=root)})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    monkeypatch.setattr(cfg_store, "load", lambda: cfg)
    return ConversationRuntime(store, config=cfg, config_store=cfg_store)


def _client_for(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
    runtime = _runtime(store, monkeypatch, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    return client, ProjectStore(str(tmp_path))


def _seed_files(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    files: dict[str, bytes],
    *,
    owner_id: str = "local",
    imported: bool = False,
) -> Path:
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
        title=None,
        owner_id=owner_id,
        created_at="2026-07-12T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=imported,
    )
    store.create_conversation(cid, owner_id=owner_id, title=None, surface="build")
    return workspace


def _namelist(content: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return sorted(zf.namelist())


# ---- criterion 2: one schema + one action set across all four candidates ------


def test_release_schema_and_action_set_identical_across_four_fixtures(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    for name in _FIXTURES:
        _seed_files(ps, store, _cid(name), _fixture_files(name))
        # WO-C2: a self-host candidate must bind to a committed version.
        ps.cut_version(_cid(name), trigger="test")

    bodies: dict[str, dict[str, object]] = {}
    for name in _FIXTURES:
        res = client.get(f"/api/projects/{_cid(name)}/release")
        assert res.status_code == 200, (name, res.text)
        body = res.json()
        assert set(body.keys()) == _RESPONSE_KEYS, name
        assert body["assessment"] == "candidate", name
        assert body["self_host"] is True, name
        assert body["command"] == _RELEASE_COMMAND, name
        assert body["spec_digest"] and body["spec_digest"].startswith("sha256:"), name
        bodies[name] = body

    # ONE identical response schema across all four assessments.
    assert {frozenset(b.keys()) for b in bodies.values()} == {frozenset(_RESPONSE_KEYS)}

    # ONE identical action set: same command + same downloadable overlay set for
    # every candidate, whatever surface it came from (Build / Agent / import /
    # AppKit). This is the no-mode-branching acceptance.
    assert {b["command"] for b in bodies.values()} == {_RELEASE_COMMAND}
    overlay_sets: dict[str, frozenset[str]] = {}
    for name in _FIXTURES:
        res = client.get(f"/api/projects/{_cid(name)}/download")
        assert res.status_code == 200, name
        overlay_sets[name] = frozenset(set(_namelist(res.content)) & _OVERLAY_NAMES)
    assert set(overlay_sets.values()) == {frozenset(_OVERLAY_NAMES)}, overlay_sets


def test_download_bundle_carries_source_plus_full_overlay(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    for name in _FIXTURES:
        files = _fixture_files(name)
        _seed_files(ps, store, _cid(name), files)
        ps.cut_version(_cid(name), trigger="test")  # WO-C2: bind the candidate to a version
        res = client.get(f"/api/projects/{_cid(name)}/download")
        assert res.status_code == 200, name
        assert res.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
            names = set(zf.namelist())
            assert _OVERLAY_NAMES <= names, (name, sorted(names))
            # the source bytes are preserved verbatim (overlay is additive)
            sample = _SOURCE_SAMPLE[name]
            assert zf.read(sample) == files[sample], name
            # the overlay is real generated content, not empty stubs
            assert b"docker compose up -d --build" in zf.read("SELFHOST.md"), name
            spec = json.loads(zf.read("release.json"))
            assert spec["tree_digest"] and spec["services"], name
            # release.json carries no secret material (names-only contract)
            assert _SENTINEL not in zf.read("release.json"), name


# ---- criterion 3: negative shapes fail closed yet stay plain-downloadable ------


def test_unknown_stack_without_intent_is_needs_review_and_plain_downloadable(
    store, tmp_path, monkeypatch
):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed_files(ps, store, "conv_opaque", dict(_OPAQUE_FILES))  # no typed intent

    body = client.get("/api/projects/conv_opaque/release").json()
    assert body["assessment"] == "needs_review"
    assert body["self_host"] is False
    assert body["ingress"] is None
    assert body["spec_digest"] is None
    # a repairable diagnostic naming the exact unverifiable contract fields
    named = {b["field"] for b in body["blockers"] if b["field"]}
    assert {"start_cmd", "port_env", "health_path", "required_env"} <= named

    # still downloadable — byte-for-byte the plain source (NO overlay added; the
    # user's own Dockerfile is source, not a generated affordance).
    dl = client.get("/api/projects/conv_opaque/download")
    assert dl.status_code == 200
    assert set(_namelist(dl.content)) == set(_OPAQUE_FILES)


# A genuinely imported, non-container, UNRECOGNIZED workspace: no server
# package.json, no python web framework, no index.html, and — crucially, unlike the
# `_OPAQUE_FILES` Dockerfile fixture above — NO container manifest. Its needs_review
# verdict can ONLY come from the imported provenance rung (WO-3 rung 4), so it
# exercises the real imported-unknown path the Dockerfile fixture bypassed (#8a).
_IMPORTED_UNKNOWN_FILES = {
    "README.md": b"# imported service\n\nbrought in from an external repo.\n",
    "Makefile": b"build:\n\tgo build ./...\n",
    "cmd/app/main.go": b"package main\n\nfunc main() {}\n",
}


def test_imported_unknown_stack_without_intent_is_needs_review_and_plain_downloadable(
    store, tmp_path, monkeypatch
):
    """WO-10 #8a: drive the REAL imported-unknown path (no container manifest). A
    genuinely imported project with no typed intent and no recognizable stack must
    fail closed to needs_review with the full repairable diagnostic, and stay plain-
    downloadable."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed_files(ps, store, "conv_imported", dict(_IMPORTED_UNKNOWN_FILES), imported=True)

    body = client.get("/api/projects/conv_imported/release").json()
    assert set(body.keys()) == _RESPONSE_KEYS
    assert body["assessment"] == "needs_review"
    assert body["self_host"] is False
    assert body["ingress"] is None
    assert body["spec_digest"] is None
    # the imported rung names build_cmd too (the container-review rung does NOT), so
    # this asserts the genuine imported path, not the Dockerfile-driven one.
    named = {b["field"] for b in body["blockers"] if b["field"]}
    assert {"build_cmd", "start_cmd", "port_env", "health_path", "required_env"} <= named

    # still downloadable — byte-for-byte the plain source (no overlay affordance).
    dl = client.get("/api/projects/conv_imported/download")
    assert dl.status_code == 200
    assert set(_namelist(dl.content)) == set(_IMPORTED_UNKNOWN_FILES)


def test_imported_project_with_recognized_stack_stays_candidate(store, tmp_path, monkeypatch):
    """The imported bit must not OVER-fire: an imported project whose stack IS
    recognized still assesses candidate — imported only rescues the UNKNOWN case."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed_files(ps, store, "conv_imported_node", _fixture_files("imported_node"), imported=True)
    ps.cut_version("conv_imported_node", trigger="test")  # WO-C2: bind to a committed version

    body = client.get("/api/projects/conv_imported_node/release").json()
    assert body["assessment"] == "candidate"
    assert body["self_host"] is True


def test_docs_only_is_not_web_with_honest_reason_and_plain_downloadable(
    store, tmp_path, monkeypatch
):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed_files(ps, store, "conv_docs", dict(_DOCS_FILES))

    body = client.get("/api/projects/conv_docs/release").json()
    assert body["assessment"] == "not_web"
    assert body["self_host"] is False
    assert body["ingress"] is None
    assert body["spec_digest"] is None
    assert any("no HTTP entrypoint" in r for r in body["reasons"])

    dl = client.get("/api/projects/conv_docs/download")
    assert dl.status_code == 200
    # exactly the workspace file-set — no overlay path added.
    assert _namelist(dl.content) == ["LICENSE", "README.md"]


# ---- criterion 4: secret sweep across all four bundles + all four bodies -------


def test_secret_sweep_finds_no_planted_sentinel_in_bundles_or_release_bodies(
    store, tmp_path, monkeypatch
):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    for name in _FIXTURES:
        cid = _cid(name)
        workspace = _seed_files(ps, store, cid, _fixture_files(name))
        # Plant real secret-bearing files DIRECTLY in the tmp workspace (never
        # committed). is_runtime_secret_path must keep them out of every input.
        (workspace / ".env").write_bytes(
            b"DATABASE_URL=postgres://u:" + _SENTINEL + b"@h/db\nSECRET=" + _SENTINEL + b"\n"
        )
        (workspace / ".dev.vars").write_bytes(b"ADMIN_TOKEN=" + _SENTINEL + b"\n")

    for name in _FIXTURES:
        cid = _cid(name)
        rel = client.get(f"/api/projects/{cid}/release")
        assert rel.status_code == 200, name
        assert _SENTINEL not in rel.content, f"{name}: sentinel leaked into /release body"

        dl = client.get(f"/api/projects/{cid}/download")
        assert dl.status_code == 200, name
        assert _SENTINEL not in dl.content, f"{name}: sentinel in raw zip bytes"
        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            entries = set(zf.namelist())
            assert ".env" not in entries and ".dev.vars" not in entries, name
            for entry in entries:
                assert _SENTINEL not in zf.read(entry), f"{name}:{entry}"


# ---- criterion 5 (unit proxy): every emitted compose.yaml parses + is well-formed


def test_emitted_compose_parses_and_has_ingress_structure_for_each_fixture(
    store, tmp_path, monkeypatch
):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    for name in _FIXTURES:
        _seed_files(ps, store, _cid(name), _fixture_files(name))
        ps.cut_version(_cid(name), trigger="test")  # WO-C2: bind the candidate to a version

    for name in _FIXTURES:
        dl = client.get(f"/api/projects/{_cid(name)}/download")
        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            doc = yaml.safe_load(zf.read("compose.yaml"))
        assert isinstance(doc, dict) and isinstance(doc.get("services"), dict), name
        services = doc["services"]
        assert services, name
        # exactly ONE ingress-shaped service: it publishes a loopback host port and
        # carries a healthcheck; long-running services restart unless-stopped.
        ingress = [s for s in services.values() if isinstance(s, dict) and "ports" in s]
        assert len(ingress) == 1, (name, sorted(services))
        web = ingress[0]
        assert web.get("restart") == "unless-stopped", name
        assert "healthcheck" in web, name
        assert "build" in web, name
        assert all(str(p).startswith("127.0.0.1:") for p in web["ports"]), name

    # The AppKit fixture additionally proves the migration wiring the [LIVE]
    # idempotence test exercises: a one-shot init service (restart "no") the app
    # waits on, plus a persistent named volume.
    dl = client.get(f"/api/projects/{_cid('appkit')}/download")
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        doc = yaml.safe_load(zf.read("compose.yaml"))
    services = doc["services"]
    oneshot = [sid for sid, s in services.items() if isinstance(s, dict) and s.get("restart") == "no"]
    assert oneshot, "AppKit compose must declare a one-shot init/migrate service"
    app_service = next(s for s in services.values() if isinstance(s, dict) and "ports" in s)
    assert "depends_on" in app_service, "AppKit app must wait on init via depends_on"
    assert doc.get("volumes"), "AppKit compose must declare a persistent named volume"


# ============================================================================
# INTEGRATION LANE — [LIVE] docker compose lifecycle (DEFERRED: no compose
# provider on this host; these SKIP cleanly, they are never faked to pass).
# ============================================================================


def _docker_compose_available() -> bool:
    """True only if a `docker compose` provider actually answers here."""
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


_DOCKER_COMPOSE_AVAILABLE = _docker_compose_available()
_REQUIRES_DOCKER = pytest.mark.skipif(
    not _DOCKER_COMPOSE_AVAILABLE,
    reason="[LIVE] deferred: no docker compose provider on this host",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _materialize_bundle(
    client: TestClient, ps: ProjectStore, store: SqliteEventStore, name: str, dest: Path
) -> Path:
    """Seed the fixture, download its real self-host zip, and unzip it — the
    single artifact a user would run `docker compose up` inside."""
    _seed_files(ps, store, _cid(name), _fixture_files(name))
    res = client.get(f"/api/projects/{_cid(name)}/download")
    assert res.status_code == 200
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        zf.extractall(dest)
    return dest


def _compose_env(host_port: int) -> dict[str, str]:
    env = dict(os.environ)
    env["HOST_PORT"] = str(host_port)
    # AppKit's fail-closed admin gate requires a secret; the others ignore it.
    env["ADMIN_TOKEN"] = "live-e2e-admin-token"
    return env


def _compose(bundle: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=bundle,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _await_http_200(port: int, path: str = "/", timeout_s: float = 180.0) -> bytes:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # fixed http://127.0.0.1 target
                f"http://127.0.0.1:{port}{path}", timeout=5
            ) as resp:
                if resp.status == 200:
                    return resp.read()
        except (urllib.error.URLError, OSError) as exc:  # not up yet
            last = str(exc)
        time.sleep(2)
    raise AssertionError(f"health never reached 200 at :{port}{path} ({last})")


@pytest.mark.integration
@_REQUIRES_DOCKER
@pytest.mark.parametrize("name", _FIXTURES)
def test_live_bundle_boots_serves_and_survives_restart(name, store, tmp_path, monkeypatch):
    """[LIVE] For each fixture: unzip → `docker compose config` (exit 0) → `up -d
    --build` → health 200 + meaningful body → `restart` → still healthy → `down`.
    One command per step; no host-specific paths (the bundle runs from its dir)."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    bundle = _materialize_bundle(client, ps, store, name, tmp_path / f"bundle-{name}")
    port = _free_port()
    env = _compose_env(port)
    try:
        cfg = _compose(bundle, "config", env=env)
        assert cfg.returncode == 0, cfg.stderr
        up = _compose(bundle, "up", "-d", "--build", env=env)
        assert up.returncode == 0, up.stderr

        body = _await_http_200(port)
        assert body and len(body.strip()) > 0, "health body must be meaningful"

        if name == "appkit":
            _assert_db_write_persists(bundle, port, env)

        restart = _compose(bundle, "restart", env=env)
        assert restart.returncode == 0, restart.stderr
        assert _await_http_200(port), "must be healthy again after restart"
    finally:
        _compose(bundle, "down", "-v", env=env)


def _assert_db_write_persists(bundle: Path, port: int, env: dict[str, str]) -> None:
    """[LIVE] AppKit only: POST a record, `restart`, then read it back through the
    admin-gated GET — proving the write survived on the persistent volume."""
    appspec = json.loads((bundle / APPSPEC_RELPATH).read_text(encoding="utf-8"))
    entity = appspec["entities"][0]
    route = f"/api/{entity['id']}"
    marker = "wo10-live-marker@example.test"
    payload = _record_payload(entity, marker)

    post = urllib.request.Request(  # fixed loopback target
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(post, timeout=10) as resp:
        assert resp.status < 400, f"record POST failed: {resp.status}"

    assert _compose(bundle, "restart", env=env).returncode == 0
    _await_http_200(port)

    get = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        headers={"Authorization": f"Bearer {env['ADMIN_TOKEN']}"},
        method="GET",
    )
    with urllib.request.urlopen(get, timeout=10) as resp:
        assert resp.status == 200
        assert marker.encode() in resp.read(), "write did not persist across restart"


def _record_payload(entity: dict[str, object], marker: str) -> dict[str, object]:
    """A minimal valid record for an AppKit entity's required fields, typed by
    each field's declared type."""
    payload: dict[str, object] = {}
    fields = entity.get("fields", [])
    assert isinstance(fields, list)
    for field in fields:
        if not (isinstance(field, dict) and field.get("required")):
            continue
        ftype = field.get("type")
        name = str(field.get("name"))
        if ftype == "email":
            payload[name] = marker
        elif ftype in ("int", "float"):
            payload[name] = 1
        elif ftype == "datetime":
            payload[name] = "2026-07-12T00:00:00Z"
        else:
            payload[name] = marker
    return payload


@pytest.mark.integration
@_REQUIRES_DOCKER
def test_live_migration_is_idempotent(store, tmp_path, monkeypatch):
    """[LIVE] AppKit: applying the schema twice is idempotent — `up` then `down`
    (keeping the volume) then `up` again lands healthy the second time too."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    bundle = _materialize_bundle(client, ps, store, "appkit", tmp_path / "bundle-migrate")
    port = _free_port()
    env = _compose_env(port)
    try:
        assert _compose(bundle, "up", "-d", "--build", env=env).returncode == 0
        assert _await_http_200(port)
        # keep the named volume so the second init re-applies schema over existing
        # state (the idempotence path).
        assert _compose(bundle, "down", env=env).returncode == 0
        second = _compose(bundle, "up", "-d", "--build", env=env)
        assert second.returncode == 0, second.stderr
        assert _await_http_200(port), "second init must land healthy (idempotent schema)"
    finally:
        _compose(bundle, "down", "-v", env=env)

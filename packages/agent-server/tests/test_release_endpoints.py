"""WO-7 — agent-server release API + upgraded Download source.

Headless tests over the ASGI app (Starlette TestClient) with a runtime that has
no real sandbox. Every fixture workspace is built in a `tmp_path` at test time —
NO committed `.env`/`*.db` on disk — so the tree is clean and the planted secret
lives only in memory→tmp, never in the repo.

Proves each WO-7 acceptance criterion:
1. `/release` on four shapes (node candidate / opaque-stack needs_review / docs-only
   not_web / AppKit candidate) returns the SAME response schema (identical key set).
2. a candidate download carries the self-host overlay; a not_web download is
   byte-equivalent in file-set to the plain filtered zip.
3. a planted `.env`/`.dev.vars` sentinel never reaches the zip or the `/release` JSON.
4. auth mirrors `download_project` (403 project_forbidden / 404 project_not_found /
   404 storage_unavailable).
5. no mode-branching (a `surface` grep over the module is empty).
6. `/release` is idempotent and spawns NO subprocess.
7. (doc grep lives in the api-endpoints.md check below.)
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.spec import (
    LocalResourceProfile,
    ReleaseIntent,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
)
from disco.tools.projects import ProjectStore
from disco.tools.projects.store import tree_digest as compute_tree_digest
from fastapi.testclient import TestClient

_SENTINEL = b"PLANTED-731-hunter2"

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

# ---- fixture workspaces (built in-memory / tmp — never committed) --------------

_APPKIT_FILES: dict[str, bytes] = {
    ".disco/appspec.json": b'{"name":"appkit-todo","version":1,"entities":[]}',
    "wrangler.toml": (
        b'name = "appkit-todo"\nmain = "worker/index.ts"\n'
        b'compatibility_date = "2024-05-01"\n\n[[d1_databases]]\n'
        b'binding = "DB"\ndatabase_name = "appkit-todo"\n'
        b'database_id = "00000000-0000-0000-0000-000000000000"\n'
    ),
    "worker/index.ts": b"export default { async fetch() { return new Response('ok'); } };\n",
    "schema.sql": b"create table if not exists todo (id integer primary key);\n",
    "package.json": b'{"name":"appkit-todo","scripts":{"dev":"wrangler dev"}}',
    ".dev.vars.example": b"",
}

_NODE_FILES: dict[str, bytes] = {
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
}

_DOCS_FILES: dict[str, bytes] = {
    "README.md": b"# Research Notes\n\nNo application here \xe2\x80\x94 a document workspace.\n",
    "LICENSE": b"MIT\n",
    "notes/todo.md": b"- read more\n",
}

# An opaque container manifest we cannot statically verify -> needs_review with
# repairable, field-naming blockers (the honest "unknown/opaque stack" path).
_OPAQUE_FILES: dict[str, bytes] = {
    "Dockerfile": b"FROM scratch\n",
    "main.go": b"package main\nfunc main() {}\n",
}

_NODE_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"),
    required_env=("API_BASE_URL", "SESSION_SECRET"),
)


# ---- harness ------------------------------------------------------------------


@pytest.fixture
def store() -> SqliteEventStore:
    return SqliteEventStore(":memory:")


def _runtime(
    store: SqliteEventStore, monkeypatch: pytest.MonkeyPatch, *, root: str | None
) -> ConversationRuntime:
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg_store = ConfigStore(path=Path("/dev/null"))
    if root is not None:
        cfg = cfg.model_copy(update={"projects": ProjectStorageSettings(projects_root=root)})
    monkeypatch.setattr(cfg_store, "load", lambda: cfg)
    return ConversationRuntime(store, config=cfg, config_store=cfg_store)


def _seed(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    files: dict[str, bytes],
    *,
    owner_id: str = "local",
    title: str | None = None,
    intent: ReleaseIntent | None = None,
    imported: bool = False,
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
        title=title,
        owner_id=owner_id,
        created_at="2026-06-06T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=imported,
    )
    if intent is not None:
        ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id=owner_id, title=title, surface="build")


def _client_for(
    store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
    runtime = _runtime(store, monkeypatch, root=str(tmp_path))
    client = TestClient(create_app(store, runtime=runtime))
    return client, ProjectStore(str(tmp_path))


def _namelist(content: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return sorted(zf.namelist())


# ---- criterion 1: identical response schema across all four assessments -------


def test_release_schema_identical_key_set_across_assessments(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_node", _NODE_FILES, intent=_NODE_INTENT)
    _seed(ps, store, "conv_opaque", _OPAQUE_FILES)
    _seed(ps, store, "conv_docs", _DOCS_FILES)
    _seed(ps, store, "conv_appkit", _APPKIT_FILES)
    # WO-C2: only a committed version that matches the live tree can be a self-host
    # candidate; commit the two candidate shapes so they bind to a real source.
    ps.cut_version("conv_node", trigger="test")
    ps.cut_version("conv_appkit", trigger="test")

    bodies = {}
    for cid in ("conv_node", "conv_opaque", "conv_docs", "conv_appkit"):
        res = client.get(f"/api/projects/{cid}/release")
        assert res.status_code == 200, res.text
        body = res.json()
        assert set(body.keys()) == _RESPONSE_KEYS, cid
        assert body["command"] == "docker compose up -d --build"
        bodies[cid] = body

    # (a) conventional node app
    node = bodies["conv_node"]
    assert node["assessment"] == "candidate"
    assert node["self_host"] is True
    assert {e["name"] for e in node["required_env"]} == {"API_BASE_URL", "SESSION_SECRET"}
    assert node["ingress"] == {
        "service": "web",
        "runtime": "node",
        "port": "PORT",
        "health_path": None,
    }
    assert node["spec_digest"] and node["spec_digest"].startswith("sha256:")

    # (b) opaque/unknown stack -> needs_review with repairable field-naming blockers
    opaque = bodies["conv_opaque"]
    assert opaque["assessment"] == "needs_review"
    assert opaque["self_host"] is False
    assert opaque["ingress"] is None
    named_fields = {b["field"] for b in opaque["blockers"] if b["field"]}
    assert {"start_cmd", "port_env", "health_path", "required_env"} <= named_fields

    # (c) docs-only -> not_web with an honest reason
    docs = bodies["conv_docs"]
    assert docs["assessment"] == "not_web"
    assert docs["self_host"] is False
    assert docs["ingress"] is None
    assert docs["spec_digest"] is None
    assert any("no HTTP entrypoint" in r for r in docs["reasons"])

    # (d) AppKit-shaped -> candidate with strategy evidence
    appkit = bodies["conv_appkit"]
    assert appkit["assessment"] == "candidate"
    assert appkit["self_host"] is True
    joined = " ".join(appkit["reasons"])
    assert "dev_server" in joined and "workerd" in joined
    assert any(e["name"] == "ADMIN_TOKEN" and e["secret"] for e in appkit["required_env"])


# ---- criterion 2: candidate download carries overlay; not_web is plain ---------


def test_download_candidate_zip_contains_source_plus_overlay(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_node", _NODE_FILES, intent=_NODE_INTENT)
    ps.cut_version("conv_node", trigger="test")

    # WO-C2: the self-host bundle is a BOUND download of the committed version.
    body = client.get("/api/projects/conv_node/release").json()
    res = client.get(
        f"/api/projects/conv_node/download?version_seq={body['version_seq']}"
        f"&spec_digest={body['spec_digest']}"
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    names = _namelist(res.content)
    assert {
        "server.js",
        "package.json",
        "compose.yaml",
        "Dockerfile",
        ".dockerignore",
        ".env.example",
        "SELFHOST.md",
        "release.json",
    } <= set(names)
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        # source bytes unchanged; overlay is real generated content
        assert zf.read("server.js") == _NODE_FILES["server.js"]
        assert b"docker compose up -d --build" in zf.read("SELFHOST.md")
        assert b"API_BASE_URL" in zf.read(".env.example")
        assert _SENTINEL not in zf.read("release.json")


def test_download_appkit_candidate_carries_dev_server_overlay(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_appkit", _APPKIT_FILES)
    ps.cut_version("conv_appkit", trigger="test")

    body = client.get("/api/projects/conv_appkit/release").json()
    res = client.get(
        f"/api/projects/conv_appkit/download?version_seq={body['version_seq']}"
        f"&spec_digest={body['spec_digest']}"
    )
    assert res.status_code == 200
    names = set(_namelist(res.content))
    assert {"compose.yaml", "Dockerfile", ".env.example", "SELFHOST.md", "release.json"} <= names
    # the four AppKit contract files are still present (source preserved)
    assert {".disco/appspec.json", "wrangler.toml", "worker/index.ts", "schema.sql"} <= names


def test_download_not_web_is_byte_equivalent_to_plain_filtered_zip(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_docs", _DOCS_FILES)

    res = client.get("/api/projects/conv_docs/download")
    assert res.status_code == 200
    # Exactly the workspace file-set — NO overlay files added.
    assert _namelist(res.content) == ["LICENSE", "README.md", "notes/todo.md"]
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert zf.read("README.md") == _DOCS_FILES["README.md"]


def test_download_candidate_but_no_overlay_keys_leak_into_not_web(store, tmp_path, monkeypatch):
    """A not_web download must not gain a single overlay path (no false affordance)."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_docs", _DOCS_FILES)
    names = set(_namelist(client.get("/api/projects/conv_docs/download").content))
    overlay_names = {
        "compose.yaml",
        "Dockerfile",
        ".dockerignore",
        ".env.example",
        "SELFHOST.md",
        "release.json",
    }
    assert not (overlay_names & names)


# ---- criterion 2 collision rule (WO-C6 atomic overlay): a workspace file at a --
# ---- generated overlay path fails the WHOLE assessment closed, never a partial --
# ---- bundle. (Reconciled from WO-7's `overlay_suppressed_by_workspace_file` +   --
# ---- partial-overlay behavior, which WO-C6 §10.1/§10.2 supersedes.) -------------


def test_overlay_collision_fails_closed_with_typed_conflict(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    files = dict(_NODE_FILES)
    files[".env.example"] = b"FOO=workspace-owned\n"
    _seed(ps, store, "conv_collide", files, intent=_NODE_INTENT)
    ps.cut_version("conv_collide", trigger="test")

    # /release fails the WHOLE assessment closed: needs_review / self_host:false /
    # spec_digest:null, with a typed `overlay_path_conflict` blocker naming the path.
    body = client.get("/api/projects/conv_collide/release").json()
    assert body["assessment"] == "needs_review"
    assert body["self_host"] is False
    assert body["spec_digest"] is None
    conflicts = [b for b in body["blockers"] if b["code"] == "overlay_path_conflict"]
    assert [b["path"] for b in conflicts] == [".env.example"]

    # a BOUND self-host download of the collided version is refused (no zip bytes).
    bound = client.get(
        f"/api/projects/conv_collide/download?version_seq={body['version_seq']}"
        f"&spec_digest=sha256:{'0' * 64}"
    )
    assert bound.status_code in (409, 410)
    assert bound.headers.get("content-type") != "application/zip"
    assert not bound.content.startswith(b"PK")

    # the PLAIN (unbound) source download still works, keeps the workspace file
    # byte-for-byte, and ships NO generated overlay file (all-or-none).
    res = client.get("/api/projects/conv_collide/download")
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = set(zf.namelist())
        assert zf.read(".env.example") == b"FOO=workspace-owned\n"
        assert not ({"compose.yaml", "Dockerfile", "SELFHOST.md", "release.json"} & names)


# ---- criterion 3: a planted secret never reaches the zip or the /release JSON --


def test_secret_never_leaks_into_zip_or_release_json(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    files = dict(_NODE_FILES)
    files[".env"] = (
        b"DATABASE_URL=postgres://u:PLANTED-731-hunter2@h/db\nSECRET=PLANTED-731-hunter2\n"
    )
    files[".dev.vars"] = b"ADMIN_TOKEN=PLANTED-731-hunter2\n"
    _seed(ps, store, "conv_secret", files, intent=_NODE_INTENT)

    # /release JSON bytes carry no sentinel.
    rel = client.get("/api/projects/conv_secret/release")
    assert rel.status_code == 200
    assert _SENTINEL not in rel.content

    # every zip entry's bytes carry no sentinel, and the secret files are absent.
    res = client.get("/api/projects/conv_secret/download")
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = set(zf.namelist())
        assert ".env" not in names and ".dev.vars" not in names
        for name in names:
            assert _SENTINEL not in zf.read(name), name


# ---- criterion 4: auth mirrors download_project exactly ------------------------


def test_release_non_owner_is_forbidden(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_other", _NODE_FILES, owner_id="intruder", intent=_NODE_INTENT)
    res = client.get("/api/projects/conv_other/release")
    assert res.status_code == 403
    assert res.json()["detail"]["reason"] == "project_forbidden"


def test_release_unknown_project_is_not_found(store, tmp_path, monkeypatch):
    client, _ps = _client_for(store, tmp_path, monkeypatch)
    res = client.get("/api/projects/conv_missing/release")
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "project_not_found"


def test_release_storage_unavailable(store):
    client = TestClient(create_app(store, runtime=None))
    res = client.get("/api/projects/conv_any/release")
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "storage_unavailable"


# ---- criterion 5: no mode-branching (the acceptance grep, mirrored) ------------


def test_release_module_has_no_surface_branching():
    src = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "disco"
        / "agent_server"
        / "routes"
        / "release.py"
    ).read_text()
    assert "surface" not in src
    for banned in ("build_mode", "prompt"):
        assert banned not in src


# ---- criterion 6: idempotent + no subprocess -----------------------------------


def test_release_is_idempotent_and_spawns_no_subprocess(store, tmp_path, monkeypatch):
    calls: list[tuple] = []

    async def _sentinel_exec(*args, **kwargs):
        calls.append(args)
        raise AssertionError("/release must not spawn a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _sentinel_exec)

    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_node", _NODE_FILES, intent=_NODE_INTENT)

    first = client.get("/api/projects/conv_node/release")
    second = client.get("/api/projects/conv_node/release")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["tree_digest"] == second.json()["tree_digest"]
    assert calls == []


# ---- criterion 7: the endpoint is documented -----------------------------------


def test_release_endpoint_is_documented():
    doc = (Path(__file__).resolve().parents[4] / "current" / "docs" / "contracts" / "api-endpoints.md").read_text()
    assert "/api/projects/{id}/release" in doc


# ---- audit #7a: an imported project with an unknown stack -> needs_review ------

# A workspace with NO recognizable stack and NO container manifest: no server
# package.json, no python web framework, no index.html, no Dockerfile. Fresh, this
# is `not_web`; IMPORTED, the same tree must fail closed to `needs_review` naming
# every repairable release-contract field (WO-3 rung 4 / WO-7 intent).
_UNKNOWN_FILES: dict[str, bytes] = {
    "README.md": b"# imported project\n\nsource brought in from elsewhere.\n",
    "src/lib.rs": b"pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
}


def test_imported_unknown_stack_is_needs_review(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_imported", _UNKNOWN_FILES, imported=True)

    body = client.get("/api/projects/conv_imported/release").json()
    assert body["assessment"] == "needs_review"
    assert body["self_host"] is False
    assert body["ingress"] is None
    assert body["spec_digest"] is None
    # the full repairable diagnostic — the imported path names build_cmd too (the
    # container-review path does NOT), so this proves the imported rung, not rung 2.
    named = {b["field"] for b in body["blockers"] if b["field"]}
    assert {"build_cmd", "start_cmd", "port_env", "health_path", "required_env"} <= named


def test_non_imported_unknown_stack_stays_not_web(store, tmp_path, monkeypatch):
    """The other branch of #7a: a NON-imported unknown workspace keeps its existing
    `not_web` verdict — the fix must not regress fresh projects into review."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_fresh", _UNKNOWN_FILES, imported=False)

    body = client.get("/api/projects/conv_fresh/release").json()
    assert body["assessment"] == "not_web"
    assert body["self_host"] is False


# ---- audit #7b: version_seq + tree_digest must describe the SAME tree ----------


def test_version_seq_and_tree_digest_describe_the_same_tree(store, tmp_path, monkeypatch):
    """WO-C2: divergent live bytes over a committed v1 fail closed to a REAL source.

    The source binding never fabricates a `max+1` sequence and never pairs a real
    seq with the divergent LIVE digest. With v1 committed and the live tree diverged,
    `/release` fails closed (`needs_review` + `source_not_snapshotted`) and its
    `(version_seq, tree_digest)` name v1 exactly — the newest real record — not the
    unsnapshotted live tree."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_ver", _NODE_FILES, intent=_NODE_INTENT)
    # Commit the seeded tree as version 1, then DIVERGE the live workspace from it.
    v1 = ps.cut_version("conv_ver", trigger="test")
    assert v1 is not None and v1.seq == 1
    (ps.path_for("conv_ver") / "server.js").write_bytes(b"// changed\n" + _NODE_FILES["server.js"])

    body = client.get("/api/projects/conv_ver/release").json()
    saved = {v.seq: v.tree_digest for v in ps.list_versions("conv_ver")}
    vs, td = body["version_seq"], body["tree_digest"]

    # A non-null source field ALWAYS resolves to a real record whose digest it names.
    assert vs in saved and td == saved[vs], (vs, td, saved)
    # The unsnapshotted, divergent live tree is NEVER named as the source.
    assert td != compute_tree_digest(ps.path_for("conv_ver"))
    # Fail closed: no speculative version 2, no self-host offer.
    assert vs == v1.seq and td == v1.tree_digest
    assert body["assessment"] == "needs_review" and body["self_host"] is False
    assert "source_not_snapshotted" in {b["code"] for b in body["blockers"]}


def test_version_seq_and_tree_digest_report_matching_saved_version(store, tmp_path, monkeypatch):
    """When the live tree EQUALS its latest saved version, the endpoint reports that
    version's own (seq, tree_digest) pair — both fields, one source."""
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_ver2", _NODE_FILES, intent=_NODE_INTENT)
    v1 = ps.cut_version("conv_ver2", trigger="test")
    assert v1 is not None

    body = client.get("/api/projects/conv_ver2/release").json()
    assert body["version_seq"] == v1.seq
    assert body["tree_digest"] == v1.tree_digest


# ---- audit #7c: a mismatched-consumer intent must not 500 ----------------------

# A SCHEMA-VALID ReleaseIntent whose sqlite resource names a consumer service that
# the generated release does not define ("worker" — the ingress is always "web").
# `ReleaseSpec`'s referential-integrity validator rejects this at spec assembly;
# the endpoint must catch it and fail closed, never surface an uncaught 500.
_MISMATCHED_CONSUMER_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"),
    resources=(
        ResourceDecl(
            id="db",
            kind=ResourceKind.sqlite,
            persistent_path="/data/app.db",
            profiles=ResourceProfiles(
                local=LocalResourceProfile(url="file:/data/app.db", volume="app-data")
            ),
            consumers=("worker",),
        ),
    ),
)


def test_mismatched_resource_consumer_does_not_500(store, tmp_path, monkeypatch):
    client, ps = _client_for(store, tmp_path, monkeypatch)
    _seed(ps, store, "conv_badspec", _NODE_FILES, intent=_MISMATCHED_CONSUMER_INTENT)
    # WO-C2: commit the tree so the assessment binds to a real version and the spec
    # is actually assembled (where the referential-integrity error trips).
    ps.cut_version("conv_badspec", trigger="test")

    res = client.get("/api/projects/conv_badspec/release")
    assert res.status_code == 200, res.text
    body = res.json()
    # Fails CLOSED to review with a typed blocker — not a candidate, not a 500.
    assert body["assessment"] == "needs_review"
    assert body["self_host"] is False
    assert body["spec_digest"] is None
    codes = {b["code"] for b in body["blockers"]}
    assert "release_spec_invalid" in codes
    # the blocker text must not echo any secret-shaped VALUE (names/ids only).
    assert _SENTINEL not in res.content

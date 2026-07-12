"""WO-C3 red matrix — honest readiness + fail-closed deterministic detection.

Plan §7 (WO-C3): the positive fixture matrix (deterministic candidate + complete
overlay), the negative/adversarial fixture matrix (each → ``needs_review`` /
``self_host:false`` / no overlay / an EXACT typed blocker code), the ``self_host``
invariant (§7.6), bounded detection (§7.11), and matrix determinism (§7.1).

Locked semantics (plan §2): #1 ``candidate`` is statically-plausible-and-unverified
(never "Ready"); #3 ``self_host == true`` means a complete, internally-consistent
overlay is available — any blocker forces ``self_host == false`` and no partial
overlay; #6 runtime uncertainty fails closed to ``needs_review`` with a typed repair
instruction.

Boundary (plan §1.2): the ``self_host`` invariant and every blocker-surfacing
criterion drive the REAL FastAPI ``/release`` (+ ``/download``) routes through the
ASGI app, a real ``ProjectStore`` on a real on-disk workspace, and a real committed
version cut (so the C2 source binding is satisfied and detection is the only thing
under test). No release/detect/route function is mocked; the ONLY ``monkeypatch`` is
``ConfigStore.load`` (a config seam, exactly the injection the settings PUT performs).
Purely-detector properties (runtime strategy, output dir, resources, typed-result
determinism) additionally exercise ``detect_release`` directly.

RED vs GREEN on baseline ``2ec1ceba``/``31ec7fc0`` (see each test):
  * RED — the fine-grained typed blocker codes (``required_env_unresolved``,
    ``port_contract_unresolved``, ``entrypoint_unresolved``, ``toolchain_unsupported``,
    ``output_dir_unresolved``, ``health_path_unresolved``, ``runtime_conflict``) are
    emitted NOWHERE on baseline: every negative fixture is over-accepted as a
    self-hostable ``candidate`` (or, for competing runtimes, returns ``needs_review``
    with the coarse ``release_field_unresolved`` code instead of ``runtime_conflict``).
    The Express/FastAPI ``GET /`` health contract is not established (``health_path``
    is null). ``self_host:true`` coexists with an overlay-suppressed blocker
    (§7.6 violated). The detector performs an unbounded whole-tree text decode so an
    out-of-bounds binary/oversized file corrupts the verdict (§7.11 violated).
  * GREEN (preservation) — Static/Vite/AppKit remain complete candidates; the
    ``self_host ⇒ no blockers`` invariant holds across the negatives; typed results
    and positive overlays are deterministic.

Randomized (plan §4 crit 8): every conversation id / title is drawn from the seeded
``closeout_name`` factory; the detector reads workspace-relative file CONTENTS only,
never the id/title, so a detection cannot recognize a fixture by its name.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.detect import DetectionResult, Provenance, detect_release
from disco.core.release.local_compose import (
    COMPOSE_PATH,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    ENV_EXAMPLE_PATH,
    RELEASE_JSON_PATH,
    SELFHOST_DOC_PATH,
)
from disco.core.release.spec import (
    ReleaseIntent,
    ResourceKind,
    RuntimeStrategy,
    SecretClass,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout

# The single-service self-host overlay path set (plan §7 "complete overlay"). A
# candidate's ``/download`` zip must carry ALL of these; a fail-closed
# ``needs_review`` must carry NONE.
_OVERLAY_NAMES = frozenset(
    {
        COMPOSE_PATH,
        DOCKERFILE_PATH,
        DOCKERIGNORE_PATH,
        ENV_EXAMPLE_PATH,
        SELFHOST_DOC_PATH,
        RELEASE_JSON_PATH,
    }
)


# ---- positive fixtures (deterministic candidate + complete overlay) ------------

_EXPRESS_FILES: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (
        b"const port=process.env.PORT;\n"
        b"require('http').createServer((_q,r)=>r.end('ok'))"
        b".listen(port,'0.0.0.0');\n"
    ),
}
_FASTAPI_FILES: dict[str, bytes] = {
    "requirements.txt": b"fastapi\nuvicorn\n",
    "main.py": (
        b"from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        b"@app.get('/')\ndef root() -> dict[str, str]:\n    return {'status': 'ok'}\n"
    ),
}
_STATIC_FILES: dict[str, bytes] = {
    "index.html": b"<!doctype html><html><body><h1>hello</h1></body></html>\n",
    "styles.css": b"body { margin: 0; }\n",
}
_VITE_FILES: dict[str, bytes] = {
    "index.html": (
        b"<!doctype html><html><body>"
        b'<script type="module" src="/src/main.js"></script>'
        b"</body></html>\n"
    ),
    "package.json": b'{"name":"spa","scripts":{"build":"vite build"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"spa"}',
    "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
    "src/main.js": b"document.body.append('spa');\n",
}
_APPKIT_FILES: dict[str, bytes] = {
    ".disco/appspec.json": b'{"name":"appkit-app","version":1}',
    "wrangler.toml": (
        b'name = "appkit-app"\n\n[[d1_databases]]\nbinding = "DB"\ndatabase_name = "appkit_prod"\n'
    ),
    "worker/index.ts": (
        b"export default {\n"
        b"  async fetch(_req: Request): Promise<Response> {\n"
        b"    return new Response('ok');\n"
        b"  },\n"
        b"};\n"
    ),
    "schema.sql": b"CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);\n",
}


# ---- negative/adversarial fixtures (each → needs_review + exact blocker) --------

_NEG_UNDECLARED_ENV: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (
        b"const secret = process.env.SESSION_SECRET;\n"
        b"require('http').createServer((_q,r)=>r.end(secret)).listen(process.env.PORT);\n"
    ),
}
_NEG_LITERAL_PORT: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(3000);\n",
}
_NEG_DYNAMIC_ENV: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (
        b"const name = 'SESSION_SECRET';\n"
        b"const secret = process.env[name];\n"
        b"require('http').createServer((_q,r)=>r.end(secret)).listen(process.env.PORT);\n"
    ),
}
_NEG_NESTED_FASTAPI: dict[str, bytes] = {
    "requirements.txt": b"fastapi\nuvicorn\n",
    "src/acme/api.py": b"from fastapi import FastAPI\n\napp = FastAPI()\n",
    "src/acme/__init__.py": b"",
}
_NEG_FLASK_NO_START: dict[str, bytes] = {
    "requirements.txt": b"flask\n",
    "app.py": (
        b"from flask import Flask\n\napp = Flask(__name__)\n\n\n"
        b"@app.route('/')\ndef home() -> str:\n    return 'ok'\n"
    ),
}
_NEG_PY_TOOLCHAIN_FILES: dict[str, bytes] = {
    "requirements.txt": b"fastapi\n",
    "main.py": b"from fastapi import FastAPI\n\napp = FastAPI()\n",
}
_NEG_PY_TOOLCHAIN_INTENT = ReleaseIntent(start_cmd=("gunicorn", "main:app"))
_NEG_BUN_FILES: dict[str, bytes] = {
    "package.json": b'{"name":"svc"}',
    "index.js": b"console.log('hi');\n",
}
_NEG_BUN_INTENT = ReleaseIntent(start_cmd=("bun", "run", "start"))
_NEG_VITE_DYNAMIC_OUTDIR: dict[str, bytes] = {
    "index.html": b"<!doctype html><html><body></body></html>\n",
    "package.json": b'{"name":"spa","scripts":{"build":"vite build"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"spa"}',
    "vite.config.js": b"export default { build: { outDir: process.env.OUT_DIR || 'build' } };\n",
}
_NEG_BUILD_UNKNOWN_OUTDIR: dict[str, bytes] = {
    "index.html": b"<!doctype html><html><body></body></html>\n",
    "package.json": b'{"name":"spa","scripts":{"build":"some-unknown-bundler --emit"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"spa"}',
}
_NEG_HEALTH_FILES: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}
_NEG_HEALTH_INTENT = ReleaseIntent(start_cmd=("node", "server.js"), health_path="/healthz")
_NEG_RUNTIME_CONFLICT: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
    "requirements.txt": b"fastapi\n",
    "main.py": b"from fastapi import FastAPI\n\napp = FastAPI()\n",
}

# The two distinctive fragments of the detector's competing-runtime evidence
# strings (`detect._runtime_conflict_result`). The API must surface BOTH so an
# owner sees why the runtimes conflict (plan §7 "runtime_conflict with both
# evidence strings"). Baseline surfaces NEITHER verbatim in the response.
_NODE_EVIDENCE_FRAGMENT = "declares a server"
_PY_EVIDENCE_FRAGMENT = "python manifest with a web-framework"

# (fixture_id, files, intent, expected_blocker_code)
_NEGATIVE_MATRIX: list[tuple[str, dict[str, bytes], ReleaseIntent | None, str]] = [
    ("node_undeclared_env", _NEG_UNDECLARED_ENV, None, "required_env_unresolved"),
    ("node_literal_port", _NEG_LITERAL_PORT, None, "port_contract_unresolved"),
    ("node_dynamic_env", _NEG_DYNAMIC_ENV, None, "required_env_unresolved"),
    ("fastapi_nested", _NEG_NESTED_FASTAPI, None, "entrypoint_unresolved"),
    ("flask_no_start", _NEG_FLASK_NO_START, None, "entrypoint_unresolved"),
    (
        "py_toolchain_absent",
        _NEG_PY_TOOLCHAIN_FILES,
        _NEG_PY_TOOLCHAIN_INTENT,
        "toolchain_unsupported",
    ),
    ("bun_run_on_node_base", _NEG_BUN_FILES, _NEG_BUN_INTENT, "toolchain_unsupported"),
    ("vite_dynamic_outdir", _NEG_VITE_DYNAMIC_OUTDIR, None, "output_dir_unresolved"),
    ("build_unknown_outdir", _NEG_BUILD_UNKNOWN_OUTDIR, None, "output_dir_unresolved"),
    ("health_no_route", _NEG_HEALTH_FILES, _NEG_HEALTH_INTENT, "health_path_unresolved"),
    ("runtime_conflict", _NEG_RUNTIME_CONFLICT, None, "runtime_conflict"),
]

# The whole matrix (positive + negative) for the typed-result determinism sweep.
_FULL_MATRIX: list[tuple[str, dict[str, bytes], ReleaseIntent | None]] = [
    ("express", _EXPRESS_FILES, None),
    ("fastapi", _FASTAPI_FILES, None),
    ("static", _STATIC_FILES, None),
    ("vite", _VITE_FILES, None),
    ("appkit", _APPKIT_FILES, None),
    *[(fid, files, intent) for fid, files, intent, _code in _NEGATIVE_MATRIX],
]

# The positive fixtures, for the "candidate + complete overlay + byte-identical
# overlay" sweeps.
_POSITIVE_MATRIX: list[tuple[str, dict[str, bytes]]] = [
    ("express", _EXPRESS_FILES),
    ("fastapi", _FASTAPI_FILES),
    ("static", _STATIC_FILES),
    ("vite", _VITE_FILES),
    ("appkit", _APPKIT_FILES),
]


# ---- bounded-detection adversarial fixtures (§7.11) ----------------------------

_CLEAN_NODE: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}
# A BINARY file (NUL bytes + a PNG magic header) that also happens to contain the
# ASCII bytes of a `postgres://` URL. A detector that text-decodes the whole tree
# reads the URL out of the binary and fails the workspace closed for an
# unrecognized DB engine — a spurious signal from an out-of-bounds decode.
_BINARY_BLOB = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
    + b"\x00\x00\x00\x00 postgres://user:pw@db.internal:5432/app \x00\x00"
    + (b"\x00\xff\x10\xfe" * 64)
)
# An OVERSIZED text file (~3 MiB) whose spurious `mysql://` marker sits at the very
# END, past any sane per-file scan cap. A bounded detector never reaches it; an
# unbounded whole-file decode does and mis-classifies the workspace.
_OVERSIZED_TEXT = (b"const noop = 1;\n" * 200_000) + b"\n// legacy dsn: mysql://user@host/legacy\n"

# (fixture_id, extra_path, extra_bytes, injected_scheme_token)
_BOUNDED_MATRIX: list[tuple[str, str, bytes, str]] = [
    ("binary_blob_with_pg_url", "assets/logo.png", _BINARY_BLOB, "postgres"),
    ("oversized_text_with_mysql_url", "data/legacy_dump.txt", _OVERSIZED_TEXT, "mysql"),
]

# An otherwise-clean node candidate whose workspace ALSO ships a file at a
# generated-overlay path (`SELFHOST.md`). Baseline keeps `self_host:true` while
# appending an `overlay_suppressed_by_workspace_file` blocker — a §7.6 violation
# (self_host:true with a non-empty blocker set).
_OVERLAY_COLLISION_FILES: dict[str, bytes] = {
    **_CLEAN_NODE,
    SELFHOST_DOC_PATH: b"# our own self-hosting notes\n",
}


# ---- harness (real ASGI app + real ProjectStore; only ConfigStore.load seamed) --


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


def _cid(make_name: object, prefix: str) -> str:
    """A seeded id in the canonical ``conv_`` namespace the release route requires
    (``^conv_[A-Za-z0-9_-]+$``); its tail varies from the recorded seed."""
    assert callable(make_name)
    return str(make_name(prefix))


def _seed_project(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    title: str,
    files: dict[str, bytes],
    *,
    intent: ReleaseIntent | None = None,
    owner_id: str = "local",
) -> None:
    """Seed a real on-disk workspace, manifest, optional host-owned intent sidecar,
    conversation record — then cut a real version so the live tree matches a
    committed ``VersionRecord`` (the C2 source binding is satisfied and detection is
    the only thing under test)."""
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
        imported=False,
    )
    if intent is not None:
        ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id=owner_id, title=title, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1, "precondition: a real version 1 was committed"


def _release(client: TestClient, cid: str) -> object:
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    return res.json()


def _blocker_codes(body: object) -> set[str]:
    assert isinstance(body, dict)
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    return {str(b["code"]) for b in blockers}


def _download_overlay(client: TestClient, cid: str) -> frozenset[str]:
    """The subset of the self-host overlay path set actually present in the project's
    ``/download`` zip (a candidate carries all of them; a fail-closed project none)."""
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = set(zf.namelist())
    return frozenset(_OVERLAY_NAMES & names)


def _detect(files: dict[str, bytes], intent: ReleaseIntent | None) -> DetectionResult:
    return detect_release(files, intent=intent, provenance=Provenance())


# ===========================================================================
# Positive matrix (§7 positive table) — deterministic candidate + complete
# overlay + the FULL per-fixture contract. Static/Vite/AppKit are GREEN
# preservation; Express/FastAPI are RED on the unestablished ``GET /`` health
# contract.
# ===========================================================================


def test_positive_express_node_full_contract(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7 (Express/Node) — RED on baseline (``GET /`` health not established).

    Node candidate: root package lock, ``npm ci`` install, exact ``PORT`` env use,
    and a proven ``GET /`` health contract. Candidate/self_host/overlay/PORT/install
    are GREEN preservation; the ingress ``health_path`` is null on baseline instead of
    the established ``/`` (plan §7 lists ``GET /`` as a required proven contract)."""
    cid = _cid(closeout_name, "conv_c3exp")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _EXPRESS_FILES)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    assert body["ingress"]["port"] == "PORT", "the ingress must bind the $PORT env contract"
    assert _download_overlay(client, cid) == _OVERLAY_NAMES, "overlay is incomplete"

    detection = _detect(_EXPRESS_FILES, None)
    ingress = detection.ingress
    assert ingress is not None
    assert ingress.runtime is RuntimeStrategy.node
    assert ingress.lockfile == "package-lock.json" and ingress.install_cmd == ("npm", "ci"), (
        "a root package lock must drive a supported `npm ci` install"
    )

    assert body["ingress"]["health_path"] == "/", (
        "the Express candidate must establish the GET / health contract (plan §7 lists "
        "'GET /'); baseline leaves health_path null (unestablished/guessed-as-default) "
        "rather than proving the '/' route exists."
    )


def test_positive_fastapi_full_contract(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7 (FastAPI) — RED on baseline (``GET /`` health not established).

    Python candidate: root module + ``app``, a declared dependency, exact ``PORT``
    env use, and a proven ``GET /`` health contract. All GREEN except the null
    ingress ``health_path`` (should be the established ``/``)."""
    cid = _cid(closeout_name, "conv_c3fa")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _FASTAPI_FILES)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    assert body["ingress"]["port"] == "PORT"
    assert _download_overlay(client, cid) == _OVERLAY_NAMES, "overlay is incomplete"

    detection = _detect(_FASTAPI_FILES, None)
    ingress = detection.ingress
    assert ingress is not None
    assert ingress.runtime is RuntimeStrategy.python
    assert "main:app" in ingress.start_cmd, "the root module + `app` must be resolved"

    assert body["ingress"]["health_path"] == "/", (
        "the FastAPI candidate must establish the GET / health contract (plan §7 lists "
        "'GET /'); baseline leaves health_path null rather than proving the '/' route."
    )


def test_positive_static_full_contract(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7 (Static) — GREEN preservation.

    A root ``index.html`` with no server/build ambiguity is a complete static
    candidate served from the workspace root. Must stay green through the fix."""
    cid = _cid(closeout_name, "conv_c3st")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _STATIC_FILES)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    assert body["ingress"]["port"] == "PORT"
    assert _download_overlay(client, cid) == _OVERLAY_NAMES, "overlay is incomplete"

    detection = _detect(_STATIC_FILES, None)
    ingress = detection.ingress
    assert ingress is not None
    assert ingress.runtime is RuntimeStrategy.static
    assert ingress.output_dir == "." and ingress.build_cmd == (), (
        "a no-build static site serves the workspace root with no output-dir ambiguity"
    )


def test_positive_vite_full_contract(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7 (Vite) — GREEN preservation.

    An npm-locked Vite build with a statically-resolved ``dist`` output directory is
    a complete static candidate. The negative matrix (``vite_dynamic_outdir`` /
    ``build_unknown_outdir``) proves the ``dist`` value is genuinely resolved rather
    than blindly assumed; here the explicit ``outDir: 'dist'`` config resolves to
    ``dist`` and must stay green."""
    cid = _cid(closeout_name, "conv_c3vi")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _VITE_FILES)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    assert body["ingress"]["port"] == "PORT"
    assert _download_overlay(client, cid) == _OVERLAY_NAMES, "overlay is incomplete"

    detection = _detect(_VITE_FILES, None)
    ingress = detection.ingress
    assert ingress is not None
    assert ingress.runtime is RuntimeStrategy.static
    assert ingress.output_dir == "dist", "the Vite build output dir must resolve to dist"
    assert ingress.build_cmd == ("npm", "run", "build")
    assert ingress.lockfile == "package-lock.json" and ingress.install_cmd == ("npm", "ci"), (
        "a build-requiring static bundle must install deps (npm ci) before the build"
    )


def test_positive_appkit_full_contract(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7 (AppKit) — GREEN preservation.

    The four AppKit contract files yield a single ``dev_server`` ingress, a
    sqlite-class D1 state resource persisted at ``/data/state``, and the admin
    read-back secret NAME. Must stay green through the fix."""
    cid = _cid(closeout_name, "conv_c3ak")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _APPKIT_FILES)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    assert body["ingress"]["port"] == "PORT" and body["ingress"]["health_path"] == "/"
    assert _download_overlay(client, cid) == _OVERLAY_NAMES, "overlay is incomplete"

    # The admin secret NAME must reach the API as a required, secret-classed env var.
    env = {e["name"]: e for e in body["required_env"]}
    assert "ADMIN_TOKEN" in env, "the AppKit admin secret name must surface in required_env"
    assert env["ADMIN_TOKEN"]["secret"] is True and env["ADMIN_TOKEN"]["required"] is True

    detection = _detect(_APPKIT_FILES, None)
    ingress = detection.ingress
    assert ingress is not None
    assert ingress.runtime is RuntimeStrategy.dev_server, "AppKit runs on the dev_server strategy"
    sqlite = [r for r in detection.resources if r.kind is ResourceKind.sqlite]
    assert len(sqlite) == 1 and sqlite[0].persistent_path == "/data/state", (
        "AppKit must persist its local D1 state at /data/state"
    )
    admin = [e for e in detection.env if e.name == "ADMIN_TOKEN"]
    assert admin and admin[0].secret is SecretClass.secret


# ===========================================================================
# Negative/adversarial matrix (§7 negative table) — each fixture must fail
# closed to needs_review, self_host:false, no overlay, and the EXACT typed
# blocker code. Every case is RED on baseline (over-accepted as a self-hostable
# candidate; runtime_conflict returns the coarse release_field_unresolved code).
# ===========================================================================


@pytest.mark.parametrize(
    "fixture_id,files,intent,expected_code",
    _NEGATIVE_MATRIX,
    ids=[row[0] for row in _NEGATIVE_MATRIX],
)
def test_negative_matrix_fails_closed_with_exact_blocker(
    fixture_id: str,
    files: dict[str, bytes],
    intent: ReleaseIntent | None,
    expected_code: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7 negative matrix + §2 #6 — RED on baseline.

    Runtime uncertainty must fail closed: ``needs_review``, ``self_host:false``, NO
    self-host overlay in the download, and the EXACT typed blocker ``code`` for the
    fixture's defect. Baseline emits none of these fine-grained codes — it either
    over-accepts the fixture as a self-hostable ``candidate`` (empty blockers) or, for
    competing runtimes, returns ``needs_review`` with the coarse
    ``release_field_unresolved`` code instead of ``runtime_conflict``."""
    cid = _cid(closeout_name, f"conv_c3n{fixture_id[:4]}")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), files, intent=intent)

    body = _release(client, cid)
    assert isinstance(body, dict)

    assert body["assessment"] == "needs_review", (
        f"[{fixture_id}] a predictably-broken runtime contract must fail closed to "
        f"needs_review; baseline over-accepts it as {body['assessment']!r}."
    )
    assert body["self_host"] is False, (
        f"[{fixture_id}] self_host must be False when the runtime contract is unresolved."
    )
    assert body["spec_digest"] is None, (
        f"[{fixture_id}] a fail-closed assessment must not bind a self-host spec_digest."
    )
    assert _download_overlay(client, cid) == frozenset(), (
        f"[{fixture_id}] a fail-closed assessment must ship NO self-host overlay; "
        "baseline streams the full overlay because it treats the fixture as a candidate."
    )

    codes = _blocker_codes(body)
    assert expected_code in codes, (
        f"[{fixture_id}] expected the exact typed blocker code {expected_code!r}, "
        f"saw {sorted(codes)}."
    )

    if expected_code == "runtime_conflict":
        blob = json.dumps(body).lower()
        assert _NODE_EVIDENCE_FRAGMENT in blob and _PY_EVIDENCE_FRAGMENT in blob, (
            "the runtime_conflict response must surface BOTH the node and python "
            "runtime evidence strings so an owner can reconcile them; baseline surfaces "
            f"neither verbatim (looked for {_NODE_EVIDENCE_FRAGMENT!r} and "
            f"{_PY_EVIDENCE_FRAGMENT!r})."
        )


# ===========================================================================
# §7.6 — the API never returns self_host:true when blockers is non-empty.
# ===========================================================================


@pytest.mark.parametrize(
    "fixture_id,files,intent,expected_code",
    _NEGATIVE_MATRIX,
    ids=[row[0] for row in _NEGATIVE_MATRIX],
)
def test_self_host_invariant_holds_across_negative_matrix(
    fixture_id: str,
    files: dict[str, bytes],
    intent: ReleaseIntent | None,
    expected_code: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7.6 (invariant, negative matrix) — GREEN preservation.

    Across the whole negative matrix the safety invariant ``self_host:true ⟹ blockers
    == []`` must ALWAYS hold. It holds vacuously on baseline (each negative is a
    candidate with self_host:true and no blockers) and must keep holding once the fix
    turns these into blocked ``needs_review`` results — this test guards the invariant
    against ever regressing into a dishonest ``self_host:true`` + blocker state."""
    del expected_code  # this test asserts the universal invariant, not a specific code
    cid = _cid(closeout_name, f"conv_c3i{fixture_id[:4]}")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), files, intent=intent)

    body = _release(client, cid)
    assert isinstance(body, dict)
    if body["self_host"] is True:
        assert _blocker_codes(body) == set(), (
            f"[{fixture_id}] the API returned self_host:true WITH blockers "
            f"{sorted(_blocker_codes(body))} — a dishonest-readiness invariant violation."
        )


def test_self_host_true_with_overlay_collision_blocker_is_forbidden(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7.6 (invariant, overlay collision) — RED on baseline.

    A candidate whose workspace already contains a file at a generated-overlay path
    (here ``SELFHOST.md``) must not advertise ``self_host:true`` while ALSO reporting a
    blocker. Baseline keeps ``self_host:true`` and appends an
    ``overlay_suppressed_by_workspace_file`` blocker — the exact dishonest-readiness
    state §7.6 (and §2 #3: any blocker forces ``self_host == false``) forbids."""
    cid = _cid(closeout_name, "conv_c3col")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _OVERLAY_COLLISION_FILES)

    body = _release(client, cid)
    assert isinstance(body, dict)
    codes = _blocker_codes(body)
    assert not (body["self_host"] is True and codes), (
        "the API returned self_host:true while reporting blockers "
        f"{sorted(codes)} (an overlay collision); §2 #3 requires any blocker to force "
        "self_host:false with no partial overlay."
    )


# ===========================================================================
# §7.11 — bounded detection: an out-of-bounds binary / oversized file must not
# corrupt the verdict via an unbounded whole-tree text decode.
# ===========================================================================


@pytest.mark.parametrize(
    "fixture_id,extra_path,extra_bytes,scheme",
    _BOUNDED_MATRIX,
    ids=[row[0] for row in _BOUNDED_MATRIX],
)
def test_detection_is_bounded_against_out_of_bounds_content(
    fixture_id: str,
    extra_path: str,
    extra_bytes: bytes,
    scheme: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7.11 — RED on baseline.

    A clean node candidate ships one out-of-bounds file: a NUL-laden BINARY blob, or
    an oversized text file whose spurious DB-URL marker sits past any sane scan cap.
    Detection must be bounded — the binary/oversized content is safely ignored or
    produces a deterministic bounded-review blocker — so the injected ``scheme://``
    URL must NEVER drive the verdict. Baseline text-decodes the entire tree
    (``_as_text`` with ``errors='ignore'``, no cap), reads the URL out of the
    out-of-bounds file, and fails the workspace closed for an unrecognized DB engine,
    surfacing the scheme in the response.

    The assertion respects the documented-cap disjunction (ignored OR bounded
    blocker): it only forbids the injected scheme from appearing anywhere in the
    response, and requires determinism."""
    cid = _cid(closeout_name, f"conv_c3b{fixture_id[:4]}")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {**_CLEAN_NODE, extra_path: extra_bytes}
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), files)

    first = client.get(f"/api/projects/{cid}/release")
    second = client.get(f"/api/projects/{cid}/release")
    assert first.status_code == 200 and second.status_code == 200, first.text
    assert first.json() == second.json(), (
        f"[{fixture_id}] bounded detection must be deterministic across two reads."
    )

    blob = json.dumps(first.json()).lower()
    assert scheme not in blob, (
        f"[{fixture_id}] the injected {scheme!r} URL — planted in an out-of-bounds "
        f"{'binary' if fixture_id.startswith('binary') else 'oversized'} file — reached "
        "the verdict; the detector performed an unbounded whole-tree text decode "
        "instead of bounding/ignoring the file (plan §7.11)."
    )


# ===========================================================================
# §7.1 — determinism: the whole matrix yields equal typed results twice, and
# positive overlays are byte-identical.
# ===========================================================================


@pytest.mark.parametrize(
    "fixture_id,files,intent",
    _FULL_MATRIX,
    ids=[row[0] for row in _FULL_MATRIX],
)
def test_matrix_typed_results_are_deterministic(
    fixture_id: str,
    files: dict[str, bytes],
    intent: ReleaseIntent | None,
) -> None:
    """WO-C3 §7.1 (typed results) — GREEN preservation.

    Every positive and negative fixture returns an EQUAL typed ``DetectionResult`` on
    two runs (the detector is a pure function of committed contents + typed intent).
    Must remain deterministic through the fix."""
    first = _detect(files, intent)
    second = _detect(files, intent)
    assert first == second, f"[{fixture_id}] detection was not deterministic across two runs."


@pytest.mark.parametrize(
    "fixture_id,files",
    _POSITIVE_MATRIX,
    ids=[row[0] for row in _POSITIVE_MATRIX],
)
def test_positive_overlays_are_byte_identical(
    fixture_id: str,
    files: dict[str, bytes],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C3 §7.1 (positive overlays) — GREEN preservation.

    Two ``/download`` requests for the same committed positive candidate emit
    byte-identical self-host overlay entries (the overlay is a pure function of the
    spec). Must stay byte-deterministic through the fix."""
    cid = _cid(closeout_name, f"conv_c3o{fixture_id[:4]}")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True

    first = _overlay_entries(client, cid)
    second = _overlay_entries(client, cid)
    assert first.keys() == _OVERLAY_NAMES, (
        f"[{fixture_id}] the positive overlay is incomplete: {sorted(first.keys())}"
    )
    assert first == second, f"[{fixture_id}] positive overlay bytes were not identical."


def _overlay_entries(client: TestClient, cid: str) -> dict[str, bytes]:
    """The overlay path -> bytes map extracted from a candidate's ``/download`` zip."""
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        for name in zf.namelist():
            if name in _OVERLAY_NAMES:
                entries[name] = zf.read(name)
    return entries

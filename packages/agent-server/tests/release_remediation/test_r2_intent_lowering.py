"""R2 (G03/G04/G05/G06) — the complete typed intent lowered through the REAL routes.

Closeout remediation R2. These regression tests live OUTSIDE the frozen closeout dirs
(no ``export_track1_closeout`` marker), so they never perturb the v4 acceptance
manifest, and they drive the SAME public boundary the frozen reds use: a real FastAPI
``GET /api/projects/{cid}/release`` (+ ``/download``) over a real ``ProjectStore`` on a
real on-disk workspace with a real committed version cut; only ``ConfigStore.load`` is
seamed. Every behaviour is asserted on the ACTUAL bytes streamed by ``/download`` (the
emitted ``Dockerfile`` inside the zip) and on the ``/release`` JSON — never on
``detect``/``emit`` called directly.

Coverage (plan §5 non-bypass criteria that need the ROUTE boundary):

* G04 — a no-build INTERPRETED candidate installs its declared dependencies: an
  Express (``express`` dep, ``node server.js``, no build) intent emits an ``npm
  install`` layer; a FastAPI (``requirements.txt``, ``uvicorn``) intent emits a ``pip
  install`` layer. A DIFFERENT dependency shape than the two frozen G04 fixtures, so
  the fix cannot be a fixture special-case. Imported Node (no intent) still derives
  ``npm ci`` from a committed lockfile via the source path.
* G05 — a typed intent whose start command names an UNPROVISIONED package manager
  (pnpm / yarn / bun / poetry / uv) fails closed with ``toolchain_unsupported``
  (needs_review, self_host:false, no overlay) — the SAME reject the source path emits.
* G06 — a typed static intent's declared ``output_dir`` lowers into the two-stage
  ``COPY --from=build /app/<output_dir>/`` copy (a NON-default dir, so it is honored
  from the declaration, not guessed).
* POSITIVE — a fully-typed intent (explicit runtime + install_cmd + output_dir + a
  scoped runtime secret env) round-trips into a self-hostable candidate that lowers
  each field correctly; an explicit ``install_cmd`` WINS over derivation.
"""

from __future__ import annotations

import io
import itertools
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.local_compose import (
    COMPOSE_PATH,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    ENV_EXAMPLE_PATH,
    RELEASE_JSON_PATH,
    SELFHOST_DOC_PATH,
)
from disco.core.release.spec import (
    EnvScope,
    EnvVarDecl,
    ReleaseIntent,
    RuntimeStrategy,
    SecretClass,
)
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

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

_ID_COUNTER = itertools.count()

# A minimal $PORT-binding node server body, shared across fixtures.
_HTTP_SERVER = b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"


def _uid(prefix: str) -> str:
    return f"{prefix}_{next(_ID_COUNTER)}"


# ---- harness (real ASGI app + real ProjectStore; only ConfigStore.load seamed) -----


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
    *,
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
        title=cid,
        owner_id="local",
        created_at="2026-07-14T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=imported,
    )
    if intent is not None:
        ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id="local", title=cid, surface="build")
    cut = ps.cut_version(cid, trigger="r2")
    assert cut is not None and cut.seq == 1


def _release(client: TestClient, cid: str) -> dict[str, object]:
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)
    return body


def _blocker_codes(body: dict[str, object]) -> set[str]:
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    return {str(b["code"]) for b in blockers}


def _overlay_texts(client: TestClient, cid: str) -> dict[str, str]:
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name).decode("utf-8", errors="replace")
    return out


def _overlay_present(texts: dict[str, str]) -> frozenset[str]:
    return frozenset(_OVERLAY_NAMES & set(texts))


# ---- fixtures (distinct from the frozen G04/G06 fixtures, so no fixture special-case) --

# An Express service with MULTIPLE deps AND a committed lockfile → `npm ci` (not the
# frozen G04 express fixture, which has one dep and no lockfile → npm install).
_EXPRESS_LOCKED: dict[str, bytes] = {
    "package.json": (
        b'{"name":"api","dependencies":{"express":"^4.19.2","cors":"^2.8.5"},'
        b'"scripts":{"start":"node index.js"}}'
    ),
    "package-lock.json": b'{"lockfileVersion":3,"name":"api"}',
    "index.js": (
        b"const express=require('express');const app=express();\n"
        b"app.get('/',(_q,r)=>r.send('ok'));app.listen(process.env.PORT);\n"
    ),
}
_EXPRESS_LOCKED_INTENT = ReleaseIntent(start_cmd=("node", "index.js"))

# A FastAPI service (pyproject-based deps → still a pip install).
_FASTAPI_REQS: dict[str, bytes] = {
    "requirements.txt": b"fastapi\nuvicorn\nhttpx\n",
    "main.py": (
        b"from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef r():\n    return {}\n"
    ),
}
_FASTAPI_INTENT = ReleaseIntent(
    start_cmd=("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}")
)

# Imported Node WITH a lockfile, NO intent → the SOURCE path derives `npm ci`.
_IMPORTED_NODE: dict[str, bytes] = {
    "package.json": b'{"name":"imp","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"imp"}',
    "server.js": _HTTP_SERVER,
}

# A Vite static bundle served from a NON-default output dir (`build`, not `dist`) so a
# lowered COPY proves the DECLARED output_dir is honored, not a Vite default.
_VITE_BUILD_DIR: dict[str, bytes] = {
    "index.html": b"<!doctype html><div id=app>hi</div><script type=module src=/src/m.js></script>",
    "package.json": (
        b'{"name":"spa","scripts":{"build":"vite build"},"devDependencies":{"vite":"^5"}}'
    ),
    "vite.config.js": b"export default { build: { outDir: 'build' } };\n",
    "src/m.js": b"document.getElementById('app').textContent='hi';\n",
}
_VITE_BUILD_INTENT = ReleaseIntent(
    build_cmd=("npm", "run", "build"), output_dir="build", health_path="/"
)


# ---- G04: a no-build interpreted candidate installs its dependencies -----------------


def test_express_no_build_intent_emits_npm_install_layer(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _uid("conv_r2exp")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _EXPRESS_LOCKED, intent=_EXPRESS_LOCKED_INTENT)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    dockerfile = _overlay_texts(client, cid)[DOCKERFILE_PATH]
    # A committed lockfile → `npm ci`, emitted BEFORE the CMD, even with NO build step.
    assert '"npm", "ci"' in dockerfile, dockerfile
    assert dockerfile.index('"npm", "ci"') < dockerfile.index('"node", "index.js"')


def test_fastapi_no_build_intent_emits_pip_install_layer(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _uid("conv_r2fa")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _FASTAPI_REQS, intent=_FASTAPI_INTENT)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    dockerfile = _overlay_texts(client, cid)[DOCKERFILE_PATH]
    assert '"pip", "install", "-r", "requirements.txt"' in dockerfile, dockerfile


def test_imported_node_source_still_derives_npm_ci(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The G04 fix must not special-case the intent path: an imported node app with a
    committed lockfile and NO typed intent still derives `npm ci` via the source path."""
    cid = _uid("conv_r2imp")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _IMPORTED_NODE, imported=True)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    dockerfile = _overlay_texts(client, cid)[DOCKERFILE_PATH]
    assert '"npm", "ci"' in dockerfile, dockerfile


# ---- G05: an unprovisioned package manager fails closed ------------------------------


@pytest.mark.parametrize(
    "head",
    ["pnpm", "yarn", "bun", "poetry", "uv"],
)
def test_typed_unprovisioned_manager_start_fails_closed(
    head: str, _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed intent whose start command heads with an unprovisioned toolchain must
    fail closed with `toolchain_unsupported` (needs_review, self_host:false, no overlay)
    — never an accepted candidate whose image cannot run it."""
    cid = _uid("conv_r2pm")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {
        "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
        "server.js": _HTTP_SERVER,
    }
    _seed(ps, _store, cid, files, intent=ReleaseIntent(start_cmd=(head, "start")))

    body = _release(client, cid)
    assert body["assessment"] == "needs_review", (head, body)
    assert body["self_host"] is False and body["spec_digest"] is None
    assert "toolchain_unsupported" in _blocker_codes(body), (head, sorted(_blocker_codes(body)))
    assert _overlay_present(_overlay_texts(client, cid)) == frozenset()


def test_typed_pnpm_build_command_fails_closed(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unprovisioned manager named in the BUILD command (not just start) also fails
    closed — it would be lowered into a `RUN` the image cannot run."""
    cid = _uid("conv_r2pmb")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {
        "package.json": b'{"name":"svc","dependencies":{"x":"1"},"scripts":{"start":"node s.js"}}',
        "s.js": _HTTP_SERVER,
    }
    intent = ReleaseIntent(start_cmd=("node", "s.js"), build_cmd=("pnpm", "run", "build"))
    _seed(ps, _store, cid, files, intent=intent)

    body = _release(client, cid)
    assert body["assessment"] == "needs_review"
    assert "toolchain_unsupported" in _blocker_codes(body)
    assert _overlay_present(_overlay_texts(client, cid)) == frozenset()


# ---- G06: a declared static output_dir lowers into the two-stage COPY ----------------


def test_static_intent_output_dir_lowers_into_two_stage_copy(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _uid("conv_r2out")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _VITE_BUILD_DIR, intent=_VITE_BUILD_INTENT)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    dockerfile = _overlay_texts(client, cid)[DOCKERFILE_PATH]
    # Two-stage: build with node, then serve the DECLARED output dir from a static image.
    assert "FROM node:22-bookworm-slim AS build" in dockerfile, dockerfile
    assert "COPY --from=build /app/build/ /site/" in dockerfile, dockerfile
    # An install layer precedes the build (a static bundle installs before it builds).
    assert '"npm", "install"' in dockerfile
    assert dockerfile.index('"npm", "install"') < dockerfile.index('"npm", "run", "build"')


# ---- POSITIVE: a fully-typed intent round-trips and lowers each field ----------------


def test_fully_typed_intent_round_trips_and_lowers(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An intent that declares an explicit runtime, an explicit install_cmd (WINNING
    over derivation), and a scoped runtime SECRET env lowers all three: the install_cmd
    verbatim, and the secret env as a name-only `${NAME:?...}` guard in compose."""
    cid = _uid("conv_r2full")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {
        "package.json": (
            b'{"name":"svc","dependencies":{"express":"1"},"scripts":{"start":"node app.js"}}'
        ),
        "app.js": _HTTP_SERVER,
    }
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.node,
        start_cmd=("node", "app.js"),
        install_cmd=("npm", "ci"),  # explicit — must WIN even though no lockfile is present
        env=(
            EnvVarDecl(
                name="APP_SECRET", scope=EnvScope.runtime, required=True, secret=SecretClass.secret
            ),
        ),
    )
    _seed(ps, _store, cid, files, intent=intent)

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    # The scoped secret env reached the API, classified secret.
    env = body["required_env"]
    assert isinstance(env, list)
    matches = [e for e in env if isinstance(e, dict) and e.get("name") == "APP_SECRET"]
    assert len(matches) == 1 and matches[0]["secret"] is True, env

    texts = _overlay_texts(client, cid)
    dockerfile = texts[DOCKERFILE_PATH]
    compose = texts[COMPOSE_PATH]
    # Explicit install_cmd wins (npm ci, not the derived npm install for a lockless app).
    assert '"npm", "ci"' in dockerfile and '"npm", "install"' not in dockerfile, dockerfile
    # The secret env is a NAME-only required guard in compose.
    assert "${APP_SECRET:?" in compose, compose


def test_benign_declaration_without_new_fields_still_accepted(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain intent that uses NONE of the new fields stays a self-hostable candidate
    with byte-stable behaviour (no install layer for a no-dependency node app)."""
    cid = _uid("conv_r2benign")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = {
        "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
        "server.js": _HTTP_SERVER,
    }
    _seed(ps, _store, cid, files, intent=ReleaseIntent(start_cmd=("node", "server.js")))

    body = _release(client, cid)
    assert body["assessment"] == "candidate" and body["self_host"] is True
    dockerfile = _overlay_texts(client, cid)[DOCKERFILE_PATH]
    # No dependencies declared → no install layer derived (byte-stable with baseline).
    assert '"npm", "install"' not in dockerfile and '"npm", "ci"' not in dockerfile
    assert '"node", "server.js"' in dockerfile

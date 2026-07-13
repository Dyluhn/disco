"""GAP G04 red — an interpreted candidate with dependencies and NO build step must
emit a runtime dependency-install layer (or fail closed), never an unrunnable image.

Boundary (plan §1.2 / §4 crit 3): every test drives the REAL FastAPI
``GET /api/projects/{cid}/release`` (+ ``/download``) routes through the ASGI app, a
real ``ProjectStore`` on a real on-disk workspace, a real host-owned intent sidecar,
and a real committed version cut. The install behaviour is asserted on the ACTUAL
bytes streamed by ``/download`` (the emitted ``Dockerfile`` inside the zip) — never on
``emit_local_compose()`` / ``_effective_install_cmd`` called directly. No
release/detect/emit function is mocked; the ONLY ``monkeypatch`` is
``ConfigStore.load`` (the config seam the settings PUT performs), never the code under
test.

The gap: a TYPED release intent describing an INTERPRETED app (``node server.js`` /
``uvicorn main:app``) with dependencies (``express`` / a ``requirements.txt``) and NO
build step yields a service with an EMPTY ``build_cmd`` and no ``install_cmd`` (the
intent path in ``detect._from_intent`` never populates one). The emitter's
``local_compose._effective_install_cmd`` returns ``()`` whenever ``build_cmd`` is empty
and no explicit ``install_cmd`` is set, so ``_node_dockerfile`` /
``_python_dockerfile`` emit COPY → CMD with NO ``npm install`` / ``pip install`` layer
between them. The declared dependencies are therefore never installed and the image is
unrunnable. The existing frozen
``test_npm_installs_before_build_and_dockerignore_excludes_node_modules`` covers only a
BUILD-requiring Vite bundle (where ``build_cmd`` is set, so the install IS derived); no
in-process test inspects the emitted Dockerfile for an install layer on a NO-BUILD
interpreted candidate.

RED on 581d1fbe: both fixtures assess ``candidate`` / ``self_host:true`` and their
emitted Dockerfile carries COPY → (ENV) → EXPOSE → CMD with NO install ``RUN`` — so
``install_present or failed_closed`` is False. The correct behaviour this test asserts
is that an install layer IS present (or the candidate fails closed); it turns GREEN once
a no-build interpreted candidate with dependencies installs them.

Randomized (plan §4 crit 8): every conversation id / title is drawn from the seeded
``closeout_name`` factory; the detector reads workspace-relative file CONTENTS only,
never the id/title, so detection cannot recognize a fixture by its name.
"""

from __future__ import annotations

import io
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
from disco.core.release.spec import ReleaseIntent
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout

# The single-service self-host overlay path set (plan §7 "complete overlay"). A
# candidate's ``/download`` zip carries ALL of these; a fail-closed ``needs_review``
# carries NONE.
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


# ---- fixtures (seeded in tmp_path from in-memory byte maps; never a fixture NAME) --

# (a) An Express service: `express` in dependencies, `start: node server.js`, and NO
# build script — an interpreted node app whose dependencies MUST be installed
# (`npm ci` / `npm install`) for the image to run. Declared via a typed intent, so the
# intent path (never the deterministic node detector, which DOES derive an install)
# owns the service and no install_cmd is populated.
_EXPRESS_FILES: dict[str, bytes] = {
    "package.json": (
        b'{"name":"svc","dependencies":{"express":"^4.19.2"},'
        b'"scripts":{"start":"node server.js"}}'
    ),
    "server.js": (
        b"const express = require('express');\n"
        b"const app = express();\n"
        b"app.get('/', (_q, r) => r.send('ok'));\n"
        b"app.listen(process.env.PORT);\n"
    ),
}
_EXPRESS_INTENT = ReleaseIntent(start_cmd=("node", "server.js"))
# The JSON exec-array forms `_run_line` emits for the two accepted node installs
# (`RUN ["npm", "ci"]` when a lockfile is present, else `RUN ["npm", "install"]`) — the
# same quoted form the frozen §8.6 test pins.
_EXPRESS_INSTALL_MARKERS = ('"npm", "ci"', '"npm", "install"')

# (b) A FastAPI service: a `requirements.txt` (fastapi + uvicorn), a `uvicorn main:app`
# start, and NO build step — an interpreted python app whose dependencies MUST be
# installed (`pip install -r requirements.txt`) for the image to run.
_FASTAPI_FILES: dict[str, bytes] = {
    "requirements.txt": b"fastapi\nuvicorn\n",
    "main.py": (
        b"from fastapi import FastAPI\n"
        b"app = FastAPI()\n"
        b"@app.get('/')\n"
        b"def root():\n"
        b"    return {'ok': True}\n"
    ),
}
_FASTAPI_INTENT = ReleaseIntent(
    start_cmd=("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}")
)
# The JSON exec-array form `_run_line` emits for the python install.
_FASTAPI_INSTALL_MARKERS = ('"pip", "install"',)


# ---- harness (real ASGI app + real ProjectStore; only ConfigStore.load seamed) -----
# Copied VERBATIM from test_c4_env_build_toolchain_matrix.py.


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
    conversation record — then cut a real version so the live tree matches a committed
    ``VersionRecord`` (the C2 source binding is satisfied and env/build lowering is the
    only thing under test)."""
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


def _overlay_texts(client: TestClient, cid: str) -> dict[str, str]:
    """The full ``path -> text`` map of the project's ``/download`` zip. A candidate's
    zip carries the self-host overlay (``Dockerfile`` / ``compose.yaml`` / …); a
    fail-closed project's zip carries none, so the caller uses ``.get(path, "")``."""
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name).decode("utf-8", errors="replace")
    return out


def _overlay_present(texts: dict[str, str]) -> frozenset[str]:
    return frozenset(_OVERLAY_NAMES & set(texts))


# ===========================================================================
# G04 — a no-build interpreted candidate with declared dependencies installs them.
# ===========================================================================


@pytest.mark.parametrize(
    ("files", "intent", "install_markers"),
    [
        (_EXPRESS_FILES, _EXPRESS_INTENT, _EXPRESS_INSTALL_MARKERS),
        (_FASTAPI_FILES, _FASTAPI_INTENT, _FASTAPI_INSTALL_MARKERS),
    ],
    ids=["express_node_no_build", "fastapi_python_no_build"],
)
def test_interpreted_candidate_with_deps_emits_dependency_install_layer(
    files: dict[str, bytes],
    intent: ReleaseIntent,
    install_markers: tuple[str, ...],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """GAP G04 — RED on 581d1fbe.

    An interpreted app declared by a typed intent (dependencies, NO build step) must
    install its dependencies in the emitted Dockerfile — an ``npm ci``/``npm install``
    layer (Express) or a ``pip install`` layer (FastAPI) — OR fail closed. On 581d1fbe
    the intent path builds a service with an empty ``build_cmd`` and no ``install_cmd``,
    so ``_effective_install_cmd`` returns ``()`` and the emitted Dockerfile jumps from
    COPY straight to CMD with NO install ``RUN``: the declared dependencies are never
    installed and the image is unrunnable. The candidate is nonetheless
    ``self_host:true`` — neither an install layer nor a fail-closed verdict — so this
    assertion fails RED."""
    cid = _cid(closeout_name, "conv_g04")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), files, intent=intent)

    body = _release(client, cid)
    assert isinstance(body, dict)
    texts = _overlay_texts(client, cid)
    dockerfile = texts.get(DOCKERFILE_PATH, "")

    install_present = any(marker in dockerfile for marker in install_markers)
    failed_closed = body["self_host"] is False and _overlay_present(texts) == frozenset()

    assert install_present or failed_closed, (
        "an interpreted candidate declared via a typed intent (declared dependencies, "
        "NO build step) emitted NO dependency-install layer and did NOT fail closed: "
        f"assessment={body['assessment']!r}, self_host={body['self_host']!r}, "
        f"blockers={sorted(_blocker_codes(body))}. `_effective_install_cmd` returns () "
        "when build_cmd is empty and no install_cmd is set, so the declared dependencies "
        f"(expected an install layer matching one of {install_markers}) are never "
        f"installed and the image is unrunnable. Emitted Dockerfile:\n{dockerfile}"
    )

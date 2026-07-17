"""R7 live-lane defect D2 — a typed-intent process start must honor the port contract.

Reproduced live (2026-07-17 R7 diagnostic at c0c4f728): the frozen fastapi fixture's
intent declares ``start_cmd=("uvicorn", "main:app")``; the bundle emitted that argv
verbatim as an exec-form ``CMD``, uvicorn bound its defaults (127.0.0.1:8000), and the
container never answered the emitted contract (compose ``PORT=8080`` + EXPOSE +
loopback healthcheck) — "ingress never reached 200". The source-detection path already
normalizes exactly this shape (``uvicorn main:app --host 0.0.0.0 --port ${PORT}``).

These tests live OUTSIDE the frozen closeout dirs (no ``export_track1_closeout``
marker) and drive the SAME public boundary the frozen suites use: the real
``/api/projects/{cid}/release`` + ``/download`` routes over a real ``ProjectStore``;
only ``ConfigStore.load`` is seamed. Assertions are on the actual downloaded
``Dockerfile`` bytes, never on ``detect``/``emit`` called directly.

The invariant is NARROW by design (owner policy: no general command interpreter):
ONLY a start argv whose head is ``uvicorn`` with neither ``--host`` nor ``--port``
(spaced or ``=``-form) is normalized; every owner-declared binding wins verbatim, and
non-uvicorn commands are never touched.
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
from disco.core.release.local_compose import DOCKERFILE_PATH
from disco.core.release.spec import ReleaseIntent
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

_ID_COUNTER = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}_{next(_ID_COUNTER)}"


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
        created_at="2026-07-17T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=False,
    )
    ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id="local", title=cid, surface="build")
    cut = ps.cut_version(cid, trigger="r7d2")
    assert cut is not None and cut.seq == 1


def _dockerfile(client: TestClient, cid: str) -> str:
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        return zf.read(DOCKERFILE_PATH).decode("utf-8")


# A DIFFERENT FastAPI shape than the frozen live fixture (extra dep, distinct body), so
# the fix cannot be a fixture special-case.
_FASTAPI_FILES: dict[str, bytes] = {
    "requirements.txt": b"fastapi\nuvicorn\nhttpx\n",
    "main.py": (
        b"from fastapi import FastAPI\n"
        b"app = FastAPI()\n"
        b"@app.get('/')\n"
        b"def root():\n"
        b"    return {'ok': True}\n"
    ),
}

_NODE_FILES: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "server.js": b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n",
}


def test_bare_uvicorn_intent_start_lowers_the_port_contract(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced live defect: a bare ``uvicorn main:app`` intent must emit a CMD
    that binds ``0.0.0.0:${PORT}`` (shell-wrapped so the reference expands at runtime),
    or the shipped container can never satisfy its own health contract."""
    cid = _uid("conv_r7d2bare")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _FASTAPI_FILES, intent=ReleaseIntent(start_cmd=("uvicorn", "main:app")))

    dockerfile = _dockerfile(client, cid)
    assert "'--host' '0.0.0.0'" in dockerfile, dockerfile
    # Inside the JSON-rendered CMD array the reference's shell quotes are escaped:
    # CMD ["sh", "-c", "exec 'uvicorn' 'main:app' '--host' '0.0.0.0' '--port' \"${PORT}\""]
    assert "'--port' \\\"${PORT}\\\"" in dockerfile, dockerfile
    # The reference must be a runtime shell expansion, never an inert exec-array literal.
    assert '"sh", "-c"' in dockerfile, dockerfile


def test_fully_bound_uvicorn_intent_stays_verbatim(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSITIVE control: an owner-declared binding wins — no duplicate flags appended."""
    cid = _uid("conv_r7d2full")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(
        ps,
        _store,
        cid,
        _FASTAPI_FILES,
        intent=ReleaseIntent(
            start_cmd=("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}")
        ),
    )

    dockerfile = _dockerfile(client, cid)
    assert dockerfile.count("--port") == 1, dockerfile
    assert dockerfile.count("--host") == 1, dockerfile


def test_declared_eq_form_port_disables_normalization(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``=``-form owner binding (``--port=${PORT}``) is a declared binding too."""
    cid = _uid("conv_r7d2eq")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(
        ps,
        _store,
        cid,
        _FASTAPI_FILES,
        intent=ReleaseIntent(start_cmd=("uvicorn", "main:app", "--host=0.0.0.0", "--port=${PORT}")),
    )

    dockerfile = _dockerfile(client, cid)
    assert dockerfile.count("--port") == 1, dockerfile
    assert "'--port' " not in dockerfile, dockerfile  # no appended spaced pair


def test_non_uvicorn_start_is_never_touched(
    _store: SqliteEventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRESERVATION: a node process intent emits its argv verbatim (the app owns its
    ``process.env.PORT`` read; nothing is appended to non-uvicorn commands)."""
    cid = _uid("conv_r7d2node")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _NODE_FILES, intent=ReleaseIntent(start_cmd=("node", "server.js")))

    dockerfile = _dockerfile(client, cid)
    assert 'CMD ["node", "server.js"]' in dockerfile, dockerfile
    assert "--host" not in dockerfile and "--port" not in dockerfile, dockerfile

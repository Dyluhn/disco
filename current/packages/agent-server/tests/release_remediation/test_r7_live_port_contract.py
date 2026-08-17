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

The invariant is NARROW by design (owner policy: no general command interpreter).
C9-04 (2026-07-17) tightened it into a recognized-shape/arity CONTRACT: exactly one
valid ``module:attr`` APP target; only ``--host``/``--port`` accepted (each once, each
with a value); NO binding -> the platform appends ``0.0.0.0`` + ``${port_env}``;
a COMPLETE binding wins verbatim; every other recognizably-uvicorn shape (bare
``uvicorn``, flag-only, dangling values, unsupported options, extra/malformed
operands, path-qualified/case-variant executables, ``python -m uvicorn``) fails
CLOSED with the typed ``uvicorn_start_incoherent`` blocker through the REAL
release/download boundary — no self-host bundle is offered. Non-uvicorn commands are
never touched.
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


def _release_body(client: TestClient, cid: str) -> dict[str, object]:
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)
    return body


def _assert_failed_closed(client: TestClient, cid: str, body: dict[str, object]) -> None:
    """needs_review + self_host:false + the typed uvicorn blocker + NO overlay offered
    through the real download — the full no-self-host-bundle proof."""
    assert body["assessment"] == "needs_review", body
    assert body["self_host"] is False and body["spec_digest"] is None, body
    blockers = body["blockers"]
    assert isinstance(blockers, list)
    codes = {str(b["code"]) for b in blockers}
    assert "uvicorn_start_incoherent" in codes, sorted(codes)
    dl = client.get(f"/api/projects/{cid}/download")
    assert dl.status_code == 200, dl.text
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert _OVERLAY_NAMES & set(zf.namelist()) == frozenset(), zf.namelist()


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


_INCOHERENT_UVICORN_INTENTS: list[tuple[str, tuple[str, ...]]] = [
    ("bare-no-app", ("uvicorn",)),
    ("factory-flag-only", ("uvicorn", "--factory")),
    ("workers-no-app", ("uvicorn", "--workers", "2")),
    ("dangling-workers", ("uvicorn", "main:app", "--workers")),
    ("only-host", ("uvicorn", "main:app", "--host", "0.0.0.0")),
    ("only-port", ("uvicorn", "main:app", "--port", "${PORT}")),
    ("dangling-port", ("uvicorn", "main:app", "--port")),
    ("module-form", ("python", "-m", "uvicorn", "main:app")),
    ("case-variant-exe", ("UVICORN", "main:app")),
    ("path-qualified-exe", ("/usr/local/bin/uvicorn", "main:app")),
    ("extra-positional", ("uvicorn", "main:app", "other:app")),
    ("malformed-app", ("uvicorn", "notanapp")),
    ("duplicate-host", ("uvicorn", "main:app", "--host", "a", "--host", "b", "--port", "1")),
    # C9-04 verifier ordering fix: an option unknown to the older generic grammar must
    # still get uvicorn_start_incoherent (not toolchain_unsupported).
    ("unix-socket-opt", ("uvicorn", "main:app", "--uds", "app.sock")),
    ("reload-opt", ("uvicorn", "main:app", "--reload")),
    ("log-level-opt", ("uvicorn", "main:app", "--log-level", "debug")),
    # C9-04 owner round: a COMPLETE binding whose VALUES do not match the platform
    # contract (host 0.0.0.0 + port ${PORT}) is a predictably-broken candidate and must
    # fail closed, NOT be accepted verbatim.
    ("host-localhost", ("uvicorn", "main:app", "--host=localhost", "--port=${PORT}")),
    ("host-loopback-spaced", ("uvicorn", "main:app", "--host", "127.0.0.1", "--port", "${PORT}")),
    ("port-literal-9999", ("uvicorn", "main:app", "--host=0.0.0.0", "--port=9999")),
    ("port-literal-8080", ("uvicorn", "main:app", "--host=0.0.0.0", "--port=8080")),
    ("port-other-var", ("uvicorn", "main:app", "--host=0.0.0.0", "--port=${OTHER}")),
    ("port-negative", ("uvicorn", "main:app", "--host=0.0.0.0", "--port=-1")),
    ("port-spaced-literal", ("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000")),
]


@pytest.mark.parametrize(
    "case_id,start_cmd",
    _INCOHERENT_UVICORN_INTENTS,
    ids=[c[0] for c in _INCOHERENT_UVICORN_INTENTS],
)
def test_incoherent_uvicorn_intents_fail_closed(
    case_id: str,
    start_cmd: tuple[str, ...],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C9-04 pinned negatives: every recognizably-uvicorn shape outside the supported
    contract predictably exits or binds the wrong interface at runtime, so it must be
    needs_review with the typed blocker and NO self-host bundle — proven through the
    real release/download boundary."""
    cid = _uid(f"conv_r7d2neg_{case_id}".replace("-", "_"))
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, _FASTAPI_FILES, intent=ReleaseIntent(start_cmd=start_cmd))
    _assert_failed_closed(client, cid, _release_body(client, cid))

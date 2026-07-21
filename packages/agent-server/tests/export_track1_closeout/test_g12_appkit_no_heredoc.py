"""G12 red — the AppKit (dev_server) Dockerfile must not use a heredoc ``COPY``.

REMEDIATION gap G12 (Export Track-1 Closeout). The AppKit interim local-run overlay
(``_emit_dev_server_overlay`` → ``_dev_server_dockerfile``) materializes its
secret-writing container entrypoint with a Dockerfile heredoc — ``COPY <<'DISCO_
ENTRYPOINT' /usr/local/bin/disco-entrypoint.sh``. A heredoc ``COPY <<...`` is a
BuildKit/Buildx-only Dockerfile feature: it is rejected by the legacy builder. But the
bundle's own ``SELFHOST.md`` and the live guard (anchor
``local_compose._dev_server_dockerfile``) declare only "Docker Engine with the Compose
v2 plugin" as the prerequisite — NOT BuildKit/Buildx — so on a host that satisfies
exactly the declared prerequisite the AppKit image fails to build.

No frozen test asserts the emitted AppKit Dockerfile is heredoc-free; this test closes
that gap. (R5/G12 is PARKED — R0 only LANDS the red; production is NOT fixed here.)

Boundary (plan §1.2): the PUBLIC boundary only — the REAL FastAPI ``GET /release`` +
``/download`` routes through the ASGI app, a real ``ProjectStore`` on a real on-disk
workspace, and a real committed version cut; the emitted ``Dockerfile`` bytes are read
from the real download zip and inspected in-process. No release/detect/emit/route
function is mocked; the ONLY ``monkeypatch`` is ``ConfigStore.load`` (the config seam a
settings PUT performs). The ``_client`` / ``_seed`` / ``_download_compose`` harness is
copied VERBATIM from ``test_c7_topology_matrix.py``; the AppKit candidate fixture
(``_APPKIT_BASE``) is copied VERBATIM from ``test_c6_collision_matrix.py`` (WO-C3's
proven AppKit ``dev_server`` positive candidate).

EXPECTED RED on 581d1fbe: the emitted AppKit ``Dockerfile`` contains a heredoc
``COPY <<'DISCO_ENTRYPOINT' …`` line. This test asserts the CORRECT behavior — the
emitted AppKit Dockerfile uses NO heredoc ``COPY`` (Engine + Compose-v2 compatible) —
and so it is RED on 581dfe.

Randomized (plan §4 crit 8): the conversation id / title come from the seeded
``closeout_name`` factory; the detector reads workspace file CONTENTS only, never the
id/title, so detection cannot recognize a fixture by name.
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
from disco.core.release.local_compose import COMPOSE_PATH, DOCKERFILE_PATH
from disco.core.release.spec import ReleaseIntent
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout


# ---- AppKit dev_server positive candidate — VERBATIM from test_c6_collision_matrix.
#
# WO-C3's proven AppKit (dev_server) positive candidate: a self-hostable candidate
# with a complete overlay on this baseline (a wrangler.toml + a D1 binding + a
# checked-in schema.sql + a worker entry).
_APPKIT_BASE: dict[str, bytes] = {
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


# ---- PUBLIC-boundary harness — VERBATIM from test_c7_topology_matrix.py ----------


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
    title: str,
    *,
    files: dict[str, bytes],
    intent: ReleaseIntent | None = None,
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


def _cid(make_name: object, prefix: str) -> str:
    assert callable(make_name)
    return str(make_name(prefix))


def _download_compose(client: TestClient, cid: str) -> dict[str, object]:
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    assert res.headers.get("content-type") == "application/zip", res.headers
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = zf.namelist()
        assert COMPOSE_PATH in names, f"download zip is missing {COMPOSE_PATH!r}: {names}"
        parsed = yaml.safe_load(zf.read(COMPOSE_PATH))
    assert isinstance(parsed, dict), "downloaded compose.yaml did not parse to a mapping"
    return parsed


def _download_dockerfile(client: TestClient, cid: str) -> bytes:
    """The emitted ``Dockerfile`` bytes from the REAL ``/download`` zip — the same
    real-boundary zip read as ``_download_compose``, for the Dockerfile member."""
    res = client.get(f"/api/projects/{cid}/download")
    assert res.status_code == 200, res.text
    assert res.headers.get("content-type") == "application/zip", res.headers
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = zf.namelist()
        assert DOCKERFILE_PATH in names, f"download zip is missing {DOCKERFILE_PATH!r}: {names}"
        return zf.read(DOCKERFILE_PATH)


# ---- the invariant under test ---------------------------------------------------


def _heredoc_copy_lines(dockerfile: str) -> list[str]:
    """Every emitted ``COPY`` instruction that uses a heredoc redirect (``COPY <<EOF``
    / ``COPY <<'EOF'`` / ``COPY <<-EOF``). Such a form requires BuildKit/Buildx and is
    rejected by the legacy Docker Engine builder the bundle's prerequisites declare."""
    offending: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY") and "<<" in stripped:
            offending.append(stripped)
    return offending


def test_g12_appkit_dockerfile_has_no_heredoc_copy(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """G12 — RED on 581dfe, at the PUBLIC boundary.

    An AppKit (``dev_server``) candidate's emitted ``Dockerfile`` must contain NO
    heredoc ``COPY <<...`` instruction, because the exported bundle declares only
    "Docker Engine with the Compose v2 plugin" as its prerequisite (NOT BuildKit /
    Buildx), and the heredoc ``COPY`` is a BuildKit-only feature. Through the REAL
    ``/release`` + ``/download``, the emitted AppKit ``Dockerfile`` is inspected.

    Baseline (581dfe) emits ``COPY <<'DISCO_ENTRYPOINT' /usr/local/bin/
    disco-entrypoint.sh`` to inline the secret-writing entrypoint, so this test is
    RED."""
    cid = _cid(closeout_name, "conv_g12appkit")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(ps, _store, cid, title=_cid(closeout_name, "proj"), files=dict(_APPKIT_BASE))

    body = client.get(f"/api/projects/{cid}/release").json()
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True, (
        "precondition: the AppKit dev_server fixture must be a self-hostable candidate "
        f"(so a real overlay Dockerfile is emitted); got {body.get('assessment')!r} / "
        f"self_host={body.get('self_host')!r}."
    )

    # Sanity: the download carries a real, parseable self-host overlay (a compose
    # document with a `services` mapping) — the context in which the Dockerfile is
    # emitted. (Uses the verbatim `_download_compose` real-boundary reader.)
    compose = _download_compose(client, cid)
    assert isinstance(compose.get("services"), dict), "the AppKit overlay lacks a services map"

    dockerfile = _download_dockerfile(client, cid).decode("utf-8")
    offending = _heredoc_copy_lines(dockerfile)
    assert not offending, (
        "the emitted AppKit Dockerfile uses a heredoc `COPY <<...`, which requires "
        "BuildKit/Buildx, but the exported bundle declares only Docker Engine + the "
        f"Compose v2 plugin as its prerequisite — offending instruction(s): {offending}. "
        "The AppKit image therefore fails to build on a host that satisfies exactly the "
        "declared prerequisite; the entrypoint must be materialized without a heredoc COPY."
    )

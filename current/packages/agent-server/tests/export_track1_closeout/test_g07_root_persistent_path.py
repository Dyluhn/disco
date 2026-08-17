"""G07 red — a filesystem-ROOT persistent path is accepted but not backed.

REMEDIATION gap G07 (Export Track-1 Closeout). A resource whose ``persistent_path``
is a filesystem-ROOT file (``/app.db`` — its parent directory is ``/``) is accepted
as a self-host ``candidate``, but the emitted Compose mounts the resource's named
volume at ``/data`` (the ``local_mount_target`` root-level fallback, spec.py's carried
``O1``), NOT at the file's parent ``/``. So ``/app.db`` lives on the container's
EPHEMERAL layer — a volume mounted at ``/data`` does not back a file at ``/app.db`` —
and the app's persistent state silently does not survive a restart, while
``self_host:true`` promises it does.

The existing ``test_c7_topology_matrix.py::test_persistent_path_must_be_absolute_
normalized_posix`` covers a relative / ``//`` / ``/./`` spelling, but NOT a
filesystem-ROOT file whose parent is ``/`` (which cannot be volume-mounted without
shadowing ``/``). This test closes that gap.

Boundary (plan §1.2): the PUBLIC boundary only — the REAL FastAPI ``GET /release`` +
``/download`` routes through the ASGI app, a real ``ProjectStore`` on a real on-disk
workspace, a real committed version cut, and the real host-owned ``release-intent``
sidecar. The emitted ``compose.yaml`` bytes are parsed IN-PROCESS with PyYAML (the
repo's yaml lib). No release/detect/emit/route function is mocked; the ONLY
``monkeypatch`` is ``ConfigStore.load`` (the config seam a settings PUT performs). The
``_client`` / ``_seed`` / ``_download_compose`` / ``_intent_resource`` harness is
copied VERBATIM from ``test_c7_topology_matrix.py``.

EXPECTED RED on 581d1fbe: ``/release`` returns ``self_host:true`` for a resource at
``/app.db`` and the emitted compose mounts the volume at ``/data`` ONLY, so ``/app.db``
is not backed. This test asserts the CORRECT behavior — the release either FAILS
CLOSED with a typed repair blocker OR emits a volume that actually backs ``/app.db`` —
and so it is RED on 581d1fbe (``self_host:true`` with a ``/data``-only mount).

Randomized (plan §4 crit 8): the conversation id / title come from the seeded
``closeout_name`` factory; the detector reads workspace file CONTENTS only, never the
id/title, so detection cannot recognize a fixture by name. The root-level
``persistent_path`` under test is a structural resource property, not a fixture
identity.
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
from disco.core.release.local_compose import COMPOSE_PATH
from disco.core.release.spec import (
    LocalResourceProfile,
    ReleaseIntent,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.export_track1_closeout


# ---- compose-document accessors (typed, defensive) — VERBATIM from C7 -----------


def _service_block(compose: dict[str, object], sid: str) -> dict[str, object]:
    services = compose["services"]
    assert isinstance(services, dict), "compose.services is not a mapping"
    assert sid in services, f"service {sid!r} absent from the emitted compose document"
    block = services[sid]
    assert isinstance(block, dict), f"compose service {sid!r} is not a mapping"
    return block


def _mount_targets(block: dict[str, object]) -> dict[str, str]:
    """`{container_target: volume_name}` for a service's ``volumes:`` list. Each
    entry is a ``<volume>:<dir>`` string; a target maps to exactly one volume."""
    vols = block.get("volumes", [])
    assert isinstance(vols, list), "service volumes is not a sequence"
    out: dict[str, str] = {}
    for entry in vols:
        assert isinstance(entry, str), "a service volume entry is not a scalar string"
        name, sep, target = entry.partition(":")
        assert sep, f"malformed volume mount entry {entry!r}"
        out[target] = name
    return out


def _top_volume_names(compose: dict[str, object]) -> set[str]:
    vols = compose.get("volumes", {})
    assert isinstance(vols, dict), "top-level volumes is not a mapping"
    return {str(key) for key in vols}


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


def _intent_resource(
    rid: str,
    *,
    path: str,
    url: str,
    volume: str,
    consumers: tuple[str, ...],
    migrate: tuple[str, ...] = (),
) -> ResourceDecl:
    return ResourceDecl(
        id=rid,
        kind=ResourceKind.sqlite,
        persistent_path=path,
        profiles=ResourceProfiles(local=LocalResourceProfile(url=url, volume=volume)),
        consumers=consumers,
        migrate_cmd=migrate,
    )


# ---- the invariant under test ---------------------------------------------------


def _persistent_path_is_backed(mount_targets: set[str], persistent_path: str) -> bool:
    """Whether some mounted container directory actually PERSISTS ``persistent_path``.

    A named volume mounted at container directory ``D`` persists the whole subtree
    rooted at ``D``. A file ``F`` therefore survives a restart iff it lives within a
    mounted directory: ``D`` is the filesystem root (``/``), ``F`` IS ``D``, or ``F``
    is strictly under ``D`` (``F`` starts with ``D`` + ``/``). For a filesystem-ROOT
    file like ``/app.db`` (whose parent is ``/``) ONLY a mount at ``/`` backs it — a
    volume mounted at ``/data`` does NOT."""
    for target in mount_targets:
        directory = target.rstrip("/")
        if directory == "":  # a volume mounted at the filesystem root ("/")
            return True
        if persistent_path == directory or persistent_path.startswith(directory + "/"):
            return True
    return False


@pytest.mark.parametrize(
    "persistent_path",
    ["/app.db", "/db.sqlite"],
    ids=["root-file-app-db", "root-file-db-sqlite"],
)
def test_g07_root_persistent_path_backed_or_fails_closed(
    persistent_path: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """G07 — RED on 581d1fbe, at the PUBLIC boundary.

    A typed intent declares ONE sqlite resource whose ``persistent_path`` is a
    filesystem-ROOT file (parent directory ``/``), consumed by the sole ``web``
    ingress. Through the REAL ``/release`` + ``/download``, the correct behavior is
    ONE of two things:

      * FAIL CLOSED — ``self_host:false`` / ``needs_review`` with a typed repair
        blocker (a root-level path cannot be volume-backed without shadowing ``/``); or
      * emit a volume that ACTUALLY backs the persistent path (a mount whose directory
        contains the file).

    It must NEVER be ``self_host:true`` with a volume mounted only at ``/data`` (or any
    other directory that does not contain the file). Baseline (581dfe) returns
    ``self_host:true`` and mounts ``<volume>:/data`` — the ``local_mount_target``
    root-level fallback — so ``/app.db`` is on the ephemeral layer and this test is
    RED."""
    cid = _cid(closeout_name, "conv_g07root")
    client, ps = _client(_store, tmp_path, monkeypatch)
    intent = ReleaseIntent(
        start_cmd=("node", "server.js"),
        resources=(
            _intent_resource(
                "db_root",
                path=persistent_path,
                url=f"file:{persistent_path}",
                volume="app-data",
                consumers=("web",),
            ),
        ),
    )
    _seed(
        ps,
        _store,
        cid,
        title=_cid(closeout_name, "proj"),
        files={"package.json": b'{"name":"svc"}', "server.js": b"x\n"},
        intent=intent,
    )

    body = client.get(f"/api/projects/{cid}/release").json()
    assert isinstance(body, dict)

    if body["self_host"] is not True:
        # ACCEPTED correct behavior: a root-level persistent path that cannot be
        # volume-backed must fail closed with a typed repair blocker — never a
        # self-host-labelled bundle whose state silently does not persist.
        assert body["assessment"] == "needs_review", (
            f"a non-self-host verdict for a root-level persistent path {persistent_path!r} "
            f"must be needs_review; got {body['assessment']!r}."
        )
        blockers = body["blockers"]
        assert isinstance(blockers, list) and blockers, (
            "a fail-closed root-level persistent path must carry a typed repair blocker "
            "explaining that the path cannot be volume-backed; got no blocker."
        )
        return

    # self_host:true → the ONLY correct candidate behavior is a volume that BACKS the
    # persistent path. A /data-only mount leaves a root-level file unbacked.
    compose = _download_compose(client, cid)
    web = _service_block(compose, "web")
    targets = set(_mount_targets(web))
    assert _persistent_path_is_backed(targets, persistent_path), (
        f"self_host:true but the persistent path {persistent_path!r} is not backed by any "
        f"mounted volume — the consuming service mounts volume(s) at {sorted(targets)} "
        f"(top-level volumes {sorted(_top_volume_names(compose))}), none of which contains "
        f"{persistent_path!r}. A filesystem-ROOT persistent path falls back to a /data-only "
        "mount (local_mount_target's root-level 'O1' fallback), leaving the data on the "
        "container's ephemeral layer while self_host:true promises it persists. The release "
        "must instead fail closed with a typed repair blocker or back the path exactly."
    )

"""WO-C7 red matrix — coherent multi-service resource + env topology.

Plan §11 (WO-C7): a released app's resources and resource-bound environment must
follow the declared CONSUMER topology — a service receives only the mounts and
env it consumes (plan §2.9), distinct resources may not collide on a persistent
path / mount target, every resource has at least one valid consumer, a
multi-service spec may not fan an unbound env var out implicitly, and the emitted
Compose document must equal the spec topology exactly.

Boundary (plan §1.2):
  * The PUBLIC-boundary tests drive the REAL FastAPI ``GET /release`` + ``/download``
    routes through the ASGI app, a real ``ProjectStore`` on a real on-disk
    workspace, and the real host-owned ``release-intent`` sidecar; they parse the
    emitted ``compose.yaml`` bytes IN-PROCESS with PyYAML (the repo's yaml lib) and
    assert topology. No release/detect/emit/route function is mocked; the ONLY
    ``monkeypatch`` is ``ConfigStore.load`` (a config seam, exactly the injection a
    settings PUT performs).
  * The multi-service ``web + worker`` topology of plan §11.1/2/8 CANNOT be expressed
    through the current ``ReleaseIntent`` (it has no ``services`` field, and the
    detector derives a single ``web`` ingress — see ``detect.py`` / ``release_declare.py``).
    That inexpressiveness is itself a RED. Those criteria are therefore asserted at
    the real spec + emitter boundary: a real ``ReleaseSpec`` is built through the
    real ``ReleaseSpec.model_validate`` assembler and lowered through the real
    ``emit_local_compose`` — the code under test, never a mock — and the emitted
    ``compose.yaml`` is parsed in-process. Pure schema-validation criteria call the
    real ``ReleaseSpec`` validator directly.

DEFERRED to the C8/live lane (noted, not asserted here):
  * §11.7's ``docker compose config --format json`` round-trip — this file does the
    in-process YAML parse; the real Compose CLI round-trip is a live-Docker proof.
  * §11.10 AppKit state-volume / migration live determinism — a running-container
    persistence proof, not a static topology assertion.

RED vs GREEN on baseline ``a8e3e710``:
  * RED — the local-compose emitter mounts EVERY resource into EVERY service and
    injects EVERY runtime env var (incl. resource-bound ones) into every service,
    ignoring ``consumers`` entirely (``_resource_mounts`` / ``_scope_env`` fan out);
    the one-shot ``migrate`` service mounts every resource, not just the one it
    migrates; and the ``ReleaseSpec`` schema does not reject duplicate persistent
    paths / mount targets, url/path disagreement, empty consumers, self/cyclic
    ``depends_on``, a non-absolute/non-normalized ``persistent_path``, or a
    multi-service unbound env var with no consumers, and cannot yet express a
    per-env ``consumers`` scope at all.
  * GREEN (preservation) — a single-service candidate mounts/injects its sole
    resource correctly with exactly one named volume; a generic ``depends_on``
    coexists with the migration ordering; duplicate DECLARED volume names at
    distinct paths are disambiguated so state cannot collapse; unknown consumer /
    unknown env-binding references already fail schema validation; a single-service
    unbound env var may default to the sole ingress.

Randomized (plan §4 crit 8): PUBLIC-boundary conversation ids / titles come from the
seeded ``closeout_name`` factory; the detector reads workspace-relative file
CONTENTS only, never the id/title, so detection cannot recognize a fixture by name.
The spec/emitter tests operate on structural service/resource ids (``web`` /
``worker`` / ``db``) that ARE the topology under test, not fixture identities.
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
from disco.core.release.local_compose import COMPOSE_PATH, emit_local_compose
from disco.core.release.spec import (
    LocalResourceProfile,
    ReleaseIntent,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
)
from disco.tools.projects import ProjectStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

pytestmark = pytest.mark.export_track1_closeout

# A valid `store.VersionRecord`-shaped digest (a bare 64-char lowercase sha256) so a
# hand-built ReleaseSpec pins to a syntactically real snapshot — the SOURCE binding
# is not what WO-C7 tests, so a constant is honest here.
_DIGEST = "a" * 64

_PROVENANCE: dict[str, object] = {
    "detector": "disco.core.release.detect",
    "detector_version": "1",
    "assessment": "candidate",
}


# ---- spec payload builders (fed to the REAL ReleaseSpec.model_validate) ---------


def _svc(
    sid: str,
    *,
    role: str = "ingress",
    runtime: str = "node",
    start: tuple[str, ...] = ("node", "server.js"),
    depends_on: tuple[str, ...] = (),
) -> dict[str, object]:
    block: dict[str, object] = {
        "id": sid,
        "role": role,
        "runtime": runtime,
        "start_cmd": list(start),
    }
    if depends_on:
        block["depends_on"] = list(depends_on)
    return block


def _resource(
    rid: str,
    *,
    path: str,
    url: str,
    volume: str,
    consumers: tuple[str, ...],
    migrate: tuple[str, ...] = (),
) -> dict[str, object]:
    block: dict[str, object] = {
        "id": rid,
        "kind": "sqlite",
        "persistent_path": path,
        "profiles": {"local": {"url": url, "volume": volume}},
        "consumers": list(consumers),
    }
    if migrate:
        block["migrate_cmd"] = list(migrate)
    return block


def _envvar(
    name: str,
    *,
    scope: str = "runtime",
    secret: str = "public",
    binding: str | None = None,
    consumers: tuple[str, ...] | None = None,
) -> dict[str, object]:
    block: dict[str, object] = {"name": name, "scope": scope, "secret": secret}
    if binding is not None:
        block["binding"] = binding
    if consumers is not None:
        # The FUTURE per-env consumer scope (§11.8/§11.11). Absent from the v1
        # schema, so a baseline `model_validate` rejects it (extra="forbid").
        block["consumers"] = list(consumers)
    return block


def _spec_payload(
    *,
    services: list[dict[str, object]],
    env: list[dict[str, object]] | None = None,
    resources: list[dict[str, object]] | None = None,
    name: str = "app",
) -> dict[str, object]:
    return {
        "kind": "node",
        "name": name,
        "version_seq": 1,
        "tree_digest": _DIGEST,
        "services": services,
        "env": env or [],
        "resources": resources or [],
        "provenance": dict(_PROVENANCE),
    }


def _emit_compose(spec: ReleaseSpec) -> dict[str, object]:
    """Lower a real ReleaseSpec through the REAL emitter and parse the emitted
    ``compose.yaml`` bytes in-process with PyYAML."""
    overlay = emit_local_compose(spec)
    parsed = yaml.safe_load(overlay[COMPOSE_PATH])
    assert isinstance(parsed, dict), "emitted compose.yaml did not parse to a mapping"
    return parsed


# ---- compose-document accessors (typed, defensive) ------------------------------


def _service_block(compose: dict[str, object], sid: str) -> dict[str, object]:
    services = compose["services"]
    assert isinstance(services, dict), "compose.services is not a mapping"
    assert sid in services, f"service {sid!r} absent from the emitted compose document"
    block = services[sid]
    assert isinstance(block, dict), f"compose service {sid!r} is not a mapping"
    return block


def _service_ids(compose: dict[str, object]) -> set[str]:
    services = compose["services"]
    assert isinstance(services, dict)
    return {str(key) for key in services}


def _env_of(block: dict[str, object]) -> dict[str, str]:
    env = block.get("environment", {})
    assert isinstance(env, dict), "service environment is not a mapping"
    return {str(name): str(value) for name, value in env.items()}


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


def _depends_keys(block: dict[str, object]) -> set[str]:
    dep = block.get("depends_on", {})
    assert isinstance(dep, dict), "service depends_on is not a mapping"
    return {str(key) for key in dep}


def _top_volume_names(compose: dict[str, object]) -> set[str]:
    vols = compose.get("volumes", {})
    assert isinstance(vols, dict), "top-level volumes is not a mapping"
    return {str(key) for key in vols}


# =================================================================================
# PUBLIC BOUNDARY — real GET /release + /download, parse the emitted compose.yaml.
# =================================================================================


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


def test_public_single_service_sqlite_mount_and_env(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """§11.5 + the single-service sole-ingress invariant — GREEN preservation.

    A node candidate whose contents reference ``DATABASE_URL=file:`` detects a single
    ``web`` ingress + one sqlite resource. Through the REAL ``/release`` +
    ``/download``, the parsed ``compose.yaml`` mounts EXACTLY ONE named volume for
    that resource into the sole service and injects the bound ``DATABASE_URL`` — a
    single-service spec correctly defaults its resource to its sole ingress. Already
    correct on baseline; the WO-C7 fix must keep it green."""
    cid = _cid(closeout_name, "conv_c7pub1")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed(
        ps,
        _store,
        cid,
        title=_cid(closeout_name, "proj"),
        files={
            "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
            "server.js": b"require('http').createServer().listen(process.env.PORT);\n",
            "drizzle.config.ts": b'// DATABASE_URL = "file:/data/app.db"\n',
        },
    )

    body = client.get(f"/api/projects/{cid}/release").json()
    assert body["assessment"] == "candidate" and body["self_host"] is True, body

    compose = _download_compose(client, cid)
    assert _service_ids(compose) == {"web"}, "a single-service candidate emitted extra services"
    web = _service_block(compose, "web")
    assert _env_of(web).get("DATABASE_URL") == "file:/data/app.db", (
        "the sole ingress did not receive the bound DATABASE_URL literal"
    )
    assert _mount_targets(web) == {"/data": "app-data"}, (
        "the sole ingress did not receive exactly the resource's named-volume mount"
    )
    assert _top_volume_names(compose) == {"app-data"}, (
        "expected exactly one named volume for the one accepted resource persistent path"
    )


def test_public_migrate_service_mounts_only_migrated_resource(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """§11.3 — RED on baseline, at the PUBLIC boundary.

    A typed intent declares TWO resources (both consumed by ``web``); only ``db_main``
    carries a ``migrate_cmd``, so the emitter derives a one-shot ``migrate`` service
    for it. Through the REAL ``/release`` + ``/download``, that migrate service must
    mount ONLY the resource it migrates (``db_main``) and carry no unrelated secret
    env. Baseline mounts EVERY resource into the migrate service
    (``_resource_mounts`` fans out), so ``db_cache`` leaks in — the mount assertion
    trips. (The 'no unrelated secret env' half is already correct: the migrate env is
    resource-bound-only, so the unbound ``SESSION_SECRET`` never reaches it.)"""
    cid = _cid(closeout_name, "conv_c7pub2")
    client, ps = _client(_store, tmp_path, monkeypatch)
    intent = ReleaseIntent(
        start_cmd=("node", "server.js"),
        required_env=("SESSION_SECRET",),
        resources=(
            _intent_resource(
                "db_main",
                path="/data/main/app.db",
                url="file:/data/main/app.db",
                volume="main-data",
                consumers=("web",),
                migrate=("node", "migrate.js"),
            ),
            _intent_resource(
                "db_cache",
                path="/data/cache/cache.db",
                url="file:/data/cache/cache.db",
                volume="cache-data",
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
    assert body["assessment"] == "candidate" and body["self_host"] is True, body

    compose = _download_compose(client, cid)
    migrate = _service_block(compose, "migrate")
    assert _mount_targets(migrate) == {"/data/main": "main-data"}, (
        "the one-shot migrate service mounted a resource it does not migrate — it must "
        "mount ONLY 'db_main'. Baseline fans every resource mount into every service."
    )
    assert "SESSION_SECRET" not in _env_of(migrate), (
        "the migrate service received an unrelated secret env var (should be "
        "resource-bound-only) — GREEN-preservation half of §11.3."
    )


# =================================================================================
# SPEC + EMITTER BOUNDARY — real ReleaseSpec.model_validate + real emit_local_compose.
# (Multi-service topology is not expressible through the intent; assert §11 target.)
# =================================================================================


def test_web_worker_bound_db_env_and_mount_isolated_to_consumer(closeout_name: object) -> None:
    """§11.1 — RED on baseline. The headline consumer-isolation contract.

    A ``web + worker`` spec where ONLY ``web`` consumes ``db`` (bound ``DATABASE_URL``).
    In the emitted compose, ``web`` gets the DB mount + ``DATABASE_URL`` and ``worker``
    gets NEITHER. Baseline injects the bound ``DATABASE_URL`` into every service's
    environment and mounts the volume into every service, so ``worker`` wrongly
    receives both."""
    spec = ReleaseSpec.model_validate(
        _spec_payload(
            services=[_svc("web"), _svc("worker", role="worker", start=("node", "worker.js"))],
            env=[_envvar("DATABASE_URL", binding="db")],
            resources=[
                _resource(
                    "db",
                    path="/data/app.db",
                    url="file:/data/app.db",
                    volume="app-data",
                    consumers=("web",),
                )
            ],
            name=_cid(closeout_name, "app"),
        )
    )
    compose = _emit_compose(spec)

    web = _service_block(compose, "web")
    assert "DATABASE_URL" in _env_of(web) and _mount_targets(web) == {"/data": "app-data"}, (
        "the consumer 'web' must receive the bound DATABASE_URL and the DB mount"
    )

    worker = _service_block(compose, "worker")
    assert "DATABASE_URL" not in _env_of(worker), (
        "'worker' does not consume 'db' but received the bound DATABASE_URL — resource-"
        "bound env must reach only declared consumers (§11.1 / plan §2.9)."
    )
    assert "/data" not in _mount_targets(worker), (
        "'worker' does not consume 'db' but received its volume mount — a resource must "
        "be mounted only into the services that consume it (§11.1)."
    )


def test_worker_only_state_resource_isolated_to_worker(closeout_name: object) -> None:
    """§11.2 — RED on baseline.

    A worker-only SQLite/state resource at a DISTINCT path, consumed only by
    ``worker`` (bound ``WORKER_STATE_URL``), must be mounted/injected into ``worker``
    only — ``web`` (the ingress) must receive NEITHER. Baseline fans the mount + bound
    env into every service, so the ingress wrongly receives them."""
    spec = ReleaseSpec.model_validate(
        _spec_payload(
            services=[_svc("web"), _svc("worker", role="worker", start=("node", "worker.js"))],
            env=[_envvar("WORKER_STATE_URL", binding="state")],
            resources=[
                _resource(
                    "state",
                    path="/var/lib/worker/state.db",
                    url="file:/var/lib/worker/state.db",
                    volume="worker-state",
                    consumers=("worker",),
                )
            ],
            name=_cid(closeout_name, "app"),
        )
    )
    compose = _emit_compose(spec)

    worker = _service_block(compose, "worker")
    assert "WORKER_STATE_URL" in _env_of(worker), "the consumer 'worker' lost its bound state URL"
    assert _mount_targets(worker) == {"/var/lib/worker": "worker-state"}, (
        "the consumer 'worker' lost its state volume mount"
    )

    web = _service_block(compose, "web")
    assert "WORKER_STATE_URL" not in _env_of(web), (
        "the ingress 'web' does not consume the worker-only state resource but received "
        "its bound env — resource-bound env must reach only its consumers (§11.2)."
    )
    assert "/var/lib/worker" not in _mount_targets(web), (
        "the ingress 'web' does not consume the worker-only state resource but received "
        "its mount (§11.2)."
    )


def test_emitted_compose_topology_equals_spec_topology(closeout_name: object) -> None:
    """§11.7 — RED on baseline (in-process YAML parse).

    The emitted Compose mounts / resource-bound env / dependencies, parsed in-process,
    must equal the spec topology EXACTLY: ``web`` (ingress, ``depends_on`` worker,
    consumes ``db``) receives the mount + ``DATABASE_URL`` + the ``worker`` dependency;
    ``worker`` (consumes nothing) receives none of the DB mount/env. Baseline fans the
    DB mount + bound env into ``worker`` too, so the parsed topology does not equal the
    spec. NOTE: the real ``docker compose config --format json`` round-trip of §11.7 is
    DEFERRED to the C8/live lane; here the equality is checked over the in-process YAML."""
    spec = ReleaseSpec.model_validate(
        _spec_payload(
            services=[
                _svc("web", depends_on=("worker",)),
                _svc("worker", role="worker", start=("node", "worker.js")),
            ],
            env=[_envvar("DATABASE_URL", binding="db")],
            resources=[
                _resource(
                    "db",
                    path="/data/app.db",
                    url="file:/data/app.db",
                    volume="app-data",
                    consumers=("web",),
                )
            ],
            name=_cid(closeout_name, "app"),
        )
    )
    compose = _emit_compose(spec)

    web = _service_block(compose, "web")
    worker = _service_block(compose, "worker")

    # web: consumes db + depends on worker.
    assert _mount_targets(web) == {"/data": "app-data"}, "web mount set != spec topology"
    assert "DATABASE_URL" in _env_of(web), "web lost its bound env"
    assert "worker" in _depends_keys(web), "the generic depends_on edge web->worker vanished"

    # worker: consumes nothing → NO db mount, NO bound DB env.
    assert _mount_targets(worker) == {}, (
        "worker's mount set is not empty — the emitted topology does not equal the spec "
        "consumer topology (worker consumes no resource) (§11.7)."
    )
    assert "DATABASE_URL" not in _env_of(worker), (
        "worker resolved a DATABASE_URL it does not consume — emitted env topology != spec."
    )


def test_secret_scoped_to_single_consumer_absent_from_others(closeout_name: object) -> None:
    """§11.8 (+ §11.11 env-consumer topology) — RED on baseline.

    A secret required by ONLY ``web`` must be absent from every other service's
    resolved environment. Expressing that needs a per-env ``consumers`` scope — which
    the v1 ``EnvVarDecl`` cannot represent (``extra='forbid'`` rejects the field), so
    the spec cannot even be assembled on baseline. That inexpressiveness is the RED:
    without per-env consumers, a multi-service secret can only fan out. Once the field
    exists, the emitted compose must scope the secret to ``web`` and keep it out of
    ``worker``."""
    payload = _spec_payload(
        services=[_svc("web"), _svc("worker", role="worker", start=("node", "worker.js"))],
        env=[_envvar("SESSION_SECRET", secret="secret", consumers=("web",))],
        name=_cid(closeout_name, "app"),
    )
    try:
        spec = ReleaseSpec.model_validate(payload)
    except ValidationError as exc:
        pytest.fail(
            "§11.8 requires a secret to be scoped to its single consumer, which needs a "
            "per-env `consumers` topology; the v1 EnvVarDecl cannot express it, so a "
            f"multi-service secret can only fan out (no implicit scoping): {exc}"
        )
    compose = _emit_compose(spec)
    assert "SESSION_SECRET" in _env_of(_service_block(compose, "web")), (
        "the secret's declared consumer 'web' did not resolve it"
    )
    assert "SESSION_SECRET" not in _env_of(_service_block(compose, "worker")), (
        "a secret consumed only by 'web' leaked into 'worker' (§11.8)."
    )


def test_named_volume_per_resource_no_shared_target(closeout_name: object) -> None:
    """§11.5 — GREEN preservation.

    Two DISTINCT resources at distinct persistent paths (distinct mount targets), both
    consumed by ``web``, emit exactly one named volume each, and no two service volume
    entries share a container target. Already correct on baseline; the fix must keep
    it green."""
    spec = ReleaseSpec.model_validate(
        _spec_payload(
            services=[_svc("web")],
            resources=[
                _resource(
                    "db_a",
                    path="/data/a/app.db",
                    url="file:/data/a/app.db",
                    volume="a-data",
                    consumers=("web",),
                ),
                _resource(
                    "db_b",
                    path="/data/b/app.db",
                    url="file:/data/b/app.db",
                    volume="b-data",
                    consumers=("web",),
                ),
            ],
            name=_cid(closeout_name, "app"),
        )
    )
    compose = _emit_compose(spec)
    assert _top_volume_names(compose) == {"a-data", "b-data"}, (
        "expected exactly one named volume per accepted resource persistent path"
    )
    targets = _mount_targets(_service_block(compose, "web"))
    assert targets == {"/data/a": "a-data", "/data/b": "b-data"}, (
        "each resource must mount at its own distinct target with its own volume"
    )


def test_duplicate_declared_volume_name_disambiguated(closeout_name: object) -> None:
    """§11.5 (partial fix note) — GREEN preservation.

    Two resources at DISTINCT paths that DECLARE the same local volume name must not
    collapse to one shared volume (which would clobber each other's state): they are
    disambiguated into two distinct named volumes. Already handled on baseline
    (``_resource_volume_names`` suffixes a collision); the fix must keep the two
    volumes distinct. (Order-determinism of that disambiguation is asserted RED in
    ``test_resource_order_does_not_change_volume_identity``.)"""
    spec = ReleaseSpec.model_validate(
        _spec_payload(
            services=[_svc("web")],
            resources=[
                _resource(
                    "alpha",
                    path="/data/alpha/x.db",
                    url="file:/data/alpha/x.db",
                    volume="shared",
                    consumers=("web",),
                ),
                _resource(
                    "bravo",
                    path="/data/bravo/y.db",
                    url="file:/data/bravo/y.db",
                    volume="shared",
                    consumers=("web",),
                ),
            ],
            name=_cid(closeout_name, "app"),
        )
    )
    compose = _emit_compose(spec)
    volumes = _top_volume_names(compose)
    assert len(volumes) == 2, (
        "two resources declaring the same volume name collapsed to one — their state "
        "would clobber (§11.5)."
    )
    targets = _mount_targets(_service_block(compose, "web"))
    assert len(set(targets.values())) == 2, "the two distinct resources share one volume"
    assert set(targets) == {"/data/alpha", "/data/bravo"}, "distinct mount targets were lost"


def test_depends_on_and_migration_ordering_coexist(closeout_name: object) -> None:
    """§11.6 (coexistence) — GREEN preservation.

    A generic ``depends_on`` (web -> worker) and the one-shot migration ordering
    (web -> migrate, via a resource ``migrate_cmd``) must coexist in the ingress
    ``depends_on`` without either overwriting the other. Already correct on baseline;
    the fix must keep both edges."""
    spec = ReleaseSpec.model_validate(
        _spec_payload(
            services=[
                _svc("web", depends_on=("worker",)),
                _svc("worker", role="worker", start=("node", "worker.js")),
            ],
            resources=[
                _resource(
                    "db",
                    path="/data/app.db",
                    url="file:/data/app.db",
                    volume="app-data",
                    consumers=("web",),
                    migrate=("node", "migrate.js"),
                )
            ],
            name=_cid(closeout_name, "app"),
        )
    )
    compose = _emit_compose(spec)
    assert "migrate" in _service_ids(compose), (
        "the resource migrate_cmd did not yield a migrate job"
    )
    web_deps = _depends_keys(_service_block(compose, "web"))
    assert {"worker", "migrate"} <= web_deps, (
        "the generic depends_on and the migration ordering did not coexist on the "
        f"ingress — got depends_on keys {sorted(web_deps)} (§11.6)."
    )


def test_resource_order_does_not_change_volume_identity(closeout_name: object) -> None:
    """§11.9 — RED on baseline.

    Two resources at distinct targets that declare the same volume name; permuting the
    resource ORDER must not change the volume IDENTITY assigned to a given persistent
    path. Baseline assigns volume names in spec order (suffixing the second collision),
    so ``/data/alpha`` gets ``shared`` in one order and ``shared-2`` in the other — an
    order-dependent volume identity that could bind a deployed service to the wrong
    volume."""
    alpha = _resource(
        "alpha",
        path="/data/alpha/x.db",
        url="file:/data/alpha/x.db",
        volume="shared",
        consumers=("web",),
    )
    bravo = _resource(
        "bravo",
        path="/data/bravo/y.db",
        url="file:/data/bravo/y.db",
        volume="shared",
        consumers=("web",),
    )
    name = _cid(closeout_name, "app")

    forward = _emit_compose(
        ReleaseSpec.model_validate(
            _spec_payload(services=[_svc("web")], resources=[alpha, bravo], name=name)
        )
    )
    reverse = _emit_compose(
        ReleaseSpec.model_validate(
            _spec_payload(services=[_svc("web")], resources=[bravo, alpha], name=name)
        )
    )
    assert _mount_targets(_service_block(forward, "web")) == _mount_targets(
        _service_block(reverse, "web")
    ), (
        "permuting resource declaration order changed the target->volume mapping — the "
        "assigned volume identity is not order-independent (§11.9)."
    )


# =================================================================================
# SCHEMA VALIDATION — the REAL ReleaseSpec.model_validate cross-field validators.
# =================================================================================

# Each case name -> whether the REAL v1 schema already rejects it on baseline. A
# RED-target case is one the §11 model invariants REQUIRE be rejected but the v1
# schema currently accepts (so `pytest.raises` trips: DID NOT RAISE); a GREEN case
# is already rejected and must stay rejected.
_INVALID_TOPOLOGY_CASES: dict[str, str] = {
    "duplicate_persistent_path": "RED",
    "duplicate_mount_target": "RED",
    "url_path_disagreement": "RED",
    "empty_consumers": "RED",
    "consumer_unknown_service": "GREEN",
    "env_binding_unknown_resource": "GREEN",
}


def _invalid_topology_payload(case: str) -> dict[str, object]:
    web = _svc("web")
    if case == "duplicate_persistent_path":
        return _spec_payload(
            services=[web],
            resources=[
                _resource(
                    "db_a",
                    path="/data/app.db",
                    url="file:/data/app.db",
                    volume="v_a",
                    consumers=("web",),
                ),
                _resource(
                    "db_b",
                    path="/data/app.db",
                    url="file:/data/app.db",
                    volume="v_b",
                    consumers=("web",),
                ),
            ],
        )
    if case == "duplicate_mount_target":
        return _spec_payload(
            services=[web],
            resources=[
                _resource(
                    "db_a",
                    path="/data/a.db",
                    url="file:/data/a.db",
                    volume="v_a",
                    consumers=("web",),
                ),
                _resource(
                    "db_b",
                    path="/data/b.db",
                    url="file:/data/b.db",
                    volume="v_b",
                    consumers=("web",),
                ),
            ],
        )
    if case == "url_path_disagreement":
        return _spec_payload(
            services=[web],
            resources=[
                _resource(
                    "db",
                    path="/data/main.db",
                    url="file:/data/other.db",
                    volume="v",
                    consumers=("web",),
                )
            ],
        )
    if case == "empty_consumers":
        return _spec_payload(
            services=[web],
            resources=[
                _resource(
                    "db", path="/data/app.db", url="file:/data/app.db", volume="v", consumers=()
                )
            ],
        )
    if case == "consumer_unknown_service":
        return _spec_payload(
            services=[web],
            resources=[
                _resource(
                    "db",
                    path="/data/app.db",
                    url="file:/data/app.db",
                    volume="v",
                    consumers=("ghost",),
                )
            ],
        )
    if case == "env_binding_unknown_resource":
        return _spec_payload(
            services=[web],
            env=[_envvar("DATABASE_URL", binding="ghost")],
            resources=[
                _resource(
                    "db",
                    path="/data/app.db",
                    url="file:/data/app.db",
                    volume="v",
                    consumers=("web",),
                )
            ],
        )
    raise AssertionError(f"unknown invalid-topology case {case!r}")


@pytest.mark.parametrize("case", sorted(_INVALID_TOPOLOGY_CASES))
def test_invalid_resource_topology_fails_schema_validation(case: str) -> None:
    """§11.4 — RED for the four un-enforced invariants, GREEN for the two already
    enforced.

    Duplicate persistent paths, duplicate mount targets, url/path disagreement, and
    empty consumers must fail ``ReleaseSpec`` schema validation (RED — the v1 schema
    accepts all four). Unknown consumer-service and unknown env-binding references
    already fail validation (GREEN — must stay rejected)."""
    payload = _invalid_topology_payload(case)
    with pytest.raises(ValidationError):
        ReleaseSpec.model_validate(payload)


@pytest.mark.parametrize("bad_path", ["data/app.db", "/data//app.db", "/data/./app.db"])
def test_persistent_path_must_be_absolute_normalized_posix(bad_path: str) -> None:
    """Model invariant (§11) — RED on baseline.

    ``persistent_path`` must be an absolute, normalized POSIX path. A relative path, a
    ``//`` double slash, or a ``/./`` segment must fail schema validation. Baseline
    only rejects a NUL byte and a ``..`` traversal segment, so each of these is
    accepted (no raise)."""
    payload = _spec_payload(
        services=[_svc("web")],
        resources=[
            _resource(
                "db",
                path=bad_path,
                url=f"file:{bad_path}",
                volume="v",
                consumers=("web",),
            )
        ],
    )
    with pytest.raises(ValidationError):
        ReleaseSpec.model_validate(payload)


@pytest.mark.parametrize("case", ["self_dependency", "dependency_cycle"])
def test_invalid_dependency_graph_fails_schema_validation(case: str) -> None:
    """§11.6 (cycles / self-deps) — RED on baseline.

    A service depending on itself, or a dependency cycle (web<->worker), must fail
    schema validation. Baseline validates ``depends_on`` only for id-shape,
    uniqueness, and referential existence — neither a self-edge nor a cycle is
    rejected."""
    if case == "self_dependency":
        payload = _spec_payload(services=[_svc("web", depends_on=("web",))])
    else:
        payload = _spec_payload(
            services=[
                _svc("web", depends_on=("worker",)),
                _svc("worker", role="worker", start=("node", "worker.js"), depends_on=("web",)),
            ]
        )
    with pytest.raises(ValidationError):
        ReleaseSpec.model_validate(payload)


def test_multi_service_unbound_env_without_consumers_is_rejected() -> None:
    """§11.11 — RED on baseline.

    In a multi-service spec, an unbound env var with NO declared consumers must fail
    schema validation: implicit fan-out is forbidden, and silently treating a missing
    consumer list as global in a multi-service v1 spec is forbidden. Baseline accepts
    the unbound var and fans it out to every service."""
    payload = _spec_payload(
        services=[_svc("web"), _svc("worker", role="worker", start=("node", "worker.js"))],
        env=[_envvar("FEATURE_FLAG")],
    )
    with pytest.raises(ValidationError):
        ReleaseSpec.model_validate(payload)


def test_single_service_unbound_env_defaults_to_sole_ingress() -> None:
    """§11.11 (single-service policy) — GREEN preservation.

    A SINGLE-service spec may default an unbound env var to its sole ingress: it is
    accepted (no consumers required). This is the explicit v1 read policy the
    multi-service rejection above must NOT break. Already accepted on baseline; the
    fix must keep it accepted."""
    spec = ReleaseSpec.model_validate(
        _spec_payload(services=[_svc("web")], env=[_envvar("FEATURE_FLAG")])
    )
    assert len(spec.services) == 1 and spec.services[0].id == "web"
    assert {var.name for var in spec.env} == {"FEATURE_FLAG"}

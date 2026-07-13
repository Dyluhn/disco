"""GAP G05 red — a typed ``pnpm start`` intent must fail closed (or provision pnpm),
never ship a self-hostable node image that contains no pnpm.

Boundary (plan §1.2 / §4 crit 3): the test drives the REAL FastAPI
``GET /api/projects/{cid}/release`` (+ ``/download``) routes through the ASGI app, a
real ``ProjectStore`` on a real on-disk workspace, a real host-owned intent sidecar,
and a real committed version cut. The verdict is asserted on the ``/release`` JSON and
on the ACTUAL bytes streamed by ``/download`` (the emitted ``Dockerfile`` inside the
zip) — never on ``detect_release`` / ``emit_local_compose`` called directly. No
release/detect/emit function is mocked; the ONLY ``monkeypatch`` is
``ConfigStore.load`` (the config seam the settings PUT performs), never the code under
test.

The gap: the SOURCE-driven package-manager reject (a committed ``pnpm-lock.yaml``, a
``packageManager`` field, a ``pnpm-workspace.yaml`` marker, or a ``pnpm``/``pnpx``
script head) fails a workspace closed with ``toolchain_unsupported``. But the TYPED
INTENT path routes the declared ``start_cmd`` head through ``detect._toolchain_blocker``
→ ``_is_supported_toolchain_head``, whose ``_SUPPORTED_NODE_HEADS`` allowlist INCLUDES
``pnpm`` (and ``yarn``). So a typed intent whose ``start_cmd`` is ``("pnpm", "start")``
is accepted as a candidate; the emitter's ``_node_dockerfile`` builds
``FROM node:22-bookworm-slim`` and emits ``CMD ["pnpm", "start"]`` — an image that
provisions NO pnpm (no ``corepack enable``, no global pnpm install), so ``pnpm start``
fails at container start. None of the existing pnpm reds drives a TYPED INTENT whose
start command names pnpm.

RED on 581d1fbe: the verdict is ``candidate`` / ``self_host:true`` with a node image
that provisions no pnpm — so ``failed_closed or pnpm_provisioned`` is False. The correct
behaviour this test asserts is ``needs_review`` + a typed ``toolchain_unsupported``
blocker (no overlay), OR pnpm actually provisioned/pinned in the emitted image; it turns
GREEN once the intent path rejects (or provisions) an unprovisioned toolchain the same
way the source path does.

Randomized (plan §4 crit 8): the conversation id / title is drawn from the seeded
``closeout_name`` factory; the detector reads workspace-relative file CONTENTS only,
never the id/title, so detection cannot recognize the fixture by its name.
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

# The exact typed blocker code the source-driven package-manager reject already emits
# for an unprovisioned node toolchain — the intent path must emit the SAME one.
_TOOLCHAIN_UNSUPPORTED_BLOCKER = "toolchain_unsupported"


# ---- fixtures (seeded in tmp_path from in-memory byte maps; never a fixture NAME) --

# A minimal node service (binds `$PORT`, reads no undeclared env, NO committed
# package-manager lockfile / marker / script-head — so the ONLY pnpm signal is the
# TYPED INTENT's start_cmd). Source alone would detect a plain npm candidate.
_PNPM_START_FILES: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "server.js": (
        b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"
    ),
}
# A host-owned typed intent whose start command NAMES pnpm — the sidecar the release
# route reads (written via `_seed_project(..., intent=...)`).
_PNPM_START_INTENT = ReleaseIntent(start_cmd=("pnpm", "start"))


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
# G05 — a typed pnpm start intent is rejected (or provisions pnpm), never shipped as a
# self-hostable node image that contains no pnpm.
# ===========================================================================


def test_typed_pnpm_start_intent_is_rejected_or_provisions_pnpm(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """GAP G05 — RED on 581d1fbe.

    A typed intent whose ``start_cmd`` is ``("pnpm", "start")`` has exactly TWO
    acceptable outcomes: either a fail-closed ``needs_review`` carrying the typed
    ``toolchain_unsupported`` blocker (``self_host:false``, no overlay) — the SAME
    reject the source-driven path already emits for pnpm — OR a self-hostable candidate
    whose emitted node image actually PROVISIONS pnpm (a ``corepack enable`` / pnpm
    install / pnpm-provisioning ``FROM``). On 581d1fbe the intent path allowlists
    ``pnpm`` (``_SUPPORTED_NODE_HEADS``), so the verdict is ``candidate`` /
    ``self_host:true`` and the emitted ``node:22-bookworm-slim`` Dockerfile runs
    ``CMD ["pnpm", "start"]`` with pnpm appearing ONLY in that CMD — no provisioning
    step — so ``failed_closed or pnpm_provisioned`` is False and this assertion fails
    RED."""
    cid = _cid(closeout_name, "conv_g05")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(
        ps, _store, cid, _cid(closeout_name, "proj"), _PNPM_START_FILES, intent=_PNPM_START_INTENT
    )

    body = _release(client, cid)
    assert isinstance(body, dict)
    codes = _blocker_codes(body)
    texts = _overlay_texts(client, cid)
    dockerfile = texts.get(DOCKERFILE_PATH, "")

    failed_closed = (
        _TOOLCHAIN_UNSUPPORTED_BLOCKER in codes
        and body["assessment"] == "needs_review"
        and body["self_host"] is False
        and _overlay_present(texts) == frozenset()
    )
    # pnpm is genuinely provisioned only by a build directive that installs/activates it
    # — a `corepack` step, a `RUN` that names pnpm (a global install), or a
    # pnpm-provisioning base image. A bare `CMD ["pnpm", "start"]` (pnpm named ONLY in
    # the CMD) does NOT provision it, so this excludes the baseline's leak.
    provision_signals = [
        line
        for line in dockerfile.splitlines()
        if "corepack" in line
        or (line.lstrip().startswith("RUN") and "pnpm" in line)
        or (line.lstrip().startswith("FROM") and "pnpm" in line)
    ]
    pnpm_provisioned = body["self_host"] is True and bool(provision_signals)

    assert failed_closed or pnpm_provisioned, (
        "a typed intent whose start_cmd names pnpm was accepted as a self-hostable "
        "candidate whose emitted node image provisions NO pnpm: "
        f"assessment={body['assessment']!r}, self_host={body['self_host']!r}, "
        f"blockers={sorted(codes)}. `_toolchain_blocker` allowlists pnpm via "
        "_SUPPORTED_NODE_HEADS, so a `pnpm start` intent slips through and the emitted "
        "node:22-bookworm-slim image runs `CMD [\"pnpm\", \"start\"]` with no pnpm "
        "installed — unrunnable. It must fail closed with toolchain_unsupported OR "
        f"provision/pin pnpm in the image. Emitted Dockerfile:\n{dockerfile}"
    )

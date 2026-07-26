"""WO-C4 red matrix — complete env, build, and toolchain lowering.

Plan §8 (WO-C4): the release contract must discover EVERY application env name, lower
a PUBLIC build var to a Compose build arg AND a Dockerfile ``ARG`` (never a runtime
``ENV``), route a SECRET build var through a real build-secret mount (or fail closed),
guard missing required names, install npm deps before the build, fail closed on a
lockfile/package-manager disagreement, keep ``.env.example`` names-only, and version
the persisted intent/spec shape.

Boundary (plan §1.2 / §4 crit 3): every test drives the REAL FastAPI
``GET /api/projects/{cid}/release`` (+ ``/download``) routes through the ASGI app, a
real ``ProjectStore`` on a real on-disk workspace, and a real committed version cut
(so the C2 source binding is satisfied and env/build lowering is the only thing under
test). The env/build/toolchain behaviour is asserted on the ACTUAL bytes streamed by
``/download`` (the emitted ``Dockerfile`` / ``compose.yaml`` / ``.env.example`` /
``release.json`` inside the zip) and on the ``/release`` JSON — never on
``emit_local_compose()`` called directly. No release/detect/route/tool function is
mocked; the ONLY ``monkeypatch`` is ``ConfigStore.load`` (the config seam the settings
PUT performs), never the code under test.

RED vs GREEN on baseline ``a8e3e710`` (see each test):
  * RED — the detector never scans application source for env NAMES, so a statically
    discovered name is silently DROPPED (no entry, no blocker); a public build var is
    never lowered to a Dockerfile ``ARG`` (the emitted ``ARG`` is absent, so the build
    arg is invisible to the build ``RUN``); a secret build var is neither mounted nor
    rejected with ``secret_build_env_unsupported``; a lockfile/package-manager
    disagreement is resolved by silent precedence (yarn beats the npm lock) instead of
    failing closed; and a legacy intent sidecar carrying an explicit schema version
    crashes the read (HTTP 500) instead of migrating or rejecting with
    ``intent_upgrade_required``.
  * GREEN (preservation) — a DECLARED runtime env name is conserved once with the
    right secret class across the API / ``.env.example`` / Compose; a required runtime
    var renders a name-only ``${NAME:?...}`` guard; npm installs before the build and
    ``.dockerignore`` excludes ``node_modules``; ``.env.example`` is names/comments
    only; and ``release.json`` is serialize/parse/serialize byte-identical.

DEFERRED to the C8 live lane (NOT authored here): §8.3 (the built Vite asset actually
contains the public marker) and §8.7/§8.8 (live ``command -v`` / toolchain-install
proofs) — those require a real Docker build and cannot be honestly proven by a static
overlay inspection.

Randomized (plan §4 crit 8): every conversation id / title is drawn from the seeded
``closeout_name`` factory; the detector reads workspace-relative file CONTENTS only,
never the id/title, so detection cannot recognize a fixture by its name.
"""

from __future__ import annotations

import io
import json
import re
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
from disco.core.release.spec import ReleaseIntent, load_release_spec, serialize_release_spec
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

# The exact typed blocker codes this frozen harness pins for WO-C4 (the fix must emit
# these verbatim). ``secret_build_env_unsupported`` and ``intent_upgrade_required`` are
# quoted verbatim from plan §8.4 / §8.11; ``package_manager_conflict`` is this harness'
# chosen code for the §8.9 lockfile/package-manager disagreement (the runtime-conflict
# analogue of C3's ``runtime_conflict``).
_SECRET_BUILD_BLOCKER = "secret_build_env_unsupported"
_INTENT_UPGRADE_BLOCKER = "intent_upgrade_required"
_LOCKFILE_CONFLICT_BLOCKER = "package_manager_conflict"


# ---- fixtures (seeded in tmp_path from in-memory byte maps; never a fixture NAME) --

# A node service that reads TWO app env NAMES from source. Neither is declared by an
# intent, so a name only reaches the verdict if the detector scans source (§8.1).
_NODE_UNDECLARED_ENV: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (
        b"const secret = process.env.SESSION_SECRET;\n"
        b"const base = process.env.API_BASE_URL;\n"
        b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"
    ),
}

# A node service used with a typed intent that DECLARES its two runtime env names — the
# conservation-through-the-chain fixture (§8.1 green).
_NODE_DECLARED_ENV: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (
        b"const secret = process.env.SESSION_SECRET;\n"
        b"const base = process.env.API_BASE_URL;\n"
        b"require('http').createServer((_q,r)=>r.end(base)).listen(process.env.PORT);\n"
    ),
}
_DECLARED_ENV_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"),
    required_env=("API_BASE_URL", "SESSION_SECRET"),
)

# A Vite bundle whose client source reads a PUBLIC build var (`import.meta.env.
# VITE_PUBLIC_BANNER`) — a build-time-only value that must reach the build via a
# Compose build arg + a Dockerfile ARG (§8.2), never a runtime ENV.
_VITE_PUBLIC_BUILD_ENV: dict[str, bytes] = {
    "index.html": (
        b'<!doctype html><html><body><script type="module" src="/src/main.js"></script>'
        b"</body></html>\n"
    ),
    "package.json": b'{"name":"spa","scripts":{"build":"vite build"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"spa"}',
    "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
    "src/main.js": b"document.body.append(import.meta.env.VITE_PUBLIC_BANNER);\n",
}
_PUBLIC_BUILD_VAR = "VITE_PUBLIC_BANNER"

# A Vite bundle whose install step needs a SECRET build token (an `.npmrc` auth token
# read at `npm ci` time). `NPM_TOKEN` is secret-shaped and build-scoped: it must be a
# real build-secret mount with zero leakage, or fail closed (§8.4). A plain ARG is a
# FAIL.
_VITE_SECRET_BUILD_ENV: dict[str, bytes] = {
    "index.html": (
        b'<!doctype html><html><body><script type="module" src="/src/main.js"></script>'
        b"</body></html>\n"
    ),
    "package.json": b'{"name":"spa","scripts":{"build":"vite build"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"spa"}',
    "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
    "src/main.js": b"document.body.append('spa');\n",
    ".npmrc": b"//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n",
}
_SECRET_BUILD_VAR = "NPM_TOKEN"

# A node service that declares a required runtime SECRET — its guard must be name-only
# (§8.5) and its `.env.example` line must be names/comments only (§8.10).
_NODE_RUNTIME_SECRET: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}
_RUNTIME_SECRET_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"),
    required_env=("SESSION_SECRET",),
)

# A Vite bundle for the install-before-build / dockerignore preservation (§8.6).
_VITE_PLAIN: dict[str, bytes] = {
    "index.html": (
        b'<!doctype html><html><body><script type="module" src="/src/main.js"></script>'
        b"</body></html>\n"
    ),
    "package.json": b'{"name":"spa","scripts":{"build":"vite build"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"spa"}',
    "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
    "src/main.js": b"document.body.append('spa');\n",
}

# A node service carrying TWO conflicting lockfiles (an npm lock AND a yarn lock): two
# package managers declared. This is a disagreement that must fail closed, NOT be
# resolved by precedence (§8.9).
_NODE_TWO_LOCKFILES: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "yarn.lock": b"# yarn lockfile v1\n",
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}

# The workspace + raw sidecar for the §8.11 legacy-intent test (the sidecar is written
# as raw JSON so it can carry an explicit schema version the current reader rejects).
_NODE_FOR_LEGACY_SIDECAR: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}
# A sidecar that explicitly declares intent schema v1. Under WO-C4 the persisted intent
# shape gains fields (build env / scope / secret class / install argv / …) and a schema
# version; a v1-tagged sidecar must therefore be MIGRATED by a tested adapter OR
# rejected with ``intent_upgrade_required`` — never silently reinterpreted, and never a
# crash. Baseline's ``ReleaseIntent`` forbids the unknown ``schema_version`` key, so the
# sidecar read raises and ``/release`` returns HTTP 500.
_LEGACY_V1_SIDECAR: dict[str, object] = {
    "schema_version": 1,
    "start_cmd": ["node", "server.js"],
    "build_cmd": [],
    "port_env": "PORT",
    "required_env": ["SESSION_SECRET"],
    "resources": [],
}


# ---- harness (real ASGI app + real ProjectStore; only ConfigStore.load seamed) -----


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


def _env_names(body: object) -> list[str]:
    assert isinstance(body, dict)
    env = body["required_env"]
    assert isinstance(env, list)
    return [str(e["name"]) for e in env]


def _env_by_name(body: object, name: str) -> dict[str, object]:
    assert isinstance(body, dict)
    env = body["required_env"]
    assert isinstance(env, list)
    matches = [e for e in env if isinstance(e, dict) and e.get("name") == name]
    assert len(matches) == 1, f"expected exactly one required_env entry named {name!r}, saw {env}"
    return matches[0]


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
# §8.1 — every statically discovered env name is conserved (present once with the
# correct scope/requiredness/secret class) or produces a blocker; a declared name is
# conserved unchanged across source -> intent -> API -> .env.example -> Compose.
# ===========================================================================


def test_declared_runtime_env_names_conserved_across_api_envexample_compose(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.1 (declared-name conservation) — GREEN preservation.

    A typed intent declares two runtime env names (one public, one secret). Each must
    appear EXACTLY ONCE in the ``/release`` ``required_env`` with the correct secret
    class, exactly once as a host-supplied line in ``.env.example``, and once as a
    guard in ``compose.yaml`` — no name dropped, added, or reclassified between stages.
    Already correct on baseline via the typed-intent path; the fix must keep it green
    while it additionally learns to discover UNDECLARED names (the RED sibling below)."""
    cid = _cid(closeout_name, "conv_c4con")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(
        ps,
        _store,
        cid,
        _cid(closeout_name, "proj"),
        _NODE_DECLARED_ENV,
        intent=_DECLARED_ENV_INTENT,
    )

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True

    # API: exactly the two declared names, each once, with the right secret class.
    assert sorted(_env_names(body)) == ["API_BASE_URL", "SESSION_SECRET"], (
        f"required_env did not conserve exactly the two declared names: {_env_names(body)}"
    )
    assert _env_by_name(body, "API_BASE_URL")["secret"] is False
    assert _env_by_name(body, "SESSION_SECRET")["secret"] is True

    texts = _overlay_texts(client, cid)
    env_example = texts[ENV_EXAMPLE_PATH]
    compose = texts[COMPOSE_PATH]
    for name in ("API_BASE_URL", "SESSION_SECRET"):
        # .env.example: exactly one host-supplied (uncommented) line for the name.
        supplied = [line for line in env_example.splitlines() if line.strip() == f"{name}="]
        assert len(supplied) == 1, (
            f"{name} must appear exactly once as a names-only .env.example line; saw {supplied}"
        )
        # Compose: a required guard references the name.
        assert f"${{{name}:?" in compose, f"{name} is not guarded in the emitted compose"


def test_statically_discovered_env_name_is_not_silently_dropped(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.1 (discovered-name conservation) — RED on baseline.

    A node service reads ``process.env.SESSION_SECRET`` from source with NO typed
    intent. §8.1 requires that name to be conserved: either present once in
    ``required_env`` (secret-classed) OR named by a blocker. It must NOT vanish. On
    baseline the detector never scans application source for env NAMES, so the name
    disappears between source and the API — the response is a self-hostable
    ``candidate`` with an EMPTY ``required_env`` and NO blocker referencing it."""
    cid = _cid(closeout_name, "conv_c4drop")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _NODE_UNDECLARED_ENV)

    body = _release(client, cid)
    assert isinstance(body, dict)

    names = _env_names(body)
    blob = json.dumps(body)
    conserved = "SESSION_SECRET" in names or "SESSION_SECRET" in blob
    assert conserved, (
        "the source-discovered env name SESSION_SECRET was silently DROPPED: it is "
        f"neither in required_env ({names}) nor named by any blocker/reason — baseline "
        "never scans application source for env names, so the name vanishes between "
        "source and the API (plan §8.1)."
    )
    # If it IS conserved as an env entry, it must be secret-classed (fail-closed).
    if "SESSION_SECRET" in names:
        assert _env_by_name(body, "SESSION_SECRET")["secret"] is True, (
            "a discovered SESSION_SECRET must classify as secret (host-derived, "
            "fail-closed); a secret-shaped name may never surface as public."
        )


# ===========================================================================
# §8.2 — a required PUBLIC build var is a Compose build arg AND a Dockerfile ARG
# visible to the build RUN; it is not persisted with ENV and is absent from the
# final runtime environment.
# ===========================================================================


def test_public_build_var_becomes_build_arg_and_dockerfile_arg(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.2 — RED on baseline.

    A Vite bundle whose client source reads ``import.meta.env.VITE_PUBLIC_BANNER`` uses
    that value only at BUILD time. The emitted overlay must (a) pass it as a Compose
    build argument and (b) declare a matching Dockerfile ``ARG VITE_PUBLIC_BANNER``
    BEFORE the build ``RUN`` (so the build actually sees it), (c) NOT persist it with a
    runtime ``ENV``, and (d) keep it out of the ingress' runtime ``environment``.
    Baseline never discovers a build-scope env at all: the emitted Compose carries no
    ``build.args`` and the Dockerfile carries no ``ARG`` — so the build arg is invisible
    to the build step (WO-C4 "Compose build args with no Dockerfile ARG")."""
    cid = _cid(closeout_name, "conv_c4pub")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _VITE_PUBLIC_BUILD_ENV)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True, (
        "precondition: the Vite bundle is a self-hostable candidate whose overlay we inspect"
    )

    texts = _overlay_texts(client, cid)
    dockerfile = texts[DOCKERFILE_PATH]
    compose = texts[COMPOSE_PATH]

    assert _PUBLIC_BUILD_VAR in compose, (
        f"the public build var {_PUBLIC_BUILD_VAR} is not passed as a Compose build "
        "argument; baseline discovers no build-scope env, so compose has no build.args."
    )
    assert f"ARG {_PUBLIC_BUILD_VAR}" in dockerfile, (
        f"the Dockerfile declares no `ARG {_PUBLIC_BUILD_VAR}`, so the Compose build arg "
        "is invisible to the build RUN (WO-C4: a build arg with no Dockerfile ARG). "
        "Baseline emits no ARG at all."
    )
    # The ARG must precede the build so the build step sees it.
    arg_at = dockerfile.index(f"ARG {_PUBLIC_BUILD_VAR}")
    build_at = dockerfile.index('"npm", "run", "build"')
    assert arg_at < build_at, (
        f"`ARG {_PUBLIC_BUILD_VAR}` must appear before the build RUN so the build sees it"
    )
    # It is a BUILD var: never persisted into the runtime image via ENV.
    assert f"ENV {_PUBLIC_BUILD_VAR}" not in dockerfile, (
        f"a public BUILD var must not be persisted with `ENV {_PUBLIC_BUILD_VAR}` "
        "(it would leak into the final runtime environment)."
    )


# ===========================================================================
# §8.4 — a required SECRET build var uses a real build-secret mount with zero
# image/history leakage, OR fails closed with `secret_build_env_unsupported`. A plain
# ARG for a secret-classed build var is a FAILURE.
# ===========================================================================


def test_secret_build_var_uses_secret_mount_or_fails_closed(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.4 — RED on baseline.

    A Vite bundle whose ``npm ci`` needs a secret ``NPM_TOKEN`` (an ``.npmrc`` auth
    token, secret-shaped and build-scoped) has exactly TWO acceptable outcomes: either
    a real Compose/BuildKit secret MOUNT for the build (``RUN --mount=type=secret``)
    with zero image/history leakage, OR a fail-closed ``needs_review`` carrying blocker
    ``secret_build_env_unsupported`` (no overlay). A plain ``ARG NPM_TOKEN`` — or, as on
    baseline, silently ignoring the token entirely and shipping a self-hostable
    candidate whose build cannot authenticate — satisfies NEITHER outcome and is the
    failure this test pins."""
    cid = _cid(closeout_name, "conv_c4sec")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _VITE_SECRET_BUILD_ENV)

    body = _release(client, cid)
    assert isinstance(body, dict)
    codes = _blocker_codes(body)
    texts = _overlay_texts(client, cid)
    dockerfile = texts.get(DOCKERFILE_PATH, "")

    failed_closed = (
        _SECRET_BUILD_BLOCKER in codes
        and body["self_host"] is False
        and _overlay_present(texts) == frozenset()
    )
    secure_mount = (
        "--mount=type=secret" in dockerfile
        and _SECRET_BUILD_VAR in dockerfile
        and f"ARG {_SECRET_BUILD_VAR}" not in dockerfile  # a plain ARG for a secret is a FAIL
    )

    assert failed_closed or secure_mount, (
        f"a secret build var ({_SECRET_BUILD_VAR}) must be a real build-secret mount OR "
        f"fail closed with blocker {_SECRET_BUILD_BLOCKER!r}; baseline does neither — it "
        "discovers no build env, so it ships a self-hostable candidate whose Dockerfile "
        "has no secret mount and no blocker (assessment="
        f"{body['assessment']!r}, blockers={sorted(codes)})."
    )


# ===========================================================================
# §8.5 — a required runtime var renders a name-only required guard so `docker compose
# config` fails when it is unset.
# ===========================================================================


def test_required_runtime_env_guard_is_name_only(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.5 (required runtime guard) — GREEN preservation.

    A required runtime var renders as ``${NAME:?<message>}`` in the emitted Compose so
    ``docker compose config``/startup fails loudly when it is unset — and the guard
    message is NAME-only (it names the var and points at ``.env.example``, never a
    value). Already correct on baseline; the fix must keep the guard name-only while it
    extends guarding to build-scope vars (asserted structurally in §8.2)."""
    cid = _cid(closeout_name, "conv_c4grd")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(
        ps,
        _store,
        cid,
        _cid(closeout_name, "proj"),
        _NODE_RUNTIME_SECRET,
        intent=_RUNTIME_SECRET_INTENT,
    )

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True

    compose = _overlay_texts(client, cid)[COMPOSE_PATH]
    guards = re.findall(r"\$\{SESSION_SECRET:\?([^}]*)\}", compose)
    assert guards, "the required runtime var SESSION_SECRET has no `${NAME:?...}` guard in compose"
    for message in guards:
        assert "SESSION_SECRET" in message and "=" not in message, (
            f"the required-guard message must be name-only, got {message!r}"
        )


# ===========================================================================
# §8.6 — npm candidates install before the build; no install/build layer depends on
# host node_modules.
# ===========================================================================


def test_npm_installs_before_build_and_dockerignore_excludes_node_modules(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.6 — GREEN preservation.

    A build-requiring npm bundle installs dependencies (``npm ci``) BEFORE the build
    (``npm run build``) in the emitted Dockerfile, and ``.dockerignore`` excludes
    ``node_modules`` so no install/build layer can depend on a host ``node_modules``.
    Already correct on baseline (an earlier remediated gap); the fix must keep it."""
    cid = _cid(closeout_name, "conv_c4ord")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _VITE_PLAIN)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True

    texts = _overlay_texts(client, cid)
    dockerfile = texts[DOCKERFILE_PATH]
    dockerignore = texts[DOCKERIGNORE_PATH]

    install_at = dockerfile.find('"npm", "ci"')
    build_at = dockerfile.find('"npm", "run", "build"')
    assert install_at != -1, "no `npm ci` install layer emitted for a build-requiring bundle"
    assert build_at != -1, "no `npm run build` layer emitted"
    assert install_at < build_at, "the npm install layer must precede the build layer"
    assert "node_modules" in {line.strip() for line in dockerignore.splitlines()}, (
        ".dockerignore must exclude node_modules so no build layer depends on host node_modules"
    )


# ===========================================================================
# §8.9 — a lockfile/package-manager disagreement fails closed; it is NOT resolved by
# precedence.
# ===========================================================================


def test_lockfile_package_manager_disagreement_fails_closed(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.9 — RED on baseline.

    A workspace carrying BOTH an npm lock and a yarn lock declares two package
    managers. That disagreement must fail closed: ``needs_review``, ``self_host:false``,
    no overlay, and a typed ``package_manager_conflict`` blocker. Baseline resolves it
    by SILENT PRECEDENCE (``_node_install`` picks ``yarn.lock`` over the npm lock) and
    ships a self-hostable candidate — exactly the "resolved by precedence" outcome §8.9
    forbids."""
    cid = _cid(closeout_name, "conv_c4lok")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _NODE_TWO_LOCKFILES)

    body = _release(client, cid)
    assert isinstance(body, dict)

    assert body["assessment"] == "needs_review", (
        "a lockfile/package-manager disagreement must fail closed to needs_review; "
        f"baseline resolves it by precedence and returns {body['assessment']!r}."
    )
    assert body["self_host"] is False and body["spec_digest"] is None
    assert _overlay_present(_overlay_texts(client, cid)) == frozenset(), (
        "a fail-closed disagreement must ship NO self-host overlay"
    )
    assert _LOCKFILE_CONFLICT_BLOCKER in _blocker_codes(body), (
        f"expected the exact typed blocker {_LOCKFILE_CONFLICT_BLOCKER!r} for a "
        f"lockfile/package-manager disagreement, saw {sorted(_blocker_codes(body))}."
    )


# ===========================================================================
# §8.10 — `.env.example` is names/comments only: no values, no secret material.
# ===========================================================================


def test_env_example_is_names_and_comments_only(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.10 — GREEN preservation.

    Every non-comment, non-blank line of the emitted ``.env.example`` is a bare
    ``NAME=`` with an EMPTY right-hand side — names and comments only, never a value or
    secret material. Already correct on baseline; the fix must keep it (public test
    values used by the live lane are supplied OUTSIDE the bundle)."""
    cid = _cid(closeout_name, "conv_c4env")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(
        ps,
        _store,
        cid,
        _cid(closeout_name, "proj"),
        _NODE_RUNTIME_SECRET,
        intent=_RUNTIME_SECRET_INTENT,
    )

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True

    env_example = _overlay_texts(client, cid)[ENV_EXAMPLE_PATH]
    assert "SESSION_SECRET=" in env_example, "precondition: the declared secret NAME is present"
    for raw in env_example.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Z_][A-Z0-9_]*=", line), (
            f".env.example carried a non-names-only line {raw!r}: an uncommented line must "
            "be a bare NAME= with no value/secret material."
        )


# ===========================================================================
# §8.11 — a persisted intent/spec shape change increments its schema version; a v1
# sidecar is migrated by a tested adapter OR rejected with `intent_upgrade_required`;
# canonical serialize/parse/serialize bytes are identical.
# ===========================================================================


def test_v1_intent_sidecar_is_migrated_or_rejected_not_crashed(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.11 (v1 sidecar) — RED on baseline.

    A host-owned intent sidecar that explicitly declares intent schema ``v1`` must,
    under the WO-C4 shape change, be MIGRATED deterministically (a valid candidate that
    honours its fields) OR rejected with a typed ``intent_upgrade_required`` blocker —
    never silently reinterpreted, and never a server crash. Baseline's ``ReleaseIntent``
    forbids the unknown ``schema_version`` key, so the sidecar read raises ``StorageError``
    and ``GET /release`` returns HTTP 500 — a crash, not a handled upgrade."""
    cid = _cid(closeout_name, "conv_c4v1")
    client, ps = _client(_store, tmp_path, monkeypatch)
    # Seed WITHOUT an intent, then write the explicit-v1 sidecar as raw JSON (host state
    # outside the workspace tree — never a mocked reader).
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _NODE_FOR_LEGACY_SIDECAR)
    ps.release_intent_for(cid).write_text(json.dumps(_LEGACY_V1_SIDECAR), encoding="utf-8")

    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, (
        "a v1-tagged intent sidecar crashed the release read instead of being migrated "
        f"or rejected: GET /release returned {res.status_code} ({res.text[:200]!r}). "
        "WO-C4 §8.11 requires a v1 sidecar to be migrated by a tested adapter or "
        "rejected with intent_upgrade_required — never a 500."
    )
    body = res.json()
    assert isinstance(body, dict)
    migrated = body["assessment"] == "candidate"
    rejected = _INTENT_UPGRADE_BLOCKER in _blocker_codes(body)
    assert migrated or rejected, (
        "a v1 sidecar must be migrated (candidate honouring its fields) OR rejected with "
        f"{_INTENT_UPGRADE_BLOCKER!r}; got assessment={body['assessment']!r}, "
        f"blockers={sorted(_blocker_codes(body))}."
    )


def test_release_json_serialize_roundtrip_is_byte_identical(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.11 (canonical bytes) — GREEN preservation.

    The ``release.json`` streamed in a candidate's ``/download`` zip is canonical: a
    parse + re-serialize reproduces byte-identical bytes, so a persisted spec cannot
    drift under an idempotent round-trip. Already correct on baseline; the fix (which
    bumps the schema version) must preserve serialize/parse/serialize byte-identity."""
    cid = _cid(closeout_name, "conv_c4rj")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(
        ps,
        _store,
        cid,
        _cid(closeout_name, "proj"),
        _NODE_RUNTIME_SECRET,
        intent=_RUNTIME_SECRET_INTENT,
    )

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True

    release_json = _overlay_texts(client, cid)[RELEASE_JSON_PATH]
    reserialized = serialize_release_spec(load_release_spec(release_json))
    assert reserialized == release_json, (
        "release.json is not serialize/parse/serialize byte-identical — the canonical "
        "form drifted under an idempotent round-trip (plan §8.11)."
    )


# ===========================================================================
# §8.11 (closeout finding C) — the EXACT upgrade blocker is surfaced (not a 500, not a
# generic ValidationError) for a sidecar this build cannot read WITHOUT GUESSING: a
# schema version NEWER than supported, OR an un-upgradeable v1 shape (a v2-forbidden
# field). A version-gate refusal stays distinct from a genuinely malformed sidecar
# (which is honestly still a 500). Route-level companion to the tool-boundary
# schema_version assertion in tools' test_c4_intent_contract.py.
# ===========================================================================

# A sidecar declaring a schema version NEWER than this build supports (v2): unreadable
# without an upgrade, so it must be `intent_upgrade_required` (needs_review) — never
# reinterpreted as v2, never a 500.
_NEWER_THAN_V2_SIDECAR: dict[str, object] = {
    "schema_version": 99,
    "start_cmd": ["node", "server.js"],
    "port_env": "PORT",
    "required_env": ["SESSION_SECRET"],
    "resources": [],
}
# An UNUPGRADEABLE v1 sidecar: a well-formed legacy shape carrying a field the v2 schema
# FORBIDS (`extra=forbid`). The v1->v2 adapter cannot map it without guessing, so it must
# be `intent_upgrade_required` — NOT a generic ValidationError / 500, and NOT silently
# reinterpreted by dropping the unknown field.
_UNUPGRADEABLE_V1_SIDECAR: dict[str, object] = {
    "schema_version": 1,
    "start_cmd": ["node", "server.js"],
    "port_env": "PORT",
    "required_env": ["SESSION_SECRET"],
    # A field the v2 ReleaseIntent schema does not define — a v1-era shape v2 forbids.
    "deploy_target": "legacy-cloud",
}


def test_newer_than_v2_intent_sidecar_is_upgrade_required_not_crash(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.11 (newer-than-v2 sidecar) — RED on tip e9c9b939 (HTTP 500).

    A host-owned intent sidecar declaring ``schema_version`` NEWER than v2 makes
    ``parse_release_intent`` raise ``IntentUpgradeError``; on the tip
    ``read_release_intent`` collapses that ``ValueError`` subclass into a
    ``StorageError`` (its ``except (ValidationError, ValueError)`` catches it), so
    ``GET /release`` returns HTTP 500. §8.11 requires it be reported as
    ``needs_review`` with the typed ``intent_upgrade_required`` blocker
    (``self_host:false``, no overlay) — a handled version gate, never a crash."""
    cid = _cid(closeout_name, "conv_c4newer")
    client, ps = _client(_store, tmp_path, monkeypatch)
    # Seed WITHOUT an intent, then write the raw newer-than-v2 sidecar as host state
    # OUTSIDE the workspace tree (never a mocked reader).
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _NODE_FOR_LEGACY_SIDECAR)
    ps.release_intent_for(cid).write_text(json.dumps(_NEWER_THAN_V2_SIDECAR), encoding="utf-8")

    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, (
        "a newer-than-v2 intent sidecar crashed the release read instead of being "
        f"reported as {_INTENT_UPGRADE_BLOCKER!r}: GET /release returned "
        f"{res.status_code} ({res.text[:200]!r}). WO-C4 §8.11 requires a version-gate "
        "refusal to be a handled needs_review, never a 500."
    )
    body = res.json()
    assert isinstance(body, dict)
    assert body["assessment"] == "needs_review", (
        f"a newer-than-v2 sidecar must be needs_review, got {body['assessment']!r}."
    )
    assert body["self_host"] is False and body["spec_digest"] is None
    assert _INTENT_UPGRADE_BLOCKER in _blocker_codes(body), (
        f"expected the exact typed blocker {_INTENT_UPGRADE_BLOCKER!r}, saw "
        f"{sorted(_blocker_codes(body))}."
    )
    assert _overlay_present(_overlay_texts(client, cid)) == frozenset(), (
        "a version-gate refusal must ship NO self-host overlay"
    )


def test_unupgradeable_v1_intent_sidecar_is_upgrade_required_not_validation_error(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.11 (unupgradeable v1 sidecar) — RED on tip e9c9b939 (HTTP 500).

    A v1 sidecar carrying a field the v2 schema FORBIDS is well-formed but cannot be
    migrated without guessing. On the tip the v1->v2 adapter drops the version tag and
    revalidates, tripping a generic ``extra_forbidden`` ``ValidationError`` that
    ``read_release_intent`` collapses into a ``StorageError`` -> HTTP 500 — the exact
    upgrade blocker is never surfaced. §8.11: "If v1 sidecars cannot be upgraded without
    guessing, they return ``needs_review`` with ``intent_upgrade_required``; they are
    not silently reinterpreted." The fix must report that typed blocker
    (``needs_review``, ``self_host:false``) rather than crash or silently drop the
    unknown field. (A genuinely MALFORMED sidecar stays an honest StorageError/500 —
    the distinction is preserved.)"""
    cid = _cid(closeout_name, "conv_c4v1bad")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _NODE_FOR_LEGACY_SIDECAR)
    ps.release_intent_for(cid).write_text(
        json.dumps(_UNUPGRADEABLE_V1_SIDECAR), encoding="utf-8"
    )

    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, (
        "an unupgradeable v1 intent sidecar crashed the release read instead of being "
        f"reported as {_INTENT_UPGRADE_BLOCKER!r}: GET /release returned "
        f"{res.status_code} ({res.text[:200]!r}). WO-C4 §8.11 requires an un-upgradeable "
        "v1 shape to be a handled needs_review with the exact upgrade blocker, never a "
        "generic ValidationError / 500."
    )
    body = res.json()
    assert isinstance(body, dict)
    assert body["assessment"] == "needs_review", (
        f"an unupgradeable v1 sidecar must be needs_review, got {body['assessment']!r}."
    )
    assert body["self_host"] is False and body["spec_digest"] is None
    assert _INTENT_UPGRADE_BLOCKER in _blocker_codes(body), (
        f"expected the exact typed blocker {_INTENT_UPGRADE_BLOCKER!r} for an "
        f"unupgradeable v1 sidecar, saw {sorted(_blocker_codes(body))}."
    )
    assert _overlay_present(_overlay_texts(client, cid)) == frozenset(), (
        "a version-gate refusal must ship NO self-host overlay"
    )


# ===========================================================================
# §8.4 (closeout finding A) — a SOURCE-DISCOVERED secret-classed build var must fail
# closed exactly like the `.npmrc` case, never fold into plain build.args while
# shipping a self-hostable candidate. "A plain ARG for a secret-classed build variable
# is a FAILURE" applies regardless of discovery source (`.npmrc` OR source-discovered).
# ===========================================================================

# A Vite bundle whose CLIENT source reads a SECRET-shaped build var discovered from
# SOURCE (not from an `.npmrc`): `import.meta.env.VITE_API_TOKEN`. `VITE_API_TOKEN` is
# secret-shaped (the `TOKEN` marker) and build-scoped, so `_build_env_decls` classifies
# it `secret=True`. It must fail closed like the `.npmrc` secret build var (§8.4) — it
# must NOT be folded into compose `build.args` as `${VITE_API_TOKEN:?...}` on a
# self-hostable candidate.
_VITE_SOURCE_SECRET_BUILD_ENV: dict[str, bytes] = {
    "index.html": (
        b'<!doctype html><html><body><script type="module" src="/src/main.js"></script>'
        b"</body></html>\n"
    ),
    "package.json": b'{"name":"spa","scripts":{"build":"vite build"}}',
    "package-lock.json": b'{"lockfileVersion":3,"name":"spa"}',
    "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
    "src/main.js": b"document.body.append(import.meta.env.VITE_API_TOKEN);\n",
}
_SOURCE_SECRET_BUILD_VAR = "VITE_API_TOKEN"


def test_source_discovered_secret_build_var_fails_closed(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.4 (source-discovered secret build var) — RED on tip e9c9b939.

    A Vite client bundle reads ``import.meta.env.VITE_API_TOKEN`` — a SECRET-shaped
    build var discovered from SOURCE, not from an ``.npmrc``. ``_build_env_decls``
    classifies it ``secret=True`` yet the tip takes NEITHER the fail-closed nor the
    secret-mount branch: it folds the var into compose ``build.args`` as
    ``${VITE_API_TOKEN:?...}`` (while ``_build_arg_names`` filters the Dockerfile
    ``ARG`` to public-only) and ships an ``assessment=candidate``, ``self_host:true``
    verdict with NO blocker. §8.4: "A plain ARG for a secret-classed build variable is
    a FAILURE." — the same holds for a plain ``build.args`` entry. The fix must fail
    closed with ``secret_build_env_unsupported`` (``needs_review``, ``self_host:false``,
    no overlay), regardless of discovery source."""
    cid = _cid(closeout_name, "conv_c4ssb")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _VITE_SOURCE_SECRET_BUILD_ENV)

    body = _release(client, cid)
    assert isinstance(body, dict)
    codes = _blocker_codes(body)
    texts = _overlay_texts(client, cid)
    compose = texts.get(COMPOSE_PATH, "")

    failed_closed = (
        _SECRET_BUILD_BLOCKER in codes
        and body["assessment"] == "needs_review"
        and body["self_host"] is False
        and _overlay_present(texts) == frozenset()
    )
    # The precise leak this test pins: a self-hostable candidate whose emitted compose
    # carries the secret build var as a plain `build.args` entry.
    leaked_into_build_args = body["self_host"] is True and _SOURCE_SECRET_BUILD_VAR in compose
    assert failed_closed, (
        f"a source-discovered secret build var ({_SOURCE_SECRET_BUILD_VAR}) must fail "
        f"closed with {_SECRET_BUILD_BLOCKER!r} (needs_review, self_host:false, no "
        f"overlay); got assessment={body['assessment']!r}, self_host={body['self_host']!r}, "
        f"blockers={sorted(codes)}, leaked_into_build_args={leaked_into_build_args}."
    )


# ===========================================================================
# §8.8 (closeout finding E) — package-manager lowering must REJECT an unsupported
# toolchain (bun/pnpm/yarn — and poetry/uv for python) with `toolchain_unsupported`,
# never silently map it to npm/pip. "Merely mapping them to Node/Python is FAILURE."
# The LIVE install/pin proof stays deferred to the C8 lane; here we pin the STATIC
# reject-instead-of-map contract only.
# ===========================================================================

_TOOLCHAIN_UNSUPPORTED_BLOCKER = "toolchain_unsupported"

# ROOT lockfiles each naming a package manager the neutral base image does NOT
# provision. Pre-fix the lockfile-driven lowering silently maps `bun.lockb` → `npm
# install`, and emits `pnpm`/`yarn` install commands the image never provisions (no
# `corepack enable`) — shipping a self-hostable candidate either way. Each single
# unsupported lockfile (with NO competing lockfile, so it is NOT the §8.9
# package_manager_conflict case) must instead fail closed with `toolchain_unsupported`.
_UNSUPPORTED_PM_LOCKFILES: dict[str, bytes] = {
    "bun.lockb": b"bun-lockfile-v1\n",
    "pnpm-lock.yaml": b"lockfileVersion: '9.0'\n",
    "yarn.lock": b"# yarn lockfile v1\n",
}


def _node_with_lockfile(lockfile: str, content: bytes) -> dict[str, bytes]:
    """A minimal node service (binds `$PORT`, no undeclared env) carrying exactly one
    package-manager lockfile — so the ONLY release-blocking signal is that lockfile's
    toolchain."""
    return {
        "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
        lockfile: content,
        "server.js": (
            b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"
        ),
    }


@pytest.mark.parametrize("lockfile", sorted(_UNSUPPORTED_PM_LOCKFILES))
def test_unsupported_package_manager_lockfile_fails_closed(
    lockfile: str,
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.8 (unsupported package-manager lowering) — RED on tip e9c9b939.

    A node service whose ROOT lockfile names a package manager the neutral base image
    does not provision (``bun.lockb`` / ``pnpm-lock.yaml`` / ``yarn.lock``) is, on the
    tip, SILENTLY LOWERED: ``bun.lockb`` maps to ``npm install`` and pnpm/yarn emit an
    install command the image never provisions — either way shipping an
    ``assessment=candidate``, ``self_host:true`` verdict. §8.8: "Bun, Poetry, uv, pnpm,
    and Yarn are either installed/pinned and live-tested or rejected with
    ``toolchain_unsupported``. Merely mapping them to Node/Python is FAILURE." The fix
    must fail closed with ``toolchain_unsupported`` (``needs_review``,
    ``self_host:false``, no overlay). (The LIVE install/pin proof is the C8 lane; this
    pins only the STATIC reject-instead-of-map contract.)"""
    cid = _cid(closeout_name, "conv_c4pm")
    client, ps = _client(_store, tmp_path, monkeypatch)
    files = _node_with_lockfile(lockfile, _UNSUPPORTED_PM_LOCKFILES[lockfile])
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)

    assert body["assessment"] == "needs_review", (
        f"a {lockfile} project names an unsupported package manager and must fail closed "
        f"to needs_review; the lockfile-driven lowering silently mapped it and returned "
        f"{body['assessment']!r}."
    )
    assert body["self_host"] is False and body["spec_digest"] is None
    assert _overlay_present(_overlay_texts(client, cid)) == frozenset(), (
        f"a fail-closed {lockfile} project must ship NO self-host overlay"
    )
    assert _TOOLCHAIN_UNSUPPORTED_BLOCKER in _blocker_codes(body), (
        f"expected the exact typed blocker {_TOOLCHAIN_UNSUPPORTED_BLOCKER!r} for a "
        f"{lockfile} project, saw {sorted(_blocker_codes(body))}."
    )


# ===========================================================================
# §8.8 (closeout finding — no-lockfile toolchain bypass) — the unsupported-toolchain
# reject must ALSO fire when the AUTHORITATIVE package-manager declaration is NOT a
# committed lockfile: a corepack ``package.json`` ``"packageManager"`` field naming
# bun/pnpm/yarn, or an unambiguous workspace marker (``pnpm-workspace.yaml``). Each is an
# explicit declaration of a toolchain the neutral base image does not provision and must
# fail closed with ``toolchain_unsupported`` — never a SILENT map onto npm. Pre-fix the
# reject keyed ONLY on committed root lockfiles, so a ``packageManager``-field-only or a
# ``pnpm-workspace.yaml``-only project bypassed the gate and shipped a self-hostable
# candidate whose install step was silently mapped to ``npm install`` (§8.8: "Merely
# mapping them to Node/Python is FAILURE").
# ===========================================================================

# A node service that binds ``$PORT`` and reads no undeclared env, whose ONLY
# release-blocking signal is a corepack ``packageManager:"pnpm@8"`` declaration with NO
# lockfile. Pre-fix the lockfile-only reject misses it and the lowering silently maps it
# to ``npm install``, shipping a self-hostable candidate.
_NODE_PM_FIELD_NO_LOCKFILE: dict[str, bytes] = {
    "package.json": (
        b'{"name":"svc","packageManager":"pnpm@8.15.0",'
        b'"scripts":{"start":"node server.js"}}'
    ),
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}

# A node service whose ONLY release-blocking signal is a ``pnpm-workspace.yaml`` marker
# with NO lockfile — an unambiguous pnpm declaration the base image does not provision.
_NODE_PNPM_WORKSPACE_NO_LOCKFILE: dict[str, bytes] = {
    "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
    "pnpm-workspace.yaml": b"packages:\n  - 'apps/*'\n",
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}

# A plain npm node service that EXPLICITLY declares ``packageManager:"npm@…"`` (npm IS
# provisioned) — the no-false-positive control: the toolchain reject must NOT fire, so it
# stays a self-hostable candidate.
_NODE_NPM_PM_FIELD: dict[str, bytes] = {
    "package.json": (
        b'{"name":"svc","packageManager":"npm@10.5.0",'
        b'"scripts":{"start":"node server.js"}}'
    ),
    "package-lock.json": b'{"lockfileVersion":3,"name":"svc"}',
    "server.js": (b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"),
}


@pytest.mark.parametrize(
    ("label", "files"),
    [
        ("packageManager_field", _NODE_PM_FIELD_NO_LOCKFILE),
        ("pnpm_workspace_marker", _NODE_PNPM_WORKSPACE_NO_LOCKFILE),
    ],
)
def test_unsupported_toolchain_without_lockfile_fails_closed(
    label: str,
    files: dict[str, bytes],
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.8 (no-lockfile toolchain bypass) — RED on tip 6c58f84e.

    A node service whose authoritative package-manager declaration is NOT a committed
    lockfile — a corepack ``packageManager:"pnpm@8"`` field, or a ``pnpm-workspace.yaml``
    marker — names a toolchain the neutral base image does not provision. On the tip the
    reject keys ONLY on committed root lockfiles, so this bypasses the gate: the lowering
    silently maps it to ``npm install`` and ships an ``assessment=candidate``,
    ``self_host:true`` verdict. §8.8 requires it to fail closed with the exact typed
    ``toolchain_unsupported`` blocker (``needs_review``, ``self_host:false``, no overlay),
    exactly like a committed unsupported lockfile."""
    cid = _cid(closeout_name, "conv_c4nolk")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), files)

    body = _release(client, cid)
    assert isinstance(body, dict)

    assert body["assessment"] == "needs_review", (
        f"a {label} project names an unprovisioned package manager with NO lockfile and "
        "must fail closed to needs_review; the tip keyed the reject only on committed "
        f"lockfiles and silently mapped it to npm, returning {body['assessment']!r}."
    )
    assert body["self_host"] is False and body["spec_digest"] is None
    assert _overlay_present(_overlay_texts(client, cid)) == frozenset(), (
        f"a fail-closed {label} project must ship NO self-host overlay"
    )
    assert _TOOLCHAIN_UNSUPPORTED_BLOCKER in _blocker_codes(body), (
        f"expected the exact typed blocker {_TOOLCHAIN_UNSUPPORTED_BLOCKER!r} for a "
        f"{label} project, saw {sorted(_blocker_codes(body))}."
    )


def test_npm_package_manager_field_stays_a_candidate(
    _store: SqliteEventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closeout_name: object,
) -> None:
    """WO-C4 §8.8 (no false toolchain reject) — GREEN preservation.

    A node service that explicitly declares ``packageManager:"npm@10"`` names the ONE
    package manager the neutral base image provisions, so the no-lockfile toolchain reject
    must NOT fire — it stays a self-hostable ``candidate`` with no ``toolchain_unsupported``
    blocker. Guards the finding-#1 fix against over-rejecting a plain npm project."""
    cid = _cid(closeout_name, "conv_c4npm")
    client, ps = _client(_store, tmp_path, monkeypatch)
    _seed_project(ps, _store, cid, _cid(closeout_name, "proj"), _NODE_NPM_PM_FIELD)

    body = _release(client, cid)
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True, (
        "an explicit npm packageManager declaration must stay a self-hostable candidate; "
        f"got assessment={body['assessment']!r}, self_host={body['self_host']!r}."
    )
    assert _TOOLCHAIN_UNSUPPORTED_BLOCKER not in _blocker_codes(body), (
        "npm is provisioned — the no-lockfile toolchain reject must not fire for it, saw "
        f"{sorted(_blocker_codes(body))}."
    )

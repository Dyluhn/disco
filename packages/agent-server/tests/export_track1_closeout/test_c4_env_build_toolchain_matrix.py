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

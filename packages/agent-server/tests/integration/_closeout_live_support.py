"""Support for the WO-C8 live clean-room bundle-lifecycle matrix (plan §12).

FROZEN acceptance support (WO-C0, plan §1.1). This module is imported by the sole
live-lane test module,
``packages/agent-server/tests/integration/test_export_track1_closeout_live.py``.
It is deliberately NOT a test module (its name does not match ``test_*``), so pytest
never collects it directly — it only provides the fixture-workspace builders, the
real ``/release`` -> bound ``/download`` bundle acquisition, the Docker Compose
orchestration, the secret sentinel scans, and the guaranteed-cleanup teardown the
six fixture tests share.

Boundary (plan §1.2/§12.1): every bundle is produced by driving the REAL FastAPI
``/release`` route (a real ``ProjectStore`` + a real committed ``VersionRecord``)
and then the REAL bound ``/download`` route (``?version_seq=&spec_digest=``); the
returned bytes are the artifact Docker builds and runs. Nothing here mocks the
route, the store, the download, or Docker. The only ``monkeypatch`` seam is
``ConfigStore.load`` (the exact injection the settings PUT performs), used to point
the runtime at the per-test projects root.

No Docker on the authoring host: the ``docker compose`` steps cannot run here, so
``require_live_runtime`` FAILS (never skips, plan §1.3/§4.5) before any lifecycle
step. On the self-hosted Docker host that runs this lane, ``require_live_runtime``
passes and the full build -> up -> HTTP -> restart -> state -> cleanup lifecycle
executes for real.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.appkit import (
    APPSPEC_RELPATH,
    default_records_auth_app_spec,
    generate,
    get_recipe,
    serialize_app_spec,
)
from disco.core.appkit.spec import AppSpec
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

# The compose host-port variable every emitted overlay interpolates on the host
# side (`127.0.0.1:${HOST_PORT:-...}:<container>`) — kept in sync with
# `local_compose._HOST_PORT_VAR`. The test assigns it a loopback-bound ephemeral
# port so ingress is reachable ONLY on 127.0.0.1 (plan §12.4).
HOST_PORT_VAR = "HOST_PORT"

# The complete single-service self-host overlay path set (plan §7 / §6.2). Used to
# separate generated overlay entries from source entries when scanning a bundle.
OVERLAY_NAMES = frozenset(
    {
        COMPOSE_PATH,
        DOCKERFILE_PATH,
        DOCKERIGNORE_PATH,
        ENV_EXAMPLE_PATH,
        SELFHOST_DOC_PATH,
        RELEASE_JSON_PATH,
    }
)

# Distinctive sentinels planted at test time (never committed). The env sentinel is
# a required RUNTIME secret intentionally present in the running container's env
# (plan §12.9: runtime inspection is sanitized); it must be absent from every
# bundle byte, extracted build context, image layer/history/filesystem, and log.
SECRET_ENV_SENTINEL = "DISCO-C8-RUNTIME-SECRET-9d1f7a3e"
# The public build marker MUST reach the built Vite asset (plan §12.10 clause 1).
PUBLIC_BUILD_MARKER = "disco-c8-public-banner-6b2c9e"
# The secret build sentinel must be ABSENT from image layers/history/files, OR the
# bundle is correctly rejected as unsupported (plan §12.10 clause 2).
SECRET_BUILD_SENTINEL = "DISCO-C8-BUILD-SECRET-4f8a1d2c"

# The compose label every resource created for a project carries. Cleanup queries
# it to prove ZERO resources with the test's unique project name remain (§12.11).
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

_DEFAULT_COMPOSE_TIMEOUT = 600
_DEFAULT_DOCKER_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Live-runtime requirement — FAILS (never skips) when Docker/Compose is absent
# (plan §1.3, §4.5). Every fixture test calls this first, so the whole lane fails
# closed on the authoring host that has no Docker.
# ---------------------------------------------------------------------------


def docker_compose_available() -> tuple[bool, str]:
    """``(True, version)`` iff a real ``docker compose`` v2 provider answers here;
    otherwise ``(False, reason)``. Any launch failure is a reason, never an
    exception the caller might mistake for "unknown"."""
    docker = shutil.which("docker")
    if docker is None:
        return False, "docker executable not found on PATH"
    try:
        proc = subprocess.run(
            [docker, "compose", "version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"`docker compose version` did not launch: {exc}"
    if proc.returncode != 0:
        return False, f"`docker compose version` exited {proc.returncode}: {proc.stderr.strip()}"
    return True, proc.stdout.strip()


def require_live_runtime() -> None:
    """Assert (never skip) that a real Docker Engine + Compose v2 are present.

    On the authoring host there is no Docker, so this raises ``AssertionError`` and
    the calling fixture test FAILS — the CORRECT red state (plan §1.3/§4.5: a
    skip/skipif/env-gated early return is an automatic failure). On the self-hosted
    Docker runner it passes and the lifecycle proceeds."""
    docker = shutil.which("docker")
    assert docker is not None, (
        "Docker Engine is required for the export-track1-closeout live lane and was "
        "not found on PATH. This lane must FAIL — not skip — without a real engine "
        "(plan §1.3/§4.5). Run it on the self-hosted Docker host."
    )
    ok, detail = docker_compose_available()
    assert ok, (
        "Docker Compose v2 is required for the export-track1-closeout live lane: "
        f"{detail}. The lane fails closed rather than skipping."
    )


def assign_loopback_port() -> int:
    """An ephemeral TCP port the kernel just confirmed is free on 127.0.0.1. The
    overlay publishes ingress at ``127.0.0.1:<port>`` so it is reachable only on
    loopback (plan §12.4)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Real ASGI release harness (mirrors the frozen C2/C3 route harness). A real
# ProjectStore rooted at the per-test tmp dir; only ConfigStore.load is seamed.
# ---------------------------------------------------------------------------


def release_client(
    store: SqliteEventStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
    """A ``TestClient`` over the real agent-server ASGI app plus the matching real
    ``ProjectStore``, both pointed at ``root``. Only ``ConfigStore.load`` is seamed
    (the exact injection the settings PUT performs)."""
    cfg = RouterConfig.model_validate(
        {
            "models": {"m": {"model_id": "m", "provider": "fake", "context_window": 8192}},
            "default_model": "m",
        }
    )
    cfg = cfg.model_copy(update={"projects": ProjectStorageSettings(projects_root=str(root))})
    cfg_store = ConfigStore(path=Path("/dev/null"))
    monkeypatch.setattr(cfg_store, "load", lambda: cfg)
    runtime = ConversationRuntime(store, config=cfg, config_store=cfg_store)
    return TestClient(create_app(store, runtime=runtime)), ProjectStore(str(root))


def seed_and_cut(
    ps: ProjectStore,
    store: SqliteEventStore,
    cid: str,
    title: str,
    files: Mapping[str, bytes],
    *,
    intent: ReleaseIntent | None = None,
    imported: bool = False,
    owner_id: str = "local",
) -> None:
    """Seed a real on-disk workspace + manifest + optional host-owned intent
    sidecar + conversation record, then cut a real ``VersionRecord`` so the live
    tree matches a committed version (the C2 source binding is satisfied and a bound
    download can name a real immutable version)."""
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
        created_at="2026-07-12T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=imported,
    )
    if intent is not None:
        ps.write_release_intent(cid, intent)
    store.create_conversation(cid, owner_id=owner_id, title=title, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1, "precondition: a real version 1 was committed"


def release_body(client: TestClient, cid: str) -> dict[str, object]:
    """The real ``GET /api/projects/{cid}/release`` JSON body."""
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)
    return body


def bound_download_to_dir(
    client: TestClient, cid: str, body: Mapping[str, object], dest: Path
) -> Path:
    """Download the bundle through the bound ``/download`` flow (plan §12.1) —
    ``?version_seq=N&spec_digest=D`` from the ``/release`` response — and extract it
    into a FRESH directory ``dest``. Returns ``dest``."""
    seq = body["version_seq"]
    digest = body["spec_digest"]
    assert seq is not None and isinstance(digest, str), (
        "a candidate bundle must expose a real version_seq + spec_digest to bind the "
        f"download to; got version_seq={seq!r} spec_digest={digest!r}"
    )
    url = f"/api/projects/{cid}/download?version_seq={seq}&spec_digest={digest}"
    res = client.get(url)
    assert res.status_code == 200, res.text
    assert res.headers.get("content-type") == "application/zip", res.headers.get("content-type")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        zf.extractall(dest)
    return dest


def bundle_zip_bytes(client: TestClient, cid: str, body: Mapping[str, object]) -> bytes:
    """The raw bound-download zip bytes (for a whole-archive secret scan, §12.9)."""
    seq = body["version_seq"]
    digest = body["spec_digest"]
    assert isinstance(digest, str)
    res = client.get(f"/api/projects/{cid}/download?version_seq={seq}&spec_digest={digest}")
    assert res.status_code == 200, res.text
    return res.content


# ---------------------------------------------------------------------------
# The six live fixture workspaces (plan §12 matrix). Built from bytes authored
# here — real, minimal, BUILDABLE project shapes modelled on the proven committed
# release_e2e fixtures. Only the project id / title / compose project name vary
# from the recorded seed (the source bytes are the contract under test).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureWorkspace:
    """A live fixture: its source byte-tree, optional host-owned intent, imported
    provenance, the health path its ingress serves, and a distinctive substring the
    GET-health body must contain (plan §12.4 "fixture-specific meaningful body")."""

    files: dict[str, bytes]
    health_path: str
    body_marker: bytes
    intent: ReleaseIntent | None = None
    imported: bool = False


def express_fixture() -> FixtureWorkspace:
    """Express/Node: express dependency (npm-install path, no lockfile), a real
    ``GET /`` HTML route, binds ``$PORT``, and a required secret RUNTIME env
    (``APP_SECRET``) so §12.8 (missing-env fails) and §12.9 (secret sanitized)
    apply."""
    files = {
        "package.json": (
            b'{"name":"c8-express","private":true,"version":"1.0.0","main":"server.js",'
            b'"scripts":{"start":"node server.js"},"dependencies":{"express":"^4.19.2"}}'
        ),
        "server.js": (
            b"const express = require('express');\n"
            b"const app = express();\n"
            b"const PORT = process.env.PORT || 8080;\n"
            b"app.get('/', (_req, res) => {\n"
            b"  res.type('html').send('<!doctype html><title>c8-express</title>"
            b"<h1>disco-c8-express-alive</h1>');\n"
            b"});\n"
            b"app.listen(PORT, '0.0.0.0');\n"
        ),
    }
    intent = ReleaseIntent(
        start_cmd=("node", "server.js"),
        required_env=("APP_SECRET",),
        health_path="/",
    )
    return FixtureWorkspace(
        files=files, health_path="/", body_marker=b"disco-c8-express-alive", intent=intent
    )


def fastapi_fixture() -> FixtureWorkspace:
    """FastAPI: root module + ``app``, declared deps, binds ``$PORT``, GET / body."""
    files = {
        "requirements.txt": b"fastapi==0.111.0\nuvicorn==0.30.1\n",
        "main.py": (
            b"from __future__ import annotations\n\n"
            b"from fastapi import FastAPI\n"
            b"from fastapi.responses import HTMLResponse\n\n"
            b"app = FastAPI()\n\n\n"
            b"@app.get('/', response_class=HTMLResponse)\n"
            b"def root() -> str:\n"
            b"    return '<!doctype html><title>c8-fastapi</title>"
            b"<h1>disco-c8-fastapi-alive</h1>'\n"
        ),
    }
    intent = ReleaseIntent(start_cmd=("uvicorn", "main:app"), health_path="/")
    return FixtureWorkspace(
        files=files, health_path="/", body_marker=b"disco-c8-fastapi-alive", intent=intent
    )


def imported_node_fixture() -> FixtureWorkspace:
    """Imported Node: a plain ``http`` server shipped WITH a lockfile (npm-ci path),
    ``imported=True`` so it travels the genuine imported provenance rung, GET / body.
    No typed intent — detection must recognize the stack."""
    files = {
        "package.json": (
            b'{"name":"c8-imported-node","private":true,"version":"1.0.0",'
            b'"main":"index.js","scripts":{"start":"node index.js"}}'
        ),
        "package-lock.json": (
            b'{"name":"c8-imported-node","version":"1.0.0","lockfileVersion":3,'
            b'"requires":true,"packages":{"":{"name":"c8-imported-node",'
            b'"version":"1.0.0"}}}'
        ),
        "index.js": (
            b"const http = require('http');\n"
            b"const PORT = process.env.PORT || 8080;\n"
            b"http.createServer((_req, res) => {\n"
            b"  res.writeHead(200, { 'Content-Type': 'text/html' });\n"
            b"  res.end('<!doctype html><title>c8-imported</title>"
            b"<h1>disco-c8-imported-alive</h1>');\n"
            b"}).listen(PORT, '0.0.0.0');\n"
        ),
    }
    return FixtureWorkspace(
        files=files, health_path="/", body_marker=b"disco-c8-imported-alive", imported=True
    )


def _vite_index_html() -> bytes:
    return (
        b"<!doctype html><html><head><title>c8-vite</title></head><body>"
        b"<div id='app'>disco-c8-vite-alive</div>"
        b"<script type='module' src='/src/main.js'></script>"
        b"</body></html>\n"
    )


def vite_fixture() -> FixtureWorkspace:
    """Vite/static: npm Vite build with a statically-resolved ``dist`` output dir,
    served as a static site. The served ``index.html`` carries a distinctive body
    marker (plan §12.4)."""
    files = {
        "index.html": _vite_index_html(),
        "package.json": (
            b'{"name":"c8-vite","private":true,"version":"1.0.0",'
            b'"scripts":{"build":"vite build"},"devDependencies":{"vite":"^5.4.0"}}'
        ),
        "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
        "src/main.js": b"document.getElementById('app').textContent = 'disco-c8-vite-alive';\n",
    }
    intent = ReleaseIntent(build_cmd=("npm", "run", "build"), output_dir="dist", health_path="/")
    return FixtureWorkspace(
        files=files, health_path="/", body_marker=b"disco-c8-vite-alive", intent=intent
    )


def public_build_env_vite_fixture() -> FixtureWorkspace:
    """public-build-env Vite: the source reads a PUBLIC build var
    (``import.meta.env.VITE_PUBLIC_BANNER``) that Vite inlines into the built asset
    at build time, plus a SECRET-shaped build var
    (``import.meta.env.VITE_ADMIN_SECRET``). Post-C4 the public var lowers to a
    Compose build arg + Dockerfile ARG and reaches the asset; the secret-shaped var
    must use a real secret mount with zero leakage OR the bundle is rejected
    (``secret_build_env_unsupported``) — the §12.10 disjunction."""
    main_js = (
        b"const banner = import.meta.env.VITE_PUBLIC_BANNER;\n"
        b"const admin = import.meta.env.VITE_ADMIN_SECRET;\n"
        b"document.getElementById('app').textContent = 'disco-c8-pubvite-alive ' + banner;\n"
        b"if (admin && admin.length < 0) { console.log(admin); }\n"
    )
    files = {
        "index.html": (
            b"<!doctype html><html><head><title>c8-pubvite</title></head><body>"
            b"<div id='app'>disco-c8-pubvite-alive</div>"
            b"<script type='module' src='/src/main.js'></script>"
            b"</body></html>\n"
        ),
        "package.json": (
            b'{"name":"c8-pubvite","private":true,"version":"1.0.0",'
            b'"scripts":{"build":"vite build"},"devDependencies":{"vite":"^5.4.0"}}'
        ),
        "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
        "src/main.js": main_js,
    }
    intent = ReleaseIntent(build_cmd=("npm", "run", "build"), output_dir="dist", health_path="/")
    return FixtureWorkspace(
        files=files, health_path="/", body_marker=b"disco-c8-pubvite-alive", intent=intent
    )


def appkit_fixture() -> tuple[FixtureWorkspace, AppSpec, str]:
    """AppKit: a records app GENERATED in-test (never a committed tree) with the
    ``.disco/appspec.json`` contract file so the detector keys off the full AppKit
    shape. Returns the workspace, the AppSpec (to derive the record route/payload),
    and the generated worker source. Its ingress serves ``/`` and persists local D1
    state on a named volume; ``ADMIN_TOKEN`` is a required secret."""
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None, "the editorial-ledger recipe must exist to generate AppKit"
    app = default_records_auth_app_spec("Disco Closeout Records", recipe)
    tree = generate(app, recipe.to_design_spec())
    tree[APPSPEC_RELPATH] = serialize_app_spec(app)
    worker_src = next(v for k, v in tree.items() if k.endswith("worker/index.ts"))
    files = {rel: content.encode("utf-8") for rel, content in tree.items()}
    workspace = FixtureWorkspace(
        files=files, health_path="/", body_marker=b"<!doctype html", intent=None
    )
    return workspace, app, worker_src


def appkit_record_plan(app: AppSpec, worker_src: str) -> tuple[str, dict[str, object], str, str]:
    """Derive, from the GENERATED AppSpec + worker source, an FK-free writable record
    route, a valid POST payload, a unique read-back marker, and a ROLE whose session
    may write that route (acceptance-v5: the generated app's documented auth model is
    session + RBAC, so §12.6 drives register→login→cookie with a real role).

    Reads the real artifacts (no hard-coded route/role): pick the first entity whose
    required fields are all scalar (no ``*_id`` foreign-key column), match it to the
    worker's literal ``/api/<table>`` route, build a typed payload from its required
    fields (email -> the marker email, numeric -> a number, else -> text), and pick
    the entity's first declared write role (else read role, else the app's first
    role — an empty write_roles means any authenticated session may write)."""
    routes = set(re.findall(r'"(/api/[A-Za-z0-9_]+)"', worker_src))

    def _is_fk(field_name: str, field_type: str) -> bool:
        return field_type in ("int", "integer") and field_name.endswith("_id")

    marker = f"c8-{os.urandom(6).hex()}@example.test"
    for entity in app.entities:
        required = [f for f in entity.fields if f.required]
        if any(_is_fk(f.name, f.type) for f in required):
            continue
        norm = str(entity.id).replace("-", "_")
        route = next((r for r in routes if r.split("/api/", 1)[-1].replace("-", "_") == norm), None)
        if route is None:
            continue
        payload: dict[str, object] = {}
        for f in required:
            name = str(f.name)
            ftype = str(f.type)
            if ftype == "email":
                payload[name] = marker
            elif ftype in ("int", "integer", "float", "real", "number"):
                payload[name] = 1
            elif ftype in ("datetime", "date"):
                payload[name] = "2026-07-12T00:00:00Z"
            else:
                payload[name] = marker
        role_pool = tuple(entity.write_roles) or tuple(entity.read_roles) or tuple(app.roles)
        assert role_pool, "the generated auth app declares no roles to register with"
        return route, payload, marker, str(role_pool[0])
    raise AssertionError(
        "no foreign-key-free writable record entity with a matching /api route was "
        f"found in the generated AppKit worker (routes={sorted(routes)})"
    )


def http_response(
    port: int,
    path: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Like ``http_status_body`` but ALSO returns the response headers (lower-cased
    names), so a login's ``Set-Cookie`` session can be captured (acceptance-v5
    §12.6 register→login→cookie flow)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=data,
        headers=dict(headers or {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def session_cookie_from(headers: Mapping[str, str]) -> str:
    """The ``session=<token>`` pair from a login response's ``Set-Cookie`` header
    (attributes stripped), ready to send back as a ``Cookie`` request header."""
    raw = headers.get("set-cookie", "")
    pair = raw.split(";", 1)[0].strip()
    assert pair.startswith("session=") and len(pair) > len("session="), (
        f"login did not set a session cookie: {raw!r}"
    )
    return pair


# ---------------------------------------------------------------------------
# Docker Compose orchestration + a unique-project cleanup that runs even on
# assertion failure and proves ZERO labelled resources remain (plan §12.11).
# ---------------------------------------------------------------------------


@dataclass
class ComposeBundle:
    """A downloaded bundle under a UNIQUE compose project name. Every ``docker
    compose`` invocation carries ``-p <project>`` so its containers/networks/volumes
    are labelled with that name and can be torn down + proven-clean afterwards."""

    project: str
    bundle_dir: Path
    captured_output: list[str] = field(default_factory=list)

    # -- primitives -------------------------------------------------------------

    def compose(
        self, *args: str, timeout: int = _DEFAULT_COMPOSE_TIMEOUT, record: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Run ``docker compose -p <project> <args>`` from the bundle dir. Fixed
        argv list, no shell. Output is captured (and recorded for the secret scan).

        ``record=False`` exists for EXACTLY ONE caller — ``exec_env``'s deliberate
        sanitized secret-presence probe (§12.9), whose stdout IS the secret by
        design and would otherwise self-poison the leak sweep it feeds
        (acceptance-v5 correction, owner-adjudicated 2026-07-17). Every ordinary
        Compose lifecycle surface stays recorded and swept."""
        proc = subprocess.run(
            ["docker", "compose", "-p", self.project, *args],
            cwd=self.bundle_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if record:
            self.captured_output.append(proc.stdout)
            self.captured_output.append(proc.stderr)
        return proc

    def docker(
        self, *args: str, timeout: int = _DEFAULT_DOCKER_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        self.captured_output.append(proc.stdout)
        self.captured_output.append(proc.stderr)
        return proc

    def docker_binary(self, *args: str, timeout: int = _DEFAULT_COMPOSE_TIMEOUT) -> bytes:
        """A docker invocation whose STDOUT is binary (``export``/``save``)."""
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.stdout

    def image_name(self, service: str) -> str:
        """The local image compose builds for a service (``<project>-<service>``)."""
        return f"{self.project}-{service}"

    # -- lifecycle --------------------------------------------------------------

    def config_json(self) -> dict[str, object]:
        """The STRUCTURAL compose model, parsed. Also asserts the plain
        ``config --quiet`` exits 0 with the REAL ``.env`` (plan §12.2).

        acceptance-v5 (owner-adjudicated 2026-07-17, ruling E): the JSON rendering
        feeds ONLY the topology assertions, so it runs with ``--no-interpolate
        --no-env-resolution`` — the structural model WITHOUT resolving externally
        supplied env values. A secret supplied through the runner's ``.env`` never
        enters this output, so the rendering stays RECORDED and inside the §12.9
        sweep (a secret literally embedded in ``compose.yaml`` itself still appears
        here and still fails the sweep). The ``config --quiet`` validation gate is
        unchanged and evaluates the real interpolation."""
        quiet = self.compose("config", "--quiet")
        assert quiet.returncode == 0, f"`docker compose config --quiet` failed:\n{quiet.stderr}"
        rendered = self.compose(
            "config", "--no-interpolate", "--no-env-resolution", "--format", "json"
        )
        assert rendered.returncode == 0, rendered.stderr
        doc = json.loads(rendered.stdout)
        assert isinstance(doc, dict)
        return doc

    def build_no_cache(self) -> None:
        proc = self.compose("build", "--no-cache")
        assert proc.returncode == 0, f"`docker compose build --no-cache` failed:\n{proc.stderr}"

    def up(self) -> subprocess.CompletedProcess[str]:
        return self.compose("up", "-d")

    def restart(self) -> None:
        proc = self.compose("restart")
        assert proc.returncode == 0, f"`docker compose restart` failed:\n{proc.stderr}"

    def down_keep_volume(self) -> None:
        proc = self.compose("down")
        assert proc.returncode == 0, f"`docker compose down` failed:\n{proc.stderr}"

    def logs(self) -> str:
        return self.compose("logs", "--no-color", "--no-log-prefix").stdout

    def exec_env(self, service: str, name: str) -> str:
        """The value of ``$name`` inside a running service container (used to PROVE
        the runtime secret is present — a SANITIZED inspection, plan §12.9). The ONE
        ``record=False`` caller: this probe's stdout is the secret by design, so it
        is excluded from the ``captured_output`` leak-sweep surface (the sweep would
        otherwise deterministically find the value the probe just printed). The
        probe must itself SUCCEED — a failed exec must never read as an empty
        (secret-free-looking) value."""
        proc = self.compose("exec", "-T", service, "printenv", name, record=False)
        assert proc.returncode == 0, (
            f"the sanitized secret-presence probe failed (exit {proc.returncode}): {proc.stderr}"
        )
        return proc.stdout.strip()

    def image_history_text(self, service: str) -> str:
        proc = self.docker(
            "image",
            "history",
            "--no-trunc",
            "--format",
            "{{.CreatedBy}} || {{.Comment}}",
            self.image_name(service),
        )
        return proc.stdout

    def image_filesystem_bytes(self, service: str) -> bytes:
        """The final image filesystem as a tar stream (plan §12.9). Creates a
        throwaway container from the built image, exports it, removes it."""
        created = self.docker("create", self.image_name(service))
        container_id = created.stdout.strip()
        assert container_id, f"could not create a container from {self.image_name(service)}"
        try:
            return self.docker_binary("export", container_id)
        finally:
            self.docker("rm", "-f", container_id)

    # -- cleanup (guaranteed) ---------------------------------------------------

    def teardown_and_assert_clean(self) -> None:
        """Remove EVERY resource created for this project, then prove ZERO remain
        (plan §12.11). Registered as a finalizer so it runs even after an in-body
        assertion failure. It is a no-op ONLY when the Docker binary is absent —
        in that case nothing could have been created (the lane already failed at
        ``require_live_runtime``), so there is nothing to clean or assert."""
        if shutil.which("docker") is None:
            return
        if (self.bundle_dir / COMPOSE_PATH).is_file():
            self.compose("down", "-v", "--rmi", "local", "--remove-orphans")
        self._force_purge_labelled()
        self._assert_zero_labelled_resources()

    def _ids(self, *args: str) -> list[str]:
        proc = self.docker(*args)
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def _label_filter(self) -> str:
        return f"label={_COMPOSE_PROJECT_LABEL}={self.project}"

    def _force_purge_labelled(self) -> None:
        containers = self._ids("ps", "-aq", "--filter", self._label_filter())
        if containers:
            self.docker("rm", "-f", *containers)
        images = self._ids("images", "-q", "--filter", f"reference={self.project}-*")
        if images:
            self.docker("rmi", "-f", *images)
        volumes = self._ids("volume", "ls", "-q", "--filter", self._label_filter())
        if volumes:
            self.docker("volume", "rm", "-f", *volumes)
        networks = self._ids("network", "ls", "-q", "--filter", self._label_filter())
        for net in networks:
            self.docker("network", "rm", net)

    def _assert_zero_labelled_resources(self) -> None:
        containers = self._ids("ps", "-aq", "--filter", self._label_filter())
        images = self._ids("images", "-q", "--filter", f"reference={self.project}-*")
        volumes = self._ids("volume", "ls", "-q", "--filter", self._label_filter())
        networks = self._ids("network", "ls", "-q", "--filter", self._label_filter())
        assert not containers, f"leaked containers for {self.project}: {containers}"
        assert not images, f"leaked test-only images for {self.project}: {images}"
        assert not volumes, f"leaked volumes for {self.project}: {volumes}"
        assert not networks, f"leaked networks for {self.project}: {networks}"


# ---------------------------------------------------------------------------
# Compose-document structural assertions (plan §12.2): no host coupling.
# ---------------------------------------------------------------------------


def ingress_service_id(config_doc: Mapping[str, object]) -> str:
    """The id of the single ingress service (the one publishing host ports). Used to
    name the built image (``<project>-<service>``) for the secret scans and to
    ``exec`` into for the sanitized runtime-env check."""
    services = config_doc.get("services")
    assert isinstance(services, dict), "compose declares no services"
    published = [
        sid
        for sid, service in services.items()
        if isinstance(service, dict) and service.get("ports")
    ]
    assert len(published) == 1, f"expected exactly one ingress service, saw {published}"
    return str(published[0])


def assert_no_host_coupling(config_doc: Mapping[str, object]) -> None:
    """The rendered compose model must use NO host ``node_modules``/python-env/source
    BIND mounts and NO prebuilt local project image — every service builds from its
    in-context Dockerfile and any mount is a named volume (plan §12.2)."""
    services = config_doc.get("services")
    assert isinstance(services, dict) and services, "compose declares no services"
    for sid, service in services.items():
        assert isinstance(service, dict), sid
        assert "build" in service, f"service {sid} has no build: (a prebuilt image is forbidden)"
        for volume in service.get("volumes", []) or []:
            vtype = volume.get("type") if isinstance(volume, dict) else "bind"
            assert vtype == "volume", (
                f"service {sid} declares a non-named-volume mount ({volume!r}); host "
                "bind mounts of source/node_modules/python-env are forbidden (§12.2)"
            )


# ---------------------------------------------------------------------------
# HTTP over the loopback ingress (fixed 127.0.0.1 target).
# ---------------------------------------------------------------------------


def await_http_ok(port: int, path: str, *, timeout_s: float = 180.0) -> bytes:
    """Poll ``http://127.0.0.1:<port><path>`` until it returns 200; return the body.
    Raises if it never becomes healthy within ``timeout_s`` (plan §12.3/§12.4)."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                if resp.status == 200:
                    return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(2)
    raise AssertionError(f"ingress never reached 200 at 127.0.0.1:{port}{path} ({last})")


def becomes_healthy(port: int, path: str, *, timeout_s: float) -> bool:
    """True iff the ingress serves 200 within ``timeout_s`` — the negative probe for
    §12.8 (a missing required env must keep it from EVER becoming healthy)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def http_status_body(
    port: int,
    path: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
) -> tuple[int, bytes]:
    """A single request against the loopback ingress, returning ``(status, body)``
    even for 4xx/5xx (so an authenticated-vs-unauthenticated read can be asserted)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=data,
        headers=dict(headers or {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------------------
# Secret sentinel scanning (plan §12.9).
# ---------------------------------------------------------------------------


def scan_dir_bytes(root: Path) -> bytes:
    """All file bytes under ``root`` concatenated — a build-context secret scan."""
    chunks: list[bytes] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(path.read_bytes())
    return b"".join(chunks)


def scan_zip_bytes(zip_content: bytes) -> bytes:
    """Every entry's bytes in a zip, concatenated — a whole-archive secret scan."""
    chunks: list[bytes] = [zip_content]
    with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
        for name in zf.namelist():
            if not name.endswith("/"):
                chunks.append(zf.read(name))
    return b"".join(chunks)


def assert_sentinel_absent(sentinel: str, surfaces: Iterable[tuple[str, bytes]]) -> None:
    """Assert ``sentinel`` appears in NONE of the named ``(label, bytes)`` surfaces
    (plan §12.9). The label names the leaking surface on failure."""
    needle = sentinel.encode("utf-8")
    for label, blob in surfaces:
        assert needle not in blob, f"secret sentinel leaked into {label}"


def write_env_file(bundle_dir: Path, values: Mapping[str, str]) -> None:
    """Write the compose ``.env`` (values SUPPLIED outside the bundle, plan §12.8).
    Compose auto-loads it from the project dir for ``${VAR}`` interpolation and build
    args; the generated ``.dockerignore`` keeps it out of the image."""
    lines = [f"{name}={value}\n" for name, value in values.items()]
    (bundle_dir / ".env").write_text("".join(lines), encoding="utf-8")

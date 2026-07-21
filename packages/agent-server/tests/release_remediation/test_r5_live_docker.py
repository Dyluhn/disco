"""R5 (G12) live proof — the AppKit dev_server bundle builds on the LEGACY builder.

INTEGRATION-marked (``@pytest.mark.integration``) so it is EXCLUDED from the required
non-live lane and runs only where a real Docker Engine + Compose v2 are present. It
proves the ONE thing a static overlay inspection cannot: that the R5 fix resolves a
REAL build failure on exactly the prerequisite the bundle declares.

The gap (G12): the AppKit ``dev_server`` Dockerfile inlined its secret-writing
entrypoint with a heredoc ``COPY <<'DISCO_ENTRYPOINT' …`` — a BuildKit/Buildx-only
Dockerfile feature the LEGACY Docker Engine builder REJECTS — yet the bundle's
``SELFHOST.md`` declares only "Docker Engine + Compose v2" (NOT Buildx). R5
materializes the (secret-free) entrypoint with a heredoc-free ``RUN printf`` instead.

Three live proofs, all against the reachable Docker:

* CRUX #1 — legacy-builder discrimination: the OLD emitted form (heredoc ``COPY <<``)
  FAILS under ``DOCKER_BUILDKIT=0`` (the legacy builder), and the NEW emitted form
  BUILDS under the same legacy builder. This directly shows the heredoc was the real
  build failure and the ``RUN printf`` resolves it.
* CRUX #2 — a full ``docker build --no-cache`` from a FRESH extraction of the REAL
  bound ``/download`` zip for a COMPLETE (buildable) AppKit fixture.
* CRUX #3 (criterion 4, runtime) — the entrypoint's runtime behavior directly: a
  required-secret-MISSING run fails loudly via ``${NAME:?}``; a present-secret run
  writes the dev-vars file with the VALUE INSIDE the container; and that value is
  absent from every image layer/history AND from the bundle bytes.

When Docker/Compose is absent this FAILS (never skips) — a live proof that cannot run
is not a pass. Run it on a Docker host with ``-m integration``.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest
from disco.agent_server import ConversationRuntime, create_app
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, ProjectStorageSettings, RouterConfig
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.local_compose import DOCKERFILE_PATH, emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    ReleaseAssessment,
    ReleaseSpec,
    RuntimeStrategy,
)
from disco.tools.projects import ProjectStore
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

_ENTRYPOINT_PATH = "/usr/local/bin/disco-entrypoint.sh"

# A distinctive required-secret VALUE planted only at run time — never at build, never
# in the bundle. It must be ABSENT from every image layer/history and the bundle bytes.
_SECRET_NAME = "ADMIN_TOKEN"
_SECRET_VALUE = "DISCO-R5-RUNTIME-SECRET-9d1f7a3e"

# A COMPLETE, buildable AppKit dev_server fixture: the four AppKit contract files PLUS
# a dep-free package.json + lockfile (so ``npm ci`` completes with no registry fetch)
# and a trivial ``node`` build (so ``npm run build`` actually runs). Detection keys off
# the AppKit contract shape, so this stays a ``dev_server`` candidate with a D1 sqlite
# resource + a required ADMIN_TOKEN secret.
_APPKIT_APP: dict[str, bytes] = {
    ".disco/appspec.json": b'{"name":"appkit-app","version":1}',
    "wrangler.toml": (
        b'name = "appkit-app"\n\n[[d1_databases]]\nbinding = "DB"\ndatabase_name = "appkit_prod"\n'
    ),
    "worker/index.ts": (
        b"export default {\n"
        b"  async fetch(_req: Request): Promise<Response> {\n"
        b'    return new Response("<!doctype html>disco-r5-appkit");\n'
        b"  },\n"
        b"};\n"
    ),
    "schema.sql": b"CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);\n",
    "package.json": (
        b'{"name":"appkit-app","private":true,"version":"1.0.0",'
        b'"scripts":{"build":"node build.js"}}'
    ),
    "package-lock.json": (
        b'{"name":"appkit-app","version":"1.0.0","lockfileVersion":3,"requires":true,'
        b'"packages":{"":{"name":"appkit-app","version":"1.0.0"}}}'
    ),
    "build.js": (
        b'const fs=require("fs");fs.mkdirSync("dist",{recursive:true});\n'
        b'fs.writeFileSync("dist/index.html","<!doctype html>disco-r5-appkit");\n'
    ),
}


# ---------------------------------------------------------------------------
# Live-runtime requirement — FAILS (never skips) when Docker/Compose is absent.
# ---------------------------------------------------------------------------


def _require_docker() -> str:
    docker = shutil.which("docker")
    assert docker is not None, "Docker Engine is required for the R5 live lane; run it on a host."
    proc = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, f"docker compose v2 is required: {proc.stderr}"
    return docker


# ---------------------------------------------------------------------------
# Real ASGI release harness (mirrors the frozen route harness) — a real
# ProjectStore rooted at the per-test tmp dir; only ConfigStore.load is seamed.
# ---------------------------------------------------------------------------


def _release_client(
    store: SqliteEventStore, root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, ProjectStore]:
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


def _seed_and_cut(
    ps: ProjectStore, store: SqliteEventStore, cid: str, title: str, files: Mapping[str, bytes]
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
        owner_id="local",
        created_at="2026-07-14T00:00:00Z",
        file_count=len(files),
        total_bytes=total,
        imported=False,
    )
    store.create_conversation(cid, owner_id="local", title=title, surface="build")
    cut = ps.cut_version(cid, trigger="closeout")
    assert cut is not None and cut.seq == 1, "precondition: a real version 1 was committed"


def _bound_download_zip(client: TestClient, cid: str) -> bytes:
    """The REAL bound ``/download`` zip bytes (``?version_seq=&spec_digest=`` from the
    ``/release`` response) — the exact artifact a user builds."""
    res = client.get(f"/api/projects/{cid}/release")
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)
    assert body["assessment"] == "candidate" and body["self_host"] is True, body
    seq, digest = body["version_seq"], body["spec_digest"]
    assert seq is not None and isinstance(digest, str), body
    dl = client.get(f"/api/projects/{cid}/download?version_seq={seq}&spec_digest={digest}")
    assert dl.status_code == 200, dl.text
    assert dl.headers.get("content-type") == "application/zip", dl.headers.get("content-type")
    return dl.content


# ---------------------------------------------------------------------------
# Direct emit helpers (for the isolated legacy-builder discrimination).
# ---------------------------------------------------------------------------


def _appkit_dockerfile() -> str:
    detection = detect_release(dict(_APPKIT_APP), intent=None, provenance=Provenance())
    assert detection.assessment is ReleaseAssessment.candidate
    assert detection.ingress is not None
    assert detection.ingress.runtime is RuntimeStrategy.dev_server
    spec = ReleaseSpec(
        kind="appkit",
        name="appkit-r5",
        version_seq=1,
        tree_digest="c" * 64,
        services=detection.services,
        env=detection.env,
        resources=detection.resources,
        provenance=DetectorProvenance(
            detector="r5",
            detector_version="1",
            assessment=ReleaseAssessment.candidate,
            evidence=detection.evidence,
        ),
    )
    return emit_local_compose(spec)[DOCKERFILE_PATH]


def _strip_npm_lines(dockerfile: str) -> list[str]:
    """The emitted Dockerfile with the ``RUN npm ci`` / ``RUN npm run build`` steps
    removed, so a legacy-builder build isolates the ENTRYPOINT materialization (the
    only instruction the OLD and NEW forms differ in) without needing the full app."""
    return [line for line in dockerfile.splitlines() if not line.startswith("RUN npm")]


def _to_old_heredoc_form(lines: list[str]) -> list[str]:
    """Rewrite the heredoc-free ``RUN printf … disco-entrypoint.sh`` line back to the
    HISTORICAL heredoc ``COPY <<'DISCO_ENTRYPOINT' …`` block (the pre-R5 baseline form),
    so a build proves that exact instruction fails on the legacy builder."""
    out: list[str] = []
    for line in lines:
        if line.startswith("RUN printf ") and _ENTRYPOINT_PATH in line:
            out.append(f"COPY <<'DISCO_ENTRYPOINT' {_ENTRYPOINT_PATH}")
            out.append("#!/bin/sh")
            out.append("set -eu")
            out.append('exec "$@"')
            out.append("DISCO_ENTRYPOINT")
            out.append(f"RUN chmod +x {_ENTRYPOINT_PATH}")
        else:
            out.append(line)
    return out


def _legacy_build(
    bundle_dir: Path, dockerfile_text: str, tag: str
) -> subprocess.CompletedProcess[str]:
    """``DOCKER_BUILDKIT=0 docker build`` (the LEGACY builder) of ``dockerfile_text``
    against ``bundle_dir`` as context."""
    (bundle_dir / "Dockerfile.r5probe").write_text(dockerfile_text, encoding="utf-8")
    return subprocess.run(
        ["docker", "build", "--no-cache", "-t", tag, "-f", "Dockerfile.r5probe", "."],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        env={"DOCKER_BUILDKIT": "0", "PATH": _path_env()},
    )


def _path_env() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def _docker(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _image_filesystem_bytes(image: str) -> bytes:
    created = _docker("create", image)
    container_id = created.stdout.strip()
    assert container_id, f"could not create a container from {image}: {created.stderr}"
    try:
        exported = subprocess.run(
            ["docker", "export", container_id], capture_output=True, timeout=300, check=False
        )
        return exported.stdout
    finally:
        _docker("rm", "-f", container_id, timeout=60)


def _rmi(*tags: str) -> None:
    for tag in tags:
        _docker("rmi", "-f", tag, timeout=120)


# ===========================================================================
# CRUX #1 — the legacy builder REJECTS the old heredoc form and ACCEPTS the new.
# ===========================================================================


def test_r5_legacy_builder_rejects_old_heredoc_accepts_new_run(tmp_path: Path) -> None:
    _require_docker()
    dockerfile = _appkit_dockerfile()
    minimal = _strip_npm_lines(dockerfile)
    new_form = "\n".join(minimal) + "\n"
    old_form = "\n".join(_to_old_heredoc_form(minimal)) + "\n"

    # A context the `COPY ./ ./` step can copy from.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for rel, data in _APPKIT_APP.items():
        dest = bundle / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    new_tag = "disco-r5-new-legacy"
    old_tag = "disco-r5-old-legacy"
    try:
        old = _legacy_build(bundle, old_form, old_tag)
        assert old.returncode != 0, (
            "the OLD heredoc `COPY <<` entrypoint form UNEXPECTEDLY built on the legacy "
            f"builder — the discrimination is void.\nSTDOUT:\n{old.stdout}\nSTDERR:\n{old.stderr}"
        )
        combined = (old.stdout + old.stderr).lower()
        assert "copy" in combined, (
            "the old-form legacy build failed for an unrelated reason (expected a COPY/heredoc "
            f"rejection).\nSTDOUT:\n{old.stdout}\nSTDERR:\n{old.stderr}"
        )

        new = _legacy_build(bundle, new_form, new_tag)
        assert new.returncode == 0, (
            "the NEW heredoc-free `RUN printf` entrypoint form FAILED on the legacy builder — "
            f"the R5 fix does not build on the declared prerequisite.\nSTDERR:\n{new.stderr}"
        )
    finally:
        _rmi(new_tag, old_tag)


# ===========================================================================
# CRUX #2 — a full `docker build --no-cache` from the REAL extracted /download zip.
# ===========================================================================


def test_r5_appkit_bundle_builds_no_cache_from_extracted_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_docker()
    store = SqliteEventStore(":memory:")
    client, ps = _release_client(store, tmp_path / "root", monkeypatch)
    cid = "conv_r5_crux2"
    _seed_and_cut(ps, store, cid, "appkit-r5", _APPKIT_APP)
    zip_bytes = _bound_download_zip(client, cid)

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(extracted)
    assert (extracted / DOCKERFILE_PATH).is_file(), "the extracted download has no Dockerfile"

    tag = "disco-r5-crux2"
    try:
        built = subprocess.run(
            ["docker", "build", "--no-cache", "-t", tag, "-f", DOCKERFILE_PATH, "."],
            cwd=extracted,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert built.returncode == 0, (
            "the emitted AppKit Dockerfile did NOT build --no-cache from a fresh extraction of "
            f"the real /download zip.\nSTDERR:\n{built.stderr}"
        )
        # the built image carries the entrypoint the plain ENTRYPOINT points at.
        cat = _docker("run", "--rm", "--entrypoint", "cat", tag, _ENTRYPOINT_PATH)
        assert cat.returncode == 0 and 'exec "$@"' in cat.stdout, cat.stdout + cat.stderr
    finally:
        _rmi(tag)


# ===========================================================================
# CRUX #3 (criterion 4, runtime) — the entrypoint writes the secret ONLY inside the
# running container: missing-secret fails loudly, present-secret writes it, and the
# VALUE is absent from every image layer/history AND the bundle bytes.
# ===========================================================================


def test_r5_entrypoint_writes_secret_only_in_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_docker()
    store = SqliteEventStore(":memory:")
    client, ps = _release_client(store, tmp_path / "root", monkeypatch)
    cid = "conv_r5_crux3"
    _seed_and_cut(ps, store, cid, "appkit-r5", _APPKIT_APP)
    zip_bytes = _bound_download_zip(client, cid)

    # The required secret VALUE is never in the exported bundle (NAMES only).
    assert _SECRET_VALUE.encode() not in zip_bytes, "the secret VALUE leaked into the bundle zip"

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(extracted)

    tag = "disco-r5-crux3"
    try:
        built = subprocess.run(
            ["docker", "build", "--no-cache", "-t", tag, "-f", DOCKERFILE_PATH, "."],
            cwd=extracted,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert built.returncode == 0, f"build failed:\n{built.stderr}"

        # (a) MISSING required secret -> the entrypoint fails loudly via `${NAME:?}`.
        missing = _docker("run", "--rm", tag, "true")
        assert missing.returncode != 0, "a missing required secret must fail the container loudly"
        assert _SECRET_NAME in (missing.stdout + missing.stderr), (missing.stdout, missing.stderr)

        # (b) PRESENT secret -> the entrypoint writes the dev-vars file WITH the value,
        #     visible only inside the running container.
        present = _docker(
            "run", "--rm", "-e", f"{_SECRET_NAME}={_SECRET_VALUE}", tag, "cat", "/app/.dev.vars"
        )
        assert present.returncode == 0, present.stderr
        assert f"{_SECRET_NAME}={_SECRET_VALUE}" in present.stdout, present.stdout

        # (c) the VALUE is baked into NO image layer / filesystem, and NO history line.
        assert _SECRET_VALUE.encode() not in _image_filesystem_bytes(tag), (
            "the runtime secret VALUE leaked into the built image filesystem"
        )
        history = _docker("image", "history", "--no-trunc", tag)
        assert _SECRET_VALUE not in history.stdout, (
            "the runtime secret VALUE leaked into image history"
        )
    finally:
        _rmi(tag)

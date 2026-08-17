"""R2 live proof — the emitted G03/G04/G05/G06 overlays BUILD, BOOT, SERVE, RESTART.

INTEGRATION-marked (``@pytest.mark.integration``) so it is EXCLUDED from the required
non-live lane and runs only where a real Docker Engine + Compose v2 are present. It
proves the R2 lowering at the ONLY layer a static overlay inspection cannot:

* G04 — an interpreted no-build candidate's dependency install layer (Express ``npm
  install``, FastAPI ``pip install``) produces a runnable image that boots, serves its
  health body, and survives a restart in a clean ``docker compose build --no-cache``;
* crit 7 — ``command -v`` inside each BUILT image proves every accepted executable
  exists (node/npm for Express, python/uvicorn for FastAPI);
* crit 3 — a static ``output_dir`` two-stage COPY builds + serves the built site, and a
  PUBLIC build-scope env var reaches the built asset; a runtime SECRET is ABSENT from
  the built image filesystem (injected at run time, never baked into a layer);
* G05 — an unprovisioned-toolchain intent yields ``needs_review`` / no ingress, so NO
  image is ever built (never a broken candidate).

The bundle under test is the REAL ``emit_local_compose`` output for a spec assembled
from the REAL ``detect_release`` — the same bytes ``/download`` streams. When
Docker/Compose is absent this FAILS (never skips) — a live proof that cannot run is not
a pass. Run it on a Docker host with ``-m integration``.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.local_compose import emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    EnvScope,
    EnvVarDecl,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseSpec,
    SecretClass,
)

pytestmark = pytest.mark.integration

# The single ingress service id every intent-derived topology uses (mirrors
# detect._INGRESS_ID) — so the compose-built image is `<project>-web`.
_INGRESS = "web"

_EXPRESS_FILES: dict[str, bytes] = {
    "package.json": (
        b'{"name":"r2-express","private":true,"main":"server.js",'
        b'"scripts":{"start":"node server.js"},"dependencies":{"express":"^4.19.2"}}'
    ),
    "server.js": (
        b"const express=require('express');const app=express();\n"
        b"app.get('/',(_q,res)=>res.type('html').send('r2-express-alive'));\n"
        b"app.listen(process.env.PORT||8080,'0.0.0.0');\n"
    ),
}
# A required RUNTIME secret — supplied at compose `up`, never at build. Its value must be
# ABSENT from every built image layer (crit 3: runtime secrets are not baked in).
_RUNTIME_SECRET_NAME = "APP_SECRET"
_RUNTIME_SECRET_VALUE = "R2-RUNTIME-SECRET-9d1f7a3e"
_EXPRESS_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"), required_env=(_RUNTIME_SECRET_NAME,), health_path="/"
)

_FASTAPI_FILES: dict[str, bytes] = {
    "requirements.txt": b"fastapi==0.111.0\nuvicorn==0.30.1\n",
    "main.py": (
        b"from fastapi import FastAPI\n"
        b"from fastapi.responses import HTMLResponse\n"
        b"app = FastAPI()\n"
        b"@app.get('/', response_class=HTMLResponse)\n"
        b"def root() -> str:\n"
        b"    return 'r2-fastapi-alive'\n"
    ),
}
_FASTAPI_INTENT = ReleaseIntent(
    start_cmd=("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}"), health_path="/"
)


def _require_docker() -> None:
    docker = shutil.which("docker")
    assert docker is not None, "Docker Engine is required for the R2 live lane; run it on a host."
    proc = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, f"docker compose v2 is required: {proc.stderr}"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spec(intent: ReleaseIntent, files: Mapping[str, bytes]) -> ReleaseSpec:
    detection = detect_release(dict(files), intent=intent, provenance=Provenance())
    assert detection.assessment is ReleaseAssessment.candidate, detection.reasons
    ingress = detection.ingress
    assert ingress is not None
    return ReleaseSpec(
        kind=ingress.runtime.value,
        name="r2-live",
        version_seq=1,
        tree_digest="0" * 64,
        services=detection.services,
        env=detection.env,
        resources=detection.resources,
        provenance=DetectorProvenance(
            detector="r2", detector_version="1", assessment=detection.assessment
        ),
    )


def _write_bundle(bundle_dir: Path, files: Mapping[str, bytes], overlay: Mapping[str, str]) -> None:
    for rel, data in files.items():
        dest = bundle_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    for rel, text in overlay.items():
        dest = bundle_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")


def _compose(
    project: str, bundle_dir: Path, *args: str, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", project, *args],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _image_name(project: str) -> str:
    return f"{project}-{_INGRESS}"


def _command_v(image: str, executable: str) -> int:
    """Run ``command -v <executable>`` inside the BUILT image (overriding its CMD) and
    return the exit code — 0 iff the accepted executable is on PATH in the final image."""
    proc = subprocess.run(
        ["docker", "run", "--rm", image, "sh", "-c", f"command -v {executable}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return proc.returncode


def _image_filesystem_bytes(image: str) -> bytes:
    """The final image filesystem as a tar stream (a throwaway container is created,
    exported, and removed) — for a runtime-secret leak scan of every image layer/file."""
    created = subprocess.run(
        ["docker", "create", image], capture_output=True, text=True, timeout=120, check=False
    )
    container_id = created.stdout.strip()
    assert container_id, f"could not create a container from {image}: {created.stderr}"
    try:
        exported = subprocess.run(
            ["docker", "export", container_id], capture_output=True, timeout=300, check=False
        )
        return exported.stdout
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_id], capture_output=True, timeout=60, check=False
        )


def _teardown(project: str, bundle_dir: Path) -> None:
    """Remove EVERY resource created for this project: containers + volumes + networks via
    ``down``, then force-remove the compose-built images (``down --rmi local`` leaves a
    ``:latest``-tagged ``<project>-<service>`` image behind), so a repeated docker-host
    run never accumulates state."""
    _compose(project, bundle_dir, "down", "-v", "--rmi", "local", "--remove-orphans", timeout=120)
    listed = subprocess.run(
        ["docker", "images", "-q", "--filter", f"reference={project}-*"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    images = [line for line in listed.stdout.splitlines() if line.strip()]
    if images:
        subprocess.run(
            ["docker", "rmi", "-f", *images], capture_output=True, timeout=120, check=False
        )


def _await_ok(port: int, path: str, *, timeout_s: float = 180.0) -> bytes:
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


def test_express_no_build_builds_boots_serves_restarts_has_node_npm_and_hides_secret(
    tmp_path: Path,
) -> None:
    """G04 + crit 7 + crit 3: the Express no-build overlay (``npm install`` layer) builds
    from scratch, boots, serves, and RESTARTS; ``node`` and ``npm`` exist in the built
    image; and the required RUNTIME secret is ABSENT from every image layer."""
    _require_docker()
    overlay = emit_local_compose(_spec(_EXPRESS_INTENT, _EXPRESS_FILES))
    assert '"npm", "install"' in overlay["Dockerfile"], overlay["Dockerfile"]

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, _EXPRESS_FILES, overlay)
    port = _free_loopback_port()
    project = f"r2exp{port}"
    (bundle / ".env").write_text(
        f"HOST_PORT={port}\n{_RUNTIME_SECRET_NAME}={_RUNTIME_SECRET_VALUE}\n", encoding="utf-8"
    )

    try:
        built = _compose(project, bundle, "build", "--no-cache")
        assert built.returncode == 0, f"build failed:\n{built.stderr}"
        up = _compose(project, bundle, "up", "-d")
        assert up.returncode == 0, f"up failed:\n{up.stderr}"
        assert b"r2-express-alive" in _await_ok(port, "/")
        restart = _compose(project, bundle, "restart")
        assert restart.returncode == 0, f"restart failed:\n{restart.stderr}"
        assert b"r2-express-alive" in _await_ok(port, "/")

        image = _image_name(project)
        assert _command_v(image, "node") == 0, "node is not on PATH in the built image"
        assert _command_v(image, "npm") == 0, "npm is not on PATH in the built image"
        # The runtime secret is injected at `up`, never at build — it must not be baked in.
        assert _RUNTIME_SECRET_VALUE.encode() not in _image_filesystem_bytes(image), (
            "the runtime secret leaked into the built image filesystem"
        )
    finally:
        _teardown(project, bundle)


def test_fastapi_no_build_builds_boots_serves_restarts_and_has_python_uvicorn(
    tmp_path: Path,
) -> None:
    """G04 + crit 7: the FastAPI no-build overlay (``pip install`` layer) builds from
    scratch, boots, serves, and RESTARTS; ``python3`` and ``uvicorn`` exist in the built
    image (the accepted executables the pip-install path provisions)."""
    _require_docker()
    overlay = emit_local_compose(_spec(_FASTAPI_INTENT, _FASTAPI_FILES))
    assert '"pip", "install", "-r", "requirements.txt"' in overlay["Dockerfile"], overlay[
        "Dockerfile"
    ]

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, _FASTAPI_FILES, overlay)
    port = _free_loopback_port()
    project = f"r2fa{port}"
    (bundle / ".env").write_text(f"HOST_PORT={port}\n", encoding="utf-8")

    try:
        built = _compose(project, bundle, "build", "--no-cache")
        assert built.returncode == 0, f"build failed:\n{built.stderr}"
        up = _compose(project, bundle, "up", "-d")
        assert up.returncode == 0, f"up failed:\n{up.stderr}"
        assert b"r2-fastapi-alive" in _await_ok(port, "/")
        restart = _compose(project, bundle, "restart")
        assert restart.returncode == 0, f"restart failed:\n{restart.stderr}"
        assert b"r2-fastapi-alive" in _await_ok(port, "/")

        image = _image_name(project)
        assert _command_v(image, "python3") == 0, "python3 is not on PATH in the built image"
        assert _command_v(image, "uvicorn") == 0, "uvicorn is not on PATH in the built image"
    finally:
        _teardown(project, bundle)


def test_static_output_dir_builds_and_serves_with_public_build_var_in_asset(
    tmp_path: Path,
) -> None:
    """G06 + crit 3: the static ``output_dir`` two-stage COPY builds from scratch and
    serves the built site, AND a declared PUBLIC build-scope env var reaches the built
    asset (the Compose build-arg -> Dockerfile ARG -> build -> asset lowering)."""
    _require_docker()
    banner = "r2-pubvite-banner-6b2c9e"
    files: dict[str, bytes] = {
        "index.html": b"<!doctype html><div id=app>src</div>\n",
        "package.json": b'{"name":"r2-pubvite","private":true,"scripts":{"build":"node build.js"}}',
        # A stand-in for a real bundler: reads the BUILD env var and writes it into the
        # output dir, exactly as Vite inlines `import.meta.env.VITE_*` at build time.
        "build.js": (
            b"const fs=require('fs');fs.mkdirSync('out',{recursive:true});\n"
            b"fs.writeFileSync('out/index.html','banner='+(process.env.VITE_PUBLIC_BANNER||'MISS'));\n"
        ),
    }
    intent = ReleaseIntent(
        build_cmd=("npm", "run", "build"),
        output_dir="out",
        health_path="/",
        env=(
            EnvVarDecl(
                name="VITE_PUBLIC_BANNER",
                scope=EnvScope.build,
                required=True,
                secret=SecretClass.public,
            ),
        ),
    )
    overlay = emit_local_compose(_spec(intent, files))
    assert "COPY --from=build /app/out/ /site/" in overlay["Dockerfile"], overlay["Dockerfile"]
    assert "ARG VITE_PUBLIC_BANNER" in overlay["Dockerfile"], overlay["Dockerfile"]

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, files, overlay)
    port = _free_loopback_port()
    project = f"r2pub{port}"
    (bundle / ".env").write_text(
        f"HOST_PORT={port}\nVITE_PUBLIC_BANNER={banner}\n", encoding="utf-8"
    )

    try:
        built = _compose(project, bundle, "build", "--no-cache")
        assert built.returncode == 0, f"build failed:\n{built.stderr}"
        up = _compose(project, bundle, "up", "-d")
        assert up.returncode == 0, f"up failed:\n{up.stderr}"
        body = _await_ok(port, "/")
        assert banner.encode() in body, f"the public build var did not reach the asset: {body!r}"
    finally:
        _teardown(project, bundle)


def test_unsupported_toolchain_intent_builds_no_image(tmp_path: Path) -> None:
    """G05: an unprovisioned-toolchain intent (``pnpm start``) yields ``needs_review`` with
    ``toolchain_unsupported`` and NO ingress — so ``emit_local_compose`` is never reached
    and NO image is ever built (never a broken candidate shipped to a live build)."""
    _require_docker()
    files = {
        "package.json": b'{"name":"svc","scripts":{"start":"node server.js"}}',
        "server.js": (
            b"require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"
        ),
    }
    detection = detect_release(
        files, intent=ReleaseIntent(start_cmd=("pnpm", "start")), provenance=Provenance()
    )
    assert detection.assessment is ReleaseAssessment.needs_review
    assert any(b.code == "toolchain_unsupported" for b in detection.blockers)
    # No ingress -> no services -> emit_local_compose is never reached -> no image is
    # ever built. A broken candidate never reaches a live Docker build.
    assert detection.ingress is None and detection.services == ()

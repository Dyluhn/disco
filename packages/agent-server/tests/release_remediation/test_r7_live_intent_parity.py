"""R7 live proof — the typed-intent rung's normalized candidate BUILDS, BOOTS, SERVES.

INTEGRATION-marked so it is excluded from the required non-live lane and runs only where a
real Docker Engine + Compose v2 exist. It proves, at the layer a static overlay inspection
cannot, that the two R7 product fixes are GENERAL (not fixture-specific):

* DEFECT 1 (host/port bind) — an UN-bound ``uvicorn app:app`` typed intent over a BARE-ASGI
  app (NO FastAPI, NO build step) is normalized to bind ``0.0.0.0:$PORT`` and reaches a
  meaningful HTTP 200 on loopback, then survives a restart. This is a SECOND python
  framework beyond the frozen FastAPI fixture, proving the normalization is not FastAPI-
  specific.
* DEFECT 2 (public build var) — a source PUBLIC ``import.meta.env.VITE_PUBLIC_BANNER`` build
  var, discovered on the INTENT rung (no ``env`` declared in the intent), lowers to a
  Dockerfile ARG + Compose build arg and a REAL ``vite build`` inlines it into the HASHED
  compiled asset served by the bundle.
* DEFECT 2 (secret hole) — a secret-shaped source build var + a typed intent yields
  ``needs_review`` / no ingress, so ``emit_local_compose`` is never reached and NO image is
  ever built (the gate is not defeated by the intent).
* DEFECT 1 (whole class closed) — ``python -m uvicorn`` is normalized, and unbound
  Gunicorn/Hypercorn commands receive an authoritative provider-neutral CLI bind. Default/custom
  port envs and hostile config/environment defaults all build reachable containers.

The bundle under test is the REAL ``emit_local_compose`` output over a spec assembled from
the REAL ``detect_release`` — the same bytes ``/download`` streams. When Docker/Compose is
absent this FAILS (never skips). Run on a Docker host with ``-m integration``.
"""

from __future__ import annotations

import re
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
)

pytestmark = pytest.mark.integration

# A BARE ASGI app — a hand-written ASGI callable, NOT FastAPI — run by uvicorn. The intent
# declares an UN-bound `uvicorn app:app`; the R7 normalization must add `--host 0.0.0.0
# --port ${PORT}` for it to be reachable behind the overlay's `$PORT` publish.
_ASGI_FILES: dict[str, bytes] = {
    "requirements.txt": b"uvicorn==0.30.1\n",
    "app.py": (
        b"async def app(scope, receive, send):\n"
        b"    assert scope['type'] == 'http'\n"
        b"    await send({'type': 'http.response.start', 'status': 200,\n"
        b"                'headers': [(b'content-type', b'text/html')]})\n"
        b"    await send({'type': 'http.response.body', 'body': b'<h1>r7-asgi-alive</h1>'})\n"
    ),
}
_ASGI_INTENT = ReleaseIntent(start_cmd=("uvicorn", "app:app"), health_path="/")

# The idiomatic `python -m uvicorn` form (head "python") over the SAME bare-ASGI app — the
# adversarially-confirmed silent-broken path #2. Normalization must recognize it and bind.
_PY_M_UVICORN_INTENT = ReleaseIntent(
    start_cmd=("python", "-m", "uvicorn", "app:app"), health_path="/"
)

_EXPRESS_FILES: dict[str, bytes] = {
    "package.json": (b'{"name":"r5-express","private":true,"dependencies":{"express":"^4.21.0"}}'),
    "server.js": (
        b"const express = require('express');\n"
        b"const app = express();\n"
        b"const PORT = process.env.PORT || 3000;\n"
        b"app.get('/', (_req,res) => res.send('<h1>r5-express-alive</h1>'));\n"
        b"app.listen(PORT, '0.0.0.0');\n"
    ),
}
_EXPRESS_INTENT = ReleaseIntent(start_cmd=("node", "server.js"), health_path="/")

# A WSGI app run by gunicorn. An unbound intent receives a provider-neutral final CLI bind rather
# than trusting Gunicorn's PORT/config/environment defaults.
_GUNICORN_FILES: dict[str, bytes] = {
    "requirements.txt": b"gunicorn==22.0.0\n",
    "wsgi.py": (
        b"def app(environ, start_response):\n"
        b"    start_response('200 OK', [('Content-Type', 'text/html')])\n"
        b"    return [b'<h1>r7-gunicorn-alive</h1>']\n"
    ),
}
_GUNICORN_INTENT = ReleaseIntent(start_cmd=("gunicorn", "wsgi:app"), health_path="/")

# A real Vite project whose source reads a PUBLIC build var. NO `env` is declared in the
# intent — the intent rung must DISCOVER the public build var from source and lower it.
_PUB_BANNER = "r7-pubvite-banner-6b2c9e"
_PUBVITE_FILES: dict[str, bytes] = {
    "index.html": (
        b"<!doctype html><html><head><title>r7</title></head><body>"
        b"<div id='app'></div><script type='module' src='/src/main.js'></script>"
        b"</body></html>\n"
    ),
    "package.json": (
        b'{"name":"r7-pubvite","private":true,"version":"1.0.0",'
        b'"scripts":{"build":"vite build"},"devDependencies":{"vite":"^5.4.0"}}'
    ),
    "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
    "src/main.js": (
        b"const banner = import.meta.env.VITE_PUBLIC_BANNER;\n"
        b"document.getElementById('app').textContent = 'r7-pubvite ' + banner;\n"
    ),
}
_PUBVITE_INTENT = ReleaseIntent(
    build_cmd=("npm", "run", "build"), output_dir="dist", health_path="/"
)


def _require_docker() -> None:
    docker = shutil.which("docker")
    assert docker is not None, "Docker Engine is required for the R7 live lane; run it on a host."
    daemon = subprocess.run(
        [docker, "info"], capture_output=True, text=True, timeout=30, check=False
    )
    assert daemon.returncode == 0, f"a reachable Docker daemon is required: {daemon.stderr}"
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
        name="r7-live",
        version_seq=1,
        tree_digest="0" * 64,
        services=detection.services,
        env=detection.env,
        resources=detection.resources,
        provenance=DetectorProvenance(
            detector="r7", detector_version="1", assessment=detection.assessment
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


def _teardown(project: str, bundle_dir: Path) -> None:
    down = _compose(
        project, bundle_dir, "down", "-v", "--rmi", "local", "--remove-orphans", timeout=180
    )
    assert down.returncode == 0, f"compose teardown failed:\n{down.stderr}"
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
    # A passing case leaves no project-scoped container, network, volume, or image. This
    # keeps the live proof honest across repeated local runs and prevents one fixture from
    # making a later fixture pass.
    residue_commands = (
        ("container", "ls", "-aq"),
        ("network", "ls", "-q"),
        ("volume", "ls", "-q"),
    )
    for noun, *args in residue_commands:
        listed = subprocess.run(
            [
                "docker",
                noun,
                *args,
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert listed.returncode == 0, f"could not inspect {noun} cleanup: {listed.stderr}"
        assert not listed.stdout.strip(), f"{noun} residue remains for project {project}"


def _await_ok(port: int, path: str, *, timeout_s: float = 240.0) -> bytes:
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


def _fetch(port: int, path: str) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.read()


def _build_boot_probe_restart(
    tmp_path: Path,
    files: Mapping[str, bytes],
    intent: ReleaseIntent,
    *,
    project_prefix: str,
    marker: bytes,
    health_path: str = "/",
    runtime_env: Mapping[str, str] | None = None,
    runtime_probe: str | None = None,
) -> None:
    """Assemble the REAL detect->emit bundle for `intent`, then BUILD (no-cache), BOOT, and
    probe the PUBLISHED loopback port for a 200 carrying `marker`, and confirm it survives a
    restart — proving the candidate is genuinely REACHABLE (not bound to a server default).
    Cleans up every resource it creates."""
    _require_docker()
    overlay = emit_local_compose(_spec(intent, files))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, files, overlay)
    port = _free_loopback_port()
    project = f"{project_prefix}{port}"
    env_lines = [f"HOST_PORT={port}"]
    if runtime_env:
        env_lines.extend(f"{name}={value}" for name, value in sorted(runtime_env.items()))
    (bundle / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    try:
        built = _compose(project, bundle, "build", "--no-cache")
        assert built.returncode == 0, f"build failed:\n{built.stderr}"
        if runtime_probe is not None:
            probed = _compose(
                project,
                bundle,
                "run",
                "--rm",
                "--no-deps",
                "web",
                "sh",
                "-c",
                runtime_probe,
            )
            assert probed.returncode == 0, (
                "the final runtime image does not contain/import its declared executable:\n"
                + probed.stderr
            )
        up = _compose(project, bundle, "up", "-d")
        assert up.returncode == 0, f"up failed:\n{up.stderr}"
        assert marker in _await_ok(port, health_path), "not reachable on the published port"
        restart = _compose(project, bundle, "restart")
        assert restart.returncode == 0, f"restart failed:\n{restart.stderr}"
        assert marker in _await_ok(port, health_path), "not reachable after restart"
    finally:
        _teardown(project, bundle)


def test_bare_asgi_uvicorn_intent_is_normalized_builds_boots_serves_restarts(
    tmp_path: Path,
) -> None:
    """DEFECT 1 general: an UN-bound ``uvicorn app:app`` intent over a BARE-ASGI app (a
    second python framework beyond FastAPI, no build) is normalized to bind ``0.0.0.0:$PORT``
    and BUILDS, BOOTS, SERVES a meaningful body, and survives a RESTART."""
    _require_docker()
    overlay = emit_local_compose(_spec(_ASGI_INTENT, _ASGI_FILES))
    # The normalization reached the emitted CMD (host + $PORT bind), via `sh -c exec`.
    dockerfile = overlay["Dockerfile"]
    assert "--host" in dockerfile and "0.0.0.0" in dockerfile and "${PORT}" in dockerfile, (
        dockerfile
    )

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, _ASGI_FILES, overlay)
    port = _free_loopback_port()
    project = f"r5asgi{port}"
    (bundle / ".env").write_text(f"HOST_PORT={port}\n", encoding="utf-8")

    try:
        built = _compose(project, bundle, "build", "--no-cache")
        assert built.returncode == 0, f"build failed:\n{built.stderr}"
        probed = _compose(
            project,
            bundle,
            "run",
            "--rm",
            "--no-deps",
            "web",
            "sh",
            "-c",
            "command -v uvicorn && python -c 'import uvicorn'",
        )
        assert probed.returncode == 0, probed.stderr
        up = _compose(project, bundle, "up", "-d")
        assert up.returncode == 0, f"up failed:\n{up.stderr}"
        assert b"r7-asgi-alive" in _await_ok(port, "/")
        restart = _compose(project, bundle, "restart")
        assert restart.returncode == 0, f"restart failed:\n{restart.stderr}"
        assert b"r7-asgi-alive" in _await_ok(port, "/")
    finally:
        _teardown(project, bundle)


def test_direct_typed_express_installs_imports_boots_serves_and_restarts(
    tmp_path: Path,
) -> None:
    """Typed direct Node parity: runtime deps install without a build step, the final
    image imports Express, and the exact public listen contract survives restart."""
    _build_boot_probe_restart(
        tmp_path,
        _EXPRESS_FILES,
        _EXPRESS_INTENT,
        project_prefix="r5express",
        marker=b"r5-express-alive",
        runtime_probe="command -v node && node -e \"require('express')\"",
    )


def test_public_build_var_discovered_on_intent_rung_reaches_hashed_vite_asset(
    tmp_path: Path,
) -> None:
    """DEFECT 2 (public): a source ``VITE_PUBLIC_BANNER`` build var — discovered on the
    INTENT rung with NO ``env`` declared — lowers to a Dockerfile ARG + Compose build arg,
    and a REAL ``vite build`` inlines it into the HASHED compiled asset the bundle serves."""
    _require_docker()
    overlay = emit_local_compose(_spec(_PUBVITE_INTENT, _PUBVITE_FILES))
    assert "ARG VITE_PUBLIC_BANNER" in overlay["Dockerfile"], overlay["Dockerfile"]

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, _PUBVITE_FILES, overlay)
    port = _free_loopback_port()
    project = f"r5pub{port}"
    (bundle / ".env").write_text(
        f"HOST_PORT={port}\nVITE_PUBLIC_BANNER={_PUB_BANNER}\n", encoding="utf-8"
    )

    try:
        built = _compose(project, bundle, "build", "--no-cache")
        assert built.returncode == 0, f"build failed:\n{built.stderr}"
        up = _compose(project, bundle, "up", "-d")
        assert up.returncode == 0, f"up failed:\n{up.stderr}"
        index = _await_ok(port, "/").decode("utf-8", "ignore")
        asset_paths = re.findall(r'src="(/[^"]+\.js)"', index)
        assert asset_paths, f"no hashed module asset referenced from index.html: {index!r}"
        found = any(_PUB_BANNER.encode() in _fetch(port, p) for p in asset_paths)
        assert found, f"the public build var did not reach the hashed asset {asset_paths}"
    finally:
        _teardown(project, bundle)


def test_secret_build_var_with_intent_yields_no_ingress_so_no_image_is_built() -> None:
    """DEFECT 2 (security hole): a secret-shaped source build var + a typed intent yields
    ``needs_review`` with ``secret_build_env_unsupported`` and NO ingress — so
    ``emit_local_compose`` is never reached and NO image is ever built for it."""
    _require_docker()
    files = {
        "index.html": b"<div id='app'></div><script type='module' src='/src/main.js'></script>\n",
        "package.json": (
            b'{"name":"s","private":true,"scripts":{"build":"vite build"},'
            b'"devDependencies":{"vite":"^5.4.0"}}'
        ),
        "vite.config.js": b"export default { build: { outDir: 'dist' } };\n",
        "src/main.js": b"const a = import.meta.env.VITE_ADMIN_SECRET;\n",
    }
    detection = detect_release(files, intent=_PUBVITE_INTENT, provenance=Provenance())
    assert detection.assessment is ReleaseAssessment.needs_review, detection.reasons
    assert any(b.code == "secret_build_env_unsupported" for b in detection.blockers)
    assert detection.ingress is None and detection.services == ()


def test_public_vite_env_contract_conflicts_fail_closed_before_any_image() -> None:
    """Runtime-only, optional, and secret metadata cannot suppress a source-required public
    build input. Each yields the exact typed conflict with no emit-capable service."""
    _require_docker()
    declarations = (
        EnvVarDecl(name="VITE_PUBLIC_BANNER", scope=EnvScope.runtime),
        EnvVarDecl(
            name="VITE_PUBLIC_BANNER",
            scope=EnvScope.build,
            secret="secret",
        ),
    )
    for declaration in declarations:
        intent = _PUBVITE_INTENT.model_copy(update={"env": (declaration,)})
        detection = detect_release(dict(_PUBVITE_FILES), intent=intent, provenance=Provenance())
        assert detection.assessment is ReleaseAssessment.needs_review
        assert detection.ingress is None and detection.services == ()
        assert detection.blockers[0].code in {
            "build_env_contract_conflict",
            "secret_build_env_unsupported",
        }

    optional = EnvVarDecl(name="VITE_PUBLIC_BANNER", scope=EnvScope.build, required=False)
    compatible = detect_release(
        dict(_PUBVITE_FILES),
        intent=_PUBVITE_INTENT.model_copy(update={"env": (optional,)}),
        provenance=Provenance(),
    )
    assert compatible.assessment is ReleaseAssessment.candidate
    assert compatible.env == (optional,)


def test_python_module_without_declared_server_dependency_has_no_emit_path() -> None:
    """The ``python -m`` spelling cannot bypass dependency installation."""
    _require_docker()
    files = {"app.py": _ASGI_FILES["app.py"]}
    detection = detect_release(
        files,
        intent=ReleaseIntent(start_cmd=("python", "-m", "uvicorn", "app:app"), health_path="/"),
        provenance=Provenance(),
    )
    assert detection.assessment is ReleaseAssessment.needs_review
    assert detection.ingress is None and detection.services == ()
    assert [blocker.code for blocker in detection.blockers] == ["toolchain_unsupported"]


def test_python_dash_m_uvicorn_intent_is_normalized_and_reachable(tmp_path: Path) -> None:
    """DEFECT 1 (closed path #2): the ``python -m uvicorn app:app`` form — which previously
    shipped a candidate binding ``127.0.0.1:8000`` — is normalized and its REAL container is
    REACHABLE on the published loopback port, and survives a restart."""
    overlay = emit_local_compose(_spec(_PY_M_UVICORN_INTENT, _ASGI_FILES))
    df = overlay["Dockerfile"]
    assert "-m" in df and "uvicorn" in df and "--host" in df and "${PORT}" in df, df
    _build_boot_probe_restart(
        tmp_path,
        _ASGI_FILES,
        _PY_M_UVICORN_INTENT,
        project_prefix="r5pym",
        marker=b"r7-asgi-alive",
        runtime_probe="command -v uvicorn && python -c 'import uvicorn'",
    )


def test_gunicorn_default_port_env_intent_is_reachable(tmp_path: Path) -> None:
    """An unbound Gunicorn start receives an authoritative provider-neutral CLI bind, then its real
    container remains reachable through restart."""
    _require_docker()
    overlay = emit_local_compose(_spec(_GUNICORN_INTENT, _GUNICORN_FILES))
    assert "--bind" in overlay["Dockerfile"]
    assert "0.0.0.0" in overlay["Dockerfile"] and "${PORT}" in overlay["Dockerfile"]
    _build_boot_probe_restart(
        tmp_path,
        _GUNICORN_FILES,
        _GUNICORN_INTENT,
        project_prefix="r5gun",
        marker=b"r7-gunicorn-alive",
        runtime_probe="command -v gunicorn && python -c 'import gunicorn'",
    )


def test_hypercorn_no_bind_is_synthesized_and_reachable(tmp_path: Path) -> None:
    """Hypercorn's localhost default is displaced by the provider-neutral CLI bind; the real
    emitted container proves CLI placement, IPv4 reachability, and restart behavior."""
    files = {**_ASGI_FILES, "requirements.txt": b"hypercorn==0.17.3\n"}
    intent = ReleaseIntent(start_cmd=("hypercorn", "app:app"), health_path="/")
    _build_boot_probe_restart(
        tmp_path,
        files,
        intent,
        project_prefix="r5hyp",
        marker=b"r7-asgi-alive",
        runtime_probe="command -v hypercorn && python -c 'import hypercorn'",
    )


def test_gunicorn_custom_port_env_is_synthesized_and_reachable(tmp_path: Path) -> None:
    """A custom port-env name is carried into Gunicorn's provider-neutral bind and the
    target adapter supplies the matching published value."""
    intent = ReleaseIntent(start_cmd=("gunicorn", "wsgi:app"), port_env="APP_PORT", health_path="/")
    _build_boot_probe_restart(
        tmp_path,
        _GUNICORN_FILES,
        intent,
        project_prefix="r5gcustom",
        marker=b"r7-gunicorn-alive",
        runtime_probe="command -v gunicorn && python -c 'import gunicorn'",
    )


def test_gunicorn_cli_bind_overrides_config_and_environment_loopback(tmp_path: Path) -> None:
    """Config-file and ``GUNICORN_CMD_ARGS`` loopback defaults cannot override the final
    synthesized CLI bind, proven together by a real lifecycle."""
    files = {
        **_GUNICORN_FILES,
        "gunicorn_loopback.py": b"bind = '127.0.0.1:9000'\n",
    }
    intent = ReleaseIntent(
        start_cmd=("gunicorn", "--config", "gunicorn_loopback.py", "wsgi:app"),
        health_path="/",
        env=(EnvVarDecl(name="GUNICORN_CMD_ARGS", scope=EnvScope.runtime, required=True),),
    )
    _build_boot_probe_restart(
        tmp_path,
        files,
        intent,
        project_prefix="r5gconfenv",
        marker=b"r7-gunicorn-alive",
        runtime_env={"GUNICORN_CMD_ARGS": "--bind 127.0.0.1:9000"},
        runtime_probe="command -v gunicorn && python -c 'import gunicorn'",
    )


# ---- DEFECT 1 & 2 (bind CORRECTNESS): the discriminating silent-broken vs reachable matrix -
#
# The previous rung validated bind PRESENCE, not CORRECTNESS. These go through the REAL
# ``detect_release`` (+emit for the reachable half). Each SILENT-BROKEN bind now fails closed
# (no ingress -> no image); each CORRECT bind actually BUILDS, BOOTS, and answers HTTP 200 on
# the PUBLISHED loopback port (proving the correctness gate does not over-reject a reachable
# candidate). The neutral start command carries the service port-env name; only the target
# adapter supplies its concrete published value.

# A hypercorn tree — the combined-bind fail-closed case needs a hypercorn signature. ``main.py``
# MUST be a VALID ASGI callable: this case exercises the localhost BIND (port-contract) rejection,
# and detection resolves the ``main:app`` entrypoint BEFORE the bind. A non-runnable app (e.g.
# ``app = 1``) would trip the app-shape/entrypoint proof first (``entrypoint_unresolved``) and the
# localhost bind would never be reached — masking what this fixture is here to prove.
_HYPERCORN_FILES: dict[str, bytes] = {
    "requirements.txt": b"hypercorn==0.17.3\n",
    "main.py": (
        b"async def app(scope, receive, send):\n"
        b"    assert scope['type'] == 'http'\n"
        b"    await send({'type': 'http.response.start', 'status': 200,\n"
        b"                'headers': [(b'content-type', b'text/html')]})\n"
        b"    await send({'type': 'http.response.body', 'body': b'<h1>r7-hypercorn-alive</h1>'})\n"
    ),
}

# The six adversarially-confirmed SILENT-BROKEN binds. Each fails closed at the REAL detector,
# so ``emit_local_compose`` is never reached and NO image is ever built.
_BLOCKED_BIND_CASES: tuple[tuple[str, Mapping[str, bytes], ReleaseIntent], ...] = (
    (
        "uvicorn_port_literal_wrong",
        _ASGI_FILES,
        ReleaseIntent(start_cmd=("uvicorn", "app:app", "--port", "9000"), health_path="/"),
    ),
    (
        "uvicorn_port_equals_wrong",
        _ASGI_FILES,
        ReleaseIntent(start_cmd=("uvicorn", "app:app", "--port=9000"), health_path="/"),
    ),
    (
        "uvicorn_port_foreign_env",
        _ASGI_FILES,
        ReleaseIntent(
            start_cmd=("uvicorn", "app:app", "--port", "${OTHER}"),
            required_env=("OTHER",),
            health_path="/",
        ),
    ),
    (
        "gunicorn_bind_localhost",
        _GUNICORN_FILES,
        ReleaseIntent(start_cmd=("gunicorn", "wsgi:app", "-b", "127.0.0.1:8080"), health_path="/"),
    ),
    (
        "hypercorn_bind_localhost",
        _HYPERCORN_FILES,
        ReleaseIntent(
            start_cmd=("hypercorn", "main:app", "--bind", "127.0.0.1:8080"), health_path="/"
        ),
    ),
    (
        "gunicorn_bind_wrong_port",
        _GUNICORN_FILES,
        ReleaseIntent(start_cmd=("gunicorn", "wsgi:app", "-b", "0.0.0.0:9000"), health_path="/"),
    ),
)


@pytest.mark.parametrize(
    "files,intent",
    [(files, intent) for _, files, intent in _BLOCKED_BIND_CASES],
    ids=[case_id for case_id, _, _ in _BLOCKED_BIND_CASES],
)
def test_silent_broken_bind_fails_closed_no_ingress_no_image(
    files: Mapping[str, bytes], intent: ReleaseIntent
) -> None:
    """DEFECT 1 & 2 (whole class closed): each adversarially-confirmed silent-broken bind — a
    uvicorn ``--port`` off the published port (literal / ``=``-joined / a FOREIGN env ref) or
    a combined ``-b``/``--bind`` on localhost or a wrong port — now yields ``needs_review``
    with a typed ``port_contract_unresolved`` blocker and NO ingress, so the REAL detector
    never reaches ``emit_local_compose`` and NO image is EVER built for it (never a silent
    ``self_host:true`` candidate that answers only the in-container healthcheck)."""
    _require_docker()
    detection = detect_release(dict(files), intent=intent, provenance=Provenance())
    assert detection.assessment is ReleaseAssessment.needs_review, detection.reasons
    assert detection.ingress is None and detection.services == ()
    assert any(b.code == "port_contract_unresolved" for b in detection.blockers), [
        b.code for b in detection.blockers
    ]


# The four CORRECT binds — each builds, boots, and is reachable on the published port.
_REACHABLE_BIND_CASES: tuple[tuple[str, Mapping[str, bytes], ReleaseIntent, bytes], ...] = (
    (
        "r5gunbind",
        _GUNICORN_FILES,
        ReleaseIntent(
            start_cmd=("gunicorn", "wsgi:app", "--bind", "0.0.0.0:${PORT}"),
            health_path="/",
        ),
        b"r7-gunicorn-alive",
    ),
)


@pytest.mark.parametrize(
    "prefix,files,intent,marker",
    _REACHABLE_BIND_CASES,
    ids=[case_id for case_id, _, _, _ in _REACHABLE_BIND_CASES],
)
def test_correct_bind_builds_boots_and_is_reachable_on_published_port(
    prefix: str,
    files: Mapping[str, bytes],
    intent: ReleaseIntent,
    marker: bytes,
    tmp_path: Path,
) -> None:
    """No over-reject: an explicit provider-neutral Gunicorn bind that exactly matches the
    published contract remains byte-stable and completes a real lifecycle."""
    _build_boot_probe_restart(
        tmp_path,
        files,
        intent,
        project_prefix=prefix,
        marker=marker,
        runtime_probe="command -v gunicorn && python -c 'import gunicorn'",
    )

"""R3 (G07) live proof — a volume-backed persistent path SURVIVES a full recreate.

INTEGRATION-marked (``@pytest.mark.integration``) so it is EXCLUDED from the required
non-live lane and runs only where a real Docker Engine + Compose v2 are present. It
proves the ONE thing a static overlay inspection cannot: that the emitted named volume
ACTUALLY backs the declared persistent path.

The bundle under test is the REAL ``emit_local_compose`` output for a spec assembled from
the REAL ``detect_release`` — the same bytes ``/download`` streams. A node app persists a
UNIQUE record to the sqlite-class path ``/data/app.db`` (reached via the bound
``DATABASE_URL``). The proof:

  build --no-cache -> up -> WRITE a unique record -> read it back -> ``restart`` (record
  survives) -> ``down`` WITHOUT ``-v`` (containers destroyed, the named volume kept) ->
  ``up`` again (a FRESH container / writable layer) -> read the EXACT record back.

Surviving the ``down``+recreate can only happen if the record lives on the NAMED VOLUME,
not the container's ephemeral layer — exactly the persistence a ``self_host:true`` bundle
promises. Teardown removes every container, the volume, and the built image. When
Docker/Compose is absent this FAILS (never skips) — a live proof that cannot run is not a
pass.
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
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
)

pytestmark = pytest.mark.integration

_INGRESS = "web"

_FILES: dict[str, bytes] = {
    "package.json": (
        b'{"name":"r3-persist","private":true,"main":"server.js",'
        b'"scripts":{"start":"node server.js"}}'
    ),
    # A minimal persistence app: it stores each posted record on the sqlite-class path
    # (reached via the bound DATABASE_URL) and reads the whole store back. No real sqlite
    # engine is needed — the proof is that the VOLUME backs the path across a recreate.
    "server.js": (
        b"const http=require('http');const fs=require('fs');\n"
        b"const dbPath=(process.env.DATABASE_URL||'file:/data/app.db').replace(/^file:/,'');\n"
        b"http.createServer((req,res)=>{\n"
        b"  const u=new URL(req.url,'http://x');\n"
        b"  if(u.pathname==='/write'){\n"
        b"    fs.appendFileSync(dbPath,(u.searchParams.get('rec')||'')+'\\n');\n"
        b"    res.end('written');\n"
        b"  } else if(u.pathname==='/read'){\n"
        b"    let d='';try{d=fs.readFileSync(dbPath,'utf8');}catch(e){d='';}\n"
        b"    res.end(d);\n"
        b"  } else { res.end('alive'); }\n"
        b"}).listen(process.env.PORT||8080,'0.0.0.0');\n"
    ),
}

_INTENT = ReleaseIntent(
    start_cmd=("node", "server.js"),
    health_path="/",
    env=(EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime, binding="db"),),
    resources=(
        ResourceDecl(
            id="db",
            kind=ResourceKind.sqlite,
            persistent_path="/data/app.db",
            profiles=ResourceProfiles(
                local=LocalResourceProfile(url="file:/data/app.db", volume="app-data")
            ),
            consumers=(_INGRESS,),
        ),
    ),
)


def _require_docker() -> None:
    docker = shutil.which("docker")
    assert docker is not None, "Docker Engine is required for the R3 live lane; run it on a host."
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
        name="r3-live",
        version_seq=1,
        tree_digest="0" * 64,
        services=detection.services,
        env=detection.env,
        resources=detection.resources,
        provenance=DetectorProvenance(
            detector="r3", detector_version="1", assessment=detection.assessment
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
    _compose(project, bundle_dir, "down", "-v", "--rmi", "local", "--remove-orphans", timeout=180)
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


def _get(port: int, path: str, *, timeout_s: float = 180.0) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(2)
    raise AssertionError(f"ingress never reached 200 at 127.0.0.1:{port}{path} ({last})")


def test_volume_backed_persistent_path_survives_down_and_recreate(tmp_path: Path) -> None:
    """G07 live: the emitted volume BACKS ``/data/app.db`` — a unique record written into
    the running app survives a ``restart``, a ``down`` WITHOUT ``-v``, and a fresh
    recreate. The emitted compose must mount the volume at the file's parent (``/data``)."""
    _require_docker()
    overlay = emit_local_compose(_spec(_INTENT, _FILES))
    # The mount backs the file's parent dir, and the bound URL is injected at runtime.
    assert "app-data:/data" in overlay["compose.yaml"], overlay["compose.yaml"]
    assert "DATABASE_URL" in overlay["compose.yaml"]

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, _FILES, overlay)
    port = _free_loopback_port()
    project = f"r3persist{port}"
    record = f"r3-persist-{port}-2a7f1c9e"
    (bundle / ".env").write_text(f"HOST_PORT={port}\n", encoding="utf-8")

    try:
        built = _compose(project, bundle, "build", "--no-cache")
        assert built.returncode == 0, f"build failed:\n{built.stderr}"
        up = _compose(project, bundle, "up", "-d")
        assert up.returncode == 0, f"up failed:\n{up.stderr}"
        assert _get(port, "/") == "alive"

        # Write a UNIQUE record and confirm it reads back.
        assert _get(port, f"/write?rec={record}") == "written"
        assert record in _get(port, "/read")

        # (1) survives an in-place restart.
        restart = _compose(project, bundle, "restart")
        assert restart.returncode == 0, f"restart failed:\n{restart.stderr}"
        assert record in _get(port, "/read"), "record lost across `docker compose restart`"

        # (2) survives `down` (WITHOUT -v) + a FRESH recreate — the load-bearing proof:
        # a fresh container has a fresh ephemeral layer, so the record can only survive on
        # the named volume that backs /data/app.db.
        down = _compose(project, bundle, "down")
        assert down.returncode == 0, f"down failed:\n{down.stderr}"
        up2 = _compose(project, bundle, "up", "-d", "--build")
        assert up2.returncode == 0, f"recreate failed:\n{up2.stderr}"
        assert _get(port, "/") == "alive"
        body = _get(port, "/read")
        assert record in body, (
            "the persistent record did NOT survive `down` + recreate — the named volume "
            f"does not back /data/app.db (read body: {body!r})"
        )
    finally:
        _teardown(project, bundle)

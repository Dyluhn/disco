"""WO-6 — the AppKit interim local-run compose strategy (`dev_server`).

Proves that an AppKit-shaped tree exports a SECRET-FREE bundle that (on a docker
host) `docker compose up -d --build`s into a working local app with persistent,
D1-backed SQLite — with ZERO changes to AppKit generation or the Cloudflare deploy
path. The app runs on the workerd DEV runtime (`wrangler dev`) as an interim host.

The fixture tree is produced by driving the REAL AppKit generator (mirroring
`test_appkit_generator.py`), then adding the `.disco/appspec.json` the tool layer
persists on disk (the generator itself never writes it) so the four AppKit
contract files are all present. Everything is in-memory / generated — no gitignored
on-disk asset.

Acceptance criteria proven here:

1. `generate()` output + detection → `candidate`, a single ingress of strategy
   `dev_server`, and a sqlite-class resource whose persistent path is `/data/state`.
2. the emitted bundle's Dockerfile has `npm ci` + `npm run build`; the app service
   command carries `--ip 0.0.0.0` + `--persist-to /data/state`; the init service
   references `schema.sql` and the same persist dir, ordered BEFORE the app via
   `service_completed_successfully`; `ADMIN_TOKEN` appears only as a `${ADMIN_TOKEN:?`
   guard in compose and as a NAME in `.env.example`; a bytes-scan finds NO secret
   value anywhere in the bundle.
3. the bundle contains NO `.dev.vars` file; the entrypoint heredoc is the ONLY place
   that writes it (exactly one `.dev.vars` reference across the whole overlay).
4. this WO's diff touches nothing under the AppKit generator / cloudflare packages.
5. [LIVE] `docker compose up -d --build` is DEFERRED (no compose provider on the
   host); the non-LIVE proxy parses the emitted `compose.yaml` with pyyaml and
   asserts its structure.

Plus folded runtime-matrix regression coverage for portable Python starts.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from shutil import which

import pytest
import yaml
from disco.core.appkit import default_lead_gen_app_spec, generate, get_recipe
from disco.core.release.detect import DetectionResult, Provenance, detect_release
from disco.core.release.local_compose import (
    COMPOSE_PATH,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    ENV_EXAMPLE_PATH,
    RELEASE_JSON_PATH,
    SELFHOST_DOC_PATH,
    emit_local_compose,
)
from disco.core.release.spec import (
    DetectorProvenance,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseSpec,
    ResourceKind,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
    load_release_spec,
)

# A real-looking admin token that must NEVER appear in the emitted bundle. It is
# never injected anywhere — the emitter takes only the (NAMES-only) spec — so its
# absence is a genuine proof that no secret VALUE can leak into the export.
SECRET_SENTINEL = "adm1n-t0ken-VALUE-must-never-leak-1a2b3c4d"

# The AppKit contract files whose joint presence is the AppKit shape.
_APPKIT_CONTRACT = (".disco/appspec.json", "wrangler.toml", "worker/index.ts", "schema.sql")

# Directories this WO must not touch (criterion 4).
_FORBIDDEN_DIFF_PREFIXES = (
    "packages/core/src/disco/core/appkit/",
    "packages/agent-server/src/disco/agent_server/appkit_cloudflare/",
)


def _appkit_fixture_tree() -> dict[str, str]:
    """The on-disk shape of a generated AppKit app: the REAL generator's tree plus
    the `.disco/appspec.json` the tool layer persists (the generator never emits
    it). This is exactly what a committed AppKit workspace looks like on disk."""
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    app = default_lead_gen_app_spec("Acme", recipe)
    tree = dict(generate(app, recipe.to_design_spec()))
    # The generator emits three of the four contract files; app_create persists the
    # spec at .disco/appspec.json. Add it so the tree is a full AppKit workspace.
    assert ".disco/appspec.json" not in tree
    tree[".disco/appspec.json"] = app.model_dump_json(indent=2)
    for contract_file in _APPKIT_CONTRACT:
        assert contract_file in tree, contract_file
    return tree


def _detect_appkit() -> DetectionResult:
    return detect_release(_appkit_fixture_tree(), intent=None, provenance=Provenance())


def _appkit_spec() -> ReleaseSpec:
    """Stitch the detector's findings to a source binding — the way the pipeline
    builds a full ReleaseSpec from a DetectionResult."""
    result = _detect_appkit()
    assert result.assessment is ReleaseAssessment.candidate
    return ReleaseSpec(
        kind="appkit",
        name="Acme",
        version_seq=1,
        tree_digest="a" * 64,
        services=result.services,
        env=result.env,
        resources=result.resources,
        provenance=DetectorProvenance(
            detector="release-detect",
            detector_version="1",
            assessment=ReleaseAssessment.candidate,
            evidence=result.evidence,
        ),
    )


# ---- criterion 1: detection → dev_server candidate + /data/state sqlite --------


def test_appkit_tree_detects_dev_server_candidate() -> None:
    result = _detect_appkit()

    assert result.assessment is ReleaseAssessment.candidate

    # EXACTLY ONE ingress service, of strategy dev_server.
    ingress_services = [s for s in result.services if s.role is ServiceRole.ingress]
    assert len(ingress_services) == 1
    assert len(result.services) == 1
    ingress = ingress_services[0]
    assert ingress.runtime is RuntimeStrategy.dev_server

    # A sqlite-class resource whose persistent path is /data/state.
    assert len(result.resources) == 1
    resource = result.resources[0]
    assert resource.kind is ResourceKind.sqlite
    assert resource.persistent_path == "/data/state"
    # its migrate command applies schema.sql to the app's real D1 database name.
    assert "--file=./schema.sql" in resource.migrate_cmd
    assert resource.migrate_cmd[:4] == ("npx", "wrangler", "d1", "execute")

    # ADMIN_TOKEN is declared as a required secret (NAMES only — no value).
    assert any(
        var.name == "ADMIN_TOKEN" and var.required and var.secret is SecretClass.secret
        for var in result.env
    )


def test_appkit_spec_validates_and_round_trips() -> None:
    # The stitched spec is a valid, revalidating ReleaseSpec (exactly one ingress,
    # referential integrity), and release.json round-trips back to an EQUAL spec.
    spec = _appkit_spec()
    overlay = emit_local_compose(spec)
    reloaded = load_release_spec(overlay[RELEASE_JSON_PATH])
    assert reloaded == spec


# ---- criterion 2 + 5: emitted-bundle structure (pyyaml proxy) ------------------


def _compose(overlay: dict[str, str]) -> dict[str, object]:
    parsed = yaml.safe_load(overlay[COMPOSE_PATH])
    assert isinstance(parsed, dict)
    return parsed


def _service(compose: dict[str, object], name: str) -> dict[str, object]:
    services = compose["services"]
    assert isinstance(services, dict)
    block = services[name]
    assert isinstance(block, dict)
    return block


def test_dockerfile_installs_and_builds() -> None:
    dockerfile = emit_local_compose(_appkit_spec())[DOCKERFILE_PATH]
    assert "FROM node:22-slim" in dockerfile
    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    # the entrypoint is wired so the container writes secrets and execs the command.
    assert "ENTRYPOINT" in dockerfile


def test_app_service_runs_wrangler_dev_with_persist() -> None:
    overlay = emit_local_compose(_appkit_spec())
    compose = _compose(overlay)
    ingress_id = _appkit_spec().services[0].id
    app = _service(compose, ingress_id)

    command = app["command"]
    assert isinstance(command, list)
    joined = " ".join(str(tok) for tok in command)
    assert "--ip 0.0.0.0" in joined
    assert "--persist-to /data/state" in joined
    assert "wrangler dev" in joined

    # published on loopback only, on the wrangler-dev port.
    ports = app["ports"]
    assert isinstance(ports, list)
    assert any("127.0.0.1:" in str(p) and ":8787" in str(p) for p in ports)

    # a GET / healthcheck on the wrangler-dev port.
    healthcheck = app["healthcheck"]
    assert isinstance(healthcheck, dict)
    hc_test = healthcheck["test"]
    assert isinstance(hc_test, list)
    hc_joined = " ".join(str(tok) for tok in hc_test)
    assert "http://127.0.0.1:8787/" in hc_joined


def test_init_service_applies_schema_before_app() -> None:
    overlay = emit_local_compose(_appkit_spec())
    compose = _compose(overlay)
    ingress_id = _appkit_spec().services[0].id
    app = _service(compose, ingress_id)

    # the app waits on a one-shot init that completed successfully.
    depends_on = app["depends_on"]
    assert isinstance(depends_on, dict)
    assert len(depends_on) == 1
    init_name = next(iter(depends_on))
    condition = depends_on[init_name]
    assert isinstance(condition, dict)
    assert condition["condition"] == "service_completed_successfully"

    init = _service(compose, init_name)
    assert init["restart"] == "no"
    # the init argv (in `entrypoint`, bypassing the secret-writing image entrypoint)
    # applies schema.sql to the SAME persist dir the app reads.
    init_entry = init["entrypoint"]
    assert isinstance(init_entry, list)
    init_joined = " ".join(str(tok) for tok in init_entry)
    assert "schema.sql" in init_joined
    assert "--persist-to /data/state" in init_joined
    assert "wrangler d1 execute" in init_joined


def test_named_volume_mounted_at_data_on_both_services() -> None:
    overlay = emit_local_compose(_appkit_spec())
    compose = _compose(overlay)
    ingress_id = _appkit_spec().services[0].id

    volumes = compose["volumes"]
    assert isinstance(volumes, dict)
    assert len(volumes) == 1
    volume_name = next(iter(volumes))

    for service_name in (ingress_id, "init"):
        block = _service(compose, service_name)
        mounts = block["volumes"]
        assert isinstance(mounts, list)
        assert mounts == [f"{volume_name}:/data"]


def test_admin_token_only_appears_as_guard_and_name() -> None:
    overlay = emit_local_compose(_appkit_spec())
    compose_text = overlay[COMPOSE_PATH]

    # in compose, ADMIN_TOKEN's VALUE is only ever the interpolation guard.
    assert "${ADMIN_TOKEN:?" in compose_text
    compose = _compose(overlay)
    ingress_id = _appkit_spec().services[0].id
    environment = _service(compose, ingress_id)["environment"]
    assert isinstance(environment, dict)
    assert str(environment["ADMIN_TOKEN"]).startswith("${ADMIN_TOKEN:?")

    # .env.example lists ADMIN_TOKEN as a NAME (an empty-value assignment line).
    env_example = overlay[ENV_EXAMPLE_PATH]
    assert "ADMIN_TOKEN=" in env_example
    assert f"ADMIN_TOKEN={SECRET_SENTINEL}" not in env_example


def test_bundle_bytes_scan_finds_no_secret_value() -> None:
    # The whole bundle = the AppKit tree + the emitted overlay. The overlay is a
    # pure function of the NAMES-only spec, so no admin token VALUE can appear.
    bundle = dict(_appkit_fixture_tree())
    bundle.update(emit_local_compose(_appkit_spec()))
    for path, content in bundle.items():
        assert SECRET_SENTINEL not in content, path


# ---- criterion 3: no .dev.vars file; entrypoint is its only writer -------------


def test_bundle_has_no_dev_vars_file() -> None:
    bundle = dict(_appkit_fixture_tree())
    bundle.update(emit_local_compose(_appkit_spec()))
    # the real secret file is never emitted (only the AppKit `.dev.vars.example`
    # template exists, which is a placeholder, not `.dev.vars`).
    assert ".dev.vars" not in bundle


def test_overlay_references_dev_vars_exactly_once_in_entrypoint() -> None:
    overlay = emit_local_compose(_appkit_spec())
    # The Dockerfile entrypoint heredoc is the ONLY place that WRITES the secret
    # file — exactly one reference, and it is the redirect target.
    dockerfile = overlay[DOCKERFILE_PATH]
    assert dockerfile.count(".dev.vars") == 1
    assert "> /app/.dev.vars" in dockerfile
    # Every OTHER overlay reference to `.dev.vars` is a defensive EXCLUSION, never a
    # write: only the `.dockerignore` may mention it (defence-in-depth — keeps a
    # stray host `.dev.vars` secret out of the build context).
    for path, content in overlay.items():
        if path in (DOCKERFILE_PATH, DOCKERIGNORE_PATH):
            continue
        assert ".dev.vars" not in content, path


def test_dockerignore_excludes_stray_dev_vars_secret() -> None:
    # Defence-in-depth: a real host `.dev.vars` (or a `.dev.vars.<env>` variant) —
    # if the owner ran `wrangler dev` before building — can never enter an image
    # layer. The committed `.dev.vars.example` placeholder stays included, mirroring
    # the `.env` / `!.env.example` treatment already in the list.
    dockerignore = emit_local_compose(_appkit_spec())[DOCKERIGNORE_PATH]
    entries = dockerignore.splitlines()
    assert ".dev.vars" in entries
    assert ".dev.vars.*" in entries
    assert "!.dev.vars.example" in entries


def test_dev_server_env_example_declares_wrangler_dev_host_port() -> None:
    # The AppKit dev_server publishes 127.0.0.1:${HOST_PORT:-8787}:8787, so its
    # `.env.example` HOST_PORT default must read 8787 — not the generic 8080.
    env_example = emit_local_compose(_appkit_spec())[ENV_EXAMPLE_PATH]
    lines = env_example.splitlines()
    assert "# HOST_PORT — host port to publish the app on (optional; default 8787)." in lines
    assert "# HOST_PORT=8787" in lines
    assert "# HOST_PORT=8080" not in lines
    assert "default 8080" not in env_example


def test_selfhost_doc_states_interim_and_v2() -> None:
    doc = emit_local_compose(_appkit_spec())[SELFHOST_DOC_PATH]
    lowered = doc.lower()
    assert "interim" in lowered
    assert "workerd dev runtime" in lowered
    assert "wrangler dev" in lowered
    assert "appkit v2" in lowered
    # the doc must not itself reference the secret file (single-reference invariant).
    assert ".dev.vars" not in doc


# ---- determinism ---------------------------------------------------------------


def test_dev_server_overlay_is_deterministic() -> None:
    a = emit_local_compose(_appkit_spec())
    b = emit_local_compose(_appkit_spec())
    assert a == b


# ---- criterion 4: this WO does not edit AppKit generation / cloudflare ---------


def test_wo6_diff_does_not_touch_appkit_packages() -> None:
    repo = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent,
    )
    if repo.returncode != 0:
        pytest.skip("not a git worktree — cannot diff-check the AppKit boundary")
    root = Path(repo.stdout.strip())
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    )
    changed: list[str] = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        # porcelain: 2 status chars, a space, then the path (handle renames).
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        changed.append(path)
    offenders = [
        path for path in changed for prefix in _FORBIDDEN_DIFF_PREFIXES if path.startswith(prefix)
    ]
    assert not offenders, offenders


# ---- folded runtime-matrix regression: portable Python only -------------------


def test_versioned_python_interpreter_without_server_or_target_fails_closed() -> None:
    # The export image is pinned to Python 3.13 and does not promise a `python3.12`
    # executable. The opaque `app` module is also not a supported HTTP server, and an
    # empty tree proves neither a dependency nor an application target. Do not turn this
    # unshippable declaration into a candidate merely because its head resembles Python.
    intent = ReleaseIntent(
        build_cmd=(),
        start_cmd=("python3.12", "-m", "app"),
        port_env="PORT",
        required_env=(),
    )
    result = detect_release({}, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert result.ingress is None
    assert any(blocker.code == "toolchain_unsupported" for blocker in result.blockers)


def test_portable_python3_uvicorn_with_real_dependency_and_target_is_candidate() -> None:
    # The portable interpreter spelling remains supported when the effective server,
    # dependency, module, and module-scope application attribute are all proven.
    intent = ReleaseIntent(
        build_cmd=(),
        start_cmd=("python3", "-m", "uvicorn", "main:app"),
        port_env="PORT",
        required_env=(),
    )
    result = detect_release(
        {
            "requirements.txt": b"uvicorn==0.30\n",
            # A dependency-free ASGI callable: a proven reachable target for uvicorn-only install.
            "main.py": b"async def app(scope, receive, send):\n    pass\n",
        },
        intent=intent,
        provenance=Provenance(),
    )
    assert result.assessment is ReleaseAssessment.candidate
    assert result.ingress is not None
    assert result.ingress.runtime is RuntimeStrategy.python
    assert result.ingress.start_cmd[-4:] == ("--host", "0.0.0.0", "--port", "${PORT}")


# ---- criterion 5: [LIVE] docker compose up — DEFERRED --------------------------


@pytest.mark.integration
def test_docker_compose_up_appkit_bundle(tmp_path: Path) -> None:
    """[LIVE] `ADMIN_TOKEN=test docker compose up -d --build` — DEFERRED.

    There is NO compose provider on the build host, so this is integration-marked
    and excluded from the required unit run. On a docker host it would: write the
    AppKit tree + overlay to disk, `docker compose up -d --build`, then verify
    `GET /` serves the SPA, `POST /api/leads` persists a lead, the lead survives a
    `docker compose restart`, and `docker compose down` (without `-v`) keeps the
    named volume. Here we skip when no provider is present. The non-LIVE proxy
    (structure asserted via pyyaml) lives in the tests above."""
    if which("docker") is None:
        pytest.skip("docker not available — AppKit compose up is deferred [LIVE]")
    bundle = dict(_appkit_fixture_tree())
    bundle.update(emit_local_compose(_appkit_spec()))
    for rel, content in bundle.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    up = subprocess.run(
        ["docker", "compose", "up", "-d", "--build"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        # the secret is supplied at compose-up time, NEVER written into the bundle.
        env={**os.environ, "ADMIN_TOKEN": "test"},
    )
    try:
        assert up.returncode == 0, up.stderr
    finally:
        subprocess.run(
            ["docker", "compose", "down"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

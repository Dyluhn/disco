"""WO-4 — acceptance tests for the LocalComposeAdapter (`local_compose`).

Proves the emitter is DETERMINISTIC and byte-identical to checked-in goldens for a
node / python / static release, that the compose document has the required
structure (one loopback-bound port, `${VAR:?}` guards for required runtime env
only, a named volume per persistent path, a one-shot migrate service wired with
`service_completed_successfully`, an ingress healthcheck), that `.env.example` is
NAMES-ONLY, that `release.json` round-trips back to an EQUAL `ReleaseSpec`, that no
planted secret sentinel ever reaches the overlay, and that a path collision is
reported as a typed `OverlayConflict` rather than silently overwritten.

The real `docker compose config` validation is marked `integration` (deferred to a
docker host — there is no compose provider here); the non-LIVE proxy parses every
emitted `compose.yaml` with a real YAML parser (pyyaml) and asserts its structure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from shutil import which

import pytest
import yaml
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.local_compose import (
    COMPOSE_PATH,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    ENV_EXAMPLE_PATH,
    RELEASE_JSON_PATH,
    SELFHOST_DOC_PATH,
    OverlayConflict,
    emit_local_compose,
    emit_local_compose_checked,
)
from disco.core.release.spec import (
    DetectorProvenance,
    EnvScope,
    EnvVarDecl,
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
    load_release_spec,
)

FIXTURES = Path(__file__).parent / "fixtures" / "release_compose"
SECRET_TREE = FIXTURES / "secret_tree"
SENTINEL = "SENTINEL-SECRET-8f3a9c2b1d4e-DO-NOT-LEAK"

_DIGEST = "a" * 64


# ---- shared spec builders (single source of truth, also used to freeze goldens) -


def _prov() -> DetectorProvenance:
    return DetectorProvenance(
        detector="release-detect",
        detector_version="1",
        assessment=ReleaseAssessment.candidate,
        evidence=("package.json start script",),
    )


def _sqlite_resource(migrate_cmd: tuple[str, ...]) -> ResourceDecl:
    return ResourceDecl(
        id="db",
        kind=ResourceKind.sqlite,
        persistent_path="/data/app.db",
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url="file:/data/app.db", volume="app-data"),
        ),
        consumers=("web",),
        migrate_cmd=migrate_cmd,
    )


def node_spec() -> ReleaseSpec:
    return ReleaseSpec(
        kind="web",
        name="Acme Node App",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.node,
                package_manager="npm",
                lockfile="package-lock.json",
                install_cmd=("npm", "ci"),
                build_cmd=("npm", "run", "build"),
                start_cmd=("npm", "start"),
                port_env="PORT",
                health_path="/healthz",
            ),
        ),
        env=(
            EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime, required=True, binding="db"),
            EnvVarDecl(
                name="OPENAI_API_KEY",
                scope=EnvScope.runtime,
                required=True,
                secret=SecretClass.secret,
            ),
            EnvVarDecl(name="LOG_LEVEL", scope=EnvScope.runtime, required=False),
        ),
        resources=(_sqlite_resource(("npm", "run", "migrate")),),
        provenance=_prov(),
    )


def python_spec() -> ReleaseSpec:
    return ReleaseSpec(
        kind="web",
        name="Acme Python API",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.python,
                install_cmd=("pip", "install", "-r", "requirements.txt"),
                start_cmd=("uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}"),
                port_env="PORT",
                health_path="/health",
            ),
        ),
        env=(
            EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime, required=True, binding="db"),
            EnvVarDecl(
                name="API_TOKEN", scope=EnvScope.runtime, required=True, secret=SecretClass.secret
            ),
            EnvVarDecl(name="DEBUG", scope=EnvScope.runtime, required=False),
        ),
        resources=(_sqlite_resource(("alembic", "upgrade", "head")),),
        provenance=_prov(),
    )


def static_spec() -> ReleaseSpec:
    return ReleaseSpec(
        kind="static",
        name="Acme Static Site",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.static,
                output_dir=".",
                port_env="PORT",
                health_path="/",
            ),
        ),
        provenance=_prov(),
    )


# (name, spec, golden-basename, has-emitted-Dockerfile)
_GOLDEN_CASES = (
    ("node", node_spec, "node", True),
    ("python", python_spec, "python", True),
    ("static", static_spec, "static", True),
)


def _read_golden(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---- (1) golden + determinism -------------------------------------------------


@pytest.mark.parametrize(("label", "builder", "base", "_has_df"), _GOLDEN_CASES)
def test_compose_and_dockerfile_match_golden(label, builder, base, _has_df):
    overlay = emit_local_compose(builder())
    assert overlay[COMPOSE_PATH] == _read_golden(f"{base}.compose.yaml"), (
        f"{label} compose.yaml drifted from golden"
    )
    assert overlay[DOCKERFILE_PATH] == _read_golden(f"{base}.Dockerfile"), (
        f"{label} Dockerfile drifted from golden"
    )


@pytest.mark.parametrize(("label", "builder", "base", "_has_df"), _GOLDEN_CASES)
def test_emission_is_deterministic(label, builder, base, _has_df):
    spec = builder()
    first = emit_local_compose(spec)
    second = emit_local_compose(spec)
    assert first == second
    # A freshly-built (equal) spec must also emit identical bytes.
    assert emit_local_compose(builder()) == first


def test_overlay_has_the_full_file_set():
    overlay = emit_local_compose(node_spec())
    assert set(overlay) == {
        COMPOSE_PATH,
        DOCKERFILE_PATH,
        DOCKERIGNORE_PATH,
        ENV_EXAMPLE_PATH,
        SELFHOST_DOC_PATH,
        RELEASE_JSON_PATH,
    }


# ---- (2) structural asserts on compose.yaml -----------------------------------


def _compose_doc(spec: ReleaseSpec) -> dict[str, object]:
    parsed = yaml.safe_load(emit_local_compose(spec)[COMPOSE_PATH])
    assert isinstance(parsed, dict)
    return parsed


def _service_block(doc: dict[str, object], name: str) -> dict[str, object]:
    services = doc["services"]
    assert isinstance(services, dict)
    block = services[name]
    assert isinstance(block, dict)
    return block


def test_exactly_one_published_port_mapping():
    doc = _compose_doc(node_spec())
    services = doc["services"]
    assert isinstance(services, dict)
    port_mappings = [
        mapping
        for service in services.values()
        if isinstance(service, dict)
        for mapping in service.get("ports", [])
    ]
    assert port_mappings == ["127.0.0.1:${HOST_PORT:-8080}:8080"]


def test_required_runtime_env_is_guarded_and_optional_is_not():
    compose = emit_local_compose(node_spec())[COMPOSE_PATH]
    # DATABASE_URL is resource-bound (a literal), so it must NOT be guarded.
    assert "${OPENAI_API_KEY:?" in compose  # required secret -> guarded
    assert "${LOG_LEVEL:?" not in compose  # optional -> NOT guarded
    assert "${LOG_LEVEL}" in compose  # optional present as a bare ref
    assert "${DATABASE_URL:?" not in compose  # bound -> literal, never guarded
    assert "file:/data/app.db" in compose


def test_named_volume_per_persistent_path():
    doc = _compose_doc(node_spec())
    volumes = doc["volumes"]
    assert isinstance(volumes, dict)
    assert "app-data" in volumes
    web = _service_block(doc, "web")
    assert "app-data:/data" in web["volumes"]


def test_migrate_oneshot_service_wired_with_completion_condition():
    doc = _compose_doc(node_spec())
    services = doc["services"]
    assert isinstance(services, dict)
    assert "migrate" in services
    migrate = services["migrate"]
    assert isinstance(migrate, dict)
    assert migrate["restart"] == "no"
    assert migrate["command"] == ["npm", "run", "migrate"]
    web = services["web"]
    assert isinstance(web, dict)
    assert web["depends_on"] == {"migrate": {"condition": "service_completed_successfully"}}


def test_ingress_has_a_healthcheck():
    doc = _compose_doc(node_spec())
    web = _service_block(doc, "web")
    assert "healthcheck" in web
    healthcheck = web["healthcheck"]
    assert isinstance(healthcheck, dict)
    assert healthcheck["test"][0] == "CMD"


def test_no_host_bind_mounts_anywhere():
    for builder in (node_spec, python_spec, static_spec):
        doc = _compose_doc(builder())
        services = doc["services"]
        assert isinstance(services, dict)
        for service in services.values():
            assert isinstance(service, dict)
            for mount in service.get("volumes", []):
                # A named-volume mount is `name:/path`; a bind mount starts with
                # `.`/`/`/`~` (a host path). None may be host binds.
                assert not mount.startswith((".", "/", "~"))


# ---- (3) .env.example (NAMES ONLY) + release.json round-trip ------------------


def test_env_example_lists_required_names_without_values():
    env_example = emit_local_compose(python_spec())[ENV_EXAMPLE_PATH]
    lines = env_example.splitlines()
    # API_TOKEN is required + not resource-bound -> appears as a bare `NAME=`.
    assert "API_TOKEN=" in lines
    # No line assigns a value to a required name.
    assert not any(line.startswith("API_TOKEN=") and line != "API_TOKEN=" for line in lines)
    # DATABASE_URL is resource-bound (auto-provided) -> must NOT be requested.
    assert not any(line.strip().lstrip("# ").startswith("DATABASE_URL=") for line in lines)
    # Optional env is present but commented out.
    assert "# DEBUG=" in lines


def test_env_example_is_secret_free():
    env_example = emit_local_compose(node_spec())[ENV_EXAMPLE_PATH]
    # Only names + empty assignments + comments — never a `KEY=<value>`.
    for line in env_example.splitlines():
        if line.startswith("#") or not line:
            continue
        assert line.endswith("="), f"unexpected value-bearing line in .env.example: {line!r}"


def test_env_example_host_port_default_reflects_published_8080():
    # node/python/static publish 127.0.0.1:${HOST_PORT:-8080}:8080, so the
    # `.env.example` HOST_PORT default must stay 8080 (the dev_server case, which
    # publishes 8787, is proven in test_release_appkit_local.py).
    for builder in (node_spec, python_spec, static_spec):
        env_example = emit_local_compose(builder())[ENV_EXAMPLE_PATH]
        lines = env_example.splitlines()
        assert "# HOST_PORT — host port to publish the app on (optional; default 8080)." in lines
        assert "# HOST_PORT=8080" in lines
        assert "8787" not in env_example


@pytest.mark.parametrize(("label", "builder", "base", "_has_df"), _GOLDEN_CASES)
def test_release_json_round_trips_to_equal_spec(label, builder, base, _has_df):
    spec = builder()
    overlay = emit_local_compose(spec)
    reloaded = load_release_spec(overlay[RELEASE_JSON_PATH])
    assert reloaded == spec


# ---- (4) secret proof ---------------------------------------------------------


def _load_tree(root: Path) -> dict[str, str]:
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            tree[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return tree


def _spec_from_secret_tree() -> ReleaseSpec:
    """Build a ReleaseSpec from the planted-secret fixture tree the way the real
    pipeline does: detect the service, then bind the source + secret env NAME. The
    secret VALUE in the tree's `.env` never enters the spec (it records NAMES)."""
    tree = _load_tree(SECRET_TREE)
    # Plant the secret IN-MEMORY. The `.env` that would hold it on disk is
    # gitignored (`.gitignore` — `.env`), so it is never committed: reading it
    # from disk makes this proof pass only in a worktree that happens to have an
    # untracked `.env` and ERROR on a fresh checkout / CI. Injecting it here keeps
    # the test self-contained — the committed fixture ships only non-secret
    # sources (package.json, package-lock.json, server.js).
    tree[".env"] = f"OPENAI_API_KEY={SENTINEL}\n"
    # Sanity: the planted secret really is in the tree we build from.
    assert any(SENTINEL in content for content in tree.values())
    result = detect_release(dict(tree), intent=None, provenance=Provenance())
    ingress = result.ingress
    assert ingress is not None and ingress.runtime is RuntimeStrategy.node
    return ReleaseSpec(
        kind="web",
        name="Leaky App",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(ingress,),
        env=(
            EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime, required=True, binding="db"),
            EnvVarDecl(
                name="OPENAI_API_KEY",
                scope=EnvScope.runtime,
                required=True,
                secret=SecretClass.secret,
            ),
        ),
        resources=(_sqlite_resource(("node", "migrate.js")),),
        provenance=_prov(),
    )


def test_no_planted_secret_reaches_the_overlay():
    spec = _spec_from_secret_tree()
    overlay = emit_local_compose(spec)
    leaks = {path: content for path, content in overlay.items() if SENTINEL in content}
    assert leaks == {}, f"secret sentinel leaked into overlay files: {sorted(leaks)}"
    # The secret NAME is legitimately present (as a name/guard), the VALUE is not.
    assert "OPENAI_API_KEY" in overlay[COMPOSE_PATH]


# ---- (5) path-collision safety ------------------------------------------------


def test_collision_returns_typed_overlay_conflict():
    spec = node_spec()
    existing = {"README.md", COMPOSE_PATH, DOCKERFILE_PATH}
    result = emit_local_compose_checked(spec, existing)
    assert isinstance(result, OverlayConflict)
    assert result.paths == (DOCKERFILE_PATH, COMPOSE_PATH) or set(result.paths) == {
        COMPOSE_PATH,
        DOCKERFILE_PATH,
    }
    assert COMPOSE_PATH in result.message and DOCKERFILE_PATH in result.message


def test_no_collision_returns_overlay():
    spec = node_spec()
    result = emit_local_compose_checked(spec, {"README.md", "src/index.js"})
    assert not isinstance(result, OverlayConflict)
    assert result == emit_local_compose(spec)


# ---- (6) YAML-parse proxy (docker compose config is deferred [LIVE]) ----------


@pytest.mark.parametrize(("label", "builder", "base", "_has_df"), _GOLDEN_CASES)
def test_compose_is_valid_yaml_with_expected_structure(label, builder, base, _has_df):
    spec = builder()
    doc = yaml.safe_load(emit_local_compose(spec)[COMPOSE_PATH])
    assert isinstance(doc, dict)
    assert "services" in doc
    services = doc["services"]
    assert isinstance(services, dict)
    web = services["web"]
    assert isinstance(web, dict)
    for key in ("build", "restart", "environment", "ports", "healthcheck"):
        assert key in web, f"{label} ingress missing {key!r}"
    assert web["restart"] == "unless-stopped"
    # `volumes` is present exactly when the release has persistent resources.
    if spec.resources:
        assert "volumes" in doc and isinstance(doc["volumes"], dict)
    else:
        assert "volumes" not in doc


# ---- container strategy + multi-service Dockerfile placement ------------------


def container_spec() -> ReleaseSpec:
    return ReleaseSpec(
        kind="web",
        name="Acme Container App",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.container,
                port_env="PORT",
                health_path="/healthz",
            ),
        ),
        provenance=_prov(),
    )


def test_container_strategy_references_own_dockerfile_and_emits_none():
    overlay = emit_local_compose(container_spec())
    # A container service ships its OWN Dockerfile — we must NOT emit/overwrite one.
    assert DOCKERFILE_PATH not in overlay
    assert not any(path.endswith(".Dockerfile") for path in overlay)
    doc = _compose_doc(container_spec())
    web = _service_block(doc, "web")
    build = web["build"]
    assert isinstance(build, dict)
    assert build["dockerfile"] == "Dockerfile"  # the user's, referenced not emitted
    assert "healthcheck" in web


def multi_service_spec() -> ReleaseSpec:
    return ReleaseSpec(
        kind="web",
        name="Acme Multi App",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.node,
                install_cmd=("npm", "ci"),
                start_cmd=("npm", "start"),
                port_env="PORT",
                health_path="/healthz",
            ),
            ReleaseService(
                id="worker",
                role=ServiceRole.worker,
                runtime=RuntimeStrategy.python,
                install_cmd=("pip", "install", "-r", "requirements.txt"),
                start_cmd=("python", "worker.py"),
                port_env="PORT",
            ),
        ),
        provenance=_prov(),
    )


def test_multi_service_uses_selfhost_dockerfiles():
    overlay = emit_local_compose(multi_service_spec())
    # Per-service Dockerfiles under selfhost/ (never a bare root Dockerfile).
    assert "selfhost/web.Dockerfile" in overlay
    assert "selfhost/worker.Dockerfile" in overlay
    assert DOCKERFILE_PATH not in overlay
    doc = _compose_doc(multi_service_spec())
    web = _service_block(doc, "web")
    worker = _service_block(doc, "worker")
    web_build = web["build"]
    worker_build = worker["build"]
    assert isinstance(web_build, dict) and web_build["dockerfile"] == "selfhost/web.Dockerfile"
    assert isinstance(worker_build, dict)
    assert worker_build["dockerfile"] == "selfhost/worker.Dockerfile"
    # Only the ingress publishes a port.
    assert "ports" in web
    assert "ports" not in worker


def test_build_scope_env_becomes_build_args():
    spec = ReleaseSpec(
        kind="web",
        name="Acme Build-Arg App",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.node,
                install_cmd=("npm", "ci"),
                start_cmd=("npm", "start"),
                port_env="PORT",
                health_path="/",
            ),
        ),
        env=(
            EnvVarDecl(
                name="NPM_TOKEN",
                scope=EnvScope.build,
                required=True,
                secret=SecretClass.secret,
            ),
            EnvVarDecl(name="SENTRY_DSN", scope=EnvScope.build, required=False),
        ),
        provenance=_prov(),
    )
    doc = _compose_doc(spec)
    web = _service_block(doc, "web")
    build = web["build"]
    assert isinstance(build, dict)
    args = build["args"]
    assert isinstance(args, dict)
    assert args["NPM_TOKEN"] == "${NPM_TOKEN:?Set NPM_TOKEN — see .env.example}"  # required guard
    assert args["SENTRY_DSN"] == "${SENTRY_DSN}"  # optional, no guard
    # Build-scope env is NOT duplicated into runtime `environment`.
    env = web["environment"]
    assert isinstance(env, dict)
    assert "NPM_TOKEN" not in env


@pytest.mark.integration
def test_docker_compose_config_accepts_bundle(tmp_path):
    """[LIVE] Real `docker compose config` validation — DEFERRED to a docker host.

    There is no compose provider on the build host, so this is integration-marked
    and excluded from the required unit run. On a host with Docker Compose v2 it
    writes the emitted bundle to disk and asserts `docker compose config` parses
    it; without one it skips."""
    compose = which("docker")
    if compose is None:
        pytest.skip("docker not available on this host — compose config is deferred [LIVE]")
    overlay = emit_local_compose(node_spec())
    for rel, content in overlay.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    proc = subprocess.run(
        ["docker", "compose", "config"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

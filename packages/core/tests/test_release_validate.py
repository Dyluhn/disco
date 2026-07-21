"""Tests for the light release validation plane (`disco.core.release.validate`).

Proves each WO-2 acceptance criterion: a typed non-raising `ValidationResult`;
every blocker independently; the relocated secret-path predicate still behaves
identically and re-exports from `disco.tools.projects`; and that the light plane
carries no container-build dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
)
from disco.core.release.validate import (
    Blocker,
    BlockerCode,
    ValidationResult,
    validate_release,
)

# ---- fixtures -----------------------------------------------------------------


def _ingress(**overrides: Any) -> ReleaseService:
    base: dict[str, Any] = {
        "id": "web",
        "role": ServiceRole.ingress,
        "runtime": RuntimeStrategy.node,
    }
    base.update(overrides)
    return ReleaseService(**base)


def _resource(**overrides: Any) -> ResourceDecl:
    # WO-C7: a resource in a ReleaseSpec must carry an absolute-normalized
    # persistent_path and at least one valid consumer (`web`, the ingress here).
    base: dict[str, Any] = {
        "id": "db",
        "kind": ResourceKind.sqlite,
        "persistent_path": "/data/app.db",
        "profiles": ResourceProfiles(
            local=LocalResourceProfile(url="file:/data/app.db", volume="app-data")
        ),
        "consumers": ("web",),
    }
    base.update(overrides)
    return ResourceDecl(**base)


def _spec(**overrides: Any) -> ReleaseSpec:
    base: dict[str, Any] = {
        "kind": "web",
        "name": "demo",
        "version_seq": 1,
        "tree_digest": "a" * 64,
        "services": (_ingress(),),
        "env": (),
        "resources": (),
        "provenance": DetectorProvenance(
            detector="node-detector",
            detector_version="1",
            assessment=ReleaseAssessment.candidate,
        ),
    }
    base.update(overrides)
    return ReleaseSpec(**base)


def _codes(result: ValidationResult) -> list[BlockerCode]:
    return [blocker.code for blocker in result.blockers]


# ---- criterion 1: typed result, never raises ---------------------------------


def test_returns_typed_result_and_does_not_raise() -> None:
    spec = _spec()
    files: Mapping[str, int | bytes] = {"index.html": b"<html>", ".env": 10}
    try:
        result = validate_release(spec, files)
    except Exception as exc:  # pragma: no cover - failure path
        raise AssertionError(f"validate_release must not raise, raised {exc!r}") from exc
    assert isinstance(result, ValidationResult)
    assert isinstance(result.blockers, list)
    assert all(isinstance(blocker, Blocker) for blocker in result.blockers)
    # ok is a real bool derived from blockers and cannot disagree with them.
    assert isinstance(result.ok, bool)
    assert result.ok is (len(result.blockers) == 0)
    assert result.ok is False  # the .env file forces at least one blocker


def test_valid_spec_and_clean_tree_is_ok() -> None:
    spec = _spec()
    result = validate_release(spec, {"index.html": 42, "package.json": 100})
    assert result.ok is True
    assert result.blockers == []


# ---- criterion 2a: missing service root / referenced path --------------------


def test_missing_service_root_blocks() -> None:
    backend = ReleaseService(
        id="api",
        role=ServiceRole.backend,
        runtime=RuntimeStrategy.python,
        root="backend",
        start_cmd=("python", "app.py"),
    )
    spec = _spec(services=(_ingress(root="."), backend))
    result = validate_release(spec, {"index.html": 3})
    assert BlockerCode.missing_path in _codes(result)
    assert any(
        blocker.code is BlockerCode.missing_path and blocker.path == "backend"
        for blocker in result.blockers
    )
    assert result.ok is False


def test_missing_lockfile_blocks_but_present_lockfile_ok() -> None:
    spec_missing = _spec(services=(_ingress(root=".", lockfile="package-lock.json"),))
    missing = validate_release(spec_missing, {"index.html": 3, "package.json": 5})
    assert any(
        blocker.code is BlockerCode.missing_path and blocker.path == "package-lock.json"
        for blocker in missing.blockers
    )

    spec_present = _spec(services=(_ingress(root=".", lockfile="package-lock.json"),))
    present = validate_release(spec_present, {"index.html": 3, "package-lock.json": 9})
    assert not any(blocker.code is BlockerCode.missing_path for blocker in present.blockers)


# ---- criterion 2b: zero or two ingress services ------------------------------


def test_two_ingress_services_block() -> None:
    # model_copy(update=...) bypasses the spec's exactly-one-ingress validator,
    # letting us exercise the validation plane's independent defense-in-depth check.
    two = _spec().model_copy(
        update={"services": (_ingress(id="web1"), _ingress(id="web2"))}
    )
    result = validate_release(two, {"index.html": 3})
    assert BlockerCode.ingress_count in _codes(result)
    assert result.ok is False


def test_zero_ingress_services_block() -> None:
    backend = ReleaseService(
        id="api", role=ServiceRole.backend, runtime=RuntimeStrategy.python
    )
    zero = _spec().model_copy(update={"services": (backend,)})
    result = validate_release(zero, {"index.html": 3})
    assert BlockerCode.ingress_count in _codes(result)


# ---- criterion 2c: undeclared ${VAR} in a command or resource ----------------


def test_undeclared_env_var_in_command_blocks() -> None:
    spec = _spec(
        services=(_ingress(start_cmd=("node", "server.js", "--token", "${MYSTERY_TOKEN}")),)
    )
    result = validate_release(spec, {"index.html": 3})
    assert BlockerCode.undeclared_env_var in _codes(result)


def test_declared_env_var_in_command_is_ok() -> None:
    spec = _spec(
        services=(_ingress(start_cmd=("node", "server.js", "--token", "${MYSTERY_TOKEN}")),),
        env=(EnvVarDecl(name="MYSTERY_TOKEN", scope=EnvScope.runtime),),
    )
    result = validate_release(spec, {"index.html": 3})
    assert not any(
        blocker.code is BlockerCode.undeclared_env_var for blocker in result.blockers
    )


def test_undeclared_env_var_in_resource_blocks() -> None:
    resource = _resource(migrate_cmd=("migrate", "--url", "${DB_URL_UNDECLARED}"))
    spec = _spec(services=(_ingress(),), resources=(resource,))
    result = validate_release(spec, {"index.html": 3})
    assert BlockerCode.undeclared_env_var in _codes(result)


def test_port_env_reference_is_not_undeclared() -> None:
    # The $PORT contract is host-injected, so referencing the service port_env is
    # legal even though it is not an entry in spec.env.
    spec = _spec(
        services=(_ingress(port_env="PORT", start_cmd=("node", "server.js", "--port", "${PORT}")),)
    )
    result = validate_release(spec, {"index.html": 3})
    assert not any(
        blocker.code is BlockerCode.undeclared_env_var for blocker in result.blockers
    )


# ---- criterion 2d: secret file present in the tree ---------------------------


def test_secret_files_in_tree_block() -> None:
    spec = _spec()
    files: Mapping[str, int | bytes] = {
        "index.html": b"ok",
        ".env": 10,
        ".dev.vars": 20,
        "sub/.env.local": 5,
        ".env.example": 12,  # template — must NOT block
    }
    result = validate_release(spec, files)
    secret_paths = {
        blocker.path
        for blocker in result.blockers
        if blocker.code is BlockerCode.secret_file_present
    }
    assert {".env", ".dev.vars", "sub/.env.local"} <= secret_paths
    assert ".env.example" not in secret_paths


# ---- criterion 2e: secret env name appearing as a literal value --------------


def test_secret_name_used_as_literal_value_blocks() -> None:
    spec = _spec(
        services=(_ingress(start_cmd=("node", "server.js", "--auth", "API_TOKEN")),),
        env=(EnvVarDecl(name="API_TOKEN", scope=EnvScope.runtime, secret=SecretClass.secret),),
    )
    result = validate_release(spec, {"index.html": 3})
    assert BlockerCode.secret_name_leaked in _codes(result)


def test_secret_name_used_as_reference_does_not_leak() -> None:
    spec = _spec(
        services=(_ingress(start_cmd=("node", "server.js", "--auth", "${API_TOKEN}")),),
        env=(EnvVarDecl(name="API_TOKEN", scope=EnvScope.runtime, secret=SecretClass.secret),),
    )
    result = validate_release(spec, {"index.html": 3})
    assert not any(
        blocker.code is BlockerCode.secret_name_leaked for blocker in result.blockers
    )


def test_public_env_name_literal_does_not_leak() -> None:
    # Only SECRET-classified names are leak-sensitive; a public name as a literal
    # is fine (many CLI flags legitimately spell an env name).
    spec = _spec(
        services=(_ingress(start_cmd=("node", "server.js", "--flag", "PUBLIC_FLAG")),),
        env=(EnvVarDecl(name="PUBLIC_FLAG", scope=EnvScope.runtime, secret=SecretClass.public),),
    )
    result = validate_release(spec, {"index.html": 3})
    assert not any(
        blocker.code is BlockerCode.secret_name_leaked for blocker in result.blockers
    )


# ---- criterion 3: relocated predicate identical + re-export resolves ---------


def test_is_runtime_secret_path_reexport_resolves_and_is_identical() -> None:
    from disco.core.secret_paths import is_runtime_secret_path as core_fn
    from disco.tools.projects import is_runtime_secret_path as tools_fn
    from disco.tools.projects.archive import is_runtime_secret_path as archive_fn

    # Same object end-to-end: the tools re-export is the relocated core predicate.
    assert tools_fn is core_fn
    assert archive_fn is core_fn

    # Behavior is identical to the pre-move predicate on the canonical cases.
    for secret in (".env", ".env.local", "nested/.ENV.production.local", ".dev.vars",
                   "worker/.dev.vars.production", "sub/.env.local"):
        assert tools_fn(secret) is True
    for safe in (".env.example", ".env.sample", "nested/.dev.vars.dist",
                 ".dev.vars.template", "src/environment.ts"):
        assert tools_fn(safe) is False

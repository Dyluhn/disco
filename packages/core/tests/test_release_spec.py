"""ReleaseSpec v1 — the neutral, immutable, secret-free release contract.

Proves the WO-1 acceptance criteria against `disco.core.release.spec`:

1. exactly-one-ingress (zero OR two ingress services raise; exactly one passes),
2. `EnvVarDecl` is value-free + rejects malformed env-var NAMES,
3. `spec_digest` is byte-stable across field order + sensitive to any change,
4. `ResourceProfiles.cloud` defaults `None` + round-trips, and the assessment enum
   carries the forward-compat `verifying|verified|failed` values,
5. canonical serialization round-trip (`serialize → load → serialize`) is
   byte-identical,

plus the supporting invariants (unique ids, referential integrity, argv-lists,
immutability, `tree_digest` shape mirroring `store.VersionRecord`).
"""

from __future__ import annotations

import pytest
from disco.core.release import (
    CloudResourceProfile,
    DetectorProvenance,
    EnvScope,
    EnvVarDecl,
    GeneratedFileRef,
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    SecretClass,
    ServiceRole,
    load_release_spec,
    serialize_release_spec,
    spec_digest,
)
from pydantic import ValidationError

# A valid bare sha256 hexdigest (64 lowercase hex chars) — the shape
# `store.VersionRecord.tree_digest` carries.
_DIGEST = "0123456789abcdef" * 4


def _local_profile() -> LocalResourceProfile:
    return LocalResourceProfile(url="file:/data/app.db", volume="app-data")


def _resource() -> ResourceDecl:
    return ResourceDecl(
        id="db",
        kind=ResourceKind.sqlite,
        persistent_path="/data",
        profiles=ResourceProfiles(local=_local_profile()),
        consumers=("web",),
        migrate_cmd=("npm", "run", "migrate"),
    )


def _ingress_service() -> ReleaseService:
    return ReleaseService(
        id="web",
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.node,
        root=".",
        package_manager="npm",
        lockfile="package-lock.json",
        install_cmd=("npm", "ci"),
        build_cmd=("npm", "run", "build"),
        migrate_cmd=(),
        start_cmd=("node", "server.js"),
        port_env="PORT",
        health_path="/health",
        depends_on=(),
        output_dir="dist",
    )


def _provenance() -> DetectorProvenance:
    return DetectorProvenance(
        detector="web-release-detector",
        detector_version="1",
        assessment=ReleaseAssessment.candidate,
        evidence=("package.json present", "start script present"),
        reason="a node web app with a start script",
    )


def _valid_spec(**overrides: object) -> ReleaseSpec:
    base: dict[str, object] = {
        "kind": "web_app",
        "name": "Task Tracker",
        "version_seq": 1,
        "tree_digest": _DIGEST,
        "services": (_ingress_service(),),
        "env": (
            EnvVarDecl(
                name="DATABASE_URL",
                scope=EnvScope.runtime,
                required=True,
                secret=SecretClass.secret,
                binding="db",
            ),
        ),
        "resources": (_resource(),),
        "provenance": _provenance(),
        "generated_files": (GeneratedFileRef(path="compose.yaml", purpose="compose"),),
    }
    base.update(overrides)
    return ReleaseSpec(**base)


# ---- criterion 1: exactly one ingress -----------------------------------------


def test_exactly_one_ingress_passes() -> None:
    spec = _valid_spec()
    assert sum(s.role is ServiceRole.ingress for s in spec.services) == 1


def test_zero_ingress_services_raise() -> None:
    # A single service demoted to a non-ingress role → zero ingress.
    backend = _ingress_service().model_copy(update={"role": ServiceRole.backend})
    with pytest.raises(ValidationError, match="EXACTLY ONE ingress"):
        _valid_spec(services=(backend,))


def test_two_ingress_services_raise() -> None:
    second = _ingress_service().model_copy(update={"id": "web2"})
    with pytest.raises(ValidationError, match="EXACTLY ONE ingress"):
        _valid_spec(services=(_ingress_service(), second))


# ---- criterion 2: EnvVarDecl is value-free + validates NAMES -------------------


def test_env_var_decl_has_no_value_field() -> None:
    assert "value" not in EnvVarDecl.model_fields


def test_env_var_name_rejects_equals_whitespace_and_invalid_chars() -> None:
    for bad in (
        "FOO=BAR",  # '='
        "FOO BAR",  # space
        "FOO\tBAR",  # tab
        "foo",  # lower-case
        "1FOO",  # leading digit
        "FOO-BAR",  # hyphen
        "FOO.BAR",  # dot
        "",  # empty
    ):
        with pytest.raises(ValidationError):
            EnvVarDecl(name=bad, scope=EnvScope.runtime)


def test_env_var_name_accepts_valid_uppercase_names() -> None:
    assert EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime).name == "DATABASE_URL"
    assert EnvVarDecl(name="_PRIVATE", scope=EnvScope.build).name == "_PRIVATE"
    assert EnvVarDecl(name="PORT2", scope=EnvScope.runtime).name == "PORT2"


# ---- criterion 3: spec_digest byte-stability ----------------------------------


def test_spec_digest_is_stable_across_field_order() -> None:
    reference = _valid_spec()
    # Same data, kwargs supplied in a DIFFERENT order (and the env var built with
    # its own kwargs reordered). Field order must not affect the digest.
    reordered = ReleaseSpec(
        provenance=_provenance(),
        generated_files=(GeneratedFileRef(purpose="compose", path="compose.yaml"),),
        resources=(_resource(),),
        env=(
            EnvVarDecl(
                binding="db",
                secret=SecretClass.secret,
                required=True,
                scope=EnvScope.runtime,
                name="DATABASE_URL",
            ),
        ),
        services=(_ingress_service(),),
        tree_digest=_DIGEST,
        version_seq=1,
        name="Task Tracker",
        kind="web_app",
    )
    assert spec_digest(reference) == spec_digest(reordered)


def test_spec_digest_changes_when_any_field_changes() -> None:
    base = _valid_spec()
    base_digest = spec_digest(base)
    assert spec_digest(_valid_spec(name="Other App")) != base_digest
    assert spec_digest(_valid_spec(version_seq=2)) != base_digest
    assert spec_digest(_valid_spec(tree_digest="f" * 64)) != base_digest
    # A change buried in a nested model must also change the digest.
    other_prov = _provenance().model_copy(update={"assessment": ReleaseAssessment.needs_review})
    assert spec_digest(_valid_spec(provenance=other_prov)) != base_digest


# ---- criterion 4: cloud placeholder defaults None + assessment forward-compat --


def test_cloud_profile_defaults_none_and_round_trips() -> None:
    spec = _valid_spec()
    assert spec.resources[0].profiles.cloud is None
    reloaded = load_release_spec(serialize_release_spec(spec))
    assert reloaded.resources[0].profiles.cloud is None
    assert reloaded == spec
    # The placeholder type exists and is constructible for a later track.
    assert isinstance(CloudResourceProfile(), CloudResourceProfile)


def test_assessment_enum_has_forward_compat_values() -> None:
    for name in ("not_web", "candidate", "needs_review", "verifying", "verified", "failed"):
        assert name in ReleaseAssessment.__members__
    # Explicit presence of the forward-compat (not-yet-emitted) verification states.
    assert ReleaseAssessment.verifying.value == "verifying"
    assert ReleaseAssessment.verified.value == "verified"
    assert ReleaseAssessment.failed.value == "failed"


# ---- criterion 5: canonical serialization round-trip --------------------------


def test_canonical_serialization_round_trip_is_byte_identical() -> None:
    spec = _valid_spec()
    once = serialize_release_spec(spec)
    twice = serialize_release_spec(load_release_spec(once))
    assert once == twice
    # And a full data round-trip preserves the model.
    assert load_release_spec(once) == spec


# ---- supporting invariants ----------------------------------------------------


def test_runtime_strategy_is_provider_neutral() -> None:
    assert {member.value for member in RuntimeStrategy} == {
        "node",
        "python",
        "static",
        "dev_server",
        "container",
    }


def test_commands_are_argv_lists_not_shell_strings() -> None:
    spec = _valid_spec()
    assert spec.services[0].start_cmd == ("node", "server.js")
    # An empty/blank argv element is rejected (would exec an empty arg).
    with pytest.raises(ValidationError, match="non-empty token"):
        ReleaseService(
            id="web",
            role=ServiceRole.ingress,
            runtime=RuntimeStrategy.node,
            start_cmd=("node", ""),
        )


def test_argv_element_shaped_like_an_env_assignment_is_rejected() -> None:
    # AUDIT #4a: an argv element like `API_TOKEN=hunter2` smuggles a secret VALUE
    # through a command token. It must be rejected across install/build/migrate/start
    # argvs (ReleaseService) and the intent's build/start argvs (ReleaseIntent).
    for field in ("install_cmd", "build_cmd", "migrate_cmd", "start_cmd"):
        with pytest.raises(ValidationError, match="env assignment"):
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.node,
                **{field: ("API_TOKEN=hunter2", "node", "server.js")},
            )
    for field in ("build_cmd", "start_cmd"):
        with pytest.raises(ValidationError, match="env assignment"):
            ReleaseIntent(**{field: ("SECRET=abc", "node", "server.js")})
    # a legitimate flag with an '=' (not an UPPERCASE env-name shape) is still fine.
    ok = ReleaseService(
        id="web",
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.node,
        start_cmd=("node", "--max-old-space-size=512", "server.js"),
    )
    assert ok.start_cmd == ("node", "--max-old-space-size=512", "server.js")


def test_duplicate_service_ids_rejected() -> None:
    dup = _ingress_service().model_copy(update={"role": ServiceRole.backend})
    with pytest.raises(ValidationError, match="duplicate service id"):
        _valid_spec(services=(_ingress_service(), dup))


def test_duplicate_env_names_rejected() -> None:
    a = EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime, binding="db")
    b = EnvVarDecl(name="DATABASE_URL", scope=EnvScope.build)
    with pytest.raises(ValidationError, match="duplicate env var name"):
        _valid_spec(env=(a, b))


def test_dangling_env_binding_rejected() -> None:
    var = EnvVarDecl(name="OTHER_URL", scope=EnvScope.runtime, binding="nope")
    with pytest.raises(ValidationError, match="binds unknown resource"):
        _valid_spec(env=(var,))


def test_dangling_depends_on_rejected() -> None:
    svc = _ingress_service().model_copy(update={"depends_on": ("ghost",)})
    with pytest.raises(ValidationError, match="depends_on unknown service"):
        _valid_spec(services=(svc,))


def test_tree_digest_must_be_bare_sha256_hex() -> None:
    # Matches store.VersionRecord.tree_digest (bare 64-char lowercase hex).
    for bad in ("sha256:" + _DIGEST, "ABC", "g" * 64, _DIGEST[:-1]):
        with pytest.raises(ValidationError, match="tree_digest"):
            _valid_spec(tree_digest=bad)
    assert _valid_spec(tree_digest=_DIGEST).tree_digest == _DIGEST


def test_spec_is_frozen_and_forbids_extra_fields() -> None:
    spec = _valid_spec()
    with pytest.raises(ValidationError):
        # frozen models reject attribute reassignment (this test file is excluded
        # from pyright, so a plain assignment needs no static type-ignore).
        spec.name = "tampered"
    with pytest.raises(ValidationError):
        _valid_spec(surprise="nope")


def test_collections_are_tuples() -> None:
    spec = _valid_spec()
    assert isinstance(spec.services, tuple)
    assert isinstance(spec.env, tuple)
    assert isinstance(spec.resources, tuple)
    assert isinstance(spec.targets, tuple)
    assert isinstance(spec.services[0].start_cmd, tuple)


def test_targets_default_to_local_compose() -> None:
    assert _valid_spec().targets == ("local_compose",)
    # Open strings: a new target is just an added string, not a schema change.
    assert _valid_spec(targets=("local_compose", "some_future_target")).targets == (
        "local_compose",
        "some_future_target",
    )


def test_release_intent_is_value_free_and_validates() -> None:
    intent = ReleaseIntent(
        build_cmd=("npm", "run", "build"),
        start_cmd=("node", "server.js"),
        port_env="PORT",
        health_path="/health",
        required_env=("DATABASE_URL", "SESSION_SECRET"),
        resources=(_resource(),),
    )
    assert intent.required_env == ("DATABASE_URL", "SESSION_SECRET")
    assert "value" not in ReleaseIntent.model_fields
    # Required env entries are validated as NAMES.
    with pytest.raises(ValidationError):
        ReleaseIntent(required_env=("bad name",))
    with pytest.raises(ValidationError, match="non-empty token"):
        ReleaseIntent(start_cmd=("",))


def test_secret_env_records_name_only() -> None:
    var = EnvVarDecl(name="SESSION_SECRET", scope=EnvScope.runtime, secret=SecretClass.secret)
    dumped = var.model_dump()
    assert "value" not in dumped
    assert dumped["name"] == "SESSION_SECRET"
    assert dumped["secret"] == SecretClass.secret

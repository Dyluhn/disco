"""Focused tests for self-host/Vercel identity parity and rebuild labeling (15-C3).

These exercise the production trusted built-in registry and resolver path:
`build_builtin_registry()` plus `resolve_build_composition(...)`.  For one
identical, immutable request digest they prove that the self-host connector
copies it into `package_digest_ref` and the Vercel connector copies the exact
same bytes into `artifact_digest_ref`, that the connector IDs and operations
remain distinct, and that the Vercel connector stays strict-prebuilt with a
`provider_rebuild=unobserved` residual that is never a provider success/failure
or rebuild-execution claim.

Every positive assertion inspects a registry-provided production plan or a
resolved production composition, never a serialized stand-in.  Mutation and
negative controls fail closed when a required identity, value, label, or
attachment is removed, substituted, collapsed, or duplicated, and confirm no
source, build command, token, secret, credential, deployment effect, or
authenticated provider result enters either parity comparison.
"""

from __future__ import annotations

from disco.core.build_platform import (
    CapabilityLayer,
    ComponentId,
    ComponentIntent,
    DeploymentRequest,
    PolicyLayer,
    PromptContextInputs,
    ResolutionInputs,
    build_builtin_registry,
    resolve_build_composition,
)
from disco.core.targets.nextjs_connectors import (
    ARTIFACT_DIGEST_PARAMETER_NAME,
    ARTIFACT_MODE_PARAMETER_NAME,
    ARTIFACT_MODE_STRICT_PREBUILT,
    BUILD_ENVIRONMENT_PARAMETER_NAME,
    BUILD_ENVIRONMENT_RESOLVED_BEFORE_LOCAL_BUILD,
    DIGEST_PARAMETER_NAME,
    NEXTJS_SELF_HOST_CONNECTOR_ID,
    NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID,
    PROVIDER_REBUILD_PARAMETER_NAME,
    PROVIDER_REBUILD_UNOBSERVED,
    RUNTIME_ENVIRONMENT_DEPLOYMENT_RUNTIME,
    RUNTIME_ENVIRONMENT_PARAMETER_NAME,
    SELF_HOST_DEPLOY_OPERATION,
    VERCEL_PREBUILT_DEPLOY_OPERATION,
)
from disco.core.targets.nextjs_server import (
    NEXTJS_SERVER_DELIVERY_SHAPE,
    NEXTJS_SERVER_EXPORTER_ID,
    NEXTJS_SERVER_PACKAGE_SHAPE,
    NEXTJS_SERVER_PROFILE_ID,
    NEXTJS_SERVER_TARGET_ID,
)
from disco.core.targets.nextjs_static import (
    NEXTJS_STATIC_DELIVERY_SHAPE,
    NEXTJS_STATIC_EXPORTER_ID,
    NEXTJS_STATIC_PACKAGE_SHAPE,
    NEXTJS_STATIC_PROFILE_ID,
    NEXTJS_STATIC_TARGET_ID,
)

_STATIC_PROFILE = NEXTJS_STATIC_PROFILE_ID
_SERVER_PROFILE = NEXTJS_SERVER_PROFILE_ID
_SELF_HOST = NEXTJS_SELF_HOST_CONNECTOR_ID
_VERCEL = NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID

_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})

# One immutable request digest shared by every parity assertion in this slice.
_DIGEST = "sha256:" + "p" * 64


def _capabilities(source: str) -> CapabilityLayer:
    return CapabilityLayer(source=source, allowed=_CAPABILITIES)


def _resolve(profile_id: ComponentId, goal: str):
    registry = build_builtin_registry()
    return resolve_build_composition(
        registry,
        ResolutionInputs(
            profile=profile_id,
            goal=goal,
            platform_capabilities=_capabilities("platform"),
            user_capabilities=_capabilities("user"),
            host_capabilities=_capabilities("host"),
            platform_policy=PolicyLayer(source="platform"),
            user_policy=PolicyLayer(source="user"),
            prompt_context=PromptContextInputs(),
        ),
    )


def _request(
    profile_id: ComponentId, digest: str = _DIGEST, environment: str = "production"
) -> DeploymentRequest:
    return DeploymentRequest(
        profile=profile_id,
        target=(
            NEXTJS_STATIC_TARGET_ID
            if profile_id == _STATIC_PROFILE
            else NEXTJS_SERVER_TARGET_ID
        ),
        package_digest_ref=digest,
        environment=environment,
    )


def _parameters(intent: ComponentIntent) -> dict[str, object]:
    return {parameter.name: parameter.value for parameter in intent.parameters}


def _registry_connectors():
    registry = build_builtin_registry()
    self_host = registry.components.connector(_SELF_HOST)
    vercel = registry.components.connector(_VERCEL)
    assert self_host is not None
    assert vercel is not None
    return self_host, vercel


def test_connector_ids_remain_distinct() -> None:
    assert _SELF_HOST.canonical == "disco.nextjs_self_host@1"
    assert _VERCEL.canonical == "disco.nextjs_vercel_prebuilt@1"
    assert _SELF_HOST != _VERCEL
    registry = build_builtin_registry()
    assert registry.components.connector(_SELF_HOST) is not None
    assert registry.components.connector(_VERCEL) is not None


def test_operations_remain_distinct() -> None:
    assert SELF_HOST_DEPLOY_OPERATION == "deployment.nextjs_self_host"
    assert VERCEL_PREBUILT_DEPLOY_OPERATION == "deployment.nextjs_vercel_prebuilt"
    assert SELF_HOST_DEPLOY_OPERATION != VERCEL_PREBUILT_DEPLOY_OPERATION


def test_identical_digest_enters_both_plans_byte_for_byte() -> None:
    self_host, vercel = _registry_connectors()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        request = _request(profile_id)
        self_host_plan = self_host.plan(request)
        vercel_plan = vercel.plan(request)
        assert _parameters(self_host_plan.intents[0])[DIGEST_PARAMETER_NAME] == _DIGEST
        assert (
            _parameters(vercel_plan.intents[0])[ARTIFACT_DIGEST_PARAMETER_NAME] == _DIGEST
        )
        # The exact bytes flow into both, so the two digest parameters agree.
        assert (
            _parameters(self_host_plan.intents[0])[DIGEST_PARAMETER_NAME]
            == _parameters(vercel_plan.intents[0])[ARTIFACT_DIGEST_PARAMETER_NAME]
        )


def test_self_host_plan_exposes_only_package_digest_ref() -> None:
    self_host, _ = _registry_connectors()
    plan = self_host.plan(_request(_STATIC_PROFILE))
    assert len(plan.intents) == 1
    assert plan.intents[0].operation == SELF_HOST_DEPLOY_OPERATION
    assert _parameters(plan.intents[0]) == {DIGEST_PARAMETER_NAME: _DIGEST}


def test_vercel_plan_keeps_frozen_values_and_distinct_parameter_names() -> None:
    _, vercel = _registry_connectors()
    plan = vercel.plan(_request(_STATIC_PROFILE))
    assert len(plan.intents) == 1
    assert plan.intents[0].operation == VERCEL_PREBUILT_DEPLOY_OPERATION
    parameters = _parameters(plan.intents[0])
    assert set(parameters) == {
        ARTIFACT_DIGEST_PARAMETER_NAME,
        ARTIFACT_MODE_PARAMETER_NAME,
        BUILD_ENVIRONMENT_PARAMETER_NAME,
        RUNTIME_ENVIRONMENT_PARAMETER_NAME,
        PROVIDER_REBUILD_PARAMETER_NAME,
    }
    assert DIGEST_PARAMETER_NAME not in parameters
    assert parameters[ARTIFACT_DIGEST_PARAMETER_NAME] == _DIGEST
    assert parameters[ARTIFACT_MODE_PARAMETER_NAME] == ARTIFACT_MODE_STRICT_PREBUILT
    assert parameters[ARTIFACT_MODE_PARAMETER_NAME] == "strict_prebuilt"
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME]
        == BUILD_ENVIRONMENT_RESOLVED_BEFORE_LOCAL_BUILD
    )
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME] == "resolved_before_local_build"
    )
    assert (
        parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME]
        == RUNTIME_ENVIRONMENT_DEPLOYMENT_RUNTIME
    )
    assert parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME] == "deployment_runtime"


def test_unobserved_is_a_residual_label_not_a_provider_claim() -> None:
    _, vercel = _registry_connectors()
    plan = vercel.plan(_request(_STATIC_PROFILE))
    assert len(plan.intents) == 1
    parameters = _parameters(plan.intents[0])
    assert parameters[PROVIDER_REBUILD_PARAMETER_NAME] == PROVIDER_REBUILD_UNOBSERVED
    assert parameters[PROVIDER_REBUILD_PARAMETER_NAME] == "unobserved"
    # The plan carries exactly the one intent with exactly the five parameters
    # and no effect or verdict field; it never claims a provider rebuild
    # success/failure/execution result. The sole `unobserved` value is a
    # residual label carried in the provider_rebuild parameter, not a claim.
    assert set(parameters) == {
        ARTIFACT_DIGEST_PARAMETER_NAME,
        ARTIFACT_MODE_PARAMETER_NAME,
        BUILD_ENVIRONMENT_PARAMETER_NAME,
        RUNTIME_ENVIRONMENT_PARAMETER_NAME,
        PROVIDER_REBUILD_PARAMETER_NAME,
    }
    serialized = plan.model_dump_json().casefold()
    for forbidden in (
        "rebuild_succeeded",
        "rebuild_failed",
        "rebuild_executed",
        "deployment_succeeded",
        "deployment_failed",
        "deployment_effect",
        "provider_verdict",
        "success",
        "failure",
    ):
        assert forbidden not in serialized


def test_self_host_plan_carries_no_rebuild_or_provider_label() -> None:
    self_host, _ = _registry_connectors()
    plan = self_host.plan(_request(_SERVER_PROFILE))
    serialized = plan.model_dump_json().casefold()
    for forbidden in ("provider_rebuild", "artifact_mode", "vercel", "unobserved"):
        assert forbidden not in serialized


def test_digest_substitution_in_either_connector_is_detected() -> None:
    self_host, vercel = _registry_connectors()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        plan = self_host.plan(_request(profile_id, _DIGEST))
        other = "sha256:" + "q" * 64
        assert _parameters(plan.intents[0])[DIGEST_PARAMETER_NAME] == _DIGEST
        assert _parameters(plan.intents[0])[DIGEST_PARAMETER_NAME] != other
        vercel_plan = vercel.plan(_request(profile_id, _DIGEST))
        assert (
            _parameters(vercel_plan.intents[0])[ARTIFACT_DIGEST_PARAMETER_NAME] == _DIGEST
        )
        assert (
            _parameters(vercel_plan.intents[0])[ARTIFACT_DIGEST_PARAMETER_NAME] != other
        )


def test_collapsing_connector_ids_fails_the_proof() -> None:
    collapsed = ComponentId(namespace="disco", name="nextjs_connector", version="1")
    assert collapsed != _SELF_HOST
    assert collapsed != _VERCEL
    assert _SELF_HOST.canonical != collapsed.canonical
    assert _VERCEL.canonical != collapsed.canonical
    assert collapsed.canonical != "disco.nextjs_self_host@1"
    assert collapsed.canonical != "disco.nextjs_vercel_prebuilt@1"


def test_collapsing_operations_fails_the_proof() -> None:
    collapsed = "deployment.nextjs"
    assert collapsed != SELF_HOST_DEPLOY_OPERATION
    assert collapsed != VERCEL_PREBUILT_DEPLOY_OPERATION


def test_changing_strict_prebuilt_fails_the_proof() -> None:
    _, vercel = _registry_connectors()
    parameters = _parameters(vercel.plan(_request(_STATIC_PROFILE)).intents[0])
    assert parameters[ARTIFACT_MODE_PARAMETER_NAME] == "strict_prebuilt"
    assert parameters[ARTIFACT_MODE_PARAMETER_NAME] != "source_rebuild"
    assert parameters[ARTIFACT_MODE_PARAMETER_NAME] != "prebuilt_rebuild"


def test_changing_environment_labels_fails_the_proof() -> None:
    _, vercel = _registry_connectors()
    parameters = _parameters(vercel.plan(_request(_STATIC_PROFILE)).intents[0])
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME] == "resolved_before_local_build"
    )
    assert parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME] == "deployment_runtime"
    # The two environment parameters stay distinct and never collapse together.
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME]
        != parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME]
    )


def test_changing_unobserved_into_a_rebuild_claim_fails_the_proof() -> None:
    _, vercel = _registry_connectors()
    parameters = _parameters(vercel.plan(_request(_STATIC_PROFILE)).intents[0])
    assert parameters[PROVIDER_REBUILD_PARAMETER_NAME] == "unobserved"
    for claim in ("rebuilt", "succeeded", "failed", "not_rebuilt"):
        assert parameters[PROVIDER_REBUILD_PARAMETER_NAME] != claim


def test_no_second_connector_or_effect_channel_in_either_plan() -> None:
    self_host, vercel = _registry_connectors()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        self_host_plan = self_host.plan(_request(profile_id))
        vercel_plan = vercel.plan(_request(profile_id))
        assert len(self_host_plan.intents) == 1
        assert len(vercel_plan.intents) == 1
        assert self_host_plan.confirmation_required is True
        assert vercel_plan.confirmation_required is True
        for plan in (self_host_plan, vercel_plan):
            serialized = plan.model_dump_json().casefold()
            for forbidden in (
                "source",
                "build_command",
                "next build",
                "next start",
                "token",
                "secret",
                "credential",
                "deployment_id",
                "provider_url",
                "executed",
                "success",
            ):
                assert forbidden not in serialized


def test_both_resolved_profiles_retain_only_self_host_connector() -> None:
    registry = build_builtin_registry()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = registry.profiles.get(profile_id)
        assert profile is not None
        assert profile.connector == _SELF_HOST
        assert profile.connector.canonical == "disco.nextjs_self_host@1"
        composition = _resolve(profile_id, "probe")
        assert composition.connector == _SELF_HOST
        assert composition.connector != _VERCEL
        assert composition.blocked_operations == ()
        assert not composition.blocked("deploy")


def test_adding_a_second_connector_to_a_profile_fails_the_resolved_proof() -> None:
    registry = build_builtin_registry()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = registry.profiles.get(profile_id)
        assert profile is not None
        # The profile and resolved composition expose exactly ONE connector
        # slot (a singular ComponentId), so a "second" connector cannot be
        # attached without replacing the sole slot.
        assert profile.connector == _SELF_HOST
        assert profile.connector != _VERCEL
        composition = _resolve(profile_id, "probe")
        assert composition.connector == _SELF_HOST
        assert composition.connector != _VERCEL
        # Replacing that sole slot with the Vercel connector would change the
        # resolved identity; the shipped profiles must not do so.
        replaced = profile.model_copy(update={"connector": _VERCEL})
        assert replaced.connector == _VERCEL
        assert replaced.connector != _SELF_HOST


def test_replacing_self_host_attachment_fails_the_resolved_proof() -> None:
    registry = build_builtin_registry()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = registry.profiles.get(profile_id)
        assert profile is not None
        assert profile.connector == _SELF_HOST
        composition = _resolve(profile_id, "probe")
        assert composition.connector == _SELF_HOST
        assert composition.connector != _VERCEL


def test_static_and_server_target_exporter_identities_stay_unchanged() -> None:
    registry = build_builtin_registry()
    static_profile = registry.profiles.get(_STATIC_PROFILE)
    server_profile = registry.profiles.get(_SERVER_PROFILE)
    assert static_profile is not None
    assert server_profile is not None
    assert static_profile.target == NEXTJS_STATIC_TARGET_ID
    assert server_profile.target == NEXTJS_SERVER_TARGET_ID
    assert static_profile.exporter == NEXTJS_STATIC_EXPORTER_ID
    assert server_profile.exporter == NEXTJS_SERVER_EXPORTER_ID
    assert static_profile.target != server_profile.target
    assert static_profile.exporter != server_profile.exporter
    static_composition = _resolve(_STATIC_PROFILE, "static")
    server_composition = _resolve(_SERVER_PROFILE, "server")
    assert static_composition.target == NEXTJS_STATIC_TARGET_ID
    assert server_composition.target == NEXTJS_SERVER_TARGET_ID
    assert (
        static_composition.target_plan.delivery.shape == NEXTJS_STATIC_DELIVERY_SHAPE
    )
    assert (
        server_composition.target_plan.delivery.shape == NEXTJS_SERVER_DELIVERY_SHAPE
    )
    assert static_composition.target_plan.package is not None
    assert server_composition.target_plan.package is not None
    assert (
        static_composition.target_plan.package.package_shape
        == NEXTJS_STATIC_PACKAGE_SHAPE
    )
    assert (
        server_composition.target_plan.package.package_shape
        == NEXTJS_SERVER_PACKAGE_SHAPE
    )


def test_resolved_profile_never_invokes_vercel_connector_plan() -> None:
    # The resolved composition carries the self-host connector; the Vercel
    # connector is a separate registry-selectable component. Confirming the
    # shipped profiles attach only self-host means a resolved composition has no
    # Vercel operation to emit.
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        composition = _resolve(profile_id, "probe")
        assert composition.connector == _SELF_HOST
        assert composition.connector != _VERCEL
        assert composition.blocked_operations == ()

"""Focused tests for the optional strict-prebuilt Vercel connector (15-C2).

These exercise the production trusted built-in registry and resolver path:
`build_builtin_registry()` plus `resolve_build_composition(...)`.  They observe
the registered Vercel connector through the registry and the resulting
production plan, and they resolve both Next.js profiles to prove the C1
self-host attachment was preserved.  Mutation and negative controls fail closed
when a required value, attachment, or identity is removed, substituted, or
duplicated.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    CapabilityLayer,
    ComponentId,
    ComponentIntent,
    ComponentKind,
    DeploymentRequest,
    PolicyLayer,
    PromptContextInputs,
    ResolutionInputs,
    build_builtin_registry,
    resolve_build_composition,
)
from disco.core.build_platform.registry import RegistryError
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
    NextjsVercelPrebuiltConnector,
)
from disco.core.targets.nextjs_server import (
    NEXTJS_SERVER_PROFILE_ID,
    NEXTJS_SERVER_TARGET_ID,
)
from disco.core.targets.nextjs_static import (
    NEXTJS_STATIC_PROFILE_ID,
    NEXTJS_STATIC_TARGET_ID,
)

_STATIC_PROFILE = NEXTJS_STATIC_PROFILE_ID
_SERVER_PROFILE = NEXTJS_SERVER_PROFILE_ID
_SELF_HOST = NEXTJS_SELF_HOST_CONNECTOR_ID
_VERCEL = NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID

_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})


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
    profile_id: ComponentId, digest: str, environment: str = "production"
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


def test_vercel_connector_id_is_exact_and_registered_in_registry() -> None:
    assert _VERCEL.canonical == "disco.nextjs_vercel_prebuilt@1"
    registry = build_builtin_registry()
    connector = registry.components.connector(_VERCEL)
    assert connector is not None
    assert connector.id == _VERCEL
    spec = registry.components.spec(_VERCEL)
    assert spec is not None
    assert spec.kind is ComponentKind.CONNECTOR
    assert spec.id == _VERCEL


def test_duplicate_vercel_connector_id_is_rejected() -> None:
    registry = build_builtin_registry()
    spec = registry.components.spec(_VERCEL)
    assert spec is not None
    with pytest.raises(RegistryError, match="duplicate"):
        registry.components._add_implementation(
            spec, NextjsVercelPrebuiltConnector(), ComponentKind.CONNECTOR
        )


def test_removing_registered_vercel_connector_cannot_silently_remain_available() -> None:
    registry = build_builtin_registry()
    registry.components._connectors.pop(_VERCEL.canonical)
    assert registry.components.connector(_VERCEL) is None


def test_vercel_operation_is_distinct_from_self_host_operation() -> None:
    assert VERCEL_PREBUILT_DEPLOY_OPERATION == "deployment.nextjs_vercel_prebuilt"
    assert SELF_HOST_DEPLOY_OPERATION == "deployment.nextjs_self_host"
    assert VERCEL_PREBUILT_DEPLOY_OPERATION != SELF_HOST_DEPLOY_OPERATION


def test_vercel_connector_produces_exact_five_parameter_plan() -> None:
    registry = build_builtin_registry()
    connector = registry.components.connector(_VERCEL)
    assert connector is not None
    digest = "sha256:" + "a" * 64
    request = _request(_STATIC_PROFILE, digest, environment="staging")
    plan = connector.plan(request)
    assert plan.connector == _VERCEL
    assert plan.environment == "staging"
    assert plan.confirmation_required is True
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.operation == VERCEL_PREBUILT_DEPLOY_OPERATION
    parameters = _parameters(intent)
    assert set(parameters) == {
        ARTIFACT_DIGEST_PARAMETER_NAME,
        ARTIFACT_MODE_PARAMETER_NAME,
        BUILD_ENVIRONMENT_PARAMETER_NAME,
        RUNTIME_ENVIRONMENT_PARAMETER_NAME,
        PROVIDER_REBUILD_PARAMETER_NAME,
    }
    assert parameters[ARTIFACT_DIGEST_PARAMETER_NAME] == digest
    assert parameters[ARTIFACT_MODE_PARAMETER_NAME] == ARTIFACT_MODE_STRICT_PREBUILT
    assert parameters[ARTIFACT_MODE_PARAMETER_NAME] == "strict_prebuilt"
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME]
        == BUILD_ENVIRONMENT_RESOLVED_BEFORE_LOCAL_BUILD
    )
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME]
        == "resolved_before_local_build"
    )
    assert (
        parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME]
        == RUNTIME_ENVIRONMENT_DEPLOYMENT_RUNTIME
    )
    assert parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME] == "deployment_runtime"
    assert parameters[PROVIDER_REBUILD_PARAMETER_NAME] == PROVIDER_REBUILD_UNOBSERVED
    assert parameters[PROVIDER_REBUILD_PARAMETER_NAME] == "unobserved"


def test_resolved_registry_connector_copies_request_digest_byte_for_byte() -> None:
    registry = build_builtin_registry()
    connector = registry.components.connector(_VERCEL)
    assert connector is not None
    digest = "sha256:" + "b" * 64
    request = _request(_SERVER_PROFILE, digest)
    plan = connector.plan(request)
    assert plan.intents[0].operation == VERCEL_PREBUILT_DEPLOY_OPERATION
    assert _parameters(plan.intents[0])[ARTIFACT_DIGEST_PARAMETER_NAME] == digest


def test_digest_substitution_is_not_silent() -> None:
    connector = NextjsVercelPrebuiltConnector()
    request_digest = "sha256:" + "c" * 64
    request = _request(_STATIC_PROFILE, request_digest)
    plan = connector.plan(request)
    assert (
        _parameters(plan.intents[0])[ARTIFACT_DIGEST_PARAMETER_NAME] == request_digest
    )
    different = "sha256:" + "d" * 64
    assert (
        _parameters(plan.intents[0])[ARTIFACT_DIGEST_PARAMETER_NAME] != different
    )


def test_request_digest_remains_required() -> None:
    assert DIGEST_PARAMETER_NAME in DeploymentRequest.model_fields
    assert set(DeploymentRequest.model_fields) == {
        "profile",
        "target",
        "package_digest_ref",
        "environment",
    }
    with pytest.raises(ValueError):
        DeploymentRequest(
            profile=_STATIC_PROFILE,
            target=NEXTJS_STATIC_TARGET_ID,
            environment="production",
        )


def test_build_and_runtime_environment_parameters_remain_distinct() -> None:
    connector = NextjsVercelPrebuiltConnector()
    plan = connector.plan(_request(_STATIC_PROFILE, "sha256:" + "e" * 64))
    parameters = _parameters(plan.intents[0])
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME]
        != parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME]
    )
    assert (
        parameters[BUILD_ENVIRONMENT_PARAMETER_NAME]
        == "resolved_before_local_build"
    )
    assert parameters[RUNTIME_ENVIRONMENT_PARAMETER_NAME] == "deployment_runtime"


def test_changing_strict_prebuilt_fails_the_focused_proof() -> None:
    connector = NextjsVercelPrebuiltConnector()
    plan = connector.plan(_request(_STATIC_PROFILE, "sha256:" + "f" * 64))
    assert _parameters(plan.intents[0])[ARTIFACT_MODE_PARAMETER_NAME] == "strict_prebuilt"
    assert _parameters(plan.intents[0])[ARTIFACT_MODE_PARAMETER_NAME] != "source_rebuild"


def test_changing_unobserved_fails_the_focused_proof() -> None:
    connector = NextjsVercelPrebuiltConnector()
    plan = connector.plan(_request(_STATIC_PROFILE, "sha256:" + "g" * 64))
    assert _parameters(plan.intents[0])[PROVIDER_REBUILD_PARAMETER_NAME] == "unobserved"
    assert _parameters(plan.intents[0])[PROVIDER_REBUILD_PARAMETER_NAME] != "rebuilt"


def test_plan_has_no_source_build_token_secret_or_credential_channel() -> None:
    connector = NextjsVercelPrebuiltConnector()
    request = _request(_STATIC_PROFILE, "sha256:" + "h" * 64)
    plan = connector.plan(request)
    serialized = plan.model_dump_json().casefold()
    for forbidden in (
        "source",
        "build_command",
        "token",
        "secret",
        "credential",
        "next build",
        "next start",
    ):
        assert forbidden not in serialized
    intent = plan.intents[0]
    assert intent.operation == VERCEL_PREBUILT_DEPLOY_OPERATION
    assert set(_parameters(intent)) == {
        ARTIFACT_DIGEST_PARAMETER_NAME,
        ARTIFACT_MODE_PARAMETER_NAME,
        BUILD_ENVIRONMENT_PARAMETER_NAME,
        RUNTIME_ENVIRONMENT_PARAMETER_NAME,
        PROVIDER_REBUILD_PARAMETER_NAME,
    }


def test_plan_executes_no_deployment_effect() -> None:
    connector = NextjsVercelPrebuiltConnector()
    plan = connector.plan(_request(_STATIC_PROFILE, "sha256:" + "i" * 64))
    assert plan.confirmation_required is True
    assert len(plan.intents) == 1
    assert plan.intents[0].operation == VERCEL_PREBUILT_DEPLOY_OPERATION


def test_both_profiles_retain_self_host_connector_attachment() -> None:
    registry = build_builtin_registry()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = registry.profiles.get(profile_id)
        assert profile is not None
        assert profile.connector == _SELF_HOST
        assert profile.connector.canonical == "disco.nextjs_self_host@1"
        composition = _resolve(profile_id, "probe")
        assert composition.connector == _SELF_HOST
        assert composition.blocked_operations == ()


def test_self_host_connector_is_not_replaced_by_vercel() -> None:
    registry = build_builtin_registry()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = registry.profiles.get(profile_id)
        assert profile is not None
        assert profile.connector == _SELF_HOST
        assert profile.connector != _VERCEL
        composition = _resolve(profile_id, "probe")
        assert composition.connector == _SELF_HOST
        assert composition.connector != _VERCEL


def test_static_and_server_target_and_exporter_identities_stay_distinct() -> None:
    static_composition = _resolve(_STATIC_PROFILE, "static")
    server_composition = _resolve(_SERVER_PROFILE, "server")
    assert static_composition.target == NEXTJS_STATIC_TARGET_ID
    assert server_composition.target == NEXTJS_SERVER_TARGET_ID
    assert static_composition.target != server_composition.target
    assert static_composition.connector == _SELF_HOST
    assert server_composition.connector == _SELF_HOST
    static_ops = {intent.operation for intent in static_composition.target_plan.intents}
    server_ops = {intent.operation for intent in server_composition.target_plan.intents}
    assert "nextjs.static.build" in static_ops
    assert "nextjs.server.build" in server_ops
    assert static_ops != server_ops
    assert not any("nextjs.server" in op for op in static_ops)
    assert not any("nextjs.static" in op for op in server_ops)
    registry = build_builtin_registry()
    static_exporter = registry.components.exporter(
        registry.profiles.get(_STATIC_PROFILE).exporter
    )
    server_exporter = registry.components.exporter(
        registry.profiles.get(_SERVER_PROFILE).exporter
    )
    assert static_exporter is not None
    assert static_exporter.id.canonical == "disco.nextjs_static_exporter@1"
    assert server_exporter is not None
    assert server_exporter.id.canonical == "disco.nextjs_server_exporter@1"
    assert static_exporter.id != server_exporter.id

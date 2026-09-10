"""Focused tests for the Next.js first-class self-host connector (15-C1).

These exercise the production trusted built-in registry and resolver path:
`build_builtin_registry()` plus `resolve_build_composition(...)`.  They observe
the registered self-host connector through the registry and the resulting
production composition and resolved production plan, not serialized fixtures
or source strings.  Mutation and negative controls fail closed when a required
value or attachment is removed, substituted, or duplicated.
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
    DIGEST_PARAMETER_NAME,
    NEXTJS_SELF_HOST_CONNECTOR_ID,
    SELF_HOST_DEPLOY_OPERATION,
    NextjsSelfHostConnector,
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
_CONNECTOR = NEXTJS_SELF_HOST_CONNECTOR_ID

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


def test_connector_id_is_exact_and_registered_in_registry() -> None:
    assert _CONNECTOR.canonical == "disco.nextjs_self_host@1"
    registry = build_builtin_registry()
    connector = registry.components.connector(_CONNECTOR)
    assert connector is not None
    assert connector.id == _CONNECTOR
    spec = registry.components.spec(_CONNECTOR)
    assert spec is not None
    assert spec.kind is ComponentKind.CONNECTOR
    assert spec.id == _CONNECTOR


def test_both_profiles_attach_the_self_host_connector() -> None:
    registry = build_builtin_registry()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = registry.profiles.get(profile_id)
        assert profile is not None
        assert profile.connector == _CONNECTOR
        assert profile.connector.canonical == "disco.nextjs_self_host@1"
        composition = _resolve(profile_id, "probe")
        assert composition.connector == _CONNECTOR
        assert composition.blocked_operations == ()


def test_connector_produces_exact_self_host_plan_from_request_digest() -> None:
    connector = NextjsSelfHostConnector()
    digest = "sha256:" + "b" * 64
    request = _request(_STATIC_PROFILE, digest, environment="staging")
    plan = connector.plan(request)
    assert plan.connector == _CONNECTOR
    assert plan.environment == "staging"
    assert plan.confirmation_required is True
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.operation == SELF_HOST_DEPLOY_OPERATION
    assert intent.operation == "deployment.nextjs_self_host"
    parameters = _parameters(intent)
    assert len(parameters) == 1
    assert DIGEST_PARAMETER_NAME in parameters
    assert parameters[DIGEST_PARAMETER_NAME] == digest


def test_resolved_production_plan_copies_request_digest_byte_for_byte() -> None:
    registry = build_builtin_registry()
    connector = registry.components.connector(_CONNECTOR)
    assert connector is not None
    digest = "sha256:" + "c" * 64
    request = _request(_SERVER_PROFILE, digest)
    plan = connector.plan(request)
    assert plan.intents[0].operation == SELF_HOST_DEPLOY_OPERATION
    assert _parameters(plan.intents[0]) == {DIGEST_PARAMETER_NAME: digest}


def test_removing_registered_connector_exposes_blocked_deploy_operation() -> None:
    registry = build_builtin_registry()
    registry.components._connectors.pop(_CONNECTOR.canonical)
    composition = resolve_build_composition(
        registry,
        ResolutionInputs(
            profile=_STATIC_PROFILE,
            goal="probe",
            platform_capabilities=_capabilities("platform"),
            user_capabilities=_capabilities("user"),
            host_capabilities=_capabilities("host"),
            platform_policy=PolicyLayer(source="platform"),
            user_policy=PolicyLayer(source="user"),
            prompt_context=PromptContextInputs(),
        ),
    )
    assert composition.blocked("deploy")
    assert composition.blocked_operations != ()
    assert any(block.operation == "deploy" for block in composition.blocked_operations)


def test_duplicate_connector_id_is_rejected() -> None:
    registry = build_builtin_registry()
    spec = registry.components.spec(_CONNECTOR)
    assert spec is not None
    with pytest.raises(RegistryError, match="duplicate"):
        registry.components._add_implementation(
            spec, NextjsSelfHostConnector(), ComponentKind.CONNECTOR
        )


def test_replacing_connector_id_cannot_silently_fall_back() -> None:
    registry = build_builtin_registry()
    replacement = ComponentId(namespace="disco", name="nextjs_self_host", version="1")
    assert replacement == _CONNECTOR
    spec = registry.components.spec(_CONNECTOR)
    assert spec is not None
    with pytest.raises(RegistryError, match="duplicate"):
        registry.components._add_spec(spec)


def test_altered_digest_is_not_silently_substituted() -> None:
    connector = NextjsSelfHostConnector()
    request_digest = "sha256:" + "d" * 64
    request = _request(_STATIC_PROFILE, request_digest)
    plan = connector.plan(request)
    assert _parameters(plan.intents[0])[DIGEST_PARAMETER_NAME] == request_digest
    different = "sha256:" + "e" * 64
    assert _parameters(plan.intents[0])[DIGEST_PARAMETER_NAME] != different


def test_digest_parameter_is_required() -> None:
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


def test_plan_has_no_source_build_token_or_secret_channel() -> None:
    connector = NextjsSelfHostConnector()
    request = _request(_STATIC_PROFILE, "sha256:" + "f" * 64)
    plan = connector.plan(request)
    serialized = plan.model_dump_json().casefold()
    for forbidden in (
        "source",
        "build_command",
        "token",
        "secret",
        "credential",
        "provider_rebuild",
        "artifact_mode",
        "vercel",
    ):
        assert forbidden not in serialized
    intent = plan.intents[0]
    assert intent.operation == SELF_HOST_DEPLOY_OPERATION
    assert intent.operation == "deployment.nextjs_self_host"
    assert set(_parameters(intent)) == {DIGEST_PARAMETER_NAME}


def test_both_attachments_remain_self_host_specific() -> None:
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        composition = _resolve(profile_id, "probe")
        connector = build_builtin_registry().components.connector(composition.connector)
        assert connector is not None
        assert connector.id == _CONNECTOR
        assert composition.connector.canonical == "disco.nextjs_self_host@1"


def test_static_and_server_targets_stay_distinct_and_unchanged() -> None:
    static_composition = _resolve(_STATIC_PROFILE, "static")
    server_composition = _resolve(_SERVER_PROFILE, "server")
    assert static_composition.target == NEXTJS_STATIC_TARGET_ID
    assert server_composition.target == NEXTJS_SERVER_TARGET_ID
    assert static_composition.target != server_composition.target
    assert static_composition.connector == _CONNECTOR
    assert server_composition.connector == _CONNECTOR
    static_ops = {intent.operation for intent in static_composition.target_plan.intents}
    server_ops = {intent.operation for intent in server_composition.target_plan.intents}
    assert "nextjs.static.build" in static_ops
    assert "nextjs.server.build" in server_ops
    assert static_ops != server_ops
    assert not any("nextjs.server" in op for op in static_ops)
    assert not any("nextjs.static" in op for op in server_ops)


def test_prior_static_server_exporters_still_resolve_with_exact_values() -> None:
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
    static_plan = _resolve(_STATIC_PROFILE, "static").target_plan
    server_plan = _resolve(_SERVER_PROFILE, "server").target_plan
    assert static_plan.delivery.shape == "nextjs.static_delivery"
    assert server_plan.delivery.shape == "nextjs.server_delivery"
    assert static_plan.package is not None
    assert server_plan.package is not None
    assert static_plan.package.package_shape == "nextjs.static_package"
    assert server_plan.package.package_shape == "nextjs.server_package"


def test_no_unrelated_connector_behavior_in_static_or_server() -> None:
    static_plan = _resolve(_STATIC_PROFILE, "static").target_plan
    server_plan = _resolve(_SERVER_PROFILE, "server").target_plan
    for plan in (static_plan, server_plan):
        assert plan.deployments == ()
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = build_builtin_registry().profiles.get(profile_id)
        assert profile is not None
        serialized = profile.model_dump_json().casefold()
        assert "provider_rebuild" not in serialized
        assert "artifact_mode" not in serialized
        assert "vercel" not in serialized

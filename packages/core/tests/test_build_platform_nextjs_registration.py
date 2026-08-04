"""Focused registration tests for the Next.js static/server built-in profiles.

These exercise the production trusted built-in registry and resolver path:
`build_builtin_registry()` plus `resolve_build_composition(...)`.  They observe
the registered target/exporter through the registry and the resulting production
composition, not serialized fixtures or source text.  Mutation and negative
controls fail closed when a required value or attachment is removed or
substituted.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform import (
    FREEFORM_ENGINE_ID,
    FREEFORM_PROFILE_ID,
    CapabilityLayer,
    ComponentId,
    ComponentKind,
    PackageRequest,
    PolicyLayer,
    PromptContextInputs,
    ResolutionInputs,
    TargetRequest,
    build_builtin_registry,
    resolve_build_composition,
)
from disco.core.build_platform.registry import RegistryError
from disco.core.build_platform.resolver import ResolutionError
from disco.core.targets.nextjs_connectors import NEXTJS_SELF_HOST_CONNECTOR_ID
from disco.core.targets.nextjs_server import (
    BUILD_COMMAND as SERVER_BUILD_COMMAND,
)
from disco.core.targets.nextjs_server import (
    ENTRY_REFERENCE as SERVER_ENTRY_REFERENCE,
)
from disco.core.targets.nextjs_server import (
    NEXTJS_SERVER_DELIVERY_SHAPE,
    NEXTJS_SERVER_EXPORTER_ID,
    NEXTJS_SERVER_PACKAGE_SHAPE,
    NEXTJS_SERVER_PREVIEW_ID,
    NEXTJS_SERVER_PROFILE_ID,
    NEXTJS_SERVER_TARGET_ID,
    NEXTJS_SERVER_VERIFIER_ID,
)
from disco.core.targets.nextjs_server import (
    OUTPUT_DIR as SERVER_OUTPUT_DIR,
)
from disco.core.targets.nextjs_server import (
    PORT as SERVER_PORT,
)
from disco.core.targets.nextjs_server import (
    START_COMMAND as SERVER_START_COMMAND,
)
from disco.core.targets.nextjs_static import (
    BUILD_COMMAND as STATIC_BUILD_COMMAND,
)
from disco.core.targets.nextjs_static import (
    ENTRY_REFERENCE as STATIC_ENTRY_REFERENCE,
)
from disco.core.targets.nextjs_static import (
    NEXTJS_STATIC_DELIVERY_SHAPE,
    NEXTJS_STATIC_EXPORTER_ID,
    NEXTJS_STATIC_PACKAGE_SHAPE,
    NEXTJS_STATIC_PREVIEW_ID,
    NEXTJS_STATIC_PROFILE_ID,
    NEXTJS_STATIC_TARGET_ID,
    NEXTJS_STATIC_VERIFIER_ID,
    NextjsStaticExporter,
    NextjsStaticTarget,
)
from disco.core.targets.nextjs_static import (
    OUTPUT_DIR as STATIC_OUTPUT_DIR,
)

_STATIC_PROFILE = NEXTJS_STATIC_PROFILE_ID
_SERVER_PROFILE = NEXTJS_SERVER_PROFILE_ID
_STATIC_TARGET = NEXTJS_STATIC_TARGET_ID
_SERVER_TARGET = NEXTJS_SERVER_TARGET_ID
_STATIC_EXPORTER = NEXTJS_STATIC_EXPORTER_ID
_SERVER_EXPORTER = NEXTJS_SERVER_EXPORTER_ID

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


def _registered_choice_ids(registry) -> set[str]:
    return {choice.id.canonical for choice in registry.profiles.choices()}


def _parameters(intent) -> dict[str, object]:
    return {parameter.name: parameter.value for parameter in intent.parameters}


def _static_target() -> NextjsStaticTarget:
    return NextjsStaticTarget()


def _static_exporter() -> NextjsStaticExporter:
    return NextjsStaticExporter()


def test_both_nextjs_profiles_are_registered_builtins() -> None:
    registry = build_builtin_registry()
    choices = _registered_choice_ids(registry)
    assert "disco.nextjs_static@1" in choices
    assert "disco.nextjs_server@1" in choices
    assert registry.profiles.get(_STATIC_PROFILE) is not None
    assert registry.profiles.get(_SERVER_PROFILE) is not None


def test_static_profile_resolves_with_distinct_identity_and_target() -> None:
    composition = _resolve(_STATIC_PROFILE, "build a static export")
    assert composition.profile.id.canonical == "disco.nextjs_static@1"
    assert composition.engine == FREEFORM_ENGINE_ID
    assert composition.target == _STATIC_TARGET
    assert composition.verifier == NEXTJS_STATIC_VERIFIER_ID
    assert composition.preview == NEXTJS_STATIC_PREVIEW_ID
    assert composition.exporter == NEXTJS_STATIC_EXPORTER_ID
    assert composition.connector == NEXTJS_SELF_HOST_CONNECTOR_ID
    assert composition.blocked_operations == ()


def test_server_profile_resolves_with_distinct_identity_and_target() -> None:
    composition = _resolve(_SERVER_PROFILE, "build and run a server")
    assert composition.profile.id.canonical == "disco.nextjs_server@1"
    assert composition.engine == FREEFORM_ENGINE_ID
    assert composition.target == _SERVER_TARGET
    assert composition.verifier == NEXTJS_SERVER_VERIFIER_ID
    assert composition.preview == NEXTJS_SERVER_PREVIEW_ID
    assert composition.exporter == NEXTJS_SERVER_EXPORTER_ID
    assert composition.connector == NEXTJS_SELF_HOST_CONNECTOR_ID
    assert composition.blocked_operations == ()


def test_registered_target_and_exporter_are_observed_through_registry() -> None:
    registry = build_builtin_registry()
    static_target = registry.components.target(_STATIC_TARGET)
    server_target = registry.components.target(_SERVER_TARGET)
    static_exporter = registry.components.exporter(_STATIC_EXPORTER)
    server_exporter = registry.components.exporter(_SERVER_EXPORTER)
    assert static_target is not None and static_target.id == _STATIC_TARGET
    assert server_target is not None and server_target.id == _SERVER_TARGET
    assert static_exporter is not None and static_exporter.id == _STATIC_EXPORTER
    assert server_exporter is not None and server_exporter.id == _SERVER_EXPORTER
    assert static_target is not server_target
    assert static_exporter is not server_exporter

    static_composition = _resolve(_STATIC_PROFILE, "static")
    server_composition = _resolve(_SERVER_PROFILE, "server")
    assert static_target.plan(
        TargetRequest(
            profile=_STATIC_PROFILE,
            engine=FREEFORM_ENGINE_ID,
            goal="static",
        )
    ) == static_composition.target_plan
    assert server_target.plan(
        TargetRequest(
            profile=_SERVER_PROFILE,
            engine=FREEFORM_ENGINE_ID,
            goal="server",
        )
    ) == server_composition.target_plan

    static_export = static_exporter.plan(
        PackageRequest(
            profile=_STATIC_PROFILE,
            target=_STATIC_TARGET,
            delivery=static_composition.target_plan.delivery,
            revision_ref="static:resolved",
        )
    )
    server_export = server_exporter.plan(
        PackageRequest(
            profile=_SERVER_PROFILE,
            target=_SERVER_TARGET,
            delivery=server_composition.target_plan.delivery,
            revision_ref="server:resolved",
        )
    )
    assert static_export.package_shape == NEXTJS_STATIC_PACKAGE_SHAPE
    assert server_export.package_shape == NEXTJS_SERVER_PACKAGE_SHAPE
    assert static_export.intents[0].operation == "nextjs.static.export"
    assert server_export.intents[0].operation == "nextjs.server.export"
    assert _parameters(static_export.intents[0]) == {
        "command": STATIC_BUILD_COMMAND,
        "output_dir": STATIC_OUTPUT_DIR,
        "entry": STATIC_ENTRY_REFERENCE,
    }
    assert _parameters(server_export.intents[0]) == {
        "command": SERVER_BUILD_COMMAND,
        "start": SERVER_START_COMMAND,
        "output_dir": SERVER_OUTPUT_DIR,
        "port": SERVER_PORT,
    }


def test_resolved_production_plans_preserve_frozen_literal_values() -> None:
    static_plan = _resolve(_STATIC_PROFILE, "static").target_plan
    server_plan = _resolve(_SERVER_PROFILE, "server").target_plan

    static_build = next(
        intent for intent in static_plan.intents if intent.operation == "nextjs.static.build"
    )
    assert _parameters(static_build) == {
        "command": STATIC_BUILD_COMMAND,
        "output_dir": STATIC_OUTPUT_DIR,
    }
    assert static_plan.delivery.entry.reference == STATIC_ENTRY_REFERENCE
    assert static_plan.preview.policy.required is False
    assert static_plan.preview.policy.unavailable == "degrade"

    server_build = next(
        intent for intent in server_plan.intents if intent.operation == "nextjs.server.build"
    )
    server_start = next(
        intent for intent in server_plan.intents if intent.operation == "nextjs.server.start"
    )
    assert _parameters(server_build) == {
        "command": SERVER_BUILD_COMMAND,
        "output_dir": SERVER_OUTPUT_DIR,
    }
    assert _parameters(server_start) == {
        "command": SERVER_START_COMMAND,
        "port": SERVER_PORT,
    }
    assert server_plan.preview.policy.required is True
    assert server_plan.preview.policy.unavailable == "block"
    assert _parameters(server_plan.preview.readiness[0]) == {
        "port": SERVER_PORT,
        "path": SERVER_ENTRY_REFERENCE,
    }


def test_static_and_server_shapes_and_operations_remain_disjoint() -> None:
    static_composition = _resolve(_STATIC_PROFILE, "static")
    server_composition = _resolve(_SERVER_PROFILE, "server")
    assert static_composition.target_plan.delivery.shape == NEXTJS_STATIC_DELIVERY_SHAPE
    assert server_composition.target_plan.delivery.shape == NEXTJS_SERVER_DELIVERY_SHAPE
    assert NEXTJS_STATIC_DELIVERY_SHAPE != NEXTJS_SERVER_DELIVERY_SHAPE
    assert static_composition.target_plan.package is not None
    assert server_composition.target_plan.package is not None
    assert static_composition.target_plan.package.package_shape == NEXTJS_STATIC_PACKAGE_SHAPE
    assert server_composition.target_plan.package.package_shape == NEXTJS_SERVER_PACKAGE_SHAPE
    assert NEXTJS_STATIC_PACKAGE_SHAPE != NEXTJS_SERVER_PACKAGE_SHAPE
    static_ops = {intent.operation for intent in static_composition.target_plan.intents}
    server_ops = {intent.operation for intent in server_composition.target_plan.intents}
    assert static_ops != server_ops
    assert "nextjs.static" in next(op for op in static_ops)
    assert "nextjs.server" in next(op for op in server_ops)
    assert not any("nextjs.static" in op for op in server_ops)
    assert not any("nextjs.server" in op for op in static_ops)


def test_static_target_never_resolves_server_target_and_vice_versa() -> None:
    static_composition = _resolve(_STATIC_PROFILE, "static")
    server_composition = _resolve(_SERVER_PROFILE, "server")
    assert static_composition.target == _STATIC_TARGET
    assert static_composition.target != _SERVER_TARGET
    assert server_composition.target == _SERVER_TARGET
    assert server_composition.target != _STATIC_TARGET


def test_resolved_construction_stays_target_neutral() -> None:
    static_composition = _resolve(_STATIC_PROFILE, "static")
    server_composition = _resolve(_SERVER_PROFILE, "server")
    construction = static_composition.construction
    assert construction.engine == FREEFORM_ENGINE_ID
    serialized = construction.model_dump_json().casefold()
    for forbidden in (
        "nextjs",
        "next build",
        "next start",
        "vercel",
        ".next",
        "out/index.html",
        "3000",
    ):
        assert forbidden not in serialized
    assert construction == server_composition.construction


def test_no_digest_or_provider_behavior_in_resolved_profiles() -> None:
    for profile_id in (_STATIC_PROFILE, _SERVER_PROFILE):
        profile = build_builtin_registry().profiles.get(profile_id)
        assert profile is not None
        assert profile.connector == NEXTJS_SELF_HOST_CONNECTOR_ID
        serialized = profile.model_dump_json().casefold()
        assert "package_digest_ref" not in serialized
        assert "provider_rebuild" not in serialized
        assert "deployment" not in serialized
        composition = _resolve(profile_id, "probe")
        assert composition.connector == NEXTJS_SELF_HOST_CONNECTOR_ID
        assert composition.blocked_operations == ()


def test_removing_static_target_blocks_resolution_without_fallback() -> None:
    registry = build_builtin_registry()
    registry.components._targets.pop(_STATIC_TARGET.canonical)
    with pytest.raises(ResolutionError, match="target adapter"):
        resolve_build_composition(
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


def test_replacing_a_registered_target_cannot_silently_fall_back() -> None:
    registry = build_builtin_registry()
    spec = registry.components.spec(_STATIC_TARGET)
    assert spec is not None
    with pytest.raises(RegistryError, match="duplicate"):
        registry.components._add_implementation(
            spec, _static_target(), ComponentKind.TARGET
        )


def test_replacing_a_registered_exporter_cannot_silently_fall_back() -> None:
    registry = build_builtin_registry()
    spec = registry.components.spec(_STATIC_EXPORTER)
    assert spec is not None
    with pytest.raises(RegistryError, match="duplicate"):
        registry.components._add_implementation(
            spec, _static_exporter(), ComponentKind.EXPORTER
        )


def test_unknown_profile_version_fails_closed() -> None:
    unknown = ComponentId(namespace="disco", name="nextjs_static", version="2")
    with pytest.raises(ResolutionError, match="not registered"):
        _resolve(unknown, "probe")


def test_unknown_component_name_fails_closed() -> None:
    unknown = ComponentId(namespace="disco", name="nextjs_missing", version="1")
    with pytest.raises(ResolutionError, match="not registered"):
        _resolve(unknown, "probe")


def test_duplicate_exact_profile_id_is_rejected() -> None:
    registry = build_builtin_registry()
    duplicate = registry.profiles.get(_STATIC_PROFILE)
    assert duplicate is not None
    with pytest.raises(RegistryError, match="duplicate"):
        registry.profiles._add(duplicate)


def test_duplicate_exact_component_id_is_rejected() -> None:
    registry = build_builtin_registry()
    spec = registry.components.spec(_STATIC_TARGET)
    assert spec is not None
    with pytest.raises(RegistryError, match="duplicate"):
        registry.components._add_spec(spec)


def test_legacy_freeform_and_appkit_profiles_still_registered() -> None:
    registry = build_builtin_registry()
    assert registry.profiles.get(FREEFORM_PROFILE_ID) is not None
    choices = _registered_choice_ids(registry)
    assert "disco.freeform_web@1" in choices
    assert "disco.appkit_web@1" in choices

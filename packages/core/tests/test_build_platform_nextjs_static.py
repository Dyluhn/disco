"""Focused tests for the Next.js static target adapter/exporter.

These exercise the production adapters and profile factory directly, not
serialized fixtures or source text.  Mutation and negative controls fail closed
when a frozen value is removed or substituted.
"""

from __future__ import annotations

import re

import pytest
from disco.core.build_platform import (
    FREEFORM_ENGINE_ID,
    DeliveryIntent,
    EntryDescriptor,
    PackagePlan,
    PackageRequest,
    PreviewPolicy,
    TargetAdapter,
    TargetPlan,
    TargetRequest,
    VerifierPlan,
)
from disco.core.targets.nextjs_static import (
    BUILD_COMMAND,
    ENTRY_REFERENCE,
    NEXTJS_STATIC_DELIVERY_SHAPE,
    NEXTJS_STATIC_PACKAGE_SHAPE,
    NEXTJS_STATIC_PROFILE_ID,
    NEXTJS_STATIC_TARGET_ID,
    OUTPUT_DIR,
    NextjsStaticExporter,
    NextjsStaticTarget,
    nextjs_static_profile,
)

_SERVER_PROFILE = NEXTJS_STATIC_PROFILE_ID.model_copy(update={"name": "nextjs_server"})
_SERVER_TARGET = NEXTJS_STATIC_TARGET_ID.model_copy(
    update={"name": "nextjs_server_target"}
)
_OTHER_ENGINE = FREEFORM_ENGINE_ID.model_copy(update={"name": "appkit"})


def _static_target() -> NextjsStaticTarget:
    return NextjsStaticTarget()


def _request(*, profile: object = None, engine: object = None) -> TargetRequest:
    return TargetRequest(
        profile=NEXTJS_STATIC_PROFILE_ID if profile is None else profile,
        engine=FREEFORM_ENGINE_ID if engine is None else engine,
        goal="build a static export",
    )


def _plan() -> TargetPlan:
    return _static_target().plan(_request())


def _parameters(intent) -> dict[str, object]:
    return {parameter.name: parameter.value for parameter in intent.parameters}


def _delivery() -> DeliveryIntent:
    return DeliveryIntent(
        shape=NEXTJS_STATIC_DELIVERY_SHAPE,
        entry=EntryDescriptor(kind="file", reference=ENTRY_REFERENCE),
    )


def _package_request() -> PackageRequest:
    return PackageRequest(
        profile=NEXTJS_STATIC_PROFILE_ID,
        target=NEXTJS_STATIC_TARGET_ID,
        delivery=_delivery(),
        revision_ref="static:unbound",
    )


def _export_plan() -> PackagePlan:
    return NextjsStaticExporter().plan(_package_request())


def test_static_profile_uses_freeform_and_static_identity() -> None:
    profile = nextjs_static_profile()
    assert profile.id.canonical == "disco.nextjs_static@1"
    assert profile.engine.canonical == "disco.freeform@1"
    assert profile.target.canonical == "disco.nextjs_static_target@1"
    assert profile.preview.canonical == "disco.nextjs_static_preview@1"
    assert profile.exporter is not None
    assert profile.exporter.canonical == "disco.nextjs_static_exporter@1"


def test_static_target_is_a_typed_target_adapter() -> None:
    assert isinstance(_static_target(), TargetAdapter)
    assert _static_target().id.canonical == "disco.nextjs_static_target@1"


def test_static_plan_emits_build_output_and_frozen_entry() -> None:
    plan = _plan()
    assert plan.target.canonical == "disco.nextjs_static_target@1"
    build_intent = next(
        intent for intent in plan.intents if intent.operation == "nextjs.static.build"
    )
    parameters = _parameters(build_intent)
    assert parameters["command"] == "next build"
    assert parameters["output_dir"] == "out"
    assert plan.delivery.shape == "nextjs.static_delivery"
    assert plan.delivery.entry.reference == "out/index.html"
    assert plan.package is not None
    assert plan.package.package_shape == "nextjs.static_package"


def test_static_plan_has_no_server_values() -> None:
    plan = _plan()
    serialized = plan.model_dump_json().casefold()
    assert "next start" not in serialized
    assert not re.search(r"(^|[^a-z])\.next($|[^a-z])", serialized)
    assert "3000" not in serialized
    assert plan.delivery.entry.reference != ".next"
    assert tuple(intent.operation for intent in plan.intents) == ("nextjs.static.build",)


def test_static_preview_is_optional_degrading_file_readiness() -> None:
    plan = _plan()
    assert plan.preview.modality == "file_readiness"
    assert plan.preview.policy.required is False
    assert plan.preview.policy.unavailable == "degrade"
    assert tuple(signal.kind for signal in plan.preview.readiness) == ("file_readiness",)
    assert tuple(
        intent.operation for intent in plan.preview.intents
    ) == ("nextjs.static.file_readiness",)


def test_static_plan_package_uses_static_only_shape() -> None:
    plan = _plan()
    assert plan.package is not None
    assert plan.package.package_shape == "nextjs.static_package"
    assert tuple(intent.operation for intent in plan.package.intents) == (
        "nextjs.static.package",
    )
    package_params = _parameters(plan.package.intents[0])
    assert package_params["command"] == "next build"
    assert package_params["output_dir"] == "out"
    assert package_params["entry"] == "out/index.html"


def test_exporter_preserves_static_identity_and_values() -> None:
    plan = _export_plan()
    assert plan.package_shape == "nextjs.static_package"
    assert tuple(intent.operation for intent in plan.intents) == ("nextjs.static.export",)
    params = _parameters(plan.intents[0])
    assert params["command"] == "next build"
    assert params["output_dir"] == "out"
    assert params["entry"] == "out/index.html"


def test_wrong_profile_is_rejected_by_target_and_exporter() -> None:
    with pytest.raises(ValueError, match="refuses profile"):
        _static_target().plan(_request(profile=_SERVER_PROFILE))
    with pytest.raises(ValueError, match="refuses profile"):
        NextjsStaticExporter().plan(
            _package_request().model_copy(update={"profile": _SERVER_PROFILE})
        )


def test_wrong_engine_is_rejected_by_target() -> None:
    with pytest.raises(ValueError, match="Freeform"):
        _static_target().plan(_request(engine=_OTHER_ENGINE))


def test_wrong_target_is_rejected_by_exporter() -> None:
    with pytest.raises(ValueError, match="refuses target"):
        NextjsStaticExporter().plan(
            _package_request().model_copy(update={"target": _SERVER_TARGET})
        )


def test_command_mutation_fails() -> None:
    plan = _plan()
    mutated = plan.model_copy(
        update={
            "intents": (
                plan.intents[0].model_copy(
                    update={
                        "parameters": tuple(
                            parameter.model_copy(update={"value": "npm run build"})
                            if parameter.name == "command"
                            else parameter
                            for parameter in plan.intents[0].parameters
                        )
                    }
                ),
            )
        }
    )
    assert not any(
        _parameters(intent).get("command") == BUILD_COMMAND for intent in mutated.intents
    )


def test_output_dir_mutation_fails() -> None:
    plan = _plan()
    build_intent = next(
        intent for intent in plan.intents if intent.operation == "nextjs.static.build"
    )
    mutated = build_intent.model_copy(
        update={
            "parameters": tuple(
                parameter.model_copy(update={"value": ".next"})
                if parameter.name == "output_dir"
                else parameter
                for parameter in build_intent.parameters
            )
        }
    )
    assert _parameters(mutated)["output_dir"] != OUTPUT_DIR


def test_entry_mutation_fails() -> None:
    plan = _plan()
    assert plan.delivery.entry.reference == ENTRY_REFERENCE
    mutated_entry = EntryDescriptor(kind="file", reference=".next/index.html")
    assert mutated_entry.reference != ENTRY_REFERENCE


def test_readiness_policy_mutation_fails() -> None:
    plan = _plan()
    assert plan.preview.policy.required is False
    assert plan.preview.policy.unavailable == "degrade"
    blocking = PreviewPolicy(required=True, unavailable="block")
    assert blocking != plan.preview.policy


def test_static_delivery_shape_mutation_fails() -> None:
    plan = _plan()
    assert plan.delivery.shape == NEXTJS_STATIC_DELIVERY_SHAPE
    substituted = plan.delivery.model_copy(update={"shape": "nextjs.server_delivery"})
    assert substituted.shape != NEXTJS_STATIC_DELIVERY_SHAPE


def test_static_identity_mutation_fails() -> None:
    assert NEXTJS_STATIC_TARGET_ID != _SERVER_TARGET
    assert NEXTJS_STATIC_PROFILE_ID != _SERVER_PROFILE


def test_package_export_identity_mutation_fails() -> None:
    assert NEXTJS_STATIC_PACKAGE_SHAPE == "nextjs.static_package"
    assert _export_plan().package_shape == NEXTJS_STATIC_PACKAGE_SHAPE
    server_package = _export_plan().model_copy(
        update={"package_shape": "nextjs.server_package"}
    )
    assert server_package.package_shape != NEXTJS_STATIC_PACKAGE_SHAPE


def test_verifier_plan_is_present_and_static_only() -> None:
    plan = _plan()
    assert isinstance(plan.verifier, VerifierPlan)
    assert len(plan.verifier.checks) == 1
    assert plan.verifier.checks[0].check_id == "static_entry_present"

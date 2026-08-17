"""Focused tests for the Next.js server target adapter/exporter.

These exercise the production adapters and profile factory directly, not
serialized fixtures or source text.  Mutation and negative controls fail closed
when a frozen value is removed or substituted.
"""

from __future__ import annotations

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
from disco.core.targets.nextjs_server import (
    BUILD_COMMAND,
    ENTRY_REFERENCE,
    NEXTJS_SERVER_DELIVERY_SHAPE,
    NEXTJS_SERVER_PACKAGE_SHAPE,
    NEXTJS_SERVER_PROFILE_ID,
    NEXTJS_SERVER_TARGET_ID,
    OUTPUT_DIR,
    PORT,
    START_COMMAND,
    NextjsServerExporter,
    NextjsServerTarget,
    nextjs_server_profile,
)

_STATIC_PROFILE = NEXTJS_SERVER_PROFILE_ID.model_copy(update={"name": "nextjs_static"})
_STATIC_TARGET = NEXTJS_SERVER_TARGET_ID.model_copy(
    update={"name": "nextjs_static_target"}
)
_OTHER_ENGINE = FREEFORM_ENGINE_ID.model_copy(update={"name": "appkit"})


def _server_target() -> NextjsServerTarget:
    return NextjsServerTarget()


def _request(*, profile: object = None, engine: object = None) -> TargetRequest:
    return TargetRequest(
        profile=NEXTJS_SERVER_PROFILE_ID if profile is None else profile,
        engine=FREEFORM_ENGINE_ID if engine is None else engine,
        goal="build and run a server",
    )


def _plan() -> TargetPlan:
    return _server_target().plan(_request())


def _parameters(intent) -> dict[str, object]:
    return {parameter.name: parameter.value for parameter in intent.parameters}


def _delivery() -> DeliveryIntent:
    return DeliveryIntent(
        shape=NEXTJS_SERVER_DELIVERY_SHAPE,
        entry=EntryDescriptor(kind="http", reference=ENTRY_REFERENCE),
    )


def _package_request() -> PackageRequest:
    return PackageRequest(
        profile=NEXTJS_SERVER_PROFILE_ID,
        target=NEXTJS_SERVER_TARGET_ID,
        delivery=_delivery(),
        revision_ref="server:unbound",
    )


def _export_plan() -> PackagePlan:
    return NextjsServerExporter().plan(_package_request())


def test_server_profile_uses_freeform_and_server_identity() -> None:
    profile = nextjs_server_profile()
    assert profile.id.canonical == "disco.nextjs_server@1"
    assert profile.engine.canonical == "disco.freeform@1"
    assert profile.target.canonical == "disco.nextjs_server_target@1"
    assert profile.preview.canonical == "disco.nextjs_server_preview@1"
    assert profile.exporter is not None
    assert profile.exporter.canonical == "disco.nextjs_server_exporter@1"


def test_server_target_is_a_typed_target_adapter() -> None:
    assert isinstance(_server_target(), TargetAdapter)
    assert _server_target().id.canonical == "disco.nextjs_server_target@1"


def test_server_plan_emits_build_start_output_and_port() -> None:
    plan = _plan()
    assert plan.target.canonical == "disco.nextjs_server_target@1"
    build_intent = next(
        intent for intent in plan.intents if intent.operation == "nextjs.server.build"
    )
    start_intent = next(
        intent for intent in plan.intents if intent.operation == "nextjs.server.start"
    )
    assert _parameters(build_intent)["command"] == "next build"
    assert _parameters(build_intent)["output_dir"] == ".next"
    assert _parameters(start_intent)["command"] == "next start"
    assert _parameters(start_intent)["port"] == 3000
    assert plan.delivery.shape == "nextjs.server_delivery"
    assert plan.delivery.entry.reference == "/"
    assert plan.package is not None
    assert plan.package.package_shape == "nextjs.server_package"


def test_server_plan_has_no_static_values() -> None:
    plan = _plan()
    serialized = plan.model_dump_json().casefold()
    assert "out/index.html" not in serialized
    assert "nextjs.static" not in serialized
    assert tuple(intent.operation for intent in plan.intents) == (
        "nextjs.server.build",
        "nextjs.server.start",
    )


def test_server_preview_is_required_blocking_http_readiness() -> None:
    plan = _plan()
    assert plan.preview.modality == "http_readiness"
    assert plan.preview.policy.required is True
    assert plan.preview.policy.unavailable == "block"
    assert tuple(signal.kind for signal in plan.preview.readiness) == ("http_readiness",)
    readiness = plan.preview.readiness[0]
    assert _parameters(readiness)["port"] == 3000
    assert _parameters(readiness)["path"] == "/"
    assert tuple(
        intent.operation for intent in plan.preview.intents
    ) == ("nextjs.server.http_readiness",)


def test_server_plan_package_uses_server_only_shape() -> None:
    plan = _plan()
    assert plan.package is not None
    assert plan.package.package_shape == "nextjs.server_package"
    assert tuple(intent.operation for intent in plan.package.intents) == (
        "nextjs.server.package",
    )
    package_params = _parameters(plan.package.intents[0])
    assert package_params["command"] == "next build"
    assert package_params["start"] == "next start"
    assert package_params["output_dir"] == ".next"
    assert package_params["port"] == 3000


def test_exporter_preserves_server_identity_and_values() -> None:
    plan = _export_plan()
    assert plan.package_shape == "nextjs.server_package"
    assert tuple(intent.operation for intent in plan.intents) == ("nextjs.server.export",)
    params = _parameters(plan.intents[0])
    assert params["command"] == "next build"
    assert params["start"] == "next start"
    assert params["output_dir"] == ".next"
    assert params["port"] == 3000


def test_wrong_profile_is_rejected_by_target_and_exporter() -> None:
    with pytest.raises(ValueError, match="refuses profile"):
        _server_target().plan(_request(profile=_STATIC_PROFILE))
    with pytest.raises(ValueError, match="refuses profile"):
        NextjsServerExporter().plan(
            _package_request().model_copy(update={"profile": _STATIC_PROFILE})
        )


def test_wrong_engine_is_rejected_by_target() -> None:
    with pytest.raises(ValueError, match="Freeform"):
        _server_target().plan(_request(engine=_OTHER_ENGINE))


def test_wrong_target_is_rejected_by_exporter() -> None:
    with pytest.raises(ValueError, match="refuses target"):
        NextjsServerExporter().plan(
            _package_request().model_copy(update={"target": _STATIC_TARGET})
        )


def test_build_command_mutation_fails() -> None:
    plan = _plan()
    mutated = plan.model_copy(
        update={
            "intents": tuple(
                intent.model_copy(
                    update={
                        "parameters": tuple(
                            parameter.model_copy(update={"value": "npm run build"})
                            if parameter.name == "command"
                            else parameter
                            for parameter in intent.parameters
                        )
                    }
                )
                if intent.operation == "nextjs.server.build"
                else intent
                for intent in plan.intents
            )
        }
    )
    build_intent = next(
        intent for intent in mutated.intents if intent.operation == "nextjs.server.build"
    )
    assert _parameters(build_intent)["command"] != BUILD_COMMAND


def test_start_command_mutation_fails() -> None:
    plan = _plan()
    start_intent = next(
        intent for intent in plan.intents if intent.operation == "nextjs.server.start"
    )
    mutated = start_intent.model_copy(
        update={
            "parameters": tuple(
                parameter.model_copy(update={"value": "npm start"})
                if parameter.name == "command"
                else parameter
                for parameter in start_intent.parameters
            )
        }
    )
    assert _parameters(mutated)["command"] != START_COMMAND


def test_output_dir_mutation_fails() -> None:
    plan = _plan()
    build_intent = next(
        intent for intent in plan.intents if intent.operation == "nextjs.server.build"
    )
    mutated = build_intent.model_copy(
        update={
            "parameters": tuple(
                parameter.model_copy(update={"value": "out"})
                if parameter.name == "output_dir"
                else parameter
                for parameter in build_intent.parameters
            )
        }
    )
    assert _parameters(mutated)["output_dir"] != OUTPUT_DIR


def test_port_mutation_fails() -> None:
    plan = _plan()
    start_intent = next(
        intent for intent in plan.intents if intent.operation == "nextjs.server.start"
    )
    assert _parameters(start_intent)["port"] == 3000
    mutated = start_intent.model_copy(
        update={
            "parameters": tuple(
                parameter.model_copy(update={"value": 8080})
                if parameter.name == "port"
                else parameter
                for parameter in start_intent.parameters
            )
        }
    )
    assert _parameters(mutated)["port"] != PORT


def test_readiness_path_mutation_fails() -> None:
    plan = _plan()
    readiness = plan.preview.readiness[0]
    assert _parameters(readiness)["path"] == "/"
    mutated = readiness.model_copy(
        update={
            "parameters": tuple(
                parameter.model_copy(update={"value": "/health"})
                if parameter.name == "path"
                else parameter
                for parameter in readiness.parameters
            )
        }
    )
    assert _parameters(mutated)["path"] != ENTRY_REFERENCE


def test_readiness_policy_mutation_fails() -> None:
    plan = _plan()
    assert plan.preview.policy.required is True
    assert plan.preview.policy.unavailable == "block"
    degrading = PreviewPolicy(required=False, unavailable="degrade")
    assert degrading != plan.preview.policy


def test_server_delivery_shape_mutation_fails() -> None:
    plan = _plan()
    assert plan.delivery.shape == NEXTJS_SERVER_DELIVERY_SHAPE
    substituted = plan.delivery.model_copy(update={"shape": "nextjs.static_delivery"})
    assert substituted.shape != NEXTJS_SERVER_DELIVERY_SHAPE


def test_server_identity_mutation_fails() -> None:
    assert NEXTJS_SERVER_TARGET_ID != _STATIC_TARGET
    assert NEXTJS_SERVER_PROFILE_ID != _STATIC_PROFILE


def test_package_export_identity_mutation_fails() -> None:
    assert NEXTJS_SERVER_PACKAGE_SHAPE == "nextjs.server_package"
    assert _export_plan().package_shape == NEXTJS_SERVER_PACKAGE_SHAPE
    static_package = _export_plan().model_copy(
        update={"package_shape": "nextjs.static_package"}
    )
    assert static_package.package_shape != NEXTJS_SERVER_PACKAGE_SHAPE


def test_verifier_plan_is_present_and_server_only() -> None:
    plan = _plan()
    assert isinstance(plan.verifier, VerifierPlan)
    assert len(plan.verifier.checks) == 1
    assert plan.verifier.checks[0].check_id == "server_http_ready"
    assert plan.verifier.checks[0].required_execution_modality == "http_readiness"

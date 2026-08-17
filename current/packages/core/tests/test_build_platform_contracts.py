from __future__ import annotations

from typing import ClassVar

import pytest
from disco.core.build_platform import (
    BuildProfile,
    CapabilityLayer,
    CompatibilityRequirements,
    ComponentId,
    ComponentIntent,
    ConstructionEngine,
    ConstructionPlan,
    ConstructionRequest,
    DeliveryIntent,
    DeploymentConnector,
    DeploymentPlan,
    DeploymentRequest,
    EntryDescriptor,
    ModuleRef,
    PackageExporter,
    PackagePlan,
    PackageRequest,
    PolicyLayer,
    PreviewPlan,
    RunAdmissionAnchor,
    TargetAdapter,
    TargetPlan,
    TargetRequest,
    TrustLevel,
    VerifierCheck,
    VerifierPlan,
    composition_digest,
    derive_run_admission_identity,
)
from pydantic import ValidationError


def _id(name: str) -> ComponentId:
    return ComponentId(namespace="disco", name=name, version="1")


class _Engine:
    id: ClassVar[ComponentId] = _id("test_engine")

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=(ComponentIntent(operation="workspace.author"),),
            requested_tools=frozenset({"file_edit"}),
        )


class _Adapter:
    id: ClassVar[ComponentId] = _id("test_target")

    def plan(self, request: TargetRequest) -> TargetPlan:
        entry = EntryDescriptor(kind="manifest", reference="artifact/manifest.task")
        return TargetPlan(
            target=self.id,
            delivery=DeliveryIntent(shape="data.job_bundle", entry=entry),
            preview=PreviewPlan(modality="dry_run", entry=entry),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="fixture",
                        intent=ComponentIntent(operation="target.verify_fixture"),
                    ),
                )
            ),
            package=PackagePlan(
                package_shape="data.job_bundle",
                intents=(ComponentIntent(operation="target.package"),),
            ),
        )


class _Connector:
    id: ClassVar[ComponentId] = _id("test_connector")

    def plan(self, request: DeploymentRequest) -> DeploymentPlan:
        return DeploymentPlan(
            connector=self.id,
            environment=request.environment,
            intents=(ComponentIntent(operation="target.deploy"),),
        )


class _Exporter:
    id: ClassVar[ComponentId] = _id("test_exporter")

    def plan(self, request: PackageRequest) -> PackagePlan:
        return PackagePlan(
            package_shape=request.delivery.shape,
            intents=(ComponentIntent(operation="target.package"),),
        )


def test_component_ids_are_namespace_and_version_explicit() -> None:
    assert _id("freeform").canonical == "disco.freeform@1"
    with pytest.raises(ValidationError):
        ComponentId(namespace="Disco", name="freeform", version="1")
    with pytest.raises(ValidationError):
        ComponentId(namespace="disco", name="freeform", version="latest")


def test_profile_is_frozen_closed_and_round_trips() -> None:
    profile = BuildProfile(
        id=_id("batch_profile"),
        label="Batch job",
        engine=_Engine.id,
        target=_Adapter.id,
        verifier=_id("job_verifier"),
        preview=_id("dry_run_preview"),
        prompt_modules=(
            ModuleRef(
                component=_id("batch_prompt"),
                trust=TrustLevel.TRUSTED_LOCAL,
                provenance="built-in",
            ),
        ),
        compatible_reference_categories=frozenset({"reference_pack"}),
        capabilities=CapabilityLayer(
            source="profile",
            allowed=frozenset({"workspace.read", "workspace.write"}),
        ),
        policy=PolicyLayer(source="profile"),
        requirements=CompatibilityRequirements(required_capabilities=frozenset({"workspace.read"})),
    )
    assert BuildProfile.model_validate(profile.model_dump(mode="json")) == profile
    with pytest.raises(ValidationError):
        BuildProfile.model_validate({**profile.model_dump(), "runtime": object()})
    with pytest.raises(ValidationError):
        profile.label = "mutated"  # type: ignore[misc]


def test_engine_and_adapter_return_intent_only_non_web_plans() -> None:
    engine = _Engine()
    adapter = _Adapter()
    assert isinstance(engine, ConstructionEngine)
    assert isinstance(adapter, TargetAdapter)
    construction = engine.plan(
        ConstructionRequest(profile=_id("batch_profile"), goal="transform fixture")
    )
    target = adapter.plan(
        TargetRequest(profile=_id("batch_profile"), engine=engine.id, goal="transform fixture")
    )
    assert construction.intents[0].operation == "workspace.author"
    assert target.delivery.shape == "data.job_bundle"
    assert target.preview.modality == "dry_run"
    serialized = target.model_dump_json()
    assert "http" not in serialized
    assert "index.html" not in serialized
    assert "port" not in serialized
    assert "verdict" not in serialized


def test_none_preview_cannot_smuggle_preview_work() -> None:
    with pytest.raises(ValidationError, match="cannot declare entry/readiness"):
        PreviewPlan(
            modality="none",
            entry=EntryDescriptor(kind="manifest", reference="artifact/task.json"),
        )


def test_component_requests_expose_no_host_authority_or_secret_channel() -> None:
    request_fields = set(ConstructionRequest.model_fields)
    assert request_fields == {"profile", "goal", "modules", "reference_ids"}
    forbidden = {
        "runtime",
        "store",
        "sandbox",
        "executor",
        "secret",
        "revision_writer",
        "publish_success",
    }
    assert request_fields.isdisjoint(forbidden)
    with pytest.raises(ValidationError):
        ConstructionRequest.model_validate(
            {"profile": _id("profile"), "goal": "x", "secret": "not allowed"}
        )


def test_engine_plan_cannot_self_verify() -> None:
    with pytest.raises(ValidationError):
        ConstructionPlan.model_validate(
            {"engine": _Engine.id, "verdict": "pass", "requested_tools": []}
        )


def test_connector_returns_confirmed_intent_not_an_effect() -> None:
    connector = _Connector()
    assert isinstance(connector, DeploymentConnector)
    plan = connector.plan(
        DeploymentRequest(
            profile=_id("batch_profile"),
            target=_Adapter.id,
            package_digest_ref="pkg:sha256:opaque",
            environment="staging",
        )
    )
    assert plan.confirmation_required is True
    assert plan.intents == (ComponentIntent(operation="target.deploy"),)


def test_exporter_returns_package_intent_not_revision_authority() -> None:
    exporter = _Exporter()
    assert isinstance(exporter, PackageExporter)
    delivery = (
        _Adapter()
        .plan(TargetRequest(profile=_id("batch_profile"), engine=_Engine.id, goal="fixture"))
        .delivery
    )
    plan = exporter.plan(
        PackageRequest(
            profile=_id("batch_profile"),
            target=_Adapter.id,
            delivery=delivery,
            revision_ref="revision:42",
        )
    )
    assert plan.package_shape == "data.job_bundle"
    assert set(PackageRequest.model_fields).isdisjoint({"store", "secret", "executor"})


def test_run_identity_is_deterministic_and_bound_to_durable_run_intent() -> None:
    plan = _Adapter().plan(
        TargetRequest(profile=_id("batch_profile"), engine=_Engine.id, goal="fixture")
    )
    digest = composition_digest(plan)
    anchor = RunAdmissionAnchor(
        conversation_id="conv-1",
        run_intent_event_id="evt-10",
        run_intent_seq=10,
        run_intent_operation="agent.run-intent.user-turn",
    )
    first = derive_run_admission_identity(digest, anchor)
    second = derive_run_admission_identity(digest, anchor)
    assert first == second
    assert first.value.startswith("run:sha256:")
    assert (
        derive_run_admission_identity(
            digest,
            anchor.model_copy(update={"run_intent_event_id": "evt-11", "run_intent_seq": 11}),
        )
        != first
    )

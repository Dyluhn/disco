"""Synthetic non-web architecture proof; deliberately not a shipped user target."""

from __future__ import annotations

from .compiler import PromptContextInputs
from .contracts import (
    BuildProfile,
    CapabilityLayer,
    ComponentId,
    ComponentIntent,
    ComponentKind,
    ConstructionPlan,
    ConstructionRequest,
    DeliveryIntent,
    EntryDescriptor,
    PackagePlan,
    PackageRequest,
    PolicyLayer,
    PreviewPlan,
    PreviewPolicy,
    ReadinessSignal,
    TargetPlan,
    TargetRequest,
    VerifierCheck,
    VerifierPlan,
)
from .registry import BuildPlatformRegistry, ComponentSpec
from .resolver import BuildComposition, ResolutionInputs, resolve_build_composition

SYNTHETIC_PROFILE_ID = ComponentId(namespace="synthetic", name="batchjob_profile", version="1")
SYNTHETIC_ENGINE_ID = ComponentId(namespace="synthetic", name="fixture_engine", version="1")
SYNTHETIC_TARGET_ID = ComponentId(namespace="synthetic", name="batchjob", version="1")
SYNTHETIC_VERIFIER_ID = ComponentId(namespace="synthetic", name="batchjob_verifier", version="1")
SYNTHETIC_PREVIEW_ID = ComponentId(namespace="synthetic", name="dry_run_preview", version="1")
SYNTHETIC_EXPORTER_ID = ComponentId(namespace="synthetic", name="batchjob_exporter", version="1")

SYNTHETIC_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})


def _capabilities(source: str) -> CapabilityLayer:
    return CapabilityLayer(source=source, allowed=SYNTHETIC_CAPABILITIES)


def _spec(component_id: ComponentId, kind: ComponentKind) -> ComponentSpec:
    return ComponentSpec(
        id=component_id,
        kind=kind,
        capabilities=_capabilities(component_id.canonical),
        policy=PolicyLayer(source=component_id.canonical),
    )


class SyntheticFixtureEngine:
    @property
    def id(self) -> ComponentId:
        return SYNTHETIC_ENGINE_ID

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=(
                ComponentIntent(
                    operation="job.author_spec",
                    required_capabilities=frozenset({"workspace.write"}),
                ),
            ),
            required_capabilities=frozenset({"workspace.write"}),
        )


class SyntheticBatchJobAdapter:
    @property
    def id(self) -> ComponentId:
        return SYNTHETIC_TARGET_ID

    def plan(self, request: TargetRequest) -> TargetPlan:
        entry = EntryDescriptor(kind="manifest", reference="job/task.manifest")
        return TargetPlan(
            target=self.id,
            intents=(
                ComponentIntent(
                    operation="job.build_fixture",
                    required_capabilities=frozenset({"workspace.read", "workspace.write"}),
                ),
            ),
            delivery=DeliveryIntent(shape="data.job_bundle", entry=entry),
            preview=PreviewPlan(
                modality="dry_run",
                entry=entry,
                readiness=(ReadinessSignal(kind="exit_zero"),),
                intents=(
                    ComponentIntent(
                        operation="job.preview_fixture",
                        required_capabilities=frozenset({"process.execute"}),
                    ),
                ),
                required_capabilities=frozenset({"process.execute"}),
                policy=PreviewPolicy(required=False, unavailable="degrade"),
            ),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="output_contract",
                        intent=ComponentIntent(
                            operation="job.verify_output",
                            required_capabilities=frozenset({"workspace.read"}),
                        ),
                    ),
                )
            ),
            package=PackagePlan(
                package_shape="data.job_bundle",
                intents=(
                    ComponentIntent(
                        operation="job.package_bundle",
                        required_capabilities=frozenset({"workspace.read"}),
                    ),
                ),
            ),
            required_capabilities=frozenset({"workspace.read", "workspace.write"}),
        )


class SyntheticBatchJobExporter:
    @property
    def id(self) -> ComponentId:
        return SYNTHETIC_EXPORTER_ID

    def plan(self, request: PackageRequest) -> PackagePlan:
        return PackagePlan(
            package_shape="data.job_bundle",
            intents=(
                ComponentIntent(
                    operation="job.package_bundle",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
            ),
        )


def build_synthetic_registry() -> BuildPlatformRegistry:
    """Register the fixture through ordinary public APIs, not a central switch."""

    registry = BuildPlatformRegistry()
    registry.register_engine(
        _spec(SYNTHETIC_ENGINE_ID, ComponentKind.ENGINE),
        SyntheticFixtureEngine(),
    )
    registry.register_target(
        _spec(SYNTHETIC_TARGET_ID, ComponentKind.TARGET),
        SyntheticBatchJobAdapter(),
    )
    registry.register_exporter(
        _spec(SYNTHETIC_EXPORTER_ID, ComponentKind.EXPORTER),
        SyntheticBatchJobExporter(),
    )
    registry.register_component(_spec(SYNTHETIC_VERIFIER_ID, ComponentKind.VERIFIER))
    registry.register_component(_spec(SYNTHETIC_PREVIEW_ID, ComponentKind.PREVIEW))
    registry.register_profile(
        BuildProfile(
            id=SYNTHETIC_PROFILE_ID,
            label="Synthetic batch-job conformance fixture",
            engine=SYNTHETIC_ENGINE_ID,
            target=SYNTHETIC_TARGET_ID,
            verifier=SYNTHETIC_VERIFIER_ID,
            preview=SYNTHETIC_PREVIEW_ID,
            exporter=SYNTHETIC_EXPORTER_ID,
            compatible_reference_categories=frozenset({"reference_pack"}),
            capabilities=_capabilities(SYNTHETIC_PROFILE_ID.canonical),
            policy=PolicyLayer(source=SYNTHETIC_PROFILE_ID.canonical),
        )
    )
    return registry


def resolve_synthetic_conformance(
    *, host_capabilities: frozenset[str] = SYNTHETIC_CAPABILITIES
) -> BuildComposition:
    ceiling = _capabilities("ceiling")
    return resolve_build_composition(
        build_synthetic_registry(),
        ResolutionInputs(
            profile=SYNTHETIC_PROFILE_ID,
            goal="transform a fixture into a packaged batch-job result",
            platform_capabilities=ceiling.model_copy(update={"source": "platform"}),
            user_capabilities=ceiling.model_copy(update={"source": "user"}),
            host_capabilities=CapabilityLayer(source="host", allowed=host_capabilities),
            platform_policy=PolicyLayer(source="platform"),
            user_policy=PolicyLayer(source="user"),
            prompt_context=PromptContextInputs(),
        ),
    )

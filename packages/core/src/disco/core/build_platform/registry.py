"""Exact, namespace-protected registry for Build Platform components."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field

from .contracts import (
    BuildProfile,
    CapabilityLayer,
    ComponentId,
    ComponentKind,
    ConstructionEngine,
    ConstructionPlan,
    ConstructionRequest,
    DeploymentConnector,
    DeploymentPlan,
    DeploymentRequest,
    FrozenModel,
    PackageExporter,
    PackagePlan,
    PackageRequest,
    PolicyLayer,
    TargetAdapter,
    TargetPlan,
    TargetRequest,
)

_PROTECTED_NAMESPACES = frozenset({"disco"})


class RegistryError(ValueError):
    """A component registration violates identity or ownership rules."""


class ComponentSpec(FrozenModel):
    id: ComponentId
    kind: ComponentKind
    features: frozenset[str] = frozenset()
    capabilities: CapabilityLayer
    policy: PolicyLayer


class ProfileChoice(FrozenModel):
    id: ComponentId
    label: str = Field(min_length=1, max_length=96)


class ConstructionEngineDefinition(FrozenModel):
    id: ComponentId
    plan: ConstructionPlan


class TargetAdapterDefinition(FrozenModel):
    id: ComponentId
    plan: TargetPlan


class PackageExporterDefinition(FrozenModel):
    id: ComponentId
    plan: PackagePlan


class DeploymentConnectorDefinition(FrozenModel):
    id: ComponentId
    plan: DeploymentPlan


class _ComponentEntry[T]:
    def __init__(self, spec: ComponentSpec, implementation: T) -> None:
        self.spec = spec
        self.implementation = implementation


class _DeclaredEngine:
    def __init__(self, definition: ConstructionEngineDefinition) -> None:
        self._definition = definition

    @property
    def id(self) -> ComponentId:
        return self._definition.id

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return self._definition.plan


class _DeclaredTarget:
    def __init__(self, definition: TargetAdapterDefinition) -> None:
        self._definition = definition

    @property
    def id(self) -> ComponentId:
        return self._definition.id

    def plan(self, request: TargetRequest) -> TargetPlan:
        return self._definition.plan


class _DeclaredExporter:
    def __init__(self, definition: PackageExporterDefinition) -> None:
        self._definition = definition

    @property
    def id(self) -> ComponentId:
        return self._definition.id

    def plan(self, request: PackageRequest) -> PackagePlan:
        return self._definition.plan


class _DeclaredConnector:
    def __init__(self, definition: DeploymentConnectorDefinition) -> None:
        self._definition = definition

    @property
    def id(self) -> ComponentId:
        return self._definition.id

    def plan(self, request: DeploymentRequest) -> DeploymentPlan:
        return self._definition.plan


class BuildPlatformRegistry:
    """One exact-ID catalog. Public registration cannot claim host namespaces."""

    def __init__(self) -> None:
        self._profiles: dict[str, BuildProfile] = {}
        self._specs: dict[str, ComponentSpec] = {}
        self._engines: dict[str, _ComponentEntry[ConstructionEngine]] = {}
        self._targets: dict[str, _ComponentEntry[TargetAdapter]] = {}
        self._exporters: dict[str, _ComponentEntry[PackageExporter]] = {}
        self._connectors: dict[str, _ComponentEntry[DeploymentConnector]] = {}

    @staticmethod
    def _key(component_id: ComponentId) -> str:
        return component_id.canonical

    @staticmethod
    def _check_public_namespace(component_id: ComponentId) -> None:
        if component_id.namespace in _PROTECTED_NAMESPACES:
            raise RegistryError(
                f"namespace {component_id.namespace!r} is host-protected; "
                "external registration cannot claim built-in IDs"
            )

    def _ensure_free(self, component_id: ComponentId) -> str:
        key = self._key(component_id)
        if key in self._specs or key in self._profiles:
            raise RegistryError(f"duplicate component id: {key}")
        return key

    @staticmethod
    def _check_spec(
        spec: ComponentSpec, expected: ComponentKind, implementation_id: ComponentId
    ) -> None:
        if spec.kind is not expected:
            raise RegistryError(
                f"component {spec.id.canonical} is {spec.kind.value}, expected {expected.value}"
            )
        if spec.id != implementation_id:
            raise RegistryError("component spec and implementation IDs differ")

    def register_profile(self, profile: BuildProfile) -> None:
        if type(profile) is not BuildProfile:
            raise RegistryError("public profile registration requires exact frozen data")
        self._check_public_namespace(profile.id)
        self._register_profile(profile)

    def register_component(self, spec: ComponentSpec) -> None:
        if type(spec) is not ComponentSpec:
            raise RegistryError("public component registration requires exact frozen data")
        self._check_public_namespace(spec.id)
        self._register_component(spec)

    def register_engine(
        self, spec: ComponentSpec, definition: ConstructionEngineDefinition
    ) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not ConstructionEngineDefinition:
            raise RegistryError("public engine registration is data-only")
        self._check_public_namespace(spec.id)
        if definition.plan.engine != definition.id:
            raise RegistryError("engine definition plan identity differs from its ID")
        self._register_engine(spec, _DeclaredEngine(definition))

    def register_target(self, spec: ComponentSpec, definition: TargetAdapterDefinition) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not TargetAdapterDefinition:
            raise RegistryError("public target registration is data-only")
        self._check_public_namespace(spec.id)
        if definition.plan.target != definition.id:
            raise RegistryError("target definition plan identity differs from its ID")
        self._register_target(spec, _DeclaredTarget(definition))

    def register_exporter(self, spec: ComponentSpec, definition: PackageExporterDefinition) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not PackageExporterDefinition:
            raise RegistryError("public exporter registration is data-only")
        self._check_public_namespace(spec.id)
        self._register_exporter(spec, _DeclaredExporter(definition))

    def register_connector(
        self, spec: ComponentSpec, definition: DeploymentConnectorDefinition
    ) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not DeploymentConnectorDefinition:
            raise RegistryError("public connector registration is data-only")
        self._check_public_namespace(spec.id)
        if definition.plan.connector != definition.id:
            raise RegistryError("connector definition plan identity differs from its ID")
        self._register_connector(spec, _DeclaredConnector(definition))

    # Host-owned built-in modules use these deliberately private registration
    # paths. User/plugin ingestion receives only the public, protected methods.
    def _register_profile(self, profile: BuildProfile) -> None:
        key = self._ensure_free(profile.id)
        self._profiles[key] = profile

    def _register_component(self, spec: ComponentSpec) -> None:
        key = self._ensure_free(spec.id)
        self._specs[key] = spec

    def _register_engine(self, spec: ComponentSpec, engine: ConstructionEngine) -> None:
        self._check_spec(spec, ComponentKind.ENGINE, engine.id)
        key = self._ensure_free(spec.id)
        self._specs[key] = spec
        self._engines[key] = _ComponentEntry(spec, engine)

    def _register_target(self, spec: ComponentSpec, target: TargetAdapter) -> None:
        self._check_spec(spec, ComponentKind.TARGET, target.id)
        key = self._ensure_free(spec.id)
        self._specs[key] = spec
        self._targets[key] = _ComponentEntry(spec, target)

    def _register_exporter(self, spec: ComponentSpec, exporter: PackageExporter) -> None:
        self._check_spec(spec, ComponentKind.EXPORTER, exporter.id)
        key = self._ensure_free(spec.id)
        self._specs[key] = spec
        self._exporters[key] = _ComponentEntry(spec, exporter)

    def _register_connector(self, spec: ComponentSpec, connector: DeploymentConnector) -> None:
        self._check_spec(spec, ComponentKind.CONNECTOR, connector.id)
        key = self._ensure_free(spec.id)
        self._specs[key] = spec
        self._connectors[key] = _ComponentEntry(spec, connector)

    def profile(self, component_id: ComponentId) -> BuildProfile | None:
        return self._profiles.get(self._key(component_id))

    def spec(self, component_id: ComponentId) -> ComponentSpec | None:
        return self._specs.get(self._key(component_id))

    def engine(self, component_id: ComponentId) -> ConstructionEngine | None:
        entry = self._engines.get(self._key(component_id))
        return entry.implementation if entry is not None else None

    def target(self, component_id: ComponentId) -> TargetAdapter | None:
        entry = self._targets.get(self._key(component_id))
        return entry.implementation if entry is not None else None

    def exporter(self, component_id: ComponentId) -> PackageExporter | None:
        entry = self._exporters.get(self._key(component_id))
        return entry.implementation if entry is not None else None

    def connector(self, component_id: ComponentId) -> DeploymentConnector | None:
        entry = self._connectors.get(self._key(component_id))
        return entry.implementation if entry is not None else None

    def profile_choices(self) -> tuple[ProfileChoice, ...]:
        choices = (
            ProfileChoice(id=profile.id, label=profile.label) for profile in self._profiles.values()
        )
        return tuple(
            sorted(choices, key=lambda choice: (choice.label.casefold(), choice.id.canonical))
        )

    def component_specs(self) -> tuple[ComponentSpec, ...]:
        return tuple(sorted(self._specs.values(), key=lambda spec: spec.id.canonical))

    def _add_builtins(
        self,
        *,
        profiles: Iterable[BuildProfile] = (),
        components: Iterable[ComponentSpec] = (),
    ) -> None:
        """Register trusted in-package declarations; not an ingestion API."""

        for component in components:
            self._register_component(component)
        for profile in profiles:
            self._register_profile(profile)

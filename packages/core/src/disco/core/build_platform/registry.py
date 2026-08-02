"""Exact, namespace-protected registry for Build Platform components."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

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


class _ComponentKeyspace:
    """Sole owner of exact-ID uniqueness across profiles and components.

    Profiles and components share one id namespace: registering a profile at an
    id already claimed by a component (or vice versa) must fail exactly like a
    same-catalog collision. Splitting the profile/component catalogs into
    separate objects must not silently split this uniqueness guarantee too.
    """

    def __init__(self) -> None:
        self._claimed: set[str] = set()

    def claim(self, component_id: ComponentId) -> str:
        key = component_id.canonical
        if key in self._claimed:
            raise RegistryError(f"duplicate component id: {key}")
        self._claimed.add(key)
        return key


class ProfileCatalog:
    """Sole owner of the profile catalog."""

    def __init__(self, keyspace: _ComponentKeyspace) -> None:
        self._keyspace = keyspace
        self._profiles: dict[str, BuildProfile] = {}

    def get(self, component_id: ComponentId) -> BuildProfile | None:
        return self._profiles.get(component_id.canonical)

    def choices(self) -> tuple[ProfileChoice, ...]:
        choices = (
            ProfileChoice(id=profile.id, label=profile.label) for profile in self._profiles.values()
        )
        return tuple(
            sorted(choices, key=lambda choice: (choice.label.casefold(), choice.id.canonical))
        )

    # Host-owned built-in modules use this deliberately private mutation path.
    # User/plugin ingestion reaches it only through BuildPlatformRegistry's
    # public, namespace-protected register_profile.
    def _add(self, profile: BuildProfile) -> None:
        key = self._keyspace.claim(profile.id)
        self._profiles[key] = profile


class ComponentCatalog:
    """Sole owner of component specs and their kind-specific implementations."""

    def __init__(self, keyspace: _ComponentKeyspace) -> None:
        self._keyspace = keyspace
        self._specs: dict[str, ComponentSpec] = {}
        self._engines: dict[str, _ComponentEntry[ConstructionEngine]] = {}
        self._targets: dict[str, _ComponentEntry[TargetAdapter]] = {}
        self._exporters: dict[str, _ComponentEntry[PackageExporter]] = {}
        self._connectors: dict[str, _ComponentEntry[DeploymentConnector]] = {}

    def spec(self, component_id: ComponentId) -> ComponentSpec | None:
        return self._specs.get(component_id.canonical)

    def engine(self, component_id: ComponentId) -> ConstructionEngine | None:
        entry = self._engines.get(component_id.canonical)
        return entry.implementation if entry is not None else None

    def target(self, component_id: ComponentId) -> TargetAdapter | None:
        entry = self._targets.get(component_id.canonical)
        return entry.implementation if entry is not None else None

    def exporter(self, component_id: ComponentId) -> PackageExporter | None:
        entry = self._exporters.get(component_id.canonical)
        return entry.implementation if entry is not None else None

    def connector(self, component_id: ComponentId) -> DeploymentConnector | None:
        entry = self._connectors.get(component_id.canonical)
        return entry.implementation if entry is not None else None

    def specs(self) -> tuple[ComponentSpec, ...]:
        return tuple(sorted(self._specs.values(), key=lambda spec: spec.id.canonical))

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

    # Host-owned built-in modules use these deliberately private mutation
    # paths. User/plugin ingestion reaches them only through
    # BuildPlatformRegistry's public, namespace-protected register_* methods.
    def _add_spec(self, spec: ComponentSpec) -> None:
        key = self._keyspace.claim(spec.id)
        self._specs[key] = spec

    def _add_implementation(
        self,
        spec: ComponentSpec,
        implementation: Any,
        expected: ComponentKind,
    ) -> None:
        """Validate kind/id, THEN claim the key — order is load-bearing.

        A spec that is both the wrong kind and a duplicate id must raise the
        kind error, not the duplicate error; that requires the identity check
        to run before the keyspace claim.
        """

        self._check_spec(spec, expected, implementation.id)
        key = self._keyspace.claim(spec.id)
        self._specs[key] = spec
        if expected is ComponentKind.ENGINE:
            self._engines[key] = _ComponentEntry(spec, implementation)
        elif expected is ComponentKind.TARGET:
            self._targets[key] = _ComponentEntry(spec, implementation)
        elif expected is ComponentKind.EXPORTER:
            self._exporters[key] = _ComponentEntry(spec, implementation)
        elif expected is ComponentKind.CONNECTOR:
            self._connectors[key] = _ComponentEntry(spec, implementation)


class BuildPlatformRegistry:
    """One exact-ID catalog. Public registration cannot claim host namespaces."""

    def __init__(self) -> None:
        keyspace = _ComponentKeyspace()
        self.profiles = ProfileCatalog(keyspace)
        self.components = ComponentCatalog(keyspace)

    @staticmethod
    def _check_public_namespace(component_id: ComponentId) -> None:
        if component_id.namespace in _PROTECTED_NAMESPACES:
            raise RegistryError(
                f"namespace {component_id.namespace!r} is host-protected; "
                "external registration cannot claim built-in IDs"
            )

    def register_profile(self, profile: BuildProfile) -> None:
        if type(profile) is not BuildProfile:
            raise RegistryError("public profile registration requires exact frozen data")
        self._check_public_namespace(profile.id)
        self.profiles._add(profile)

    def register_component(self, spec: ComponentSpec) -> None:
        if type(spec) is not ComponentSpec:
            raise RegistryError("public component registration requires exact frozen data")
        self._check_public_namespace(spec.id)
        self.components._add_spec(spec)

    def register_engine(
        self, spec: ComponentSpec, definition: ConstructionEngineDefinition
    ) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not ConstructionEngineDefinition:
            raise RegistryError("public engine registration is data-only")
        self._check_public_namespace(spec.id)
        if definition.plan.engine != definition.id:
            raise RegistryError("engine definition plan identity differs from its ID")
        self.components._add_implementation(spec, _DeclaredEngine(definition), ComponentKind.ENGINE)

    def register_target(self, spec: ComponentSpec, definition: TargetAdapterDefinition) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not TargetAdapterDefinition:
            raise RegistryError("public target registration is data-only")
        self._check_public_namespace(spec.id)
        if definition.plan.target != definition.id:
            raise RegistryError("target definition plan identity differs from its ID")
        self.components._add_implementation(spec, _DeclaredTarget(definition), ComponentKind.TARGET)

    def register_exporter(self, spec: ComponentSpec, definition: PackageExporterDefinition) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not PackageExporterDefinition:
            raise RegistryError("public exporter registration is data-only")
        self._check_public_namespace(spec.id)
        self.components._add_implementation(
            spec, _DeclaredExporter(definition), ComponentKind.EXPORTER
        )

    def register_connector(
        self, spec: ComponentSpec, definition: DeploymentConnectorDefinition
    ) -> None:
        if type(spec) is not ComponentSpec or type(definition) is not DeploymentConnectorDefinition:
            raise RegistryError("public connector registration is data-only")
        self._check_public_namespace(spec.id)
        if definition.plan.connector != definition.id:
            raise RegistryError("connector definition plan identity differs from its ID")
        self.components._add_implementation(
            spec, _DeclaredConnector(definition), ComponentKind.CONNECTOR
        )

    def _add_builtins(
        self,
        *,
        profiles: Iterable[BuildProfile] = (),
        components: Iterable[ComponentSpec] = (),
    ) -> None:
        """Register trusted in-package declarations; not an ingestion API."""

        for component in components:
            self.components._add_spec(component)
        for profile in profiles:
            self.profiles._add(profile)

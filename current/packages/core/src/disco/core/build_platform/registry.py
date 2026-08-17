"""Exact, namespace-protected registry for Build Platform components."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

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


class UnregistrationError(RegistryError):
    """A bare unregistration was refused or an explicit one was malformed."""


class PersistedReferenceError(RegistryError):
    """A persisted reference could not be resolved, migrated, or rolled back."""


class ComponentSpec(FrozenModel):
    id: ComponentId
    kind: ComponentKind
    features: frozenset[str] = frozenset()
    capabilities: CapabilityLayer
    policy: PolicyLayer


class ProfileChoice(FrozenModel):
    id: ComponentId
    label: str = Field(min_length=1, max_length=96)


# The one descriptor shape this registry can migrate deterministically.  A
# persisted reference carrying a different version is unsupported and must fail
# closed rather than fall back to a nearest-name or alias.
SUPPORTED_DESCRIPTOR_VERSION: Literal[1] = 1


class PersistedProfileDescriptor(FrozenModel):
    """A typed, deterministic migration record for one persisted profile ID.

    The persisted reference binds ``persisted_profile`` (the ID a stored
    consumer still holds) to the exact live built-in profile ``profile`` it
    resolves through this registry's descriptor/migration path.  It also
    preserves the original profile, target, exporter, and self-host connector
    identities so resolution can never silently reinterpret or reattach them.
    ``descriptor_version`` makes the migration typed: any version other than
    the supported one is unknown/unsupported and fails closed.
    """

    persisted_profile: ComponentId
    profile: ComponentId
    target: ComponentId
    exporter: ComponentId | None
    connector: ComponentId | None
    descriptor_version: int = SUPPORTED_DESCRIPTOR_VERSION


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

    def _add_mirror(self, persisted_id: ComponentId, mirror: BuildProfile) -> None:
        """Register a persisted mirror through the shared exact-ID keyspace.

        The persisted ID is claimed in the same keyspace as every profile and
        component, so a persisted reference whose ID collides with any existing
        profile or component fails closed (raising before any storage) instead
        of overwriting it.  The claim is the single mutation that can fail, so
        the caller can hold the descriptor write until after it succeeds.
        """
        key = self._keyspace.claim(persisted_id)
        self._profiles[key] = mirror

    def _restore_mirror(self, persisted_id: ComponentId, mirror: BuildProfile) -> None:
        """Restore a persisted mirror whose identity is already claimed.

        The exact ID was reserved by the shared keyspace when the persisted
        reference was first registered, so rollback must not re-claim it.  It
        only refuses to overwrite a mirror that is still present, which keeps
        rollback from duplicating or silently replacing an already-claimed
        identity.
        """
        key = persisted_id.canonical
        if key in self._profiles:
            raise RegistryError(f"duplicate component id: {key}")
        self._profiles[key] = mirror


class PersistedReferenceCatalog:
    """Sole owner of persisted built-in profile references and their migration.

    A persisted reference is created only after the referenced live built-in
    profile is already registered.  It is a typed descriptor that binds an old
    consumer-held profile ID to an exact live profile and preserves the
    original profile/target/exporter/self-host identities.  The persisted ID
    itself is registered as a mirror of the live profile under the persisted ID
    through the shared exact-ID keyspace, so it is a normal, resolvable profile
    that a stored consumer may hold.  A bare unregistration of a persisted ID
    is rejected with a typed reason; only an explicit rollback restores the
    prior registration deterministically.
    """

    def __init__(self) -> None:
        self._descriptors: dict[str, PersistedProfileDescriptor] = {}

    def get(self, persisted_profile: ComponentId) -> PersistedProfileDescriptor | None:
        return self._descriptors.get(persisted_profile.canonical)

    def has(self, persisted_profile: ComponentId) -> bool:
        return persisted_profile.canonical in self._descriptors

    @staticmethod
    def _mirror(descriptor: PersistedProfileDescriptor, live: BuildProfile) -> BuildProfile:
        """Register the persisted ID as a mirror of the live built-in profile."""
        return live.model_copy(update={"id": descriptor.persisted_profile})

    def register(
        self, registry: BuildPlatformRegistry, descriptor: PersistedProfileDescriptor
    ) -> None:
        """Validate, then claim the mirror atomically, then record the descriptor.

        The mirror's exact-ID claim is the single mutation that can fail; the
        descriptor is only recorded after that claim succeeds, so a collision
        (or any earlier validation failure) leaves neither a mirror nor a
        descriptor behind — no partial state.
        """
        if descriptor.persisted_profile.canonical in self._descriptors:
            raise RegistryError(
                f"persisted reference already exists: {descriptor.persisted_profile.canonical}"
            )
        if descriptor.descriptor_version != SUPPORTED_DESCRIPTOR_VERSION:
            raise PersistedReferenceError(
                f"unsupported persisted descriptor version: {descriptor.descriptor_version}"
            )
        if descriptor.persisted_profile.canonical == descriptor.profile.canonical:
            raise PersistedReferenceError(
                "persisted reference must differ from its resolved live profile"
            )
        live = registry.profiles.get(descriptor.profile)
        if live is None:
            raise PersistedReferenceError(
                f"persisted reference binds to missing live profile {descriptor.profile.canonical}"
            )
        if descriptor.target != live.target:
            raise PersistedReferenceError(
                "persisted reference target differs from the bound live profile"
            )
        if descriptor.exporter != live.exporter:
            raise PersistedReferenceError(
                "persisted reference exporter differs from the bound live profile"
            )
        if descriptor.connector != live.connector:
            raise PersistedReferenceError(
                "persisted reference connector differs from the bound live profile"
            )
        mirror = self._mirror(descriptor, live)
        registry.profiles._add_mirror(descriptor.persisted_profile, mirror)
        self._descriptors[descriptor.persisted_profile.canonical] = descriptor

    def resolve(
        self, registry: BuildPlatformRegistry, persisted_profile: ComponentId
    ) -> BuildProfile:
        """Resolve a persisted ID through its descriptor to a live profile.

        No nearest-name, alias, or provider fallback is permitted: the exact
        persisted ID must have a typed descriptor, that descriptor must be the
        supported version, and its bound live profile must currently be
        registered.  Any failure closes with a typed reason and raises.
        """
        descriptor = self._descriptors.get(persisted_profile.canonical)
        if descriptor is None:
            raise PersistedReferenceError(
                f"no persisted reference for {persisted_profile.canonical}"
            )
        if descriptor.descriptor_version != SUPPORTED_DESCRIPTOR_VERSION:
            raise PersistedReferenceError(
                f"unsupported persisted descriptor version: {descriptor.descriptor_version}"
            )
        live = registry.profiles.get(descriptor.profile)
        if live is None:
            raise PersistedReferenceError(
                f"persisted {persisted_profile.canonical} binds to missing "
                f"profile {descriptor.profile.canonical}"
            )
        # The descriptor's preserved identities must agree with the live profile
        # so the migration can never silently reinterpret the component, target,
        # exporter, or connector.
        if (
            descriptor.target != live.target
            or descriptor.exporter != live.exporter
            or descriptor.connector != live.connector
        ):
            raise PersistedReferenceError(
                f"persisted descriptor identity drift for {persisted_profile.canonical}"
            )
        return live

    def rollback(
        self, registry: BuildPlatformRegistry, persisted_profile: ComponentId
    ) -> None:
        """Restore a prior descriptor/registration deterministically.

        The persisted ID is re-registered as a mirror of the exact live built-in
        profile the descriptor already names, proving the persisted ID is
        neither lost nor silently reinterpreted across a rollback.  The exact ID
        was reserved by the shared keyspace at initial registration, so rollback
        restores the already-claimed identity and refuses to duplicate or
        overwrite a mirror that is still present.
        """
        descriptor = self._descriptors.get(persisted_profile.canonical)
        if descriptor is None:
            raise PersistedReferenceError(
                f"no persisted reference to roll back: {persisted_profile.canonical}"
            )
        live = registry.profiles.get(descriptor.profile)
        if live is None:
            raise PersistedReferenceError(
                f"cannot roll back {persisted_profile.canonical}: bound profile "
                f"{descriptor.profile.canonical} is not registered"
            )
        mirror = self._mirror(descriptor, live)
        registry.profiles._restore_mirror(descriptor.persisted_profile, mirror)


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
        self.persisted = PersistedReferenceCatalog()

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

    def register_persisted_reference(self, descriptor: PersistedProfileDescriptor) -> None:
        """Typed, deterministic persisted-reference compatibility seam.

        A persisted reference may only be recorded for an exact live built-in
        profile that is already registered, and only for the supported
        descriptor version.  The persisted ID is claimed through the shared
        exact-ID keyspace, so a persisted ID that collides with any existing
        profile or component ID fails closed with no overwrite and no partial
        descriptor/catalog state.  The descriptor preserves the original
        profile, target, exporter, and self-host connector identities so
        resolution never silently reinterprets or reattaches them.
        """
        if type(descriptor) is not PersistedProfileDescriptor:
            raise RegistryError("persisted reference registration requires exact frozen data")
        self.persisted.register(self, descriptor)

    def resolve_persisted_reference(self, persisted_profile: ComponentId) -> BuildProfile:
        """Resolve a persisted built-in profile ID through its descriptor path.

        Returns the exact live profile the persisted ID migrates to.  Fails
        closed on an unknown, removed, unsupported, or drifted persisted
        reference; there is no nearest-name, alias, or provider fallback.  The
        returned live profile ID is what must be passed to
        ``resolve_build_composition(...)`` for real composition.
        """
        return self.persisted.resolve(self, persisted_profile)

    def rollback_persisted_reference(self, persisted_profile: ComponentId) -> None:
        """Deterministically restore the prior descriptor/registration."""
        self.persisted.rollback(self, persisted_profile)

    def unregister_profile(self, profile_id: ComponentId) -> None:
        """Bare unregistration: remove a live profile registration, never a descriptor.

        A live, unpersisted profile can be unregistered and then resolves as
        unavailable/blocked rather than silently selecting another component.
        Once a profile ID has a persisted reference, a bare unregister is
        rejected with a typed reason so the persisted ID keeps resolving through
        its descriptor/migration path.
        """
        if self.persisted.has(profile_id):
            raise UnregistrationError(
                f"cannot bare-unregister persisted profile {profile_id.canonical}; "
                "resolve or roll back its persisted reference instead"
            )
        key = profile_id.canonical
        if key not in self.profiles._profiles:
            raise UnregistrationError(f"profile {profile_id.canonical} is not registered")
        self.profiles._profiles.pop(key)

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

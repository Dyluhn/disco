"""Versioned host profiles, their registry and persisted-reference compatibility.

Core ships **definitions**, never claims.  A :class:`HostProfileDefinition` is a
versioned, target-neutral declaration of *what must be probed* on a class of
host.  It advertises nothing on its own.  A :class:`HostProfile` — the thing
that actually carries capability claims — exists only as the product of binding
a definition to real observations, so there is no path by which a shipped
constant asserts that some platform supports something.

That split is the whole point of the package:

* **Below host ports, never a Core conditional.**  Nothing here inspects a
  machine.  The definitions are data keyed by identity; the adapter that knows
  how to look at a real host implements
  :class:`~.host_capabilities.HostProbeRunner` on the far side of the port.
  ``os_label`` is documentation — no function in this module reads it.
* **An OS label cannot advertise.**  Advertising requires a ``PRESENT``
  observation stamped with the binding profile's own identity, so two profiles
  sharing a label but not evidence advertise different sets, and a profile with
  a label and no evidence advertises nothing.
* **Optional capabilities are optional structurally.**  A probe marked optional
  may not appear in any phase requirement, so its absence cannot block a phase.
  gVisor is declared this way: an optional Linux runtime capability, evidenced
  per host, never an ambient sandbox that other profiles inherit.
* **A persisted profile ID is never silently reinterpreted.**  Resolving an old
  identity returns a descriptor naming what happened to it; it never returns a
  successor's capabilities wearing the old identity.

The projections :meth:`HostProfile.capability_layer` and
:meth:`HostProfile.support` feed the existing resolver seam rather than a second
capability model, so a host profile can only ever *narrow* the effective grant:
it is one more layer in an intersection.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from enum import Enum

from pydantic import Field, model_validator

from .contracts import CapabilityLayer, CapabilitySupport, ComponentId, FrozenModel, Parameter
from .host_capabilities import (
    ADVERTISING_GRADES,
    EvidenceGrade,
    HostCapabilityProbe,
    HostCapabilityResult,
    HostPhase,
    PhaseImpact,
    PhaseRequirement,
    ProbeMethod,
    ProbeObservation,
    bind_observations,
    grade_observation,
    phase_impacts,
)

HOST_NAMESPACE = "host"


class HostProfileError(ValueError):
    """A host profile operation failed closed."""


class CompatibilityStatus(str, Enum):
    """What a persisted profile identity still means.

    ``SUPERSEDED`` is deliberately still selectable: that is the documented
    rollback path — an operator picks the prior supported profile explicitly,
    by its own identity, rather than a newer profile being substituted for it.
    ``RETIRED`` is not selectable, but still resolves to a descriptor so an
    already-persisted reference is never orphaned.
    """

    LIVE = "live"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


def _require_unique(values: Sequence[Hashable], message: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(message)


def _require_coherent_requirements(
    probes: tuple[HostCapabilityProbe, ...],
    phase_requirements: tuple[PhaseRequirement, ...],
) -> None:
    """Every requirement is probed, and no optional capability gates a phase.

    Split out of the model validator so each rule stays independently readable:
    the two failures mean different things, and a reader should not have to
    disentangle them from an identity check and three uniqueness checks.
    """
    declared = {probe.capability for probe in probes}
    required: set[str] = set()
    for requirement in phase_requirements:
        required |= requirement.capabilities

    unprobed = sorted(required - declared)
    if unprobed:
        raise ValueError(
            "a phase requires capabilities with no declared probe: " + ", ".join(unprobed)
        )

    optional = {probe.capability for probe in probes if probe.optional}
    blocking_optional = sorted(optional & required)
    if blocking_optional:
        raise ValueError(
            "an optional capability may not be required by a phase: " + ", ".join(blocking_optional)
        )

    declared_phases = {requirement.phase for requirement in phase_requirements}
    for probe in probes:
        if probe.phases and not probe.phases <= declared_phases:
            raise ValueError(f"probe {probe.probe_id} names a phase the profile does not declare")


class HostProfileDefinition(FrozenModel):
    """A versioned declaration of what a class of host must be probed for."""

    id: ComponentId
    os_label: str = Field(min_length=2, max_length=96)
    summary: str = Field(min_length=3, max_length=240)
    probes: tuple[HostCapabilityProbe, ...]
    phase_requirements: tuple[PhaseRequirement, ...] = ()

    @model_validator(mode="after")
    def _declaration_is_coherent(self) -> HostProfileDefinition:
        if self.id.namespace != HOST_NAMESPACE:
            raise ValueError(f"a host profile must live in the {HOST_NAMESPACE!r} namespace")
        _require_unique(
            [probe.probe_id for probe in self.probes],
            "a host profile declares each probe id at most once",
        )
        _require_unique(
            [probe.capability for probe in self.probes],
            "a host capability has more than one probe, so it has no single owner",
        )
        _require_unique(
            [requirement.phase for requirement in self.phase_requirements],
            "a phase declares its requirement at most once",
        )
        _require_coherent_requirements(self.probes, self.phase_requirements)
        return self

    @property
    def optional_capabilities(self) -> frozenset[str]:
        return frozenset(probe.capability for probe in self.probes if probe.optional)

    @property
    def probed_capabilities(self) -> frozenset[str]:
        return frozenset(probe.capability for probe in self.probes)

    def probe(self, probe_id: str) -> HostCapabilityProbe | None:
        return next((item for item in self.probes if item.probe_id == probe_id), None)

    def bind(
        self,
        observations: tuple[ProbeObservation, ...],
        *,
        degraded: dict[str, str] | None = None,
        experimental: frozenset[str] = frozenset(),
    ) -> HostProfile:
        """Bind real observations into the one claim-carrying record.

        This is the only constructor of a :class:`HostProfile` that a caller
        should use.  Every declared probe must have exactly one observation
        stamped with this profile's identity — a missing one is an error, not a
        default, so a capability nobody checked cannot reach a claim.
        """
        matched = bind_observations(self.probes, observations, profile_id=self.id.canonical)
        results = tuple(
            grade_observation(
                probe,
                observation,
                degraded_capabilities=degraded or {},
                experimental_capabilities=experimental,
            )
            for probe, observation in zip(self.probes, matched, strict=True)
        )
        return HostProfile(definition=self, results=results)


class HostProfile(FrozenModel):
    """A definition bound to evidence — the only thing that advertises."""

    definition: HostProfileDefinition
    results: tuple[HostCapabilityResult, ...]

    @model_validator(mode="after")
    def _every_claim_is_evidenced(self) -> HostProfile:
        declared = {probe.probe_id: probe for probe in self.definition.probes}
        seen: set[str] = set()
        for result in self.results:
            probe = declared.get(result.probe_id)
            if probe is None:
                raise ValueError(
                    f"result claims capability {result.capability!r} from undeclared probe "
                    f"{result.probe_id!r}"
                )
            if probe.capability != result.capability:
                raise ValueError(
                    f"probe {result.probe_id} owns {probe.capability!r}, not {result.capability!r}"
                )
            if result.observation.observed_on != self.definition.id.canonical:
                raise ValueError(
                    f"result for {result.capability!r} rests on evidence observed on "
                    f"{result.observation.observed_on!r}, not {self.definition.id.canonical!r}"
                )
            if result.probe_id in seen:
                raise ValueError(f"probe {result.probe_id} is graded more than once")
            seen.add(result.probe_id)
        missing = sorted(set(declared) - seen)
        if missing:
            raise ValueError(
                "a declared probe has no graded result: " + ", ".join(missing),
            )
        return self

    @property
    def id(self) -> ComponentId:
        return self.definition.id

    @property
    def advertised(self) -> frozenset[str]:
        """Exactly the capabilities a ``PRESENT`` observation justifies."""
        return frozenset(result.capability for result in self.results if result.advertised)

    @property
    def degraded(self) -> frozenset[str]:
        return frozenset(
            result.capability for result in self.results if result.grade is EvidenceGrade.DEGRADED
        )

    @property
    def experimental(self) -> frozenset[str]:
        return frozenset(
            result.capability
            for result in self.results
            if result.grade is EvidenceGrade.EXPERIMENTAL
        )

    @property
    def unsupported(self) -> frozenset[str]:
        return frozenset(
            result.capability
            for result in self.results
            if result.grade is EvidenceGrade.UNSUPPORTED
        )

    @property
    def advertised_optional(self) -> frozenset[str]:
        return self.advertised & self.definition.optional_capabilities

    def result_for(self, capability: str) -> HostCapabilityResult | None:
        return next((item for item in self.results if item.capability == capability), None)

    def capability_layer(self) -> CapabilityLayer:
        """Project into the intersection the resolver already computes.

        The host profile becomes one more ceiling among platform, user, profile
        and component layers.  It can therefore only narrow the effective grant
        — the property is structural, inherited from the intersection, not a
        check this module performs.
        """
        denials = tuple(
            Parameter(
                name=result.capability,
                value=f"{result.grade.value}: {result.detail or result.observation.detail}",
            )
            for result in sorted(self.results, key=lambda item: item.capability)
            if not result.advertised
        )
        return CapabilityLayer(
            source=self.definition.id.canonical,
            allowed=self.advertised,
            denials=denials,
        )

    def support(self) -> tuple[CapabilitySupport, ...]:
        """The per-capability support statement, explicit for every result."""
        return tuple(
            result.support() for result in sorted(self.results, key=lambda item: item.capability)
        )

    def phase_impacts(self) -> tuple[PhaseImpact, ...]:
        """Resolve every declared phase independently against this evidence."""
        return phase_impacts(
            self.advertised,
            self.degraded,
            self.definition.phase_requirements,
        )

    def impact_for(self, phase: HostPhase) -> PhaseImpact | None:
        return next((item for item in self.phase_impacts() if item.phase is phase), None)

    def capability_matrix(self) -> tuple[tuple[str, str, str, str], ...]:
        """The retained evidence matrix: capability, grade, probe, outcome."""
        return tuple(
            (
                result.capability,
                result.grade.value,
                result.probe_id,
                result.observation.outcome.value,
            )
            for result in sorted(self.results, key=lambda item: item.capability)
        )


class HostProfileCompatibility(FrozenModel):
    """What a persisted profile identity resolves to, stated rather than guessed."""

    persisted_id: ComponentId
    status: CompatibilityStatus
    selectable: bool
    resolved_to: ComponentId | None = None
    detail: str = Field(min_length=3, max_length=400)

    @model_validator(mode="after")
    def _successor_is_not_a_substitute(self) -> HostProfileCompatibility:
        if self.status is CompatibilityStatus.LIVE and self.resolved_to is not None:
            raise ValueError("a live profile identity resolves to itself and names no successor")
        if self.status is not CompatibilityStatus.LIVE and self.resolved_to is None:
            raise ValueError("a superseded or retired identity must name what replaced it")
        if self.status is CompatibilityStatus.RETIRED and self.selectable:
            raise ValueError("a retired profile identity is not selectable")
        return self


def _lineage(component: ComponentId) -> tuple[str, str]:
    return (component.namespace, component.name)


def _version_key(component: ComponentId) -> tuple[int, ...]:
    return tuple(int(part) for part in component.version.split("."))


class HostProfileRegistry:
    """Holds bound profiles and the ledger of what each identity now means.

    Registration takes an already-evidenced profile: the registry is not a
    second place where a capability could be asserted.  Its own job is the
    identity question — which profiles are selectable, and what an old
    persisted reference still resolves to.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, HostProfile] = {}
        self._status: dict[str, CompatibilityStatus] = {}
        self._successor: dict[str, ComponentId] = {}
        self._detail: dict[str, str] = {}

    def register(self, profile: HostProfile) -> None:
        key = profile.id.canonical
        if key in self._profiles:
            raise HostProfileError(f"host profile already registered: {key}")
        self._profiles[key] = profile
        self._status[key] = CompatibilityStatus.LIVE
        self._detail[key] = "live registered profile"

    def supersede(self, old: ComponentId, new: ComponentId, *, detail: str) -> None:
        """Record that a newer version exists, without retiring the old one.

        The old identity keeps its own evidence and stays selectable, because
        rollback means choosing the prior supported profile deliberately.  What
        changes is that resolving the old identity now names its successor.
        """
        self._require(old)
        self._require(new)
        if _lineage(old) != _lineage(new):
            raise HostProfileError("a profile may only be superseded within its own lineage")
        if _version_key(new) <= _version_key(old):
            raise HostProfileError("a superseding profile must carry a later version")
        self._status[old.canonical] = CompatibilityStatus.SUPERSEDED
        self._successor[old.canonical] = new
        self._detail[old.canonical] = detail

    def retire(self, component: ComponentId, *, successor: ComponentId, detail: str) -> None:
        """Withdraw an identity from selection while keeping it resolvable."""
        self._require(component)
        self._require(successor)
        self._status[component.canonical] = CompatibilityStatus.RETIRED
        self._successor[component.canonical] = successor
        self._detail[component.canonical] = detail

    def select(self, component: ComponentId) -> HostProfile:
        """Return the profile registered under *exactly* this identity.

        A retired identity is refused rather than upgraded.  Returning the
        successor here would be the silent reinterpretation the playbook
        forbids: the caller asked for one profile's evidence and would receive
        another's under the same name.
        """
        key = component.canonical
        profile = self._profiles.get(key)
        if profile is None:
            raise HostProfileError(f"host profile {key} is not registered")
        if self._status[key] is CompatibilityStatus.RETIRED:
            raise HostProfileError(
                f"host profile {key} is retired and cannot be selected; "
                f"resolve_persisted() names its successor "
                f"{self._successor[key].canonical}"
            )
        return profile

    def resolve_persisted(self, component: ComponentId) -> HostProfileCompatibility:
        """Resolve an already-persisted identity to a descriptor, never a swap."""
        key = component.canonical
        if key not in self._profiles:
            raise HostProfileError(
                f"persisted host profile {key} has no compatibility descriptor: "
                "it was never registered, so no meaning can be asserted for it"
            )
        status = self._status[key]
        return HostProfileCompatibility(
            persisted_id=component,
            status=status,
            selectable=status is not CompatibilityStatus.RETIRED,
            resolved_to=self._successor.get(key),
            detail=self._detail[key],
        )

    def prior_supported(self, component: ComponentId) -> HostProfile | None:
        """The rollback target: the newest selectable earlier version in lineage."""
        self._require(component)
        current = _version_key(component)
        candidates = [
            profile
            for key, profile in self._profiles.items()
            if _lineage(profile.id) == _lineage(component)
            and _version_key(profile.id) < current
            and self._status[key] is not CompatibilityStatus.RETIRED
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda profile: _version_key(profile.id))

    def registered_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def selectable_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                key
                for key in self._profiles
                if self._status[key] is not CompatibilityStatus.RETIRED
            )
        )

    def _require(self, component: ComponentId) -> None:
        if component.canonical not in self._profiles:
            raise HostProfileError(f"host profile {component.canonical} is not registered")


def _probe(
    probe_id: str,
    capability: str,
    method: ProbeMethod,
    subject: str,
    phases: frozenset[HostPhase] = frozenset(),
    *,
    optional: bool = False,
) -> HostCapabilityProbe:
    return HostCapabilityProbe(
        probe_id=probe_id,
        capability=capability,
        method=method,
        subject=subject,
        owner="disco.core.build_platform.host_profiles",
        phases=phases,
        optional=optional,
    )


#: The probes every host class is checked for.  Shared *declarations* are safe
#: in a way shared *results* would not be: each profile still has to evidence
#: every one of these on its own before it may advertise anything.
_PORTABLE_PROBES: tuple[HostCapabilityProbe, ...] = (
    _probe(
        "host.workspace_read",
        "workspace.read",
        ProbeMethod.FILESYSTEM_ACCESS,
        "read a file inside the workspace root",
        frozenset({HostPhase.TARGET, HostPhase.VERIFY, HostPhase.PACKAGE}),
    ),
    _probe(
        "host.workspace_write",
        "workspace.write",
        ProbeMethod.FILESYSTEM_ACCESS,
        "create and remove a file inside the workspace root",
        frozenset({HostPhase.CONSTRUCT, HostPhase.TARGET}),
    ),
    _probe(
        "host.process_execute",
        "process.execute",
        ProbeMethod.PROCESS_SPAWN,
        "spawn a child process and read its exit status",
        frozenset({HostPhase.CONSTRUCT, HostPhase.PACKAGE}),
    ),
    _probe(
        "host.network_egress",
        "network.egress",
        ProbeMethod.NETWORK_EGRESS,
        "open an outbound connection to a declared endpoint",
        frozenset({HostPhase.DEPLOY}),
    ),
    _probe(
        "host.display_interactive",
        "display.interactive",
        ProbeMethod.DISPLAY_SERVER,
        "an attached interactive display server",
        frozenset({HostPhase.PREVIEW}),
    ),
    _probe(
        "host.toolchain_node",
        "toolchain.node",
        ProbeMethod.TOOLCHAIN_VERSION,
        "a Node.js toolchain on PATH",
        frozenset({HostPhase.CONSTRUCT}),
    ),
    _probe(
        "host.signing_identity",
        "signing.code_signature",
        ProbeMethod.SIGNING_IDENTITY,
        "a usable code-signing identity",
        frozenset({HostPhase.PACKAGE}),
    ),
    _probe(
        "host.container_runtime",
        "container.oci_runtime",
        ProbeMethod.CONTAINER_RUNTIME,
        "an OCI-compatible container runtime",
        optional=True,
    ),
)

#: The phase requirements shared by every host class.  ``preview`` depends on
#: the interactive display and nothing else, and ``package`` is the only phase
#: that depends on signing — which is what makes a missing display degrade
#: preview alone, and a missing signing identity block packaging alone.
_PORTABLE_PHASE_REQUIREMENTS: tuple[PhaseRequirement, ...] = (
    PhaseRequirement(
        phase=HostPhase.CONSTRUCT,
        capabilities=frozenset({"workspace.write", "process.execute", "toolchain.node"}),
    ),
    PhaseRequirement(
        phase=HostPhase.TARGET,
        capabilities=frozenset({"workspace.read", "workspace.write"}),
    ),
    PhaseRequirement(
        phase=HostPhase.PREVIEW,
        capabilities=frozenset({"display.interactive"}),
    ),
    PhaseRequirement(
        phase=HostPhase.VERIFY,
        capabilities=frozenset({"workspace.read"}),
    ),
    PhaseRequirement(
        phase=HostPhase.PACKAGE,
        capabilities=frozenset({"workspace.read", "process.execute", "signing.code_signature"}),
    ),
    PhaseRequirement(
        phase=HostPhase.DEPLOY,
        capabilities=frozenset({"network.egress"}),
    ),
)

#: gVisor.  Declared by the Linux lineage only, and marked optional, so its
#: absence blocks no phase and no other profile inherits it.  It is a runtime
#: *choice* available on some Linux hosts, not a sandbox the platform assumes.
GVISOR_CAPABILITY = "runtime.gvisor_sandbox"

_GVISOR_PROBE = _probe(
    "host.gvisor_sandbox",
    GVISOR_CAPABILITY,
    ProbeMethod.SANDBOX_RUNTIME,
    "the runsc user-space kernel on PATH",
    optional=True,
)


LINUX_HOST_V1 = HostProfileDefinition(
    id=ComponentId(namespace=HOST_NAMESPACE, name="linux", version="1"),
    os_label="Linux",
    summary="Linux host, probe-evidenced per machine",
    probes=_PORTABLE_PROBES,
    phase_requirements=_PORTABLE_PHASE_REQUIREMENTS,
)

LINUX_HOST_V2 = HostProfileDefinition(
    id=ComponentId(namespace=HOST_NAMESPACE, name="linux", version="2"),
    os_label="Linux",
    summary="Linux host, adding the optional gVisor runtime capability",
    probes=_PORTABLE_PROBES + (_GVISOR_PROBE,),
    phase_requirements=_PORTABLE_PHASE_REQUIREMENTS,
)

WSL2_HOST_V1 = HostProfileDefinition(
    id=ComponentId(namespace=HOST_NAMESPACE, name="wsl2", version="1"),
    os_label="WSL2",
    summary="WSL2 host; no capability is claimed without its own evidence",
    probes=_PORTABLE_PROBES,
    phase_requirements=_PORTABLE_PHASE_REQUIREMENTS,
)

MACOS_HOST_V1 = HostProfileDefinition(
    id=ComponentId(namespace=HOST_NAMESPACE, name="macos", version="1"),
    os_label="macOS",
    summary="macOS host; no capability is claimed without its own evidence",
    probes=_PORTABLE_PROBES,
    phase_requirements=_PORTABLE_PHASE_REQUIREMENTS,
)

#: Every shipped definition, by canonical identity.  Definitions only — binding
#: one still requires observations, so importing this grants no capability.
HOST_PROFILE_DEFINITIONS: tuple[HostProfileDefinition, ...] = (
    LINUX_HOST_V1,
    LINUX_HOST_V2,
    WSL2_HOST_V1,
    MACOS_HOST_V1,
)


def definition_for(component: ComponentId) -> HostProfileDefinition | None:
    """Look a definition up by identity — the only lookup key that exists."""
    return next(
        (item for item in HOST_PROFILE_DEFINITIONS if item.id.canonical == component.canonical),
        None,
    )


def advertising_grades() -> frozenset[EvidenceGrade]:
    """Re-exported so a caller cannot re-derive a different advertising rule."""
    return ADVERTISING_GRADES

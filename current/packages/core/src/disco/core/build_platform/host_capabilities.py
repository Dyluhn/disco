"""Probe declarations, capability evidence and phase-local impact.

This module owns the vocabulary in which a host may *claim* a capability.  It
deliberately contains no operating-system knowledge at all: it cannot name a
platform, detect one, or branch on one.  What it owns is the rule that makes a
claim honest —

* **Every advertised capability has exactly one declared probe.**  A capability
  with no :class:`HostCapabilityProbe` cannot appear in a result, so "advertised
  but never checked" is unrepresentable rather than merely discouraged.
* **Every result carries the observation it came from.**  A grade is not a field
  an author sets freely: :class:`HostCapabilityResult` refuses ``supported`` or
  ``degraded`` unless its :class:`ProbeObservation` actually found the subject
  ``present``.  An OS label cannot reach those grades, because an OS label is
  not an observation.
* **Evidence is local to the profile it was taken on.**  Each observation names
  the profile it was observed on, and :mod:`.host_profiles` refuses to bind an
  observation to any other profile.  Evidence gathered on one host therefore
  cannot be re-used to advertise a capability on a different one.
* **Impact is phase local.**  A missing capability blocks exactly the phases
  that declared a requirement on it.  Losing an interactive display degrades
  preview; it does not block construction or packaging.

Executing a probe is not this module's job.  :class:`HostProbeRunner` is the
port; the adapter that knows how to look at a real machine lives below it, in
the host process, which is where platform-specific operations belong.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from .contracts import CapabilitySupport, FrozenModel, SupportLevel

_PROBE_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")


class HostPhase(str, Enum):
    """The build phases a host capability can be required by.

    These are exactly the operation names
    :func:`~.resolver._plan_requirements` computes, so a phase requirement here
    and an ``OperationBlock`` there describe the same thing rather than two
    parallel vocabularies that could drift.
    """

    CONSTRUCT = "construct"
    TARGET = "target"
    PREVIEW = "preview"
    VERIFY = "verify"
    PACKAGE = "package"
    DEPLOY = "deploy"


class ProbeMethod(str, Enum):
    """How a probe establishes its subject, named rather than free text."""

    EXECUTABLE_PRESENT = "executable_present"
    FILESYSTEM_ACCESS = "filesystem_access"
    PROCESS_SPAWN = "process_spawn"
    NETWORK_EGRESS = "network_egress"
    DISPLAY_SERVER = "display_server"
    CONTAINER_RUNTIME = "container_runtime"
    SANDBOX_RUNTIME = "sandbox_runtime"
    TOOLCHAIN_VERSION = "toolchain_version"
    SIGNING_IDENTITY = "signing_identity"


class ProbeOutcome(str, Enum):
    """What a probe actually found.

    ``NOT_RUN`` is a first-class outcome, not an error state.  A profile for a
    platform this host is not is bound entirely from ``NOT_RUN`` observations,
    which is how "we have no evidence yet" is said out loud instead of being
    quietly rendered as support.
    """

    PRESENT = "present"
    ABSENT = "absent"
    NOT_RUN = "not_run"
    ERROR = "error"


class EvidenceGrade(str, Enum):
    """The claim a profile makes about a capability, ranked by evidence.

    ``SUPPORTED`` and ``DEGRADED`` are the only grades that advertise, and both
    require a ``PRESENT`` observation.  ``EXPERIMENTAL`` is the honest grade for
    a capability a profile intends to support but has not evidenced on this
    host; it advertises nothing.
    """

    SUPPORTED = "supported"
    DEGRADED = "degraded"
    EXPERIMENTAL = "experimental"
    UNSUPPORTED = "unsupported"


#: The grades that put a capability into a profile's advertised ceiling.
ADVERTISING_GRADES: frozenset[EvidenceGrade] = frozenset(
    {EvidenceGrade.SUPPORTED, EvidenceGrade.DEGRADED}
)

#: The observation outcome each grade requires.  This is the load-bearing map:
#: it is what stops a grade from being a free-text opinion about a platform.
_REQUIRED_OUTCOMES: dict[EvidenceGrade, frozenset[ProbeOutcome]] = {
    EvidenceGrade.SUPPORTED: frozenset({ProbeOutcome.PRESENT}),
    EvidenceGrade.DEGRADED: frozenset({ProbeOutcome.PRESENT}),
    EvidenceGrade.EXPERIMENTAL: frozenset({ProbeOutcome.NOT_RUN, ProbeOutcome.ERROR}),
    EvidenceGrade.UNSUPPORTED: frozenset(
        {ProbeOutcome.ABSENT, ProbeOutcome.NOT_RUN, ProbeOutcome.ERROR}
    ),
}

#: How an evidence grade projects into the host support contract the resolver
#: already consumes.  ``EXPERIMENTAL`` deliberately projects to ``UNSUPPORTED``:
#: the resolver's intersection must not grant an unevidenced capability, and the
#: richer grade survives in the detail text and in the profile's own results.
_SUPPORT_LEVEL: dict[EvidenceGrade, SupportLevel] = {
    EvidenceGrade.SUPPORTED: SupportLevel.SUPPORTED,
    EvidenceGrade.DEGRADED: SupportLevel.DEGRADED,
    EvidenceGrade.EXPERIMENTAL: SupportLevel.UNSUPPORTED,
    EvidenceGrade.UNSUPPORTED: SupportLevel.UNSUPPORTED,
}


class HostCapabilityError(ValueError):
    """A host capability declaration or binding failed closed."""


class HostCapabilityProbe(FrozenModel):
    """One declared check: the only route by which a capability may be claimed.

    ``phases`` is what makes impact phase local.  A probe that names only
    :attr:`HostPhase.PREVIEW` can, when it fails, block nothing else — the
    blast radius is a property of the declaration, not of the caller's care.
    """

    probe_id: str = Field(min_length=3, max_length=96)
    capability: str = Field(min_length=3, max_length=96)
    method: ProbeMethod
    subject: str = Field(min_length=1, max_length=240)
    owner: str = Field(min_length=3, max_length=240)
    phases: frozenset[HostPhase] = frozenset()
    optional: bool = False

    @model_validator(mode="after")
    def _identifiers_are_namespaced(self) -> HostCapabilityProbe:
        if not _PROBE_ID.fullmatch(self.probe_id):
            raise ValueError("probe id must be a namespaced lowercase identifier")
        if not _CAPABILITY.fullmatch(self.capability):
            raise ValueError("capability must be an open namespaced identifier")
        if self.optional and self.phases:
            raise ValueError(
                "an optional capability may not be required by a phase: "
                "its absence must block nothing"
            )
        return self


class ProbeObservation(FrozenModel):
    """One probe result, stamped with the profile it was observed on.

    ``observed_on`` is the anti-inheritance mechanism.  A profile admits an
    observation only when this field equals its own canonical identity, so
    evidence collected on one host cannot be presented as evidence for another.
    """

    probe_id: str = Field(min_length=3, max_length=96)
    outcome: ProbeOutcome
    observed_on: str = Field(min_length=3, max_length=200)
    detail: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def _probe_id_is_namespaced(self) -> ProbeObservation:
        if not _PROBE_ID.fullmatch(self.probe_id):
            raise ValueError("probe id must be a namespaced lowercase identifier")
        return self


class HostCapabilityResult(FrozenModel):
    """A graded capability claim bound to the observation that justifies it."""

    capability: str = Field(min_length=3, max_length=96)
    probe_id: str = Field(min_length=3, max_length=96)
    grade: EvidenceGrade
    observation: ProbeObservation
    detail: str = Field(default="", max_length=400)
    optional: bool = False

    @model_validator(mode="after")
    def _grade_is_earned(self) -> HostCapabilityResult:
        if not _CAPABILITY.fullmatch(self.capability):
            raise ValueError("capability must be an open namespaced identifier")
        if self.observation.probe_id != self.probe_id:
            raise ValueError(
                f"result for probe {self.probe_id} carries an observation of "
                f"{self.observation.probe_id}"
            )
        if self.observation.outcome not in _REQUIRED_OUTCOMES[self.grade]:
            raise ValueError(
                f"grade {self.grade.value} requires a probe outcome in "
                f"{sorted(item.value for item in _REQUIRED_OUTCOMES[self.grade])}, "
                f"but the observation is {self.observation.outcome.value}"
            )
        if self.grade is not EvidenceGrade.SUPPORTED and not self.detail:
            raise ValueError(f"grade {self.grade.value} must state why, in detail")
        return self

    @property
    def advertised(self) -> bool:
        return self.grade in ADVERTISING_GRADES

    def support(self) -> CapabilitySupport:
        """Project into the host support contract the resolver already reads.

        The grade name is carried into ``detail`` so an ``experimental`` result
        is distinguishable from a plain ``unsupported`` one downstream, even
        though both must narrow the effective grant identically.
        """
        detail = self.detail or self.observation.detail
        return CapabilitySupport(
            capability=self.capability,
            level=_SUPPORT_LEVEL[self.grade],
            detail=f"{self.grade.value}: {detail}",
        )


class PhaseRequirement(FrozenModel):
    """The capabilities one phase needs before it may proceed."""

    phase: HostPhase
    capabilities: frozenset[str] = frozenset()


class PhaseImpact(FrozenModel):
    """What a profile's evidence means for exactly one phase."""

    phase: HostPhase
    blocked: bool
    degraded: bool
    missing: tuple[str, ...] = ()
    degraded_capabilities: tuple[str, ...] = ()
    reason: str = ""


def bind_observations(
    probes: tuple[HostCapabilityProbe, ...],
    observations: Iterable[ProbeObservation],
    *,
    profile_id: str,
) -> tuple[ProbeObservation, ...]:
    """Match one observation to every declared probe, or fail closed.

    A probe with no observation is an error rather than a silent default.  That
    is deliberate: a default would let a profile ship with a capability nobody
    ever checked, which is the exact defect this package exists to prevent.
    """
    by_id: dict[str, ProbeObservation] = {}
    for observation in observations:
        if observation.observed_on != profile_id:
            raise HostCapabilityError(
                f"observation for {observation.probe_id} was taken on "
                f"{observation.observed_on!r} and cannot be evidence for {profile_id!r}"
            )
        if observation.probe_id in by_id:
            raise HostCapabilityError(f"probe {observation.probe_id} has more than one observation")
        by_id[observation.probe_id] = observation

    declared = {probe.probe_id for probe in probes}
    missing = sorted(declared - set(by_id))
    if missing:
        raise HostCapabilityError(
            f"{profile_id}: no observation for declared probe(s) {', '.join(missing)}"
        )
    extra = sorted(set(by_id) - declared)
    if extra:
        raise HostCapabilityError(
            f"{profile_id}: observation for undeclared probe(s) {', '.join(extra)}"
        )
    return tuple(by_id[probe.probe_id] for probe in probes)


def phase_impacts(
    advertised: frozenset[str],
    degraded: frozenset[str],
    requirements: Iterable[PhaseRequirement],
) -> tuple[PhaseImpact, ...]:
    """Resolve each phase independently against the advertised ceiling.

    Each phase is answered from its own requirement set and nothing else, which
    is what "phase local" means operationally: there is no path by which one
    phase's missing capability can reach another phase's result.
    """
    impacts: list[PhaseImpact] = []
    for requirement in sorted(requirements, key=lambda item: item.phase.value):
        missing = tuple(sorted(requirement.capabilities - advertised))
        soft = tuple(sorted(requirement.capabilities & degraded))
        if missing:
            reason = "missing required capability: " + ", ".join(missing)
        elif soft:
            reason = "available with reduced capability: " + ", ".join(soft)
        else:
            reason = "every required capability is evidenced"
        impacts.append(
            PhaseImpact(
                phase=requirement.phase,
                blocked=bool(missing),
                degraded=bool(soft) and not missing,
                missing=missing,
                degraded_capabilities=soft,
                reason=reason,
            )
        )
    return tuple(impacts)


def pending_observations(
    probes: tuple[HostCapabilityProbe, ...],
    *,
    profile_id: str,
    detail: str,
) -> tuple[ProbeObservation, ...]:
    """Declare, explicitly, that no probe has been run for a profile.

    This exists so an unevidenced platform can be modelled as a real versioned
    profile without any capability being claimed for it.  The alternative —
    omitting the profile, or shipping it with assumed results — is precisely
    the dishonest option this package refuses.
    """
    return tuple(
        ProbeObservation(
            probe_id=probe.probe_id,
            outcome=ProbeOutcome.NOT_RUN,
            observed_on=profile_id,
            detail=detail,
        )
        for probe in probes
    )


@runtime_checkable
class HostProbeRunner(Protocol):
    """The port a real host adapter implements.

    Core declares what must be checked; something below this port knows how to
    look at a machine.  Keeping the knowledge on that side is what stops a
    platform conditional from entering Core.
    """

    def run(self, probe: HostCapabilityProbe, *, profile_id: str) -> ProbeObservation: ...


def run_probes(
    runner: HostProbeRunner,
    probes: tuple[HostCapabilityProbe, ...],
    *,
    profile_id: str,
) -> tuple[ProbeObservation, ...]:
    """Drive every declared probe through a runner, in declaration order.

    A runner that raises, or that returns an observation for the wrong probe or
    the wrong profile, yields an ``ERROR`` observation rather than being trusted
    or silently skipped — an adapter cannot fail its way into a supported claim.
    """
    results: list[ProbeObservation] = []
    for probe in probes:
        try:
            observation = runner.run(probe, profile_id=profile_id)
        except Exception as exc:
            # An adapter fault is evidence about the host, not a reason to abort
            # the sweep: it becomes an ERROR observation, which cannot advertise.
            results.append(
                ProbeObservation(
                    probe_id=probe.probe_id,
                    outcome=ProbeOutcome.ERROR,
                    observed_on=profile_id,
                    detail=f"probe runner raised {type(exc).__name__}",
                )
            )
            continue
        if observation.probe_id != probe.probe_id or observation.observed_on != profile_id:
            results.append(
                ProbeObservation(
                    probe_id=probe.probe_id,
                    outcome=ProbeOutcome.ERROR,
                    observed_on=profile_id,
                    detail=(
                        "probe runner returned an observation for "
                        f"{observation.probe_id} on {observation.observed_on}"
                    ),
                )
            )
            continue
        results.append(observation)
    return tuple(results)


def grade_observation(
    probe: HostCapabilityProbe,
    observation: ProbeObservation,
    *,
    degraded_capabilities: Mapping[str, str] = {},
    experimental_capabilities: frozenset[str] = frozenset(),
) -> HostCapabilityResult:
    """Grade one observation under the fixed outcome→grade discipline.

    Grading is mechanical on purpose.  ``PRESENT`` earns ``SUPPORTED`` unless
    the caller names a specific, stated reduction; anything else lands on
    ``EXPERIMENTAL`` or ``UNSUPPORTED``.  There is no argument by which a
    caller can promote an absent subject.
    """
    if observation.outcome is ProbeOutcome.PRESENT:
        reduction = degraded_capabilities.get(probe.capability)
        if reduction:
            return HostCapabilityResult(
                capability=probe.capability,
                probe_id=probe.probe_id,
                grade=EvidenceGrade.DEGRADED,
                observation=observation,
                detail=reduction,
                optional=probe.optional,
            )
        return HostCapabilityResult(
            capability=probe.capability,
            probe_id=probe.probe_id,
            grade=EvidenceGrade.SUPPORTED,
            observation=observation,
            detail=observation.detail,
            optional=probe.optional,
        )
    if (
        probe.capability in experimental_capabilities
        and observation.outcome is not ProbeOutcome.ABSENT
    ):
        return HostCapabilityResult(
            capability=probe.capability,
            probe_id=probe.probe_id,
            grade=EvidenceGrade.EXPERIMENTAL,
            observation=observation,
            detail=f"pending evidence: {observation.detail}",
            optional=probe.optional,
        )
    return HostCapabilityResult(
        capability=probe.capability,
        probe_id=probe.probe_id,
        grade=EvidenceGrade.UNSUPPORTED,
        observation=observation,
        detail=observation.detail,
        optional=probe.optional,
    )

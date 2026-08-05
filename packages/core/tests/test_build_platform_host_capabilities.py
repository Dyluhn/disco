"""The rules that make a host capability claim earned rather than asserted.

Every property here is one the package acceptance rests on: an advertised
capability has a probe and an observation; a grade cannot outrun its evidence;
an optional capability blocks nothing; and impact is resolved per phase from
that phase's own requirements.
"""

from __future__ import annotations

import pytest
from disco.core.build_platform.contracts import SupportLevel
from disco.core.build_platform.host_capabilities import (
    ADVERTISING_GRADES,
    EvidenceGrade,
    HostCapabilityError,
    HostCapabilityProbe,
    HostCapabilityResult,
    HostPhase,
    HostProbeRunner,
    PhaseRequirement,
    ProbeMethod,
    ProbeObservation,
    ProbeOutcome,
    bind_observations,
    grade_observation,
    pending_observations,
    phase_impacts,
    run_probes,
)
from pydantic import ValidationError

PROFILE = "host.fixture@1"


def _probe(
    probe_id: str = "host.workspace_read",
    capability: str = "workspace.read",
    phases: frozenset[HostPhase] = frozenset({HostPhase.VERIFY}),
    *,
    optional: bool = False,
) -> HostCapabilityProbe:
    return HostCapabilityProbe(
        probe_id=probe_id,
        capability=capability,
        method=ProbeMethod.FILESYSTEM_ACCESS,
        subject="a workspace file",
        owner="test",
        phases=frozenset() if optional else phases,
        optional=optional,
    )


def _obs(
    outcome: ProbeOutcome,
    probe_id: str = "host.workspace_read",
    observed_on: str = PROFILE,
    detail: str = "fixture observation",
) -> ProbeObservation:
    return ProbeObservation(
        probe_id=probe_id, outcome=outcome, observed_on=observed_on, detail=detail
    )


class TestGradeRequiresEvidence:
    """A grade may never claim more than its observation established."""

    @pytest.mark.parametrize(
        "grade",
        [EvidenceGrade.SUPPORTED, EvidenceGrade.DEGRADED],
    )
    def test_advertising_grade_requires_a_present_observation(self, grade: EvidenceGrade) -> None:
        with pytest.raises(ValidationError, match="requires a probe outcome"):
            HostCapabilityResult(
                capability="workspace.read",
                probe_id="host.workspace_read",
                grade=grade,
                observation=_obs(ProbeOutcome.ABSENT),
                detail="claimed without evidence",
            )

    def test_not_run_cannot_reach_a_supported_grade(self) -> None:
        with pytest.raises(ValidationError, match="requires a probe outcome"):
            HostCapabilityResult(
                capability="workspace.read",
                probe_id="host.workspace_read",
                grade=EvidenceGrade.SUPPORTED,
                observation=_obs(ProbeOutcome.NOT_RUN),
            )

    def test_present_observation_cannot_be_graded_unsupported_by_accident(self) -> None:
        """A PRESENT observation may be graded down, but only with a stated reason."""
        result = HostCapabilityResult(
            capability="workspace.read",
            probe_id="host.workspace_read",
            grade=EvidenceGrade.DEGRADED,
            observation=_obs(ProbeOutcome.PRESENT),
            detail="read-only mount",
        )
        assert result.advertised is True
        assert result.support().level is SupportLevel.DEGRADED

    def test_a_non_supported_grade_must_state_why(self) -> None:
        with pytest.raises(ValidationError, match="must state why"):
            HostCapabilityResult(
                capability="workspace.read",
                probe_id="host.workspace_read",
                grade=EvidenceGrade.UNSUPPORTED,
                observation=_obs(ProbeOutcome.ABSENT),
                detail="",
            )

    def test_result_and_observation_must_name_the_same_probe(self) -> None:
        with pytest.raises(ValidationError, match="carries an observation of"):
            HostCapabilityResult(
                capability="workspace.read",
                probe_id="host.workspace_read",
                grade=EvidenceGrade.SUPPORTED,
                observation=_obs(ProbeOutcome.PRESENT, probe_id="host.other_probe"),
            )

    def test_only_supported_and_degraded_advertise(self) -> None:
        assert ADVERTISING_GRADES == frozenset({EvidenceGrade.SUPPORTED, EvidenceGrade.DEGRADED})
        for grade, outcome in (
            (EvidenceGrade.EXPERIMENTAL, ProbeOutcome.NOT_RUN),
            (EvidenceGrade.UNSUPPORTED, ProbeOutcome.ABSENT),
        ):
            result = HostCapabilityResult(
                capability="workspace.read",
                probe_id="host.workspace_read",
                grade=grade,
                observation=_obs(outcome),
                detail="no evidence on this host",
            )
            assert result.advertised is False

    def test_experimental_narrows_the_grant_but_keeps_its_name(self) -> None:
        """Experimental must not grant, yet must stay distinguishable downstream."""
        result = HostCapabilityResult(
            capability="workspace.read",
            probe_id="host.workspace_read",
            grade=EvidenceGrade.EXPERIMENTAL,
            observation=_obs(ProbeOutcome.NOT_RUN),
            detail="pending evidence on this host",
        )
        support = result.support()
        assert support.level is SupportLevel.UNSUPPORTED
        assert support.detail.startswith("experimental:")


class TestGrading:
    """Grading is mechanical: no argument promotes an absent subject."""

    def test_present_grades_supported(self) -> None:
        result = grade_observation(_probe(), _obs(ProbeOutcome.PRESENT))
        assert result.grade is EvidenceGrade.SUPPORTED

    def test_present_with_a_named_reduction_grades_degraded(self) -> None:
        result = grade_observation(
            _probe(),
            _obs(ProbeOutcome.PRESENT),
            degraded_capabilities={"workspace.read": "read-only mount"},
        )
        assert result.grade is EvidenceGrade.DEGRADED
        assert result.detail == "read-only mount"

    def test_absent_never_becomes_experimental(self) -> None:
        """A probe that ran and found nothing is unsupported, not 'pending'."""
        result = grade_observation(
            _probe(),
            _obs(ProbeOutcome.ABSENT),
            experimental_capabilities=frozenset({"workspace.read"}),
        )
        assert result.grade is EvidenceGrade.UNSUPPORTED

    def test_not_run_may_be_declared_experimental(self) -> None:
        result = grade_observation(
            _probe(),
            _obs(ProbeOutcome.NOT_RUN),
            experimental_capabilities=frozenset({"workspace.read"}),
        )
        assert result.grade is EvidenceGrade.EXPERIMENTAL
        assert result.advertised is False

    def test_not_run_without_a_declaration_is_unsupported(self) -> None:
        result = grade_observation(_probe(), _obs(ProbeOutcome.NOT_RUN))
        assert result.grade is EvidenceGrade.UNSUPPORTED


class TestObservationBinding:
    """Evidence is local to the profile it was taken on."""

    def test_observation_from_another_profile_is_refused(self) -> None:
        with pytest.raises(HostCapabilityError, match="cannot be evidence for"):
            bind_observations(
                (_probe(),),
                [_obs(ProbeOutcome.PRESENT, observed_on="host.macos@1")],
                profile_id=PROFILE,
            )

    def test_a_probe_without_an_observation_is_an_error_not_a_default(self) -> None:
        probes = (_probe(), _probe("host.process_execute", "process.execute"))
        with pytest.raises(HostCapabilityError, match="no observation for declared probe"):
            bind_observations(probes, [_obs(ProbeOutcome.PRESENT)], profile_id=PROFILE)

    def test_an_observation_for_an_undeclared_probe_is_refused(self) -> None:
        with pytest.raises(HostCapabilityError, match="undeclared probe"):
            bind_observations(
                (_probe(),),
                [
                    _obs(ProbeOutcome.PRESENT),
                    _obs(ProbeOutcome.PRESENT, probe_id="host.smuggled"),
                ],
                profile_id=PROFILE,
            )

    def test_duplicate_observations_are_refused(self) -> None:
        with pytest.raises(HostCapabilityError, match="more than one observation"):
            bind_observations(
                (_probe(),),
                [_obs(ProbeOutcome.PRESENT), _obs(ProbeOutcome.ABSENT)],
                profile_id=PROFILE,
            )

    def test_pending_observations_are_all_not_run(self) -> None:
        probes = (_probe(), _probe("host.process_execute", "process.execute"))
        observations = pending_observations(
            probes, profile_id=PROFILE, detail="not probed on this host"
        )
        assert {item.outcome for item in observations} == {ProbeOutcome.NOT_RUN}
        assert {item.observed_on for item in observations} == {PROFILE}


class TestProbeRunnerPort:
    """An adapter fault is evidence, never a route to a supported claim."""

    def test_a_raising_runner_yields_an_error_observation(self) -> None:
        class Exploding:
            def run(self, probe: HostCapabilityProbe, *, profile_id: str) -> ProbeObservation:
                raise RuntimeError("adapter is broken")

        observations = run_probes(Exploding(), (_probe(),), profile_id=PROFILE)
        assert observations[0].outcome is ProbeOutcome.ERROR
        assert "RuntimeError" in observations[0].detail

    def test_a_runner_answering_the_wrong_probe_yields_an_error(self) -> None:
        class Confused:
            def run(self, probe: HostCapabilityProbe, *, profile_id: str) -> ProbeObservation:
                return _obs(ProbeOutcome.PRESENT, probe_id="host.something_else")

        observations = run_probes(Confused(), (_probe(),), profile_id=PROFILE)
        assert observations[0].outcome is ProbeOutcome.ERROR
        assert observations[0].probe_id == "host.workspace_read"

    def test_a_runner_answering_for_another_profile_yields_an_error(self) -> None:
        class Borrowing:
            def run(self, probe: HostCapabilityProbe, *, profile_id: str) -> ProbeObservation:
                return _obs(ProbeOutcome.PRESENT, observed_on="host.macos@1")

        observations = run_probes(Borrowing(), (_probe(),), profile_id=PROFILE)
        assert observations[0].outcome is ProbeOutcome.ERROR
        assert observations[0].observed_on == PROFILE

    def test_an_error_observation_cannot_be_graded_supported(self) -> None:
        class Exploding:
            def run(self, probe: HostCapabilityProbe, *, profile_id: str) -> ProbeObservation:
                raise RuntimeError("adapter is broken")

        observation = run_probes(Exploding(), (_probe(),), profile_id=PROFILE)[0]
        assert grade_observation(_probe(), observation).advertised is False

    def test_the_port_is_structurally_checkable(self) -> None:
        class Minimal:
            def run(self, probe: HostCapabilityProbe, *, profile_id: str) -> ProbeObservation:
                return _obs(ProbeOutcome.PRESENT)

        assert isinstance(Minimal(), HostProbeRunner)


class TestPhaseLocality:
    """A missing capability reaches exactly the phases that declared it."""

    REQUIREMENTS = (
        PhaseRequirement(phase=HostPhase.CONSTRUCT, capabilities=frozenset({"workspace.write"})),
        PhaseRequirement(phase=HostPhase.PREVIEW, capabilities=frozenset({"display.interactive"})),
        PhaseRequirement(
            phase=HostPhase.PACKAGE, capabilities=frozenset({"signing.code_signature"})
        ),
    )

    def test_preview_loss_reaches_preview_alone(self) -> None:
        impacts = phase_impacts(
            frozenset({"workspace.write", "signing.code_signature"}),
            frozenset(),
            self.REQUIREMENTS,
        )
        blocked = {impact.phase for impact in impacts if impact.blocked}
        assert blocked == {HostPhase.PREVIEW}

    def test_signing_loss_reaches_package_alone(self) -> None:
        impacts = phase_impacts(
            frozenset({"workspace.write", "display.interactive"}),
            frozenset(),
            self.REQUIREMENTS,
        )
        blocked = {impact.phase for impact in impacts if impact.blocked}
        assert blocked == {HostPhase.PACKAGE}
        package = next(item for item in impacts if item.phase is HostPhase.PACKAGE)
        assert package.missing == ("signing.code_signature",)

    def test_a_degraded_capability_marks_the_phase_degraded_not_blocked(self) -> None:
        impacts = phase_impacts(
            frozenset({"workspace.write", "display.interactive", "signing.code_signature"}),
            frozenset({"display.interactive"}),
            self.REQUIREMENTS,
        )
        preview = next(item for item in impacts if item.phase is HostPhase.PREVIEW)
        assert preview.blocked is False
        assert preview.degraded is True
        assert preview.degraded_capabilities == ("display.interactive",)

    def test_impacts_are_deterministically_ordered(self) -> None:
        impacts = phase_impacts(frozenset(), frozenset(), reversed(self.REQUIREMENTS))
        assert [item.phase.value for item in impacts] == ["construct", "package", "preview"]

    def test_every_impact_states_a_reason(self) -> None:
        impacts = phase_impacts(frozenset({"workspace.write"}), frozenset(), self.REQUIREMENTS)
        assert all(item.reason for item in impacts)


class TestOptionalCapabilities:
    """An optional capability is optional structurally, not by convention."""

    def test_an_optional_probe_may_not_name_a_phase(self) -> None:
        with pytest.raises(ValidationError, match="may not be required by a phase"):
            HostCapabilityProbe(
                probe_id="host.gvisor_sandbox",
                capability="runtime.gvisor_sandbox",
                method=ProbeMethod.SANDBOX_RUNTIME,
                subject="runsc",
                owner="test",
                phases=frozenset({HostPhase.PACKAGE}),
                optional=True,
            )

    def test_an_absent_optional_capability_blocks_nothing(self) -> None:
        requirements = (
            PhaseRequirement(
                phase=HostPhase.CONSTRUCT, capabilities=frozenset({"workspace.write"})
            ),
        )
        impacts = phase_impacts(frozenset({"workspace.write"}), frozenset(), requirements)
        assert [item.blocked for item in impacts] == [False]

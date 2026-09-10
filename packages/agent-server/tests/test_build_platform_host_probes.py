"""The host-side probe adapter: real evidence, and no route to a false claim.

These tests run on whatever machine executes them. They therefore assert
*properties* of the evidence rather than a specific capability set — with one
exception: the probes must agree with the machine, which is checked by
comparing each probe against an independent look at the same subject.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from disco.agent_server.build_platform_host_probes import (
    HostCapabilityProbeRunner,
    HostProbeMismatch,
    detect_profile_definition,
    probe_host_profile,
    unprobed_host_profile,
)
from disco.core.build_platform.host_capabilities import (
    EvidenceGrade,
    HostProbeRunner,
    ProbeOutcome,
)
from disco.core.build_platform.host_profiles import (
    GVISOR_CAPABILITY,
    HOST_PROFILE_DEFINITIONS,
    LINUX_HOST_V2,
    MACOS_HOST_V1,
    WSL2_HOST_V1,
    HostProfileDefinition,
)


def _detected() -> HostProfileDefinition:
    """The definition describing this machine.

    Deliberately an assertion rather than a skip. A host that matches no
    shipped definition is a real gap in the shipped profile set, and a skipped
    suite would report that gap as success.
    """
    definition = detect_profile_definition()
    assert definition is not None, (
        "this host matches no shipped host profile definition, so the adapter "
        "cannot be exercised here"
    )
    return definition


class TestThePort:
    def test_the_adapter_satisfies_the_core_port(self) -> None:
        assert isinstance(HostCapabilityProbeRunner(), HostProbeRunner)

    def test_detection_returns_a_definition_and_grants_nothing(self) -> None:
        definition = _detected()
        assert isinstance(definition, HostProfileDefinition)
        assert not hasattr(definition, "advertised")

    def test_an_unknown_probe_is_not_run_rather_than_assumed(self) -> None:
        from disco.core.build_platform.host_capabilities import (
            HostCapabilityProbe,
            ProbeMethod,
        )

        probe = HostCapabilityProbe(
            probe_id="host.invented_probe",
            capability="invented.capability",
            method=ProbeMethod.TOOLCHAIN_VERSION,
            subject="nothing this adapter knows",
            owner="test",
        )
        observation = HostCapabilityProbeRunner().run(probe, profile_id="host.linux@2")
        assert observation.outcome is ProbeOutcome.NOT_RUN


class TestEvidenceIsReal:
    """Each probe is cross-checked against an independent look at its subject."""

    def test_this_host_binds_a_profile_from_its_own_probes(self) -> None:
        profile = probe_host_profile(_detected())
        assert len(profile.results) == len(profile.definition.probes)
        assert {result.observation.observed_on for result in profile.results} == {
            profile.id.canonical
        }

    def test_every_advertised_capability_rests_on_a_present_observation(self) -> None:
        profile = probe_host_profile(_detected())
        for capability in sorted(profile.advertised):
            result = profile.result_for(capability)
            assert result is not None
            assert result.observation.outcome is ProbeOutcome.PRESENT

    def test_the_gvisor_probe_agrees_with_the_machine(self) -> None:
        """Advertised exactly when the profile declares it AND runsc is present.

        Stated as one expression covering both branches rather than skipping
        the undeclared case: a profile that never declares the probe must not
        advertise the capability either, which is the stronger claim.
        """
        definition = _detected()
        profile = probe_host_profile(definition)
        declared = GVISOR_CAPABILITY in definition.probed_capabilities
        expected = declared and shutil.which("runsc") is not None
        assert (GVISOR_CAPABILITY in profile.advertised) is expected

    def test_the_node_probe_agrees_with_the_machine(self) -> None:
        profile = probe_host_profile(_detected())
        expected = shutil.which("node") is not None
        assert ("toolchain.node" in profile.advertised) is expected

    def test_the_display_probe_agrees_with_the_environment(self) -> None:
        profile = probe_host_profile(_detected())
        expected = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        assert ("display.interactive" in profile.advertised) is expected

    def test_the_egress_probe_is_off_by_default_and_says_so(self) -> None:
        profile = probe_host_profile(_detected())
        result = profile.result_for("network.egress")
        assert result is not None
        assert result.observation.outcome is ProbeOutcome.NOT_RUN
        assert "disabled" in result.observation.detail
        assert result.advertised is False

    def test_a_missing_tool_reports_absent_not_supported(self, tmp_path: Path) -> None:
        """With nothing on PATH, tool probes must fall to absent, never supported."""
        definition = _detected()
        runner = HostCapabilityProbeRunner(workspace_root=tmp_path)
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        previous = os.environ.get("PATH")
        os.environ["PATH"] = str(empty)
        try:
            probe = next(
                item for item in definition.probes if item.probe_id == "host.toolchain_node"
            )
            observation = runner.run(probe, profile_id=definition.id.canonical)
        finally:
            if previous is None:
                del os.environ["PATH"]
            else:
                os.environ["PATH"] = previous
        assert observation.outcome is ProbeOutcome.ABSENT
        assert "no Node.js toolchain on PATH" in observation.detail

    def test_an_unwritable_root_reports_absent(self, tmp_path: Path) -> None:
        runner = HostCapabilityProbeRunner(workspace_root=tmp_path / "does-not-exist")
        definition = _detected()
        probe = next(item for item in definition.probes if item.probe_id == "host.workspace_write")
        observation = runner.run(probe, profile_id=definition.id.canonical)
        assert observation.outcome is ProbeOutcome.ABSENT


class TestMisdetectionCannotProduceAFalseClaim:
    """The guard that keeps genuine observations from wearing a false label."""

    def test_evidencing_a_foreign_platform_is_refused(self) -> None:
        definition = _detected()
        foreign = next(
            item
            for item in (LINUX_HOST_V2, WSL2_HOST_V1, MACOS_HOST_V1)
            if item.id.name != definition.id.name
        )
        with pytest.raises(HostProbeMismatch, match="not evidence for another platform"):
            probe_host_profile(foreign)

    def test_every_version_in_this_hosts_lineage_may_be_evidenced(self) -> None:
        """The refusal is per lineage, not per exact version.

        Written over whatever lineage this host is in, so it asserts something
        on every machine instead of skipping off its home platform. An older
        version in the lineage must bind, and must advertise only what it
        itself declares a probe for.
        """
        definition = _detected()
        assert probe_host_profile(definition).id == definition.id
        siblings = [
            item
            for item in HOST_PROFILE_DEFINITIONS
            if item.id.name == definition.id.name and item.id.canonical != definition.id.canonical
        ]
        for sibling in siblings:
            profile = probe_host_profile(sibling)
            assert profile.id == sibling.id
            assert profile.advertised <= sibling.probed_capabilities

    def test_an_unprobed_platform_is_modelled_explicitly_and_claims_nothing(self) -> None:
        definition = _detected()
        foreign = next(
            item
            for item in (LINUX_HOST_V2, WSL2_HOST_V1, MACOS_HOST_V1)
            if item.id.name != definition.id.name
        )
        profile = unprobed_host_profile(
            foreign, reason="not probed: this host is not that platform"
        )
        assert profile.advertised == frozenset()
        assert profile.experimental == foreign.probed_capabilities
        assert {result.grade for result in profile.results} == {EvidenceGrade.EXPERIMENTAL}
        assert {result.observation.outcome for result in profile.results} == {ProbeOutcome.NOT_RUN}

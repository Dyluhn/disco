"""Host profiles: truthfulness, phase-locality, optionality and compatibility.

This file holds the package's acceptance properties. The load-bearing ones are
proved through *resolved production* — the real
:func:`~disco.core.build_platform.resolver.resolve_build_composition` driving the
persistent non-web conformance target — rather than against the host profile
records in isolation, because the claim being made is about what a build
actually does with a host's evidence.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from disco.core.build_platform import (
    SYNTHETIC_PROFILE_ID,
    CapabilityLayer,
    ComponentId,
    PolicyLayer,
    SupportLevel,
    build_synthetic_registry,
)
from disco.core.build_platform.compiler import PromptContextInputs
from disco.core.build_platform.host_capabilities import (
    EvidenceGrade,
    HostCapabilityError,
    HostCapabilityProbe,
    HostPhase,
    PhaseRequirement,
    ProbeMethod,
    ProbeObservation,
    ProbeOutcome,
    pending_observations,
)
from disco.core.build_platform.host_profiles import (
    GVISOR_CAPABILITY,
    HOST_PROFILE_DEFINITIONS,
    LINUX_HOST_V1,
    LINUX_HOST_V2,
    MACOS_HOST_V1,
    WSL2_HOST_V1,
    CompatibilityStatus,
    HostProfile,
    HostProfileDefinition,
    HostProfileError,
    HostProfileRegistry,
    definition_for,
)
from disco.core.build_platform.resolver import (
    BuildComposition,
    ResolutionInputs,
    resolve_build_composition,
)
from pydantic import ValidationError

# --------------------------------------------------------------------------
# fixtures: hypothetical host instances, each bound from its own observations
# --------------------------------------------------------------------------

#: Capabilities a fully-equipped host evidences. Used to build hypothetical
#: instances of a profile; the *real* evidence for this machine is gathered by
#: the adapter's probe sweep, never hardcoded here.
_ALL_PRESENT = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "process.execute",
        "network.egress",
        "display.interactive",
        "toolchain.node",
        "signing.code_signature",
        "container.oci_runtime",
        GVISOR_CAPABILITY,
    }
)


def _observations(
    definition: HostProfileDefinition,
    present: frozenset[str],
) -> tuple[ProbeObservation, ...]:
    """Fixture observations for a hypothetical instance of *definition*."""
    return tuple(
        ProbeObservation(
            probe_id=probe.probe_id,
            outcome=(ProbeOutcome.PRESENT if probe.capability in present else ProbeOutcome.ABSENT),
            observed_on=definition.id.canonical,
            detail=f"fixture instance: {probe.capability} "
            + ("present" if probe.capability in present else "absent"),
        )
        for probe in definition.probes
    )


def _bind(
    definition: HostProfileDefinition,
    present: frozenset[str] = _ALL_PRESENT,
) -> HostProfile:
    return definition.bind(_observations(definition, present))


def _resolve_with(profile: HostProfile) -> BuildComposition:
    """Drive the persistent non-web target with this host profile's evidence."""
    ceiling = CapabilityLayer(
        source="ceiling",
        allowed=frozenset({"workspace.read", "workspace.write", "process.execute"}),
    )
    return resolve_build_composition(
        build_synthetic_registry(),
        ResolutionInputs(
            profile=SYNTHETIC_PROFILE_ID,
            goal="transform a fixture into a packaged batch-job result",
            platform_capabilities=ceiling.model_copy(update={"source": "platform"}),
            user_capabilities=ceiling.model_copy(update={"source": "user"}),
            host_capabilities=profile.capability_layer(),
            host_support=profile.support(),
            platform_policy=PolicyLayer(source="platform"),
            user_policy=PolicyLayer(source="user"),
            prompt_context=PromptContextInputs(),
        ),
    )


# --------------------------------------------------------------------------


class TestCoreShipsNoPlatformKnowledge:
    """Below host ports, never a Core conditional."""

    def test_core_build_platform_contains_no_platform_conditional(self) -> None:
        """No module in the Build Platform Core may inspect the operating system.

        Scanned with the AST rather than by text so a match is a real
        reference, not a mention inside a docstring.
        """
        import disco.core.build_platform as build_platform

        root = Path(str(build_platform.__file__)).parent
        banned_modules = {"platform"}
        banned_attributes = {
            ("sys", "platform"),
            ("os", "name"),
            ("os", "uname"),
            ("platform", "system"),
            ("platform", "release"),
            ("platform", "machine"),
            ("platform", "uname"),
        }
        offences: list[str] = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in banned_modules:
                            offences.append(f"{path.name}:{node.lineno} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").split(".")[0] in banned_modules:
                        offences.append(f"{path.name}:{node.lineno} imports from {node.module}")
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if (node.value.id, node.attr) in banned_attributes:
                        offences.append(
                            f"{path.name}:{node.lineno} reads {node.value.id}.{node.attr}"
                        )
        assert offences == [], "platform knowledge leaked into Core: " + "; ".join(offences)

    def test_core_never_branches_on_an_operating_system_name(self) -> None:
        """OS names may appear as data (labels); never inside a comparison."""
        import disco.core.build_platform as build_platform

        root = Path(str(build_platform.__file__)).parent
        os_names = {"linux", "darwin", "windows", "macos", "wsl", "wsl2", "win32", "posix"}
        offences: list[str] = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                for operand in [node.left, *node.comparators]:
                    if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                        if operand.value.strip().lower() in os_names:
                            offences.append(
                                f"{path.name}:{node.lineno} compares to {operand.value!r}"
                            )
        assert offences == [], "an OS-name switch entered Core: " + "; ".join(offences)

    def test_core_ships_definitions_not_bound_profiles(self) -> None:
        """Importing Core grants no capability, because nothing shipped is bound."""
        assert all(isinstance(item, HostProfileDefinition) for item in HOST_PROFILE_DEFINITIONS)
        assert not any(isinstance(item, HostProfile) for item in HOST_PROFILE_DEFINITIONS)

    def test_every_shipped_definition_is_versioned_and_host_namespaced(self) -> None:
        for definition in HOST_PROFILE_DEFINITIONS:
            assert definition.id.namespace == "host"
            assert definition.id.version

    def test_the_three_named_platforms_are_all_shipped(self) -> None:
        names = {definition.id.name for definition in HOST_PROFILE_DEFINITIONS}
        assert {"linux", "wsl2", "macos"} <= names


class TestAnOsLabelCannotAdvertise:
    """A profile cannot advertise from an OS name alone."""

    def test_a_profile_with_no_evidence_advertises_nothing(self) -> None:
        for definition in (WSL2_HOST_V1, MACOS_HOST_V1):
            profile = definition.bind(
                pending_observations(
                    definition.probes,
                    profile_id=definition.id.canonical,
                    detail="not probed: this host is not that platform",
                )
            )
            assert profile.advertised == frozenset()
            assert profile.capability_layer().allowed == frozenset()

    def test_unevidenced_platforms_may_be_declared_experimental_and_still_grant_nothing(
        self,
    ) -> None:
        definition = MACOS_HOST_V1
        profile = definition.bind(
            pending_observations(
                definition.probes,
                profile_id=definition.id.canonical,
                detail="no macOS host has been probed",
            ),
            experimental=definition.probed_capabilities,
        )
        assert profile.experimental == definition.probed_capabilities
        assert profile.advertised == frozenset()
        assert {item.level for item in profile.support()} == {SupportLevel.UNSUPPORTED}

    def test_the_same_os_label_with_different_evidence_advertises_differently(self) -> None:
        """os_label is documentation: identical labels, different evidence, different result."""
        rich = _bind(LINUX_HOST_V1, _ALL_PRESENT)
        poor = _bind(LINUX_HOST_V1, frozenset({"workspace.read"}))
        assert rich.definition.os_label == poor.definition.os_label
        assert rich.advertised != poor.advertised
        assert poor.advertised == frozenset({"workspace.read"})

    def test_evidence_gathered_for_one_profile_cannot_advertise_another(self) -> None:
        """The anti-inheritance property: macOS may not wear Linux's evidence."""
        linux_observations = _observations(LINUX_HOST_V1, _ALL_PRESENT)
        with pytest.raises(HostCapabilityError, match="cannot be evidence for"):
            MACOS_HOST_V1.bind(linux_observations)

    def test_a_hand_built_profile_cannot_smuggle_foreign_evidence(self) -> None:
        """Bypassing bind() does not bypass the locality rule."""
        source = _bind(LINUX_HOST_V1, _ALL_PRESENT)
        with pytest.raises(ValidationError, match="rests on evidence observed on"):
            HostProfile(definition=MACOS_HOST_V1, results=source.results)

    def test_a_profile_missing_a_graded_probe_is_refused(self) -> None:
        source = _bind(LINUX_HOST_V1, _ALL_PRESENT)
        with pytest.raises(ValidationError, match="no graded result"):
            HostProfile(definition=LINUX_HOST_V1, results=source.results[:-1])

    def test_every_advertised_capability_names_its_probe_and_observation(self) -> None:
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT)
        declared = {probe.probe_id for probe in LINUX_HOST_V2.probes}
        for capability in sorted(profile.advertised):
            result = profile.result_for(capability)
            assert result is not None
            assert result.probe_id in declared
            assert result.observation.outcome is ProbeOutcome.PRESENT
            assert result.observation.observed_on == LINUX_HOST_V2.id.canonical


class TestDefinitionDiscipline:
    """One capability, one probe, one owner; no phase requires the unprobed."""

    def test_a_capability_may_not_have_two_probes(self) -> None:
        probe = HostCapabilityProbe(
            probe_id="host.first",
            capability="workspace.read",
            method=ProbeMethod.FILESYSTEM_ACCESS,
            subject="a file",
            owner="test",
        )
        with pytest.raises(ValidationError, match="no single owner"):
            HostProfileDefinition(
                id=ComponentId(namespace="host", name="fixture", version="1"),
                os_label="Fixture",
                summary="two probes, one capability",
                probes=(probe, probe.model_copy(update={"probe_id": "host.second"})),
            )

    def test_a_phase_may_not_require_an_unprobed_capability(self) -> None:
        with pytest.raises(ValidationError, match="no declared probe"):
            HostProfileDefinition(
                id=ComponentId(namespace="host", name="fixture", version="1"),
                os_label="Fixture",
                summary="requires what it never checks",
                probes=(),
                phase_requirements=(
                    PhaseRequirement(
                        phase=HostPhase.PACKAGE,
                        capabilities=frozenset({"signing.code_signature"}),
                    ),
                ),
            )

    def test_a_definition_must_be_host_namespaced(self) -> None:
        with pytest.raises(ValidationError, match="host.* namespace"):
            HostProfileDefinition(
                id=ComponentId(namespace="synthetic", name="fixture", version="1"),
                os_label="Fixture",
                summary="wrong namespace",
                probes=(),
            )

    def test_definition_lookup_is_by_identity_only(self) -> None:
        assert definition_for(LINUX_HOST_V2.id) is LINUX_HOST_V2
        assert definition_for(ComponentId(namespace="host", name="linux", version="99")) is None


class TestGvisorIsOptional:
    """gVisor is an optional Linux runtime capability, not a universal sandbox."""

    def test_gvisor_is_declared_only_by_the_linux_lineage(self) -> None:
        declaring = {
            definition.id.canonical
            for definition in HOST_PROFILE_DEFINITIONS
            if GVISOR_CAPABILITY in definition.probed_capabilities
        }
        assert declaring == {LINUX_HOST_V2.id.canonical}
        assert GVISOR_CAPABILITY not in WSL2_HOST_V1.probed_capabilities
        assert GVISOR_CAPABILITY not in MACOS_HOST_V1.probed_capabilities

    def test_gvisor_is_declared_optional(self) -> None:
        assert GVISOR_CAPABILITY in LINUX_HOST_V2.optional_capabilities

    def test_gvisor_absence_blocks_no_phase(self) -> None:
        without = _bind(LINUX_HOST_V2, _ALL_PRESENT - {GVISOR_CAPABILITY})
        assert GVISOR_CAPABILITY not in without.advertised
        assert [impact.phase for impact in without.phase_impacts() if impact.blocked] == []

    def test_gvisor_presence_changes_no_phase_result(self) -> None:
        """Optional means the phase answer is identical either way."""
        with_gvisor = _bind(LINUX_HOST_V2, _ALL_PRESENT)
        without = _bind(LINUX_HOST_V2, _ALL_PRESENT - {GVISOR_CAPABILITY})
        assert with_gvisor.phase_impacts() == without.phase_impacts()

    def test_gvisor_is_never_ambient_across_profiles(self) -> None:
        """No profile inherits it: a profile that never probed it cannot advertise it."""
        for definition in (LINUX_HOST_V1, WSL2_HOST_V1, MACOS_HOST_V1):
            profile = _bind(definition, _ALL_PRESENT)
            assert GVISOR_CAPABILITY not in profile.advertised

    def test_an_evidenced_gvisor_host_advertises_it_as_optional(self) -> None:
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT)
        assert GVISOR_CAPABILITY in profile.advertised_optional


class TestIntersectionOnlyNarrows:
    """A host profile is one more ceiling; it can never widen the grant."""

    def test_a_host_profile_cannot_widen_the_effective_grant(self) -> None:
        """Even a host advertising everything cannot exceed the other layers."""
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT)
        composition = _resolve_with(profile)
        assert composition.effective_capabilities.allowed <= frozenset(
            {"workspace.read", "workspace.write", "process.execute"}
        )
        assert GVISOR_CAPABILITY not in composition.effective_capabilities.allowed
        assert "toolchain.node" not in composition.effective_capabilities.allowed

    def test_a_narrower_host_narrows_the_grant_further(self) -> None:
        rich = _resolve_with(_bind(LINUX_HOST_V2, _ALL_PRESENT))
        poor = _resolve_with(_bind(LINUX_HOST_V2, _ALL_PRESENT - {"process.execute"}))
        assert poor.effective_capabilities.allowed < rich.effective_capabilities.allowed

    def test_adding_host_evidence_is_monotone_across_every_profile(self) -> None:
        for definition in HOST_PROFILE_DEFINITIONS:
            full = _bind(definition, _ALL_PRESENT)
            partial = _bind(definition, frozenset({"workspace.read"}))
            assert partial.advertised <= full.advertised

    def test_an_unadvertised_capability_is_denied_with_its_named_grade(self) -> None:
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT - {"process.execute"})
        layer = profile.capability_layer()
        denial = next(item for item in layer.denials if item.name == "process.execute")
        assert isinstance(denial.value, str)
        assert denial.value.startswith("unsupported:")


class TestPhaseLocalityThroughResolvedProduction:
    """Degradation is phase local, proved against the persistent non-web target."""

    def test_a_fully_evidenced_host_leaves_the_non_web_target_green(self) -> None:
        composition = _resolve_with(_bind(LINUX_HOST_V2, _ALL_PRESENT))
        assert composition.blocked_operations == ()

    def test_preview_capability_loss_blocks_preview_alone(self) -> None:
        """The non-web target needs process.execute only for preview."""
        composition = _resolve_with(_bind(LINUX_HOST_V2, _ALL_PRESENT - {"process.execute"}))
        blocked = {block.operation for block in composition.blocked_operations}
        assert blocked == {"preview"}
        assert composition.blocked("preview") is True
        for phase in ("construct", "target", "verify", "package"):
            assert composition.blocked(phase) is False

    def test_the_preview_block_names_the_missing_capability(self) -> None:
        composition = _resolve_with(_bind(LINUX_HOST_V2, _ALL_PRESENT - {"process.execute"}))
        block = next(item for item in composition.blocked_operations if item.operation == "preview")
        assert block.code == "capability_denied"
        assert block.missing_capabilities == ("process.execute",)

    def test_the_host_support_statement_reaches_the_composition(self) -> None:
        composition = _resolve_with(_bind(LINUX_HOST_V2, _ALL_PRESENT - {"process.execute"}))
        denial = next(
            item
            for item in composition.effective_capabilities.denied
            if item.capability == "process.execute"
        )
        assert "host-support" in denial.denied_by
        assert "unsupported:" in denial.reason

    def test_each_capability_blocks_exactly_the_phases_that_declare_it(self) -> None:
        """The locality property, proved for every capability rather than one.

        Removing a single capability must reach precisely the phases whose
        requirement set names it — no more (that would be leakage) and no less
        (that would be a phase silently proceeding without something it said it
        needed). Optional capabilities appear in no requirement set, so this
        also proves their absence blocks nothing.
        """
        for capability in sorted(LINUX_HOST_V2.probed_capabilities):
            profile = _bind(LINUX_HOST_V2, _ALL_PRESENT - {capability})
            blocked = {impact.phase for impact in profile.phase_impacts() if impact.blocked}
            declaring = {
                requirement.phase
                for requirement in LINUX_HOST_V2.phase_requirements
                if capability in requirement.capabilities
            }
            assert blocked == declaring, f"{capability} reached the wrong phases"

    def test_the_host_and_target_phase_models_are_separately_scoped(self) -> None:
        """Two different questions, and neither answer leaks into the other.

        The host declares which phases *it* considers dependent on a
        capability; a target's plan declares what *this build* actually needs.
        They are not required to coincide, and here they do not: the host
        models ``process.execute`` as a construct/package dependency, while the
        non-web target needs it only for preview. The resolver composes the
        two, and its answer is the authoritative one for a build.
        """
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT - {"process.execute"})
        host_affected = {impact.phase.value for impact in profile.phase_impacts() if impact.blocked}
        composition = _resolve_with(profile)
        resolver_blocked = {
            block.operation
            for block in composition.blocked_operations
            if block.code == "capability_denied"
        }
        assert host_affected == {"construct", "package"}
        assert resolver_blocked == {"preview"}
        for block in composition.blocked_operations:
            assert block.missing_capabilities == ("process.execute",)

    def test_an_unevidenced_platform_blocks_every_phase_it_declares(self) -> None:
        """Honest consequence: claiming nothing means being able to do nothing."""
        profile = MACOS_HOST_V1.bind(
            pending_observations(
                MACOS_HOST_V1.probes,
                profile_id=MACOS_HOST_V1.id.canonical,
                detail="no macOS host has been probed",
            )
        )
        composition = _resolve_with(profile)
        blocked = {block.operation for block in composition.blocked_operations}
        assert blocked == {"construct", "target", "preview", "verify", "package"}


class TestPersistedProfileCompatibility:
    """Persisted profile IDs remain resolvable, and are never reinterpreted."""

    @staticmethod
    def _registry() -> HostProfileRegistry:
        registry = HostProfileRegistry()
        registry.register(_bind(LINUX_HOST_V1, _ALL_PRESENT))
        registry.register(_bind(LINUX_HOST_V2, _ALL_PRESENT))
        registry.supersede(
            LINUX_HOST_V1.id,
            LINUX_HOST_V2.id,
            detail="v2 adds the optional gVisor runtime probe",
        )
        return registry

    def test_a_live_identity_resolves_to_itself(self) -> None:
        descriptor = self._registry().resolve_persisted(LINUX_HOST_V2.id)
        assert descriptor.status is CompatibilityStatus.LIVE
        assert descriptor.resolved_to is None
        assert descriptor.selectable is True

    def test_a_superseded_identity_names_its_successor(self) -> None:
        descriptor = self._registry().resolve_persisted(LINUX_HOST_V1.id)
        assert descriptor.status is CompatibilityStatus.SUPERSEDED
        assert descriptor.resolved_to == LINUX_HOST_V2.id
        assert descriptor.selectable is True

    def test_a_superseded_identity_still_returns_its_own_evidence(self) -> None:
        """The load-bearing negative: an old ID is never swapped for a new one."""
        registry = self._registry()
        selected = registry.select(LINUX_HOST_V1.id)
        assert selected.id == LINUX_HOST_V1.id
        assert GVISOR_CAPABILITY not in selected.definition.probed_capabilities
        assert selected.advertised != registry.select(LINUX_HOST_V2.id).advertised

    def test_a_retired_identity_is_refused_for_selection(self) -> None:
        registry = self._registry()
        registry.retire(
            LINUX_HOST_V1.id, successor=LINUX_HOST_V2.id, detail="withdrawn from selection"
        )
        with pytest.raises(HostProfileError, match="is retired"):
            registry.select(LINUX_HOST_V1.id)

    def test_a_retired_identity_still_resolves_to_a_descriptor(self) -> None:
        registry = self._registry()
        registry.retire(
            LINUX_HOST_V1.id, successor=LINUX_HOST_V2.id, detail="withdrawn from selection"
        )
        descriptor = registry.resolve_persisted(LINUX_HOST_V1.id)
        assert descriptor.status is CompatibilityStatus.RETIRED
        assert descriptor.selectable is False
        assert descriptor.resolved_to == LINUX_HOST_V2.id

    def test_an_unregistered_identity_fails_closed(self) -> None:
        with pytest.raises(HostProfileError, match="no compatibility descriptor"):
            self._registry().resolve_persisted(MACOS_HOST_V1.id)

    def test_rollback_selects_the_prior_supported_profile(self) -> None:
        prior = self._registry().prior_supported(LINUX_HOST_V2.id)
        assert prior is not None
        assert prior.id == LINUX_HOST_V1.id

    def test_rollback_skips_a_retired_prior(self) -> None:
        registry = self._registry()
        registry.retire(
            LINUX_HOST_V1.id, successor=LINUX_HOST_V2.id, detail="withdrawn from selection"
        )
        assert registry.prior_supported(LINUX_HOST_V2.id) is None

    def test_the_oldest_version_has_no_prior(self) -> None:
        assert self._registry().prior_supported(LINUX_HOST_V1.id) is None

    def test_supersession_stays_within_a_lineage(self) -> None:
        registry = self._registry()
        registry.register(
            MACOS_HOST_V1.bind(
                pending_observations(
                    MACOS_HOST_V1.probes,
                    profile_id=MACOS_HOST_V1.id.canonical,
                    detail="unprobed",
                )
            )
        )
        with pytest.raises(HostProfileError, match="own lineage"):
            registry.supersede(MACOS_HOST_V1.id, LINUX_HOST_V2.id, detail="wrong lineage")

    def test_supersession_must_move_forward(self) -> None:
        registry = self._registry()
        with pytest.raises(HostProfileError, match="later version"):
            registry.supersede(LINUX_HOST_V2.id, LINUX_HOST_V1.id, detail="backwards")

    def test_a_profile_registers_once(self) -> None:
        registry = self._registry()
        with pytest.raises(HostProfileError, match="already registered"):
            registry.register(_bind(LINUX_HOST_V1, _ALL_PRESENT))

    def test_selectable_ids_exclude_only_retired_profiles(self) -> None:
        registry = self._registry()
        assert registry.selectable_ids() == ("host.linux@1", "host.linux@2")
        registry.retire(
            LINUX_HOST_V1.id, successor=LINUX_HOST_V2.id, detail="withdrawn from selection"
        )
        assert registry.selectable_ids() == ("host.linux@2",)
        assert registry.registered_ids() == ("host.linux@1", "host.linux@2")


class TestCapabilityMatrixEvidence:
    """The retained artifacts the playbook's evidence clause names."""

    def test_the_matrix_names_capability_grade_probe_and_outcome(self) -> None:
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT - {GVISOR_CAPABILITY})
        matrix = dict(
            (capability, (grade, probe, outcome))
            for capability, grade, probe, outcome in profile.capability_matrix()
        )
        assert matrix[GVISOR_CAPABILITY] == (
            "unsupported",
            "host.gvisor_sandbox",
            "absent",
        )
        assert matrix["workspace.read"][0] == "supported"

    def test_the_matrix_covers_every_declared_probe(self) -> None:
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT)
        assert len(profile.capability_matrix()) == len(LINUX_HOST_V2.probes)

    def test_support_statements_are_explicit_for_every_capability(self) -> None:
        profile = _bind(LINUX_HOST_V2, _ALL_PRESENT - {"network.egress"})
        support = {item.capability: item for item in profile.support()}
        assert set(support) == LINUX_HOST_V2.probed_capabilities
        assert support["network.egress"].level is SupportLevel.UNSUPPORTED
        assert support["network.egress"].detail.startswith("unsupported:")

    def test_a_degraded_result_is_explicit_in_the_matrix(self) -> None:
        profile = LINUX_HOST_V2.bind(
            _observations(LINUX_HOST_V2, _ALL_PRESENT),
            degraded={"display.interactive": "software rendering only"},
        )
        assert profile.degraded == frozenset({"display.interactive"})
        result = profile.result_for("display.interactive")
        assert result is not None
        assert result.grade is EvidenceGrade.DEGRADED
        assert result.support().level is SupportLevel.DEGRADED

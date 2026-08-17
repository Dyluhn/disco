"""The mobile-shaped registration proof.

**Expo and React Native are not supported.**  These tests assert that the
ordinary public registry and lifecycle accommodate a mobile-*shaped* target —
opaque artifact, no preview at all, target-specific verification, packaging with
no served directory — and nothing more than that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from build_platform_registration_shapes import (
    MOBILE_ENGINE_ID,
    MOBILE_EXPORTER_ID,
    MOBILE_OPERATIONS,
    MOBILE_PREVIEW_ID,
    MOBILE_PROFILE_ID,
    MOBILE_TARGET_ID,
    MOBILE_TOOLCHAIN_CAPABILITY,
    MOBILE_TOOLCHAIN_HOST,
    MOBILE_VERIFIER_ID,
    SHAPE_CAPABILITIES,
    BundleVerificationError,
    ShapeHost,
    build_registration_registry,
    register_mobile_shape,
    register_variant,
    resolve_shape,
    run_lifecycle,
    unevidenced_toolchain_profile,
)
from disco.core.build_platform import (
    BuildPlatformRegistry,
    ComponentId,
    ComponentIntent,
    DeliveryIntent,
    EntryDescriptor,
    PreviewPlan,
    PreviewPolicy,
    ReadinessSignal,
    TargetPlan,
    VerifierCheck,
    VerifierPlan,
)
from disco.core.build_platform.host_capabilities import (
    EvidenceGrade,
    HostCapabilityError,
    HostCapabilityResult,
    ProbeObservation,
    ProbeOutcome,
)
from disco.core.verification import VerificationClaimKind

#: Tokens that would betray a web assumption anywhere in the resolved
#: composition.  ``port_listening`` and ``served`` are included because a
#: packaging step that needed a served directory would have to say so somewhere.
_WEB_TOKENS = (
    "http",
    "browser",
    "index.html",
    "port_listening",
    "localhost",
    "served",
    "webview",
    "devserver",
)


def test_mobile_shape_registers_through_the_ordinary_public_registry() -> None:
    registry = register_mobile_shape(BuildPlatformRegistry())

    profile = registry.profiles.get(MOBILE_PROFILE_ID)
    assert profile is not None
    assert profile.id == MOBILE_PROFILE_ID
    assert profile.engine == MOBILE_ENGINE_ID
    assert profile.target == MOBILE_TARGET_ID
    assert profile.exporter == MOBILE_EXPORTER_ID
    assert [choice.id for choice in registry.profiles.choices()] == [MOBILE_PROFILE_ID]

    assert registry.components.engine(MOBILE_ENGINE_ID) is not None
    assert registry.components.target(MOBILE_TARGET_ID) is not None
    assert registry.components.exporter(MOBILE_EXPORTER_ID) is not None
    for component_id in (MOBILE_VERIFIER_ID, MOBILE_PREVIEW_ID):
        spec = registry.components.spec(component_id)
        assert spec is not None and spec.id == component_id

    # The registry is exact: no nearest-name, no cross-kind fallback.
    unknown = ComponentId(namespace="proof", name="unknown", version="1")
    assert registry.profiles.get(unknown) is None
    assert registry.components.spec(unknown) is None
    assert registry.profiles.get(MOBILE_TARGET_ID) is None
    assert registry.components.engine(MOBILE_TARGET_ID) is None
    assert registry.components.target(MOBILE_ENGINE_ID) is None
    assert registry.components.connector(MOBILE_TARGET_ID) is None


def test_mobile_composition_is_opaque_non_web_and_previews_not_at_all() -> None:
    composition = resolve_shape(MOBILE_PROFILE_ID)

    assert composition.profile.id == MOBILE_PROFILE_ID
    assert composition.blocked_operations == ()
    assert composition.target_plan.delivery.shape == "bundle.mobile_app"
    assert composition.target_plan.delivery.mode == "artifact"
    assert composition.target_plan.delivery.entry.kind == "bundle_manifest"
    assert composition.target_plan.delivery.entry.reference == "dist/mobile/app.bundle.manifest"

    # Preview is `none`, and a `none` modality carries no work of any kind.
    assert composition.target_plan.preview.modality == "none"
    assert composition.target_plan.preview.policy.required is False
    assert composition.target_plan.preview.entry is None
    assert composition.target_plan.preview.readiness == ()
    assert composition.target_plan.preview.intents == ()
    assert composition.target_plan.preview.required_capabilities == frozenset()

    serialized = composition.model_dump_json().casefold()
    for forbidden in _WEB_TOKENS:
        assert forbidden not in serialized


def test_mobile_packaging_needs_no_served_directory_or_network() -> None:
    composition = resolve_shape(MOBILE_PROFILE_ID)
    package = composition.target_plan.package
    assert package is not None
    assert package.package_shape == "bundle.mobile_archive"

    # Packaging reads the workspace and nothing else: no egress, no display, no
    # process spawn — so there is no directory being served and nothing to serve it.
    required = frozenset(
        capability for intent in package.intents for capability in intent.required_capabilities
    )
    assert required == frozenset({"workspace.read"})
    assert "network.egress" not in composition.required_runtime_capabilities
    assert "display.interactive" not in composition.required_runtime_capabilities


def test_mobile_lifecycle_composes_verifies_and_packages_synthetically(tmp_path: Path) -> None:
    composition = resolve_shape(MOBILE_PROFILE_ID)
    host = ShapeHost(tmp_path)

    run_lifecycle(composition, host)

    assert tuple(host.executed) == MOBILE_OPERATIONS
    # `none` preview means the preview step does not merely no-op: it does not exist.
    assert "mobile.preview_manifest" not in host.executed
    assert host.preview_result is None
    assert host.package_digest is not None and host.package_digest.startswith("sha256:")

    manifest_path = tmp_path / "dist" / "mobile" / "app.bundle.manifest"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["entry"] == "app.payload"
    assert not list(tmp_path.rglob("index.html"))


def test_mobile_verifier_is_target_specific_and_rejects_a_corrupted_bundle(
    tmp_path: Path,
) -> None:
    composition = resolve_shape(MOBILE_PROFILE_ID)
    (check,) = composition.target_plan.verifier.checks
    assert check.issuer == MOBILE_VERIFIER_ID
    assert check.receipt_kind == "proof.mobile_bundle@1"
    assert check.required_execution_modality == "workspace_artifact"
    assert check.accepted_claim_kinds == frozenset({VerificationClaimKind.TARGET_SPECIFIC})
    assert [claim.kind for claim in check.claims] == [VerificationClaimKind.TARGET_SPECIFIC]

    host = ShapeHost(tmp_path)
    for intent in composition.construction.intents:
        host.execute(intent)
    for intent in composition.target_plan.intents:
        host.execute(intent)
    (tmp_path / "dist" / "mobile" / "app.payload").write_bytes(b"tampered")

    with pytest.raises(BundleVerificationError):
        host.execute(check.intent)
    assert host.package_digest is None


def test_mobile_preview_none_cannot_smuggle_in_preview_work() -> None:
    """A `none` modality that declares entry/readiness is unrepresentable."""
    entry = EntryDescriptor(kind="bundle_manifest", reference="dist/mobile/app.bundle.manifest")
    with pytest.raises(ValueError):
        PreviewPlan(modality="none", entry=entry)
    with pytest.raises(ValueError):
        PreviewPlan(modality="none", readiness=(ReadinessSignal(kind="exit_zero"),))


def test_mobile_preview_is_optional_where_a_mandatory_preview_would_block() -> None:
    """The contrast that shows preview is genuinely not required.

    Both halves run through the real resolver on the *same* host, which is what
    makes this a control rather than a pair of unrelated observations: the
    shipped shape does not block on preview, and a variant differing only in
    declaring preview work does — so the first result comes from the
    declaration, not from the host being generous.
    """
    without_execute = SHAPE_CAPABILITIES - {"process.execute"}

    shipped = resolve_shape(MOBILE_PROFILE_ID, host_capabilities=without_execute)
    assert not shipped.blocked("preview")

    registry = register_mobile_shape(BuildPlatformRegistry())
    variant_id = register_variant(
        registry,
        variant="mandatory_preview",
        target_plan_for=_mandatory_preview_plan,
    )
    variant = resolve_shape(variant_id, host_capabilities=without_execute, registry=registry)
    assert variant.blocked("preview")
    (block,) = [b for b in variant.blocked_operations if b.operation == "preview"]
    assert block.code == "capability_denied"
    assert block.missing_capabilities == ("process.execute",)
    # Phase-local even for the variant: only preview fell over.
    assert not variant.blocked("construct")
    assert not variant.blocked("target")
    assert not variant.blocked("verify")


def _mandatory_preview_plan(target_id: ComponentId) -> TargetPlan:
    """A variant that differs from the mobile shape only in requiring preview."""
    entry = EntryDescriptor(kind="bundle_manifest", reference="dist/mobile/app.bundle.manifest")
    return TargetPlan(
        target=target_id,
        delivery=DeliveryIntent(shape="bundle.mobile_app", entry=entry),
        preview=PreviewPlan(
            modality="device_mirror",
            entry=entry,
            policy=PreviewPolicy(required=True, unavailable="block"),
            intents=(
                ComponentIntent(
                    operation="mobile.preview_on_device",
                    required_capabilities=frozenset({"process.execute"}),
                ),
            ),
            required_capabilities=frozenset({"process.execute"}),
        ),
        verifier=VerifierPlan(
            checks=(
                VerifierCheck(
                    check_id="mobile_variant_contract",
                    intent=ComponentIntent(
                        operation="mobile.verify_bundle",
                        required_capabilities=frozenset({"workspace.read"}),
                    ),
                ),
            )
        ),
    )


def test_absent_mobile_toolchain_is_unsupported_and_blocks_only_its_own_phase() -> None:
    """An absent Android SDK is said in PKG-17's vocabulary, not a new one."""
    profile = unevidenced_toolchain_profile(MOBILE_TOOLCHAIN_HOST)

    result = profile.result_for(MOBILE_TOOLCHAIN_CAPABILITY)
    assert result is not None
    assert result.observation.outcome is ProbeOutcome.NOT_RUN
    assert result.grade is EvidenceGrade.UNSUPPORTED
    assert result.advertised is False
    assert profile.advertised == frozenset()

    # Phase-local: the toolchain gates `package` and reaches no other phase.
    impacts = {impact.phase.value: impact for impact in profile.phase_impacts()}
    assert impacts["package"].blocked is True
    assert impacts["package"].missing == (MOBILE_TOOLCHAIN_CAPABILITY,)
    assert set(impacts) == {"package"}

    (support,) = profile.support()
    assert support.capability == MOBILE_TOOLCHAIN_CAPABILITY
    assert support.level.value == "unsupported"
    assert support.detail.startswith("unsupported:")


def test_an_unevidenced_mobile_toolchain_cannot_be_graded_supported() -> None:
    """The grade discipline, not this fixture's restraint, is what refuses."""
    definition = MOBILE_TOOLCHAIN_HOST
    (probe,) = definition.probes
    assert probe.capability == MOBILE_TOOLCHAIN_CAPABILITY

    profile = unevidenced_toolchain_profile(definition)
    # Every route to an advertising grade requires a PRESENT observation, and no
    # observation here is PRESENT — because no SDK command was run to make one.
    assert all(result.observation.outcome is not ProbeOutcome.PRESENT for result in profile.results)
    assert profile.advertised == frozenset()
    assert profile.degraded == frozenset()


def test_a_grade_cannot_outrun_its_observation_by_direct_construction() -> None:
    """The grade discipline defends the *direct* record-construction path.

    `grade_observation()` is mechanical and can never emit an unearned pairing,
    so a test that only grades through it would stay green with the validator
    deleted.  Constructing `HostCapabilityResult` here is what actually reaches
    the guard — the same gap that came back NOT PROVEN at PKG-17.
    """

    def observation(outcome: ProbeOutcome) -> ProbeObservation:
        return ProbeObservation(
            probe_id="proof.control_probe",
            outcome=outcome,
            observed_on="host.grade_control_proof@1",
            detail="control observation for the grade discipline",
        )

    for grade, outcome in (
        (EvidenceGrade.SUPPORTED, ProbeOutcome.NOT_RUN),
        (EvidenceGrade.SUPPORTED, ProbeOutcome.ABSENT),
        (EvidenceGrade.DEGRADED, ProbeOutcome.NOT_RUN),
        (EvidenceGrade.EXPERIMENTAL, ProbeOutcome.ABSENT),
    ):
        with pytest.raises(ValueError):
            HostCapabilityResult(
                capability="toolchain.control_capability",
                probe_id="proof.control_probe",
                grade=grade,
                observation=observation(outcome),
                detail="control",
            )

    # Positive control against over-rejection: the earned pairing constructs.
    earned = HostCapabilityResult(
        capability="toolchain.control_capability",
        probe_id="proof.control_probe",
        grade=EvidenceGrade.UNSUPPORTED,
        observation=observation(ProbeOutcome.NOT_RUN),
        detail="control: probe deliberately not executed",
    )
    assert earned.advertised is False


def test_binding_a_profile_with_an_unobserved_probe_fails_closed() -> None:
    """A declared probe with no observation is an error, never a default.

    A default would let a profile advertise a capability nobody checked, which
    is the exact defect the host package exists to prevent.
    """
    with pytest.raises(HostCapabilityError):
        MOBILE_TOOLCHAIN_HOST.bind(())


def test_mobile_shape_coexists_with_the_desktop_shape_in_one_registry() -> None:
    registry = build_registration_registry()
    profile = registry.profiles.get(MOBILE_PROFILE_ID)
    assert profile is not None
    composition = resolve_shape(MOBILE_PROFILE_ID, registry=registry)
    assert composition.blocked_operations == ()
    assert composition.target == MOBILE_TARGET_ID

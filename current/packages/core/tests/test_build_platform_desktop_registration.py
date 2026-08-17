"""The desktop-shaped registration proof.

**Tauri and Electron are not supported.**  These tests assert that the ordinary
public registry and lifecycle accommodate a desktop-*shaped* target — opaque
artifact, an optional preview that opens no window, target-specific
verification, packaging with no served directory — and nothing more than that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from build_platform_registration_shapes import (
    DESKTOP_ENGINE_ID,
    DESKTOP_EXPORTER_ID,
    DESKTOP_OPERATIONS,
    DESKTOP_PREVIEW_ID,
    DESKTOP_PROFILE_ID,
    DESKTOP_TARGET_ID,
    DESKTOP_TOOLCHAIN_CAPABILITY,
    DESKTOP_TOOLCHAIN_HOST,
    DESKTOP_VERIFIER_ID,
    SHAPE_CAPABILITIES,
    BundleVerificationError,
    ShapeHost,
    register_desktop_shape,
    register_variant,
    resolve_shape,
    run_lifecycle,
    unevidenced_toolchain_profile,
)
from disco.core.build_platform import (
    BuildPlatformRegistry,
    CapabilitySupport,
    ComponentId,
    ComponentIntent,
    DeliveryIntent,
    EntryDescriptor,
    PackagePlan,
    PreviewPlan,
    SupportLevel,
    TargetPlan,
    VerifierCheck,
    VerifierPlan,
)
from disco.core.build_platform.host_capabilities import EvidenceGrade, ProbeOutcome
from disco.core.verification import HostVerificationClaim, VerificationClaimKind

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


def test_desktop_shape_registers_through_the_ordinary_public_registry() -> None:
    registry = register_desktop_shape(BuildPlatformRegistry())

    profile = registry.profiles.get(DESKTOP_PROFILE_ID)
    assert profile is not None
    assert profile.engine == DESKTOP_ENGINE_ID
    assert profile.target == DESKTOP_TARGET_ID
    assert profile.exporter == DESKTOP_EXPORTER_ID
    assert [choice.id for choice in registry.profiles.choices()] == [DESKTOP_PROFILE_ID]

    assert registry.components.engine(DESKTOP_ENGINE_ID) is not None
    assert registry.components.target(DESKTOP_TARGET_ID) is not None
    assert registry.components.exporter(DESKTOP_EXPORTER_ID) is not None
    for component_id in (DESKTOP_VERIFIER_ID, DESKTOP_PREVIEW_ID):
        spec = registry.components.spec(component_id)
        assert spec is not None and spec.id == component_id

    unknown = ComponentId(namespace="proof", name="unknown", version="1")
    assert registry.profiles.get(unknown) is None
    assert registry.components.target(DESKTOP_ENGINE_ID) is None
    assert registry.components.connector(DESKTOP_TARGET_ID) is None


def test_desktop_composition_is_opaque_non_web_with_an_optional_preview() -> None:
    composition = resolve_shape(DESKTOP_PROFILE_ID)

    assert composition.blocked_operations == ()
    assert composition.target_plan.delivery.shape == "bundle.desktop_app"
    assert composition.target_plan.delivery.mode == "artifact"
    assert composition.target_plan.delivery.entry.kind == "bundle_manifest"
    assert composition.target_plan.delivery.entry.reference == "dist/desktop/app.bundle.manifest"

    preview = composition.target_plan.preview
    assert preview.modality == "manifest_dry_run"
    assert preview.policy.required is False
    assert preview.policy.unavailable == "degrade"
    # The preview needs a process, never a display: it is a dry run, not a window.
    assert preview.required_capabilities == frozenset({"process.execute"})
    assert "display.interactive" not in composition.required_runtime_capabilities

    serialized = composition.model_dump_json().casefold()
    for forbidden in _WEB_TOKENS:
        assert forbidden not in serialized


def test_desktop_optional_preview_blocks_alone_when_the_host_lacks_it() -> None:
    """Preview degrades by itself; every other phase proceeds."""
    composition = resolve_shape(
        DESKTOP_PROFILE_ID, host_capabilities=SHAPE_CAPABILITIES - {"process.execute"}
    )
    assert composition.blocked("preview")
    assert not composition.blocked("construct")
    assert not composition.blocked("target")
    assert not composition.blocked("verify")
    assert not composition.blocked("package")

    (block,) = [b for b in composition.blocked_operations if b.operation == "preview"]
    assert block.missing_capabilities == ("process.execute",)


def test_desktop_packaging_needs_no_served_directory_or_network() -> None:
    composition = resolve_shape(DESKTOP_PROFILE_ID)
    package = composition.target_plan.package
    assert package is not None
    assert package.package_shape == "bundle.desktop_archive"

    required = frozenset(
        capability for intent in package.intents for capability in intent.required_capabilities
    )
    assert required == frozenset({"workspace.read"})
    assert "network.egress" not in composition.required_runtime_capabilities
    assert composition.target_plan.deployments == ()


def test_desktop_lifecycle_composes_previews_verifies_and_packages(tmp_path: Path) -> None:
    composition = resolve_shape(DESKTOP_PROFILE_ID)
    host = ShapeHost(tmp_path)

    run_lifecycle(composition, host)

    assert tuple(host.executed) == DESKTOP_OPERATIONS
    assert host.preview_result is not None and host.preview_result.startswith("sha256:")
    assert host.package_digest is not None and host.package_digest.startswith("sha256:")

    manifest_path = tmp_path / "dist" / "desktop" / "app.bundle.manifest"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["shape"] == "desktop"
    # The preview reported the manifest's own digest — no window, no renderer.
    assert host.preview_result == manifest["digest"]
    assert not list(tmp_path.rglob("index.html"))


def test_desktop_verifier_is_target_specific_and_rejects_a_corrupted_bundle(
    tmp_path: Path,
) -> None:
    composition = resolve_shape(DESKTOP_PROFILE_ID)
    (check,) = composition.target_plan.verifier.checks
    assert check.issuer == DESKTOP_VERIFIER_ID
    assert check.receipt_kind == "proof.desktop_bundle@1"
    assert check.required_execution_modality == "workspace_artifact"
    assert check.accepted_claim_kinds == frozenset({VerificationClaimKind.TARGET_SPECIFIC})

    host = ShapeHost(tmp_path)
    for intent in composition.construction.intents:
        host.execute(intent)
    for intent in composition.target_plan.intents:
        host.execute(intent)
    (tmp_path / "dist" / "desktop" / "app.payload").write_bytes(b"tampered")

    with pytest.raises(BundleVerificationError):
        host.execute(check.intent)
    assert host.package_digest is None


def test_a_browser_runtime_claim_cannot_be_attached_to_a_target_specific_verifier() -> None:
    """The HTML/browser verifier negative, refused by the contract itself.

    A check that accepts only ``TARGET_SPECIFIC`` and carries an ``HTTP_READY``
    or ``RENDERED_CONTENT`` claim is rejected at construction — so a web
    verifier cannot be smuggled into either shape by editing a claim list.
    """
    for browser_kind in (
        VerificationClaimKind.HTTP_READY,
        VerificationClaimKind.RENDERED_CONTENT,
        VerificationClaimKind.ROUTE,
    ):
        with pytest.raises(ValueError):
            VerifierCheck(
                check_id="desktop_browser_contract",
                intent=ComponentIntent(operation="desktop.verify_bundle"),
                accepted_claim_kinds=frozenset({VerificationClaimKind.TARGET_SPECIFIC}),
                claims=(
                    HostVerificationClaim(
                        claim_id="desktop.browser_claim",
                        kind=browser_kind,
                        expected="a served page responded",
                        source_authority=DESKTOP_TARGET_ID.canonical,
                    ),
                ),
            )


def test_a_served_directory_variant_blocks_where_the_shipped_shape_does_not() -> None:
    """The served-directory negative, driven through the real resolver.

    The shipped desktop shape packages from the workspace alone.  A variant that
    differs only in requiring network egress to serve a directory blocks
    `package` on the same host — which is what shows the shipped shape's
    independence is declared, not incidental.
    """
    shipped = resolve_shape(DESKTOP_PROFILE_ID)
    assert not shipped.blocked("package")

    registry = register_desktop_shape(BuildPlatformRegistry())
    variant_id = register_variant(
        registry,
        variant="served_package",
        target_plan_for=_served_directory_plan,
    )
    variant = resolve_shape(variant_id, registry=registry)

    assert variant.blocked("package")
    (block,) = [b for b in variant.blocked_operations if b.operation == "package"]
    assert block.code == "capability_denied"
    assert block.missing_capabilities == ("network.egress",)
    assert not variant.blocked("construct")
    assert not variant.blocked("target")


def _served_directory_plan(target_id: ComponentId) -> TargetPlan:
    entry = EntryDescriptor(kind="bundle_manifest", reference="dist/desktop/app.bundle.manifest")
    return TargetPlan(
        target=target_id,
        delivery=DeliveryIntent(shape="bundle.desktop_app", entry=entry),
        preview=PreviewPlan(modality="none"),
        verifier=VerifierPlan(
            checks=(
                VerifierCheck(
                    check_id="desktop_variant_contract",
                    intent=ComponentIntent(
                        operation="desktop.verify_bundle",
                        required_capabilities=frozenset({"workspace.read"}),
                    ),
                ),
            )
        ),
        package=PackagePlan(
            package_shape="bundle.desktop_archive",
            intents=(
                ComponentIntent(
                    operation="desktop.serve_bundle_directory",
                    required_capabilities=frozenset({"workspace.read", "network.egress"}),
                ),
            ),
        ),
    )


def test_absent_desktop_toolchain_is_unsupported_and_blocks_only_its_own_phase() -> None:
    """An absent Rust toolchain is said in PKG-17's vocabulary, not a new one."""
    profile = unevidenced_toolchain_profile(DESKTOP_TOOLCHAIN_HOST)

    result = profile.result_for(DESKTOP_TOOLCHAIN_CAPABILITY)
    assert result is not None
    assert result.observation.outcome is ProbeOutcome.NOT_RUN
    assert result.grade is EvidenceGrade.UNSUPPORTED
    assert result.advertised is False
    assert profile.advertised == frozenset()

    impacts = {impact.phase.value: impact for impact in profile.phase_impacts()}
    assert impacts["package"].blocked is True
    assert impacts["package"].missing == (DESKTOP_TOOLCHAIN_CAPABILITY,)
    assert set(impacts) == {"package"}

    (support,) = profile.support()
    assert support.level.value == "unsupported"
    assert support.detail.startswith("unsupported:")


def test_host_support_denies_a_capability_that_every_layer_grants() -> None:
    """The host-support filter, reached on the only path that exercises it.

    The toolchain capabilities are absent from every capability layer, so the
    intersection alone already excludes them and the ``host_support`` filter
    never runs — a test built only on those would stay green with the filter
    deleted.  Here ``process.execute`` *is* granted by platform, user, host,
    profile and every component layer, and is denied purely because host
    support reports it unsupported.
    """
    support = (
        CapabilitySupport(
            capability="process.execute",
            level=SupportLevel.UNSUPPORTED,
            detail="control: host support reports this capability unsupported",
        ),
    )
    composition = resolve_shape(DESKTOP_PROFILE_ID, host_support=support)

    assert "process.execute" not in composition.effective_capabilities.allowed
    assert composition.blocked("preview")
    assert not composition.blocked("construct")
    assert not composition.blocked("target")
    assert not composition.blocked("verify")
    assert not composition.blocked("package")

    (block,) = [b for b in composition.blocked_operations if b.operation == "preview"]
    assert block.missing_capabilities == ("process.execute",)

    # The denial names host-support as a source, not just a missing layer.
    (denial,) = [
        d for d in composition.effective_capabilities.denied if d.capability == "process.execute"
    ]
    assert "host-support" in denial.denied_by


def test_an_unsupported_toolchain_narrows_the_resolver_and_never_widens_it() -> None:
    """The host support projection reaches production and can only subtract.

    Feeding the unevidenced toolchain profile's own ``support()`` into the
    resolver leaves the shipped shape exactly as it was — it requires no
    toolchain — while the capability itself stays denied.  A projection that
    could *grant* an unevidenced capability would show up here as a widened
    allowance.
    """
    profile = unevidenced_toolchain_profile(DESKTOP_TOOLCHAIN_HOST)
    composition = resolve_shape(DESKTOP_PROFILE_ID, host_support=profile.support())

    assert composition.blocked_operations == ()
    assert DESKTOP_TOOLCHAIN_CAPABILITY not in composition.effective_capabilities.allowed
    assert composition.effective_capabilities.allowed == SHAPE_CAPABILITIES

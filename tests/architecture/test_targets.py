"""Target adapter mutation tests for the architecture gate.

Tests persistent non-web central-target-switch negative and registered
target-adapter positive through the real ``disco.core.build_platform`` boundary.

These tests require ``pydantic`` and the ``disco.core.build_platform`` package.
The test environment must be provisioned with ``--with pydantic`` (or
``uv sync``) so the contracts module imports. There is no skip — a missing
dependency is a provisioning failure, not a silent pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

# Import the build_platform contracts for target-adapter tests
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "core" / "src"))

from disco.core.build_platform import (  # noqa: E402
    SYNTHETIC_CAPABILITIES,
    SYNTHETIC_PROFILE_ID,
    build_synthetic_registry,
    resolve_synthetic_conformance,
)
from disco.core.build_platform.contracts import (  # noqa: E402
    DeliveryIntent,
    EntryDescriptor,
    Parameter,
    PreviewPlan,
    ReadinessSignal,
)

# ---------------------------------------------------------------------------
# Non-web conformance — no central target switch
# ---------------------------------------------------------------------------


class TestNonWebConformance:
    """The synthetic non-web conformance must not contain a central target switch."""

    def test_synthetic_profile_has_no_web_target(self):
        """The synthetic profile must not declare a web/HTTP target."""
        composition = resolve_synthetic_conformance()
        serialized = composition.model_dump_json()
        for forbidden in ("http", "browser", "index.html", "port_listening"):
            assert forbidden not in serialized.casefold(), (
                f"non-web conformance contains forbidden term '{forbidden}'"
            )

    def test_synthetic_delivery_shape_is_data_job_bundle(self):
        """The synthetic delivery shape must be data.job_bundle, not a web shape."""
        composition = resolve_synthetic_conformance()
        assert composition.target_plan.delivery.shape == "data.job_bundle"

    def test_synthetic_preview_modality_is_dry_run(self):
        """The synthetic preview modality must be dry_run, not a browser modality."""
        composition = resolve_synthetic_conformance()
        assert composition.target_plan.preview.modality == "dry_run"

    def test_synthetic_entry_reference_is_job_manifest(self):
        """The synthetic entry reference must be a job manifest, not an HTML path."""
        composition = resolve_synthetic_conformance()
        assert composition.target_plan.delivery.entry.reference == "job/task.manifest"


# ---------------------------------------------------------------------------
# Registered target adapter positive
# ---------------------------------------------------------------------------


class TestRegisteredTargetAdapter:
    def test_synthetic_registry_uses_public_registry(self):
        """The synthetic registry must use the public registry, not a central switch."""
        registry = build_synthetic_registry()
        assert registry.profiles.get(SYNTHETIC_PROFILE_ID) is not None
        assert [choice.id for choice in registry.profiles.choices()] == [SYNTHETIC_PROFILE_ID]

    def test_synthetic_registry_resolves_engine(self):
        """The registry must resolve the engine for the synthetic profile."""
        registry = build_synthetic_registry()
        profile = registry.profiles.get(SYNTHETIC_PROFILE_ID)
        assert profile is not None
        engine = registry.components.engine(profile.engine)
        assert engine is not None

    def test_synthetic_registry_resolves_target(self):
        """The registry must resolve the target adapter for the synthetic profile."""
        composition = resolve_synthetic_conformance()
        assert composition.target_plan is not None
        assert composition.target_plan.delivery is not None

    def test_synthetic_blocked_operations_empty_with_full_capabilities(self):
        """With full capabilities, no operations should be blocked."""
        composition = resolve_synthetic_conformance()
        assert composition.blocked_operations == ()

    def test_synthetic_blocked_when_capability_missing(self):
        """Removing a required capability must block the relevant operation."""
        composition = resolve_synthetic_conformance(
            host_capabilities=SYNTHETIC_CAPABILITIES - {"process.execute"}
        )
        assert composition.blocked("preview")
        assert not composition.blocked("construct")


# ---------------------------------------------------------------------------
# Target-neutral Core — no framework/target switches
# ---------------------------------------------------------------------------


class TestTargetNeutralCore:
    def test_delivery_shape_is_open_identifier(self):
        """The delivery shape must be an open namespaced identifier, not a closed enum."""
        entry = EntryDescriptor(kind="job.manifest", reference="job/task.manifest")
        intent = DeliveryIntent(shape="custom.target.shape", entry=entry)
        assert intent.shape == "custom.target.shape"

    def test_preview_modality_includes_none(self):
        """The preview modality must explicitly include 'none'."""
        plan = PreviewPlan(modality="none")
        assert plan.modality == "none"
        # 'none' modality must not declare entry or readiness work
        assert plan.entry is None
        assert plan.readiness == ()

    def test_entry_descriptor_is_opaque(self):
        """The entry descriptor must be opaque (kind + reference + scalars)."""
        entry = EntryDescriptor(kind="job.manifest", reference="job/task.manifest")
        assert entry.kind == "job.manifest"
        assert entry.reference == "job/task.manifest"

    def test_readiness_signal_is_neutral(self):
        """The readiness signal must be neutral (kind + scalars), not HTTP-specific."""
        signal = ReadinessSignal(
            kind="file.exists",
            parameters=(Parameter(name="path", value="out.json"),),
        )
        assert signal.kind == "file.exists"
        assert signal.parameters[0].name == "path"

    def test_custom_delivery_shape_does_not_edit_core(self):
        """A new target can register a shape without editing Core — no central switch."""
        entry = EntryDescriptor(kind="mobile.app", reference="app/Info.plist")
        intent = DeliveryIntent(shape="ios.app_bundle", entry=entry)
        # Core accepts arbitrary namespaced shapes without a closed enum
        assert intent.shape == "ios.app_bundle"
        assert intent.entry.kind == "mobile.app"

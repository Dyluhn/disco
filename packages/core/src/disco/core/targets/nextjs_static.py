"""Target-owned Next.js static adapter and exporter.

This module owns all Next.js static vocabulary.  It is the target-specific
layer that invokes the target-neutral Build Platform Core contracts.  It
deliberately carries no server identity, no port, and no live HTTP requirement:
the server target is a separate boundary (15-T3) and must never be silently
substituted for the static plan.
"""

from __future__ import annotations

from ..build_platform import (
    FREEFORM_ENGINE_ID,
    BuildProfile,
    CapabilityLayer,
    ComponentId,
    ComponentIntent,
    DeliveryIntent,
    EntryDescriptor,
    PackageExporter,
    PackagePlan,
    PackageRequest,
    Parameter,
    PolicyLayer,
    PreviewPlan,
    PreviewPolicy,
    ReadinessSignal,
    TargetAdapter,
    TargetPlan,
    TargetRequest,
    VerifierCheck,
    VerifierPlan,
)

NEXTJS_STATIC_PROFILE_ID = ComponentId(namespace="disco", name="nextjs_static", version="1")
NEXTJS_STATIC_TARGET_ID = ComponentId(namespace="disco", name="nextjs_static_target", version="1")
NEXTJS_STATIC_PREVIEW_ID = ComponentId(namespace="disco", name="nextjs_static_preview", version="1")
NEXTJS_STATIC_EXPORTER_ID = ComponentId(
    namespace="disco", name="nextjs_static_exporter", version="1"
)
NEXTJS_STATIC_VERIFIER_ID = ComponentId(
    namespace="disco", name="nextjs_static_verifier", version="1"
)

NEXTJS_STATIC_DELIVERY_SHAPE = "nextjs.static_delivery"
NEXTJS_STATIC_PACKAGE_SHAPE = "nextjs.static_package"

BUILD_COMMAND = "next build"
OUTPUT_DIR = "out"
ENTRY_REFERENCE = "out/index.html"

BUILD_OPERATION = "nextjs.static.build"
READINESS_OPERATION = "nextjs.static.file_readiness"
PACKAGE_OPERATION = "nextjs.static.package"
EXPORT_OPERATION = "nextjs.static.export"

_STATIC_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})


class NextjsStaticTarget(TargetAdapter):
    """Static-only Next.js target; never attaches a start command or a port."""

    @property
    def id(self) -> ComponentId:
        return NEXTJS_STATIC_TARGET_ID

    def plan(self, request: TargetRequest) -> TargetPlan:
        if request.profile != NEXTJS_STATIC_PROFILE_ID:
            raise ValueError(
                f"static target refuses profile {request.profile.canonical}; "
                "expected the static profile"
            )
        if request.engine != FREEFORM_ENGINE_ID:
            raise ValueError(
                f"static target requires the Freeform engine, got {request.engine.canonical}"
            )
        entry = EntryDescriptor(kind="file", reference=ENTRY_REFERENCE)
        return TargetPlan(
            target=self.id,
            intents=(
                ComponentIntent(
                    operation=BUILD_OPERATION,
                    parameters=(
                        Parameter(name="command", value=BUILD_COMMAND),
                        Parameter(name="output_dir", value=OUTPUT_DIR),
                    ),
                ),
            ),
            delivery=DeliveryIntent(shape=NEXTJS_STATIC_DELIVERY_SHAPE, entry=entry),
            preview=PreviewPlan(
                modality="file_readiness",
                entry=entry,
                readiness=(ReadinessSignal(kind="file_readiness"),),
                intents=(
                    ComponentIntent(
                        operation=READINESS_OPERATION,
                        parameters=(
                            Parameter(name="command", value=BUILD_COMMAND),
                            Parameter(name="entry", value=ENTRY_REFERENCE),
                        ),
                    ),
                ),
                policy=PreviewPolicy(required=False, unavailable="degrade"),
            ),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="static_entry_present",
                        issuer=NEXTJS_STATIC_VERIFIER_ID,
                        receipt_kind="disco.nextjs_static_entry@1",
                        required_execution_modality="file_readiness",
                        intent=ComponentIntent(
                            operation=READINESS_OPERATION,
                            parameters=(Parameter(name="entry", value=ENTRY_REFERENCE),),
                        ),
                    ),
                )
            ),
            package=PackagePlan(
                package_shape=NEXTJS_STATIC_PACKAGE_SHAPE,
                intents=(
                    ComponentIntent(
                        operation=PACKAGE_OPERATION,
                        parameters=(
                            Parameter(name="command", value=BUILD_COMMAND),
                            Parameter(name="output_dir", value=OUTPUT_DIR),
                            Parameter(name="entry", value=ENTRY_REFERENCE),
                        ),
                    ),
                ),
            ),
            required_capabilities=frozenset({"workspace.read"}),
        )


class NextjsStaticExporter(PackageExporter):
    """Static-only exporter; preserves the frozen entry/output package shape."""

    @property
    def id(self) -> ComponentId:
        return NEXTJS_STATIC_EXPORTER_ID

    def plan(self, request: PackageRequest) -> PackagePlan:
        if request.profile != NEXTJS_STATIC_PROFILE_ID:
            raise ValueError(
                f"static exporter refuses profile {request.profile.canonical}; "
                "expected the static profile"
            )
        if request.target != NEXTJS_STATIC_TARGET_ID:
            raise ValueError(
                f"static exporter refuses target {request.target.canonical}; "
                "expected the static target"
            )
        return PackagePlan(
            package_shape=NEXTJS_STATIC_PACKAGE_SHAPE,
            intents=(
                ComponentIntent(
                    operation=EXPORT_OPERATION,
                    parameters=(
                        Parameter(name="command", value=BUILD_COMMAND),
                        Parameter(name="output_dir", value=OUTPUT_DIR),
                        Parameter(name="entry", value=ENTRY_REFERENCE),
                    ),
                ),
            ),
        )


def nextjs_static_profile() -> BuildProfile:
    """Build the static profile through the Freeform engine and static adapter set."""
    return BuildProfile(
        id=NEXTJS_STATIC_PROFILE_ID,
        label="Next.js static",
        engine=FREEFORM_ENGINE_ID,
        target=NEXTJS_STATIC_TARGET_ID,
        verifier=NEXTJS_STATIC_VERIFIER_ID,
        preview=NEXTJS_STATIC_PREVIEW_ID,
        exporter=NEXTJS_STATIC_EXPORTER_ID,
        compatible_reference_categories=frozenset(
            {"reference_pack", "starter_recipe", "library_recipe"}
        ),
        capabilities=CapabilityLayer(
            source=NEXTJS_STATIC_PROFILE_ID.canonical, allowed=_STATIC_CAPABILITIES
        ),
        policy=PolicyLayer(source=NEXTJS_STATIC_PROFILE_ID.canonical),
    )

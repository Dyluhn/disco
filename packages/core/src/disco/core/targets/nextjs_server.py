"""Target-owned Next.js server adapter and exporter.

This module owns all Next.js server vocabulary.  It is the target-specific
layer that invokes the target-neutral Build Platform Core contracts.  It
deliberately carries no static `out`/`out/index.html` entry and no
optional/degrading file-readiness preview: the static target is a separate
boundary (15-T2) and must never be silently substituted for the server plan.
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

NEXTJS_SERVER_PROFILE_ID = ComponentId(namespace="disco", name="nextjs_server", version="1")
NEXTJS_SERVER_TARGET_ID = ComponentId(namespace="disco", name="nextjs_server_target", version="1")
NEXTJS_SERVER_PREVIEW_ID = ComponentId(namespace="disco", name="nextjs_server_preview", version="1")
NEXTJS_SERVER_EXPORTER_ID = ComponentId(
    namespace="disco", name="nextjs_server_exporter", version="1"
)
NEXTJS_SERVER_VERIFIER_ID = ComponentId(
    namespace="disco", name="nextjs_server_verifier", version="1"
)

NEXTJS_SERVER_DELIVERY_SHAPE = "nextjs.server_delivery"
NEXTJS_SERVER_PACKAGE_SHAPE = "nextjs.server_package"

BUILD_COMMAND = "next build"
START_COMMAND = "next start"
OUTPUT_DIR = ".next"
PORT = 3000
ENTRY_REFERENCE = "/"

BUILD_OPERATION = "nextjs.server.build"
START_OPERATION = "nextjs.server.start"
READINESS_OPERATION = "nextjs.server.http_readiness"
PACKAGE_OPERATION = "nextjs.server.package"
EXPORT_OPERATION = "nextjs.server.export"

_SERVER_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})


class NextjsServerTarget(TargetAdapter):
    """Server-only Next.js target; never emits a static `out` entry."""

    @property
    def id(self) -> ComponentId:
        return NEXTJS_SERVER_TARGET_ID

    def plan(self, request: TargetRequest) -> TargetPlan:
        if request.profile != NEXTJS_SERVER_PROFILE_ID:
            raise ValueError(
                f"server target refuses profile {request.profile.canonical}; "
                "expected the server profile"
            )
        if request.engine != FREEFORM_ENGINE_ID:
            raise ValueError(
                f"server target requires the Freeform engine, got {request.engine.canonical}"
            )
        entry = EntryDescriptor(kind="http", reference=ENTRY_REFERENCE)
        readiness = (
            ReadinessSignal(
                kind="http_readiness",
                parameters=(
                    Parameter(name="port", value=PORT),
                    Parameter(name="path", value=ENTRY_REFERENCE),
                ),
            ),
        )
        readiness_intent = ComponentIntent(
            operation=READINESS_OPERATION,
            parameters=(
                Parameter(name="command", value=START_COMMAND),
                Parameter(name="port", value=PORT),
                Parameter(name="path", value=ENTRY_REFERENCE),
            ),
        )
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
                ComponentIntent(
                    operation=START_OPERATION,
                    parameters=(
                        Parameter(name="command", value=START_COMMAND),
                        Parameter(name="port", value=PORT),
                    ),
                ),
            ),
            delivery=DeliveryIntent(shape=NEXTJS_SERVER_DELIVERY_SHAPE, entry=entry),
            preview=PreviewPlan(
                modality="http_readiness",
                entry=entry,
                readiness=readiness,
                intents=(readiness_intent,),
                policy=PreviewPolicy(required=True, unavailable="block"),
            ),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="server_http_ready",
                        issuer=NEXTJS_SERVER_VERIFIER_ID,
                        receipt_kind="disco.nextjs_server_ready@1",
                        required_execution_modality="http_readiness",
                        intent=readiness_intent,
                    ),
                )
            ),
            package=PackagePlan(
                package_shape=NEXTJS_SERVER_PACKAGE_SHAPE,
                intents=(
                    ComponentIntent(
                        operation=PACKAGE_OPERATION,
                        parameters=(
                            Parameter(name="command", value=BUILD_COMMAND),
                            Parameter(name="start", value=START_COMMAND),
                            Parameter(name="output_dir", value=OUTPUT_DIR),
                            Parameter(name="port", value=PORT),
                        ),
                    ),
                ),
            ),
            required_capabilities=frozenset({"workspace.read"}),
        )


class NextjsServerExporter(PackageExporter):
    """Server-only exporter; preserves the frozen build/start/output/port values."""

    @property
    def id(self) -> ComponentId:
        return NEXTJS_SERVER_EXPORTER_ID

    def plan(self, request: PackageRequest) -> PackagePlan:
        if request.profile != NEXTJS_SERVER_PROFILE_ID:
            raise ValueError(
                f"server exporter refuses profile {request.profile.canonical}; "
                "expected the server profile"
            )
        if request.target != NEXTJS_SERVER_TARGET_ID:
            raise ValueError(
                f"server exporter refuses target {request.target.canonical}; "
                "expected the server target"
            )
        return PackagePlan(
            package_shape=NEXTJS_SERVER_PACKAGE_SHAPE,
            intents=(
                ComponentIntent(
                    operation=EXPORT_OPERATION,
                    parameters=(
                        Parameter(name="command", value=BUILD_COMMAND),
                        Parameter(name="start", value=START_COMMAND),
                        Parameter(name="output_dir", value=OUTPUT_DIR),
                        Parameter(name="port", value=PORT),
                    ),
                ),
            ),
        )


def nextjs_server_profile() -> BuildProfile:
    """Build the server profile through the Freeform engine and server adapter set."""
    return BuildProfile(
        id=NEXTJS_SERVER_PROFILE_ID,
        label="Next.js server",
        engine=FREEFORM_ENGINE_ID,
        target=NEXTJS_SERVER_TARGET_ID,
        verifier=NEXTJS_SERVER_VERIFIER_ID,
        preview=NEXTJS_SERVER_PREVIEW_ID,
        exporter=NEXTJS_SERVER_EXPORTER_ID,
        compatible_reference_categories=frozenset(
            {"reference_pack", "starter_recipe", "library_recipe"}
        ),
        capabilities=CapabilityLayer(
            source=NEXTJS_SERVER_PROFILE_ID.canonical, allowed=_SERVER_CAPABILITIES
        ),
        policy=PolicyLayer(source=NEXTJS_SERVER_PROFILE_ID.canonical),
    )

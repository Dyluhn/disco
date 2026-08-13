from __future__ import annotations

from disco.core.build_platform import (
    ARTIFACT_TARGET_ID,
    FREEFORM_ARTIFACT_PROFILE_ID,
    FREEFORM_ENGINE_ID,
    FREEFORM_LIFECYCLE_OPERATIONS,
    FREEFORM_PROFILE_ID,
    LEGACY_EXPORTER_ID,
    ConstructionEngine,
    PackageRequest,
    PolicyDecision,
    ToolDescriptor,
    build_builtin_registry,
)
from disco.core.build_platform.builtin_profiles import (
    resolve_builtin_composition_for_delivery,
)


def test_freeform_engine_describes_the_existing_flexible_lifecycle() -> None:
    tools = frozenset({"file_read", "file_write", "shell", "verify_web_app"})
    registry = build_builtin_registry(freeform_tools=tools)
    engine = registry.components.engine(FREEFORM_ENGINE_ID)
    assert isinstance(engine, ConstructionEngine)

    composition = resolve_builtin_composition_for_delivery(
        appkit=False,
        goal="build any valid web application shape",
        tool_catalog=tuple(
            ToolDescriptor(name=name, description=f"legacy {name}") for name in sorted(tools)
        ),
        visible_tools=tools,
        delivery_kind="app",
    )

    assert composition.profile.id == FREEFORM_PROFILE_ID
    assert composition.blocked_operations == ()
    assert tuple(intent.operation for intent in composition.construction.intents) == (
        FREEFORM_LIFECYCLE_OPERATIONS
    )
    assert frozenset(tool.name for tool in composition.prompt_context.visible_tools) == tools
    assert composition.target_plan.delivery.shape == "web.legacy_deliverable"
    assert tuple(intent.operation for intent in composition.target_plan.intents) == (
        "host.detect_delivery",
        "host.bind_revision",
    )
    assert composition.target_plan.preview.modality == "legacy_host"
    assert composition.target_plan.verifier.checks[0].intent.operation == (
        "host.verify_deliverable"
    )
    assert composition.target_plan.package is not None
    assert composition.target_plan.package.package_shape == "web.legacy_archive"
    raw_rule = next(
        rule for rule in composition.effective_policy.rules if rule.key == "mutation.raw_files"
    )
    assert raw_rule.decision is PolicyDecision.ALLOW


def test_freeform_engine_is_shape_flexible_and_effect_free() -> None:
    composition = resolve_builtin_composition_for_delivery(
        appkit=False,
        goal="import an existing service and preserve its chosen runtime",
        tool_catalog=(ToolDescriptor(name="file_read", description="read"),),
        visible_tools=frozenset({"file_read"}),
        delivery_kind="app",
    )
    serialized = composition.model_dump_json().casefold()
    for scenario_specific in ("vite", "next.js", "react", "node", "python", "index.html"):
        assert scenario_specific not in serialized
    for forbidden_authority in (
        "sandbox",
        "event_store",
        "secret",
        "subprocess",
        "publish_success",
    ):
        assert forbidden_authority not in serialized


def test_freeform_file_delivery_resolves_a_nonweb_target() -> None:
    composition = resolve_builtin_composition_for_delivery(
        appkit=False,
        goal="hand off a terminal script",
        tool_catalog=(ToolDescriptor(name="file_read", description="read"),),
        visible_tools=frozenset({"file_read"}),
        delivery_kind="files",
    )
    assert composition.profile.id == FREEFORM_ARTIFACT_PROFILE_ID
    assert composition.target == ARTIFACT_TARGET_ID
    assert composition.target_plan.delivery.mode == "artifact"
    assert composition.target_plan.preview.modality == "none"
    assert composition.target_plan.verifier.checks == ()
    assert composition.target_plan.verifier.policy.required is False
    assert composition.target_plan.package is not None
    assert composition.target_plan.package.package_shape == "artifact.legacy_archive"
    exporter = build_builtin_registry().components.exporter(LEGACY_EXPORTER_ID)
    assert exporter is not None
    package = exporter.plan(
        PackageRequest(
            profile=composition.profile.id,
            target=composition.target,
            delivery=composition.target_plan.delivery,
            revision_ref="revision:test",
        )
    )
    assert package.package_shape == "artifact.legacy_archive"


def test_reference_category_mismatch_blocks_context_without_weakening_construction() -> None:
    composition = resolve_builtin_composition_for_delivery(
        appkit=False,
        goal="build",
        tool_catalog=(),
        visible_tools=frozenset(),
        requested_reference_categories=frozenset({"unknown_future_category"}),
        delivery_kind="app",
    )
    assert composition.blocked("context")
    assert not composition.blocked("construct")
    assert composition.profile.id == FREEFORM_PROFILE_ID

from __future__ import annotations

from disco.core.build_platform import (
    APPKIT_PROFILE_ID,
    BUILTIN_CAPABILITIES,
    FREEFORM_PROFILE_ID,
    CapabilityLayer,
    ModuleBody,
    PolicyLayer,
    PromptContextInputs,
    ResolutionInputs,
    ToolDescriptor,
    build_builtin_registry,
    builtin_module_bodies,
    compare_observe_only,
    expected_legacy_snapshot,
    resolve_build_composition,
)


def _composition(*, appkit: bool, tools: frozenset[str]):
    registry = build_builtin_registry(
        freeform_tools=(frozenset() if appkit else tools),
        appkit_tools=(tools if appkit else frozenset()),
    )
    profile_id = APPKIT_PROFILE_ID if appkit else FREEFORM_PROFILE_ID
    profile = registry.profiles.get(profile_id)
    assert profile is not None
    prompt_id = profile.prompt_modules[0].component
    bodies = tuple(body for body in builtin_module_bodies() if body.component == prompt_id)
    assert len(bodies) == 1 and isinstance(bodies[0], ModuleBody)
    layer = CapabilityLayer(source="all", allowed=BUILTIN_CAPABILITIES)
    catalog = tuple(ToolDescriptor(name=name, description=f"{name} tool") for name in sorted(tools))
    return resolve_build_composition(
        registry,
        ResolutionInputs(
            profile=profile_id,
            goal="shadow",
            platform_capabilities=layer.model_copy(update={"source": "platform"}),
            user_capabilities=layer.model_copy(update={"source": "user"}),
            host_capabilities=layer.model_copy(update={"source": "host"}),
            platform_policy=PolicyLayer(source="platform"),
            user_policy=PolicyLayer(source="user"),
            prompt_context=PromptContextInputs(
                module_bodies=bodies,
                tool_catalog=catalog,
                host_visible_tools=tools,
            ),
        ),
    )


def test_freeform_observe_only_comparison_matches_all_fields() -> None:
    tools = frozenset({"file_edit", "shell", "verify_web_app"})
    record = compare_observe_only(
        expected_legacy_snapshot(appkit=False, visible_tools=tools),
        _composition(appkit=False, tools=tools),
    )
    assert record.matches
    assert record.legacy_authoritative is True
    assert record.active_route == "legacy"
    assert record.composition_digest is not None
    assert record.platform_blocks == ()
    assert all(comparison.matches for comparison in record.comparisons)


def test_appkit_observe_only_comparison_preserves_strict_policy() -> None:
    tools = frozenset({"app_create", "file_read", "submit_plan"})
    record = compare_observe_only(
        expected_legacy_snapshot(appkit=True, visible_tools=tools),
        _composition(appkit=True, tools=tools),
    )
    assert record.matches
    policy = next(comparison for comparison in record.comparisons if comparison.field == "policy")
    assert "mutation.raw_files=deny" in policy.platform
    assert "mutation.semantic=allow" in policy.platform


def test_shadow_mismatch_is_visible_and_cannot_change_active_route() -> None:
    tools = frozenset({"file_edit"})
    legacy = expected_legacy_snapshot(appkit=False, visible_tools=tools)
    legacy = legacy.model_copy(update={"visible_tools": ("different_tool",)})
    record = compare_observe_only(legacy, _composition(appkit=False, tools=tools))
    assert not record.matches
    mismatch = next(
        comparison for comparison in record.comparisons if comparison.field == "visible_tools"
    )
    assert mismatch.matches is False
    assert record.active_route == "legacy"
    assert record.legacy_authoritative is True
